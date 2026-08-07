#!/usr/bin/env python3
"""인사이트 단 — 판정된 가설에서 **골라 내고 표현한다** (D28 · D29).

## 이 단의 경계

분석(`analyze.py`)이 방법론을 정하고, 실행하고, 5관문으로 판정까지 끝낸다.
여기서는 **판정된 것에서 무엇을 실을지 고르고 PDF로 낸다.** 검정을 다시 하지 않는다.

    analyze.py  →  판정된 가설 전체 + 분석 계획서
    insight.py  →  강한 주장 5 + 약한 단서 20 → PDF 2층 + insights 테이블

이 경계를 흐리면 "왜 이 분석을 했는가"(계획서)와 "왜 이걸 실었는가"(선별)가 뒤섞여
어느 쪽도 추적할 수 없게 된다.

## 어조

MD가 "참고용·정성적"이라고 못박았다(2026-08-03 인터뷰). 강한 주장도 실행 지시가 아니라
**관측 진술**로 쓴다 — "20% 할인하라"가 아니라 "20% 구간에서 증분이 3배였다".

    python3 insight.py --db data/intel.db --context "brand:로우클래식" --out output/
"""
import argparse
import os
import sys
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze as an                                        # noqa: E402
from intel_db import connect as db_connect                  # noqa: E402
from schema_v3 import default_db_target, is_libsql_url      # noqa: E402
import chart                                                # noqa: E402
from pdf_doc import Doc                                     # noqa: E402

# **개수 제한을 폐기했다** (D56 — 2026-08-04 사용자 지시: "강한 가설 10개, 약한
# 가설 20개의 개수 제한은 폐기할게. 인사이트 개수가 많은 것 보다 내가 말한 내용들이
# 잘 지켜진 소수의 인사이트&액션플랜들이 더 의미있어").
#
# 자격은 처음부터 5관문이 정했고, 이 숫자는 **표시 상한**일 뿐이었다. 그런데 상한이
# 있으면 "10칸을 채워야 한다"는 압력이 생겨 지문 중복 제거·청중 균형이 자리 다툼을
# 하게 된다. 이제 **5관문과 §선별 규칙을 통과한 것을 전부 싣는다** — 3개면 3개다.
# 무한대가 아니라 큰 값을 두는 이유는 폭주 방지뿐이다(축이 수십 개인 문맥에서
# 지문 중복 제거가 실패하면 수백 개가 나올 수 있다).
STRONG_MAX = 200
WEAK_MAX = 200

# ── 선별 ────────────────────────────────────────────────────────────────────
# MD의 결정은 리오더이고, 1순위 질문은 "할인 얼마 줬을 때 얼마나 팔리나"다
# (2026-08-03 인터뷰). 효과 크기만으로 줄을 세우면 이 질문과 무관한 가설이 앞자리를
# 차지한다 — 통계적으로 센 것과 **결정에 쓸모 있는 것**은 다르다.
#
# **누적판매량이 1순위 Y다** (D56 — 2026-08-04 사용자 지시: "제일 중요한 Y는
# '누적판매량'이니까, 그게 Y가 되는 문장들을 최대한 많이 찾는 걸 목표로 해").
# 하트·후기는 대리 지표이고 누적판매는 결과 그 자체다. 무신사는 원시 정수를 안 주고
# 구간 표기만 주므로(D48) `purchase_band`(순서형)가 실질적인 그 축이다 — 둘을 같은
# 무게로 둔다. 그 다음이 리오더 판단에 직접 걸리는 품절·소진이다.
DECISION_FIELDS = {"purchase_count": 6.0, "purchase_band": 6.0,
                   "cvr_view_buy": 4.5, "cvr_like_buy": 4.5,
                   "discount_rate": 3.0, "price_sale": 2.0, "price_original": 1.5,
                   "opt_out_rate": 2.0, "sold_out": 2.0, "sold_min": 2.5,
                   "stock_sum": 1.5, "rank": 1.5}


def _relevance(h):
    """리오더 결정과의 거리. 사건 연구(할인→반응)가 최상단이다."""
    if h["kind"] == "event_study":
        return 4.0
    fields = ([h.get("x"), h.get("y")] if h["kind"] == "correlation"
              else [h.get("metric"), h.get("cat_field")])
    return max([DECISION_FIELDS.get(f, 1.0) for f in fields if f] or [1.0])


def _signature(h):
    """같은 이야기의 반복을 막기 위한 지문.

    지문을 **(범주축, 지표)** 로 잡는다. 그룹쌍까지 넣으면 "미니 대 하의", "미디 대 하의",
    "롱 대 하의"가 서로 다른 지문이 되어 5칸 중 3칸을 같은 발견이 차지한다 — 독자가
    얻는 정보는 "카테고리에 따라 후기 수가 다르다" 하나뿐인데 말이다.

    계층이 안 맞는 쌍은 D42가 따로 거른다. 여기 지문은 그것과 무관하게 굵게 잡는다 —
    대등한 형제끼리라도 같은 축·같은 지표면 독자에게는 발견 하나다.
    """
    if h["kind"] == "group_compare":
        return ("g", h["cat_field"], _metric_family(h["metric"]))
    if h["kind"] == "correlation":
        return ("c", _metric_family(h["x"]), _metric_family(h["y"]))
    return ("e",)


# **대리 지표 여러 개는 발견 하나다** (D56). 2026-08-04 실물 확인: 자사 리포트의
# 강한 주장 5개 중 3개가 `플랫폼 × 하트` · `플랫폼 × 후기 수` · `플랫폼 × 평점`이었고,
# **액션 플랜이 글자 그대로 같았다**("인사이트별로 크게 다르지도 않아서 별 의미가 없어").
# 하트·후기·평점은 같은 것(고객 반응)을 다른 자로 잰 값이라, 축이 같으면 독자가 얻는
# 정보는 하나뿐이다. 지문을 **지표 하나**가 아니라 **지표 가족**으로 잡는다.
#
# 누적판매는 따로 둔다 — 그게 1순위 Y이고(D56), 대리 지표와 한 칸을 다투면 안 된다.
METRIC_FAMILY = {
    "purchase_count": "판매", "purchase_band": "판매",
    "cvr_view_buy": "판매", "cvr_like_buy": "판매", "sold_min": "판매",
    "like_count": "관심", "like_band": "관심", "review_count": "관심",
    "rating": "관심", "viewers_now": "관심", "view_count": "관심",
    "view_band": "관심", "cvr_view_like": "관심",
    "price_sale": "가격", "price_original": "가격", "discount_rate": "가격",
    "sold_out": "재고", "opt_out_rate": "재고", "stock_sum": "재고", "opt_total": "재고",
}


def _metric_family(metric):
    """지표 가족. 같은 것을 다른 자로 잰 값들은 한 지문으로 묶인다."""
    return METRIC_FAMILY.get(metric, metric)


# 한 청중이 요약을 독점하지 못하게 한다(2026-08-03 인터뷰). 1차 통과 최대치가
# 청중 3종 × CAP이라 STRONG_MAX와 함께 올려야 한다 — 안 그러면 남는 자리를
# 청중 균형을 안 보는 2차 채움이 가져간다.
AUDIENCE_CAP = 4


def _select(hyps, cap):
    """관련성 × 효과 크기로 정렬하고, 같은 지문·같은 청중 편중을 걷어낸다.

    **홀드아웃이 실제로 재현된 것을 앞에 둔다.** 표본이 작으면 홀드아웃을 나눌 수 없어
    "확인 불가"가 되는데, 그걸 탈락시키면 강한 주장이 매번 0개가 되고(2026-08-03 완화),
    그렇다고 확인된 것과 나란히 두면 라벨이 헐거워진다. 자격은 주되 순서로 가른다.
    """
    hyps.sort(key=lambda h: (h.get("holdout_unverified", False),
                             -(_relevance(h) * abs(h.get("effect") or 0))))
    seen, per_aud, out = set(), defaultdict(int), []
    # 1차: 지문 중복 제거 + 청중 상한
    for h in hyps:
        sig = _signature(h)
        if sig in seen:
            h["dropped_as_duplicate"] = True
            continue
        if per_aud[h.get("audience")] >= AUDIENCE_CAP:
            continue
        seen.add(sig)
        per_aud[h["audience"]] += 1
        out.append(h)
        if len(out) >= cap:
            return out
    # 2차: 청중 상한 때문에 자리가 남았으면 그때 채운다 — 상한은 편중 방지용이지
    # 빈칸을 만들라는 규칙이 아니다.
    #
    # **다만 순서는 청중이 적은 쪽부터다** (D55). 전에는 남은 자리를 원래 순서대로
    # 채워서 상한이 사실상 없는 것과 같았다 — 2026-08-04 여성 데님 리포트의 강한
    # 주장 **10개가 전부 `마케팅`**이었다(CAP=4인데). 1차에서 상한에 막힌 청중이
    # 2차에서 남은 자리를 그대로 쓸어 담았기 때문이다. 지금은 매 칸마다 **그때까지
    # 가장 적게 실린 청중**의 후보를 먼저 본다. 그 청중에 후보가 없으면 다음으로
    # 넘어가므로 빈칸은 여전히 생기지 않는다 — 채우되, 쏠린 채로 채우지 않는다.
    # 청중별 대기열로 나눠 "그때까지 가장 적게 실린 청중"의 첫 후보를 뽑는다.
    # 전신은 매 칸마다 min(key=rest.index)로 전체를 훑어 O(n²)이었다 — cap을
    # 200으로 올린 뒤(D56)로는 후보 수백 개 문맥에서 눈에 띈다(PR #12 2차 리뷰).
    # 대기열은 원래 순서(관련성×효과 정렬)를 보존하므로 뽑히는 결과는 같다.
    from collections import deque
    picked_ids = {id(h) for h in out}
    queues = {}
    for h in hyps:
        if id(h) in picked_ids or _signature(h) in seen:
            continue
        queues.setdefault(h.get("audience"), deque()).append(h)
    while queues and len(out) < cap:
        aud = min(queues, key=lambda a_: (per_aud[a_], str(a_)))
        q = queues[aud]
        pick = q.popleft()
        if not q:
            del queues[aud]
        sig = _signature(pick)
        if sig in seen:
            continue
        seen.add(sig)
        per_aud[aud] += 1
        out.append(pick)
    return out


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".") if abs(v) < 100 else "{:,.0f}".format(v)
    return "{:,}".format(v)


# ── 액션 제안 (D47) ─────────────────────────────────────────────────────────
# 2026-08-04 피드백: "인사이트 이후 **액션 제안**이 필요 — 읽었을 때 리턴이 있어야
# 함". 관측 진술만 실으면 읽는 사람이 "그래서 뭘 하지"를 매번 스스로 번역해야 한다.
#
# **지시하지 않는다.** MD가 "참고용·정성적"이라고 못박았고(2026-08-03 인터뷰) 우리는
# 상관만 봤다. 그래서 "무엇을 해라"가 아니라 **"이 숫자로 무엇을 정할 수 있나"**를 쓴다.
# 문장이 확정적일수록 근거보다 세 보인다 — 그 간극이 이 리포트가 제일 조심할 것이다.

# ── 왜 그럴 수 있나 — **추측을 허용한다** (D56) ─────────────────────────────
# 2026-08-04 사용자 지시: "지금 액션플랜들이 그냥 다 당연한 말들이고 인사이트별로 크게
# 다르지도 않아서 별 의미가 없어. (…) 액션플랜은 **추측이 들어가도 돼.** 그냥 가능한
# 선택지를 많이 제시하는 데 의의가 있어."
#
# 예시로 준 것: "'데님 팬츠'가 검색에 더 잘 걸리기 때문일 수 있으므로 수정 검토".
# 즉 **가설 → 그래서 해볼 것**이 한 묶음이어야 한다. 관측만 다시 읊으면 쓸모가 없다.
#
# **어조는 그대로 조건부다.** 추측을 허용한 것이지 단정을 허용한 것이 아니다 —
# "…일 수 있다"로 쓰고, 확인 방법을 함께 적는다. 우리는 여전히 상관만 봤다.
#
# 축마다 그럴듯한 기전이 다르다. 여기 목록은 **가설의 씨앗**이고, 정답표가 아니다.
MECHANISMS = {
    "category": {
        "마케팅": [
            "`{lo}` 대신 `{hi}` 표기가 검색에 더 잘 걸려서일 수 있다 — 두 표기를 사이트 검색창에 "
            "넣어 노출 수를 비교하고, 다르면 **상품명·태그를 `{hi}` 쪽으로 수정** 검토",
            "`{hi}` 쪽에 시즌·기획전이 겹쳤을 수 있다 — 해당 기간 기획전 목록을 뽑아 우리 참여 여부를 확인",
            "`{hi}` 상위 노출 상품의 **키워드 구성**을 20개 뽑아 우리 상품명과 겹치는 단어를 센다",
        ],
        "디자인": [
            "`{hi}`와 `{lo}`가 담는 상품군 자체가 달라서일 수 있다 — 양쪽 목록 20개씩 훑어 같은 종류인지 확인",
            "`{hi}` 상위 상품의 **실루엣·디테일 공통점**을 적어 다음 시즌 스케치에 반영 검토",
            "우리 라인업이 `{hi}`에 몇 개 걸려 있는지 세고 **구성비 목표**를 정한다",
        ],
        "판매전략": [
            "`{hi}`에 우리 상품이 적으면 **분류를 옮기거나 추가**하는 것만으로 {metric}이 늘 수 있다 — 등록 정보부터 확인",
            "`{hi}`와 `{lo}`의 가격 분포를 겹쳐 그려 **우리 가격이 어느 구간에 있는지** 표시",
            "`{hi}` 쪽 상품의 품절률을 함께 보고 **초도 물량 배분**을 다시 계산",
        ],
    },
    "brand": {
        "마케팅": [
            "`{hi}`의 팔로워·인지도가 더 커서일 수 있다 — 두 브랜드 스토어 팔로워 수를 적어 {metric}과 나란히 본다",
            "`{hi}`가 상품을 더 많이·오래 걸어 누적 지표가 유리했을 수 있다 — **상품 수로 나눠** 1상품당 {metric}으로 재비교",
            "`{hi}`의 콘텐츠(룩북·협업·셀럽 착용) 이력을 훑어 우리가 안 한 채널을 목록화",
        ],
        "디자인": [
            "`{hi}`의 상위 판매 상품 5개를 열어 **우리 라인업에 없는 조건**(핏·소재·디테일)을 뽑는다",
            "`{hi}`와 우리의 **컬러 구성비**를 비교해 빠진 색이 있는지 본다",
        ],
        "판매전략": [
            "`{hi}`와 `{lo}`의 가격대 포지션이 달라서일 수 있다 — 가격 분포를 겹쳐 그리고 우리 위치를 표시",
            "`{hi}`의 상시 할인율을 재서 **우리 할인 정책과 비교**한다",
        ],
    },
    "site": {
        "마케팅": [
            "두 플랫폼 고객층이 달라서일 수 있다 — 같은 상품의 후기 성별·연령 분포를 {hi}와 {lo}에서 비교",
            "{hi}의 노출 로직(기획전·랭킹 편입)에 더 많이 걸렸을 수 있다 — 해당 기간 참여 이력 확인",
            "{lo}에서 **브랜드관·콘텐츠 노출**을 안 쓰고 있는지 점검",
        ],
        "디자인": [
            "{hi}와 {lo}의 **대표컷 규격·연출**이 다를 수 있다 — 같은 상품의 썸네일을 나란히 놓고 비교",
            "{lo}용 상세페이지가 축약본이면 **정보량 차이**가 {metric}을 갈랐을 수 있다 — 두 상세를 비교",
        ],
        "판매전략": [
            "쿠폰·수수료로 실판매가가 달라서일 수 있다 — **쿠폰 적용가 기준으로** {metric}을 다시 비교",
            "{lo}에 상품을 덜 올렸을 수 있다 — 두 플랫폼의 우리 상품 수를 세어 노출량 차이인지 본다",
        ],
    },
    "fit": {
        "디자인": [
            "`{hi}` 수요가 실제로 더 커서일 수 있다 — 검색량·연관 검색어로 두 핏 이름을 비교",
            "우리 라인업의 `{hi}` 비중을 세어 **다음 시즌 구성비**의 근거로 쓴다",
            "`{hi}` 상위 상품의 패턴(밑위·기장·워싱)을 적어 샘플 기획에 반영 검토",
        ],
        "마케팅": [
            "`{hi}`를 상품명·태그에 명시한 상품이 더 잘 걸리는지 검색으로 확인",
            "`{hi}` 착용 콘텐츠가 우리에게 있는지 점검하고 없으면 촬영 목록에 넣는다",
        ],
        "판매전략": [
            "`{lo}`에 공급이 몰려 경쟁이 심할 수 있다 — **전체 상품 수 대비 {metric}**으로 재계산",
            "`{hi}`의 가격대를 재서 **우리 가격이 그 구간에 있는지** 확인",
        ],
    },
    "sold_out": {
        "판매전략": [
            "잘 팔려서 품절인지 물량을 적게 잡아서 품절인지 갈라야 한다 — 초도 수량과 소진 기간을 함께 본다",
            "품절이 오래 방치됐으면 **리오더 타이밍을 놓친 것**일 수 있다 — 품절 지속일을 잰다",
            "사이즈별로 어디부터 비는지 확인해 **다음 발주 사이즈 배분**에 반영",
        ],
        "마케팅": [
            "품절 상품의 **재입고 알림 신청 수**를 확인해 수요 크기를 가늠",
            "품절이 길면 노출 면에서 빼고 **대체 상품을 올릴지** 정한다",
        ],
        "디자인": [
            "품절이 빠른 상품의 **공통 디자인 요소**를 적어 다음 기획에 반영 검토",
            "같은 실루엣의 컬러 변형을 낼지 검토한다 — 원형이 이미 검증됐다",
        ],
    },
    "_proxy_name": {
        "마케팅": [
            "상품명에 `{hi}`{hi_josa_eul} 적은 것 자체가 검색 노출을 바꿨을 수 있다 — `{hi}`로 검색해 우리 상품이 몇 번째에 걸리는지 본다",
            "`{hi}`가 최근 유행어라 신상품에 몰렸을 수 있다 — {axis} 값별 등록 시점 분포를 본다",
            "**다음 등록분 절반의 상품명만 `{hi}` 쪽으로 바꿔** {metric} 전후를 비교(가장 싸게 해볼 수 있는 실험이다)",
        ],
        "디자인": [
            "`{hi}`가 실제 상품 속성을 반영하고 `{lo}`는 표기만 생략한 것일 수 있다 — `{lo}` 상품 10개를 열어 확인",
            "`{hi}` 상품의 **실제 디자인 공통점**을 적어 표기와 실물이 일치하는지 본다",
        ],
        "판매전략": [
            "`{hi}`를 쓰는 브랜드가 특정 가격대에 몰려 있을 수 있다 — {axis} 값별 가격 분포를 겹쳐 그린다",
            "`{hi}` 상품의 할인율·품절률을 함께 봐서 **{metric}이 표기 때문인지 정책 때문인지** 가른다",
        ],
    },
    "_proxy_image": {
        "디자인": [
            "`{hi}` 연출이 클릭률을 올렸을 수 있다 — 같은 상품 썸네일을 `{hi}` 쪽으로 바꿔 등록하고 {metric} 전후 비교",
            "**다음 촬영분 절반에만 `{hi}` 가이드를 적용**해 A/B로 확인",
            "`{hi}` 컷의 구도·조명·배경을 적어 **촬영 가이드 한 줄**로 만든다",
        ],
        "마케팅": [
            "`{hi}`를 쓰는 브랜드군이 따로 있을 수 있다 — {axis} 값별 브랜드 구성을 세어 본다",
            "`{hi}` 썸네일을 광고 소재로 돌려 **클릭률이 실제로 갈리는지** 확인",
        ],
        "판매전략": [
            "`{lo}` 상품이 원래 반응이 약한 카테고리·가격대에 몰려 있을 수 있다 — 조건을 고정하고 재비교",
            "촬영 비용 차이를 재서 **`{hi}` 연출을 전 품목에 쓸지** 판단한다",
        ],
    },
}

AUDIENCES = ("판매전략", "디자인", "마케팅")
MECH_PER_AUDIENCE = 2      # **청중마다 최소 이만큼** (2026-08-04 사용자 지시)


def _mech_pool(h):
    f = h.get("cat_field") or h.get("x") or ""
    if f in MECHANISMS:
        return MECHANISMS[f]
    if f.startswith("px_"):
        img = any(k in f for k in ("thumb", "image", "model", "logo", "shot",
                                   "pattern", "color_count"))
        return MECHANISMS["_proxy_image" if img else "_proxy_name"]
    if f.startswith("attr_"):
        return MECHANISMS["fit"]
    return {}


def _mechanisms(h):
    """이 발견으로 **청중마다 무엇을 할 수 있나**. `[(청중, 문장), …]`.

    2026-08-04 사용자 지시 둘을 함께 만족시킨다:
      1. "인사이트별로 액션 플랜이 달라야 진짜 인사이트지" — 문장에 **그 카드의 실제 값**
         (`{hi}`·`{lo}`·`{axis}`·`{metric}`)이 들어가고, 풀에서 고르는 **시작 위치를
         카드 지문으로 돌린다**. 같은 축의 다른 카드는 다른 조합을 받는다
      2. "마케팅 이런 배지는 인사이트 자체가 아니라 액션플랜별로. 각 배지별로 최소 2개" —
         청중 3종 각각 `MECH_PER_AUDIENCE`개씩 낸다. 하나의 발견을 놓고 **마케터는 무엇을,
         디자이너는 무엇을** 하면 되는지가 한 카드 안에 같이 있어야 한다

    시작 위치는 `zlib.crc32`다 — 파이썬 `hash()`는 프로세스마다 달라져 같은 데이터에
    같은 리포트가 안 나온다(고정 시드 원칙 위반).
    """
    import zlib
    pool = _mech_pool(h)
    if not pool:
        return []
    ma, mb = h.get("median_a"), h.get("median_b")
    ga, gb = h.get("group_a") or "", h.get("group_b") or ""
    hi, lo = (ga, gb) if (ma or 0) > (mb or 0) else (gb, ga)
    ctx = {"hi": hi, "lo": lo,
           "hi_josa_eul": an._josa(hi, "을를")[len(hi):] if hi else "를",
           "axis": h.get("cat_label") or h.get("x_label") or "이 축",
           "metric": h.get("metric_label") or h.get("y_label") or "이 지표"}
    key = "|".join(str(x) for x in (h.get("cat_field"), ga, gb, h.get("metric")))
    seed = zlib.crc32(key.encode("utf-8"))
    out = []
    for aud in AUDIENCES:
        lines = pool.get(aud) or []
        if not lines:
            continue
        off = seed % len(lines)
        for i in range(min(MECH_PER_AUDIENCE, len(lines))):
            t = lines[(off + i) % len(lines)]
            try:
                out.append((aud, _fix_josa(t.format(**ctx))))
            except (KeyError, IndexError):
                out.append((aud, t))
    return out


_JOSA_RE = __import__("re").compile(
    r"(`[^`]+`|\{hi\}|\{lo\})(으로|를|을|이|가|은|는|와|과|로)(?![가-힣])")
_JOSA_PAIR = {"을": "을를", "를": "을를", "이": "이가", "가": "이가",
              "은": "은는", "는": "은는", "와": "와과", "과": "와과",
              "로": "로으로", "으로": "로으로"}


def _fix_josa(text):
    """문장 안의 조사를 앞 단어 받침에 맞춰 고친다 (D56).

    기전 템플릿은 `{hi}`·`{lo}`에 값이 치환되므로 조사를 미리 박을 수 없다 —
    `진청` 뒤에는 `을`, `중청` 뒤에는 `을`, `한국어` 뒤에는 `를`이다. 템플릿마다
    변형을 두는 대신 **치환이 끝난 문장을 한 번 훑어** 고친다. 새 템플릿을 쓸 때
    조사를 신경 쓰지 않아도 되는 것이 이 방식의 값어치다.

    백틱으로 감싼 값만 손댄다 — 일반 문장의 조사까지 건드리면 멀쩡한 문구가 깨진다.
    """
    def _repl(m):
        word, josa = m.group(1), m.group(2)
        bare = word.strip("`")
        return word + an._josa(bare, _JOSA_PAIR[josa])[len(bare):]
    return _JOSA_RE.sub(_repl, text)


def _tagged(pairs):
    """`[(청중, 문장)]` → `"**[청중]** 문장"`.

    2026-08-04 사용자 지시: "마케팅 이런 배지는 인사이트 자체에 달리는 게 아니라
    액션플랜별로 달리는 게 맞는 거 같아". 한 발견을 놓고 마케터·디자이너·판매전략이
    각각 무엇을 할 수 있는지가 카드 안에서 갈려 보여야 한다.
    """
    return ["**[%s]** %s" % (aud, text) for aud, text in pairs]


def _action_for(h, res, claim=None):
    """카드의 액션 — **AI가 쓴 것이 있으면 그것이 우선이다** (D64).

    2026-08-04 사용자가 원하는 액션 플랜의 실물 예시를 줬다("다색 표기 상품이 높다 →
    옵션 통합 등록으로 전환해 하트·후기가 한 곳에 쌓이게 유도"). 이런 문장은 발견의
    **기전을 읽고 추론**해야 나온다 — {hi}/{lo} 치환 템플릿이 낼 수 있는 수준이 아니다.

    그래서 층을 가른다(이 프로젝트의 기존 패턴 — 규칙표가 기본, AI가 예외):
      엔진(여기)  = 검정·판정·조판. 결정적이고 재현된다
      AI(호출자)  = 발견을 읽고 액션 플랜을 써서 `--ai-actions` JSON으로 넘긴다
    키는 **주장 문장 그대로**다 — 같은 데이터·같은 시드면 주장이 그대로 재현되므로
    안정적인 키가 된다. 없으면 템플릿 액션으로 내려간다(리포트가 비지 않게).
    """
    ov = (res or {}).get("ai_actions") or {}
    hit = ov.get(claim if claim is not None else h.get("claim"))
    if hit:
        return hit
    return action_hint(h)


def action_hint(h):
    """이 발견으로 **무엇을 하면 되나**. 여러 줄을 돌려준다 (D51 · D56).

    2026-08-04 사용자 요청: "액션플랜 좀 더 쉽고 구체적인 걸로 여러 가지가
    적히면 좋겠다". 전에는 한 줄이었고 문장이 추상적이었다("참고선으로 쓴다").
    같은 날 2차 피드백으로 **기전 추측을 넣고 선택지를 늘렸다**(위 MECHANISMS).

    쓰는 법 네 가지를 지킨다:
      1. **동사로 시작한다** — "확인한다"·"비교한다"·"정한다". 명사로 끝내지 않는다
      2. **숫자를 문장에 넣는다** — 그 카드의 값을 그대로. 다시 안 찾아봐도 되게
      3. **기전을 추측하되 조건부로 쓴다** — "…일 수 있다" + 확인 방법이 한 묶음
      4. **지시하지 않는다** — 우리는 상관만 봤다. "무엇을 확인할지"까지가 우리 몫이다
    """
    v = lambda x: _fmt(x)
    metric = h.get("metric_label") or h.get("y_label") or "이 지표"
    ga, gb = h.get("group_a"), h.get("group_b")
    ma, mb = h.get("median_a"), h.get("median_b")
    n = "{:,}".format(h.get("n") or 0)

    if h.get("verdict") == "weak":
        # **한 줄만 준다** (D56). 전에는 모든 약한 단서에 같은 두 줄이 붙어
        # ("아직 정하지 마라" + "지금 값으로 기획을 바꾸면 뒤집힐 수 있다")
        # 20~30개 카드에 똑같은 문장이 반복됐다 — 사용자 지적. 재확인 방법은
        # 관문마다 다르므로 그것만 남기고, 공통 경고는 섹션 머리말이 한 번 말한다.
        # "아직 정하지 마라 — 다음 관측으로 재확인" 한 줄은 읽는 사람이 할 수 있는
        # 일이 없다(D62). 왜 보류인지·뭘 하면 판정이 나는지를 카드 값으로 쓴다.
        return recheck_lines(h)

    out = []
    kind = h.get("kind")
    if kind == "group_compare":
        if h.get("lever_metric"):
            # 공급자가 정한 값이라 성과가 아니다 — 정책을 되묻는 쪽으로 쓴다
            out += ["**우리가 정한 값이다.** %s가 왜 %s와 %s에서 갈렸는지 "
                    "의도한 것인지 먼저 확인한다" % (metric, ga, gb),
                    "의도한 정책이면 그대로 두고, 아니면 **어느 쪽에 맞출지** 정한다",
                    "같은 값에서 **반응(하트·후기·판매)이 어떻게 갈리는지** 이어서 본다 "
                    "— 그게 성과다"]
        else:
            gap = ""
            if ma is not None and mb is not None:
                gap = " (%s 대 %s)" % (v(ma), v(mb))
            hi = ga if (ma or 0) > (mb or 0) else gb
            lo = gb if (ma or 0) > (mb or 0) else ga
            # 첫 줄은 **요점 + 당장 할 일 하나**를 평문으로 (D56 3차 — "그래서 뭘
            # 하라는 건지 쉽게"). 통계 용어 없이, 읽고 바로 움직일 수 있게.
            out += ["**요점: `%s` 쪽이 %s 잘 나온다**%s. 당장 할 일 — 우리 상품 중 "
                    "`%s` 조건이 몇 개인지 세고, 적으면 다음 기획에서 늘릴지 검토"
                    % (hi, metric, gap, hi)]
            # 기전 추측 — 카드마다 달라지고, **줄마다 청중 배지가 붙는다** (D56)
            out += _tagged(_mechanisms(h))
            out += ["확실히 하려면 — `%s` 상품 5개와 `%s` 상품 5개를 나란히 열어 "
                    "무엇이 다른지 직접 본다" % (lo, hi)]
    elif kind == "correlation":
        d = h.get("direction")
        if d in ("response_pair", "lever_pair"):
            out += ["**둘 다 %s이다 — 어느 쪽이 먼저인지 데이터가 답하지 않는다** "
                    "(선후를 모른다)" % (
                        "고객 반응" if d == "response_pair" else "우리가 정한 값"),
                    "한쪽을 올리면 다른 쪽이 따라온다고 읽지 마라",
                    "선후를 보려면 **시점을 나눠** 앞선 변화가 뒤선 변화를 예고하는지 확인한다"]
        else:
            out += ["%s를 움직였을 때 %s가 따라 움직인 관측이다 (n=%s)"
                    % (h.get("x_label"), h.get("y_label"), n)]
            out += _tagged(_mechanisms(h))
            out += ["**폭을 정할 때 참고**하되, 같은 시기에 다른 조건(노출·시즌·경쟁)이 "
                    "함께 바뀌지 않았는지 확인한다",
                    "확실히 하려면 **한 상품 안에서 그 값만 바꿔** 전후를 비교한다"]
    elif kind == "did":
        out += ["가격 인하의 **순효과가 이 크기다** — 다음 인하 폭의 기준선으로 삼는다",
                "이보다 큰 반응을 기대한다면 **노출·시즌 같은 다른 조건이 함께** 바뀌어야 한다",
                "인하 직후 며칠에 몰렸는지 **기간을 나눠** 확인한다"]
    elif kind == "dose":
        out += ["**구간마다 반응이 다르다** — 어느 구간 위로는 더 줘도 얻는 게 적은지 본다",
                "그 지점을 **다음 할인의 상한**으로 두고 시험한다"]
    elif kind == "depletion":
        out += ["소진이 빠른 쪽에 **재입고·물량 배분을 먼저** 검토한다",
                "품절 직전 **사이즈별 잔량**을 확인해 어느 사이즈부터 비는지 본다"]
    elif kind == "paired":
        out += ["같은 상품인데 **플랫폼별로 갈린다** — 가격·노출 조건을 어디에 맞출지 정한다",
                "낮은 쪽 플랫폼에서 **왜 그렇게 됐는지**(쿠폰·기획전 참여) 확인한다"]
    if not out:
        out = ["다음 관측에서 같은 방향이 나오는지 먼저 본다"]
    return out


# ── '영향이 없었다'도 인사이트 (D47) ────────────────────────────────────────
# 피드백: "**'영향이 없었다'도 인사이트** — 신경 쓰지 않아도 된다는 판단 근거가 되므로".
# 지금까지 기각된 가설은 "같은 막다른 길을 다시 파지 않기 위해" 목록으로만 실렸다.
# 그런데 **효과 크기가 작아서 기각된 것**은 다른 이야기다 — 그건 "차이가 없더라"이고,
# 팀원에게는 "여기 신경 쓰지 마라"라는 쓸 수 있는 답이다.
#
# 표본이 작아서 기각된 것과는 **반드시 갈라야 한다.** 전자는 "없다"이고 후자는
# "모른다"인데, 섞으면 안 본 것이 없는 것이 된다.
NULL_EFFECT_MAX = 0.15          # |효과| 이 아래면 "차이가 없다"로 읽는다


def null_findings(hyps, cap=6):
    """**차이가 없다고 말할 수 있는** 발견. 표본 부족으로 못 본 것은 제외한다."""
    out = []
    for h in hyps:
        if h.get("verdict") != "rejected":
            continue
        # 표본 부족은 "모른다"이지 "없다"가 아니다. 한글 부분 문자열로 가르면
        # 문구가 바뀌는 날 조용히 오분류된다(PR #9 리뷰) — **관문 이름으로 가른다.**
        # `fails`에 관문 코드가 없는 옛 항목은 문자열로 폴백한다.
        codes = h.get("fail_codes") or []
        if "sample" in codes or (not codes and
                                 any("표본" in f for f in h.get("fails", []))):
            continue
        eff = abs(h.get("effect") or 0)
        if eff > NULL_EFFECT_MAX or (h.get("n") or 0) < 40:
            continue
        out.append(h)
    out.sort(key=lambda h: (-(h.get("n") or 0), abs(h.get("effect") or 0)))
    return out[:cap]


def _null_claim(h):
    """무영향 발견의 문장. 원 문장은 "A가 B보다 높다"라 그대로 쓰면 정반대로 읽힌다."""
    if h.get("kind") == "group_compare":
        # 조사는 받침을 본다 (D56) — "데님와"가 아니라 "데님과"다
        return "%s에서 %s %s %s 차이가 없다" % (
            h.get("cat_label"), an._josa(h.get("group_a"), "와과"),
            an._josa(h.get("group_b"), "은는"), h.get("metric_label", ""))
    if h.get("kind") == "correlation":
        return "%s %s 함께 움직이지 않는다" % (
            an._josa(h.get("x_label"), "와과"), an._josa(h.get("y_label"), "은는"))
    return "이 조건에서는 차이가 나타나지 않았다 — %s" % h.get("claim", "")


def _null_action(h):
    """차이 없음 카드의 액션 — **그 축의 값으로** "이제 뭘 해도 되는지"를 말한다.

    차이가 없다는 결론의 쓸모는 **자유**다: 그 조건은 성과에 영향이 없었으니
    다른 기준(원가·재고·브랜드 방향)으로 마음대로 정해도 된다. 이 말을 일반론이
    아니라 그 카드의 축·그룹·지표 이름으로 적는다.
    """
    metric = h.get("metric_label") or h.get("y_label") or "성과"
    if h.get("kind") == "group_compare":
        ga, gb = h.get("group_a") or "", h.get("group_b") or ""
        axis = h.get("cat_label") or "이 조건"
        return ["`%s` `%s` 어느 쪽이든 %s 비슷했다 — **%s %s 눈치 안 보고 "
                "원가·재고·브랜드 방향으로 정하면 된다**"
                % (ga, gb, an._josa(metric, "은는"),
                   an._josa(axis, "은는"), metric)]
    if h.get("kind") == "correlation":
        x = h.get("x_label") or "그 값"
        return ["%s 바꿔서 %s 끌어올릴 근거가 없다 — **%s 조정은 후순위로 미루고** "
                "차이가 확인된 축(① 목록)에 먼저 손댄다"
                % (an._josa(x, "을를"), an._josa(metric, "을를"), x)]
    return ["이 조건은 %s에 영향이 없었다 — 다른 기준으로 자유롭게 정한다" % metric]


def recheck_lines(h):
    """약한 단서마다 **왜 보류인지 + 뭘 하면 알 수 있는지**를 카드의 값으로 쓴다 (D62).

    전신은 관문별 고정 한 줄이었다 — "다음 관측으로 재확인" 같은 문구는 읽는 사람이
    할 수 있는 일이 없다(2026-08-05 사용자: "성의없게 쓰지 말고 재확인 할 수 있는
    방안을 좀 더 쉽고 읽는 사람이 알 수 있게"). 세 가지를 지킨다:

      1. **그 카드의 값이 들어간다** — 그룹 이름·효과 크기·n·기준. 일반론 금지
      2. **읽는 사람이 할 일과 시스템이 할 일을 가른다** — "다음 수집 뒤 리포트를
         다시 뽑으면 자동 재판정된다"는 시스템 몫이고, "이 질문이 궁금하면 지목해
         달라"는 사람 몫이다. 섞어 쓰면 둘 다 안 한다
      3. **끝맺음이 있다** — "그때도 이 수준이면 차이 없음으로 접는다"까지. 보류가
         영원히 보류로 남지 않게 종료 조건을 적는다

    **관문은 코드로 가른다**(`fail_codes`) — 한글 문구 매칭은 gate()의 문구를 다듬는
    날 조용히 오분류된다(PR #9 리뷰). 코드가 없는 옛 항목은 문구로 폴백한다.
    """
    codes = h.get("fail_codes") or []
    fails = h.get("fails") or []
    has = lambda code, word: (code in codes) if codes else any(word in f for f in fails)
    is_corr = h.get("kind") in ("correlation", "dose_response")
    # 카드의 값들 — 문장에 그대로 박는다
    a, b = h.get("group_a") or h.get("x_label") or "한쪽",            h.get("group_b") or h.get("y_label") or "다른 쪽"
    metric = h.get("metric_label") or h.get("y_label") or "지표"
    eff = abs(h.get("effect") or 0)
    thr = 0.25 if is_corr else 0.30
    n = h.get("n") or 0
    out = []

    if has("effect_small", "효과 크기가 작다"):
        out.append("「%s」와 「%s」의 %s 차이가 실제로 있긴 한데 **크기가 작다**"
                   "(%.2f — 판정 기준 %.2f). 이 정도로는 기획을 바꿀 근거가 안 된다. "
                   "다음 수집 뒤 리포트를 다시 뽑아 차이가 커졌는지 보고, **그때도 이 "
                   "수준이면 '차이 없음'으로 접는다**" % (a, b, metric, eff, thr))
    if has("sample", "표본이 작다"):
        na, nb = h.get("n_a"), h.get("n_b")
        who = ("「%s」 %s개 대 「%s」 %s개" % (a, "{:,}".format(na or 0),
                                          b, "{:,}".format(nb or 0))
               if na is not None else "%s건" % "{:,}".format(n))
        out.append("표본이 %s뿐이라 **우연과 못 가른다**(기준 20건 이상). 상품이 더 "
                   "모이면 자동 재판정된다 — 다음 수집에서 이 카테고리를 한 번 더 "
                   "훑으면 충분하다. 급하면 **적은 쪽 그룹만 겨냥해 수집을 요청**해라"
                   % who)
    if has("holdout", "홀드아웃"):
        out.append("확인차 데이터를 반으로 나눠 다시 쟀더니 **방향이 흔들렸다** — "
                   "절반의 우연이 만든 패턴일 수 있다. 다음 수집분이 들어와 리포트를 "
                   "다시 뽑으면 새 데이터로 자동 재확인된다. **새 데이터에서도 같은 "
                   "방향이면 그때 믿는다**")
    if has("fdr", "다중비교"):
        out.append("이번 리포트는 가설 수백 개를 한꺼번에 검정했다 — 많이 뽑으면 "
                   "우연히 통과하는 게 반드시 나와서 보정을 거는데, 이 발견이 거기 "
                   "걸렸다. **이 질문이 진짜 궁금하면 지목해 달라** — 이것만 단독으로 "
                   "재검정하면 우연 기준이 느슨해져 판정이 난다")
    if has("segment", "세그먼트"):
        # gate()가 남긴 세그먼트 이름을 문장에서 건진다 — "카테고리=미니에서 …"
        segs = [f.split("에서")[0] for f in fails if "사라지거나 뒤집힌다" in f][:2]
        seg_txt = "·".join(segs) if segs else "일부 그룹"
        out.append("전체로는 관계가 보이는데 **%s에서는 사라지거나 뒤집힌다** — 전체 "
                   "평균을 믿지 말고 그룹을 나눠 따로 읽어라. 어느 쪽이 진짜인지는 "
                   "그 그룹 표본이 더 쌓여야 갈린다" % seg_txt)
    if has("vanity", "누적 지표"):
        out.append("%s와 %s는 둘 다 **오래 판 상품일수록 같이 커지는 누적값**이다 — "
                   "관계가 아니라 나이 효과일 수 있다. 관측이 두 시점 이상 쌓이면 "
                   "그 사이 **증가분끼리** 다시 비교한다" % (a, b))
    if has("no_p", "p값을 계산하지"):
        out.append("이 유형은 우연 여부를 재는 검정이 없다 — 방향 참고까지만 쓴다")
    if not out:
        out.append("다음 수집 뒤 리포트를 다시 뽑으면 자동 재검정된다 — "
                   "그때도 보류면 잊어도 된다")
    return out[:3]      # 관문 셋 이상 걸린 카드는 어차피 상세로 간다 — 지면 보호


def recheck_hint(h):
    """recheck_lines의 문자열판 — DB `recheck` 컬럼·옛 호출부 호환용."""
    return " · ".join(recheck_lines(h))


# ── 실행 ────────────────────────────────────────────────────────────────────
def ensure_rule_proxies(db_path, contexts, cards_path=None, quiet=False):
    """**리포트를 만들면 rule 프록시가 자동으로 생긴다** (D51 — D43의 갭 해소).

    D43이 "기본으로 만든다"로 정책을 뒤집었는데 **스크립트 연결을 안 했다.**
    `proxy_auto.py`를 만들어 놓고 SKILL 문서의 §0 절차로만 둬서, AI가 그 단계를
    직접 밟아야 생겼고 `insight.py`만 돌리면 **하나도 안 생겼다**(사용자 확인).

    여기서 하는 것은 **rule 프록시뿐**이다 — 상품명·브랜드명에서 규칙으로 뽑는
    것이라 비용이 0이고 네트워크도 안 탄다. **vision 프록시는 여기서 안 만든다**
    (서브 에이전트 배치가 필요하고 비싸다) — 그건 §0 절차 그대로 AI가 판단한다.
    """
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    cards = cards_path or os.path.join(here, "..", "references",
                                       "proxy-cards-default.json")
    if not os.path.exists(cards):
        return None
    # libsql:// URL은 dirname이 "libsql:/"로 파생돼 열 수 없는 경로가 된다 —
    # Turso 정본일 때 스크래치 JSON은 로컬 data/에 둔다 (D72)
    base = "data" if is_libsql_url(str(db_path)) else (os.path.dirname(db_path) or ".")
    os.makedirs(base, exist_ok=True)
    out = os.path.join(base, ".px-auto.json")
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(here, "proxy_auto.py"), "--db", db_path,
             "--cards", cards, "--out", out]
            + sum([["--context", c] for c in contexts], []),
            capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            if not quiet:
                print("프록시 자동 생성 실패(계속 진행): %s" % (r.stderr or "")[-200:],
                      file=sys.stderr)
            return None
        # 적재는 intel_db가 한다 — 값 공간 밖 판정 거부·numeric 캐스팅이 거기 있다
        # **`--db`를 반드시 넘긴다.** intel_db의 DEFAULT_DB는 `data/intel.db`, 즉
        # CWD 상대 경로다. 안 넘기면 `skills/commerce-intel/scripts/`에서 돌렸을 때
        # 거기에 빈 DB를 새로 파고 판정을 통째로 거기 넣는다 — proxy_auto는 정본 DB를
        # 읽고 적재만 딴 데로 가서, "채택 11,989건"을 찍고도 정본에는 한 건도 안 남는다
        # (2026-08-04 실측: 그렇게 만들어진 유령 DB 36MB를 발견했다).
        load = subprocess.run(
            [sys.executable, os.path.join(here, "intel_db.py"),
             "--db", db_path, "proxy-load", out],
            capture_output=True, text=True, timeout=180)
        if not quiet:
            for line in (r.stdout or "").splitlines():
                if line.strip().startswith(("채택", "버림", "카드 버림")):
                    print("  " + line.strip())
            if load.returncode != 0:
                print("프록시 적재 실패(계속 진행): %s" % (load.stderr or "")[-200:],
                      file=sys.stderr)
        return out
    except Exception as e:
        if not quiet:
            print("프록시 자동 생성 건너뜀: %s" % e, file=sys.stderr)
        return None


def build(db_path, contexts, ai_notes=None, auto_proxy=True, own_brands=None,
          signals=None):
    """분석 단을 돌리고 그 판정 결과에서 실을 것을 고른다.

    `signals` — eda.py --out 산출물(dict). 요청 문맥이 같으면 분석 단이 EDA를
    재수행하지 않는다 (D74). 주의: auto_proxy가 이 사이에 새 프록시 판정을
    만들 수 있다 — EDA의 축 검사는 데이터 품질 수준이라 무시해도 되는 차이다.
    """
    if auto_proxy:
        ensure_rule_proxies(db_path, contexts)
    res = an.analyze(db_path, contexts, ai_notes, own_brands=own_brands,
                     signals=signals)
    if not res["ok"]:
        return None, res
    hyps = res["hypotheses"]
    strong = _select([h for h in hyps if h["verdict"] == "strong"], STRONG_MAX)
    weak = _select([h for h in hyps if h["verdict"] == "weak"], WEAK_MAX)
    rejected = [h for h in hyps if h["verdict"] == "rejected"]
    folded = len(res["data"]["items"]) - len(an.style_rows(res["data"]["items"], proxies=res["data"]["meta"].get("proxies")))
    return {"generated": res["generated"], "plan": res["plan"], "folded_variants": folded,
            "strong": strong, "weak": weak, "rejected": rejected,
            "eda": res["eda"], "data": res["data"],
            "lineage_skipped": res.get("lineage_skipped") or {},   # D42 — 서두 경고가 쓴다
            "null_findings": null_findings(hyps),                  # D47 — 차이가 없었다
            "strong_pool": sum(1 for h in hyps if h["verdict"] == "strong"),
            "verified": sum(1 for h in strong if not h.get("holdout_unverified"))}, res

# ── PDF ─────────────────────────────────────────────────────────────────────
def honesty_points(res):
    eda_res = res["eda"]
    g = eda_res["grain"]
    pts = [
        "이 리포트는 **관측된 상관**을 보여준다. 인과가 아니다 — 왜 그런지는 데이터가 답하지 않는다.",
        "하트·후기·누적판매·조회수는 **누적 절대값**이다(오래 노출된 상품이 유리하다) — "
        "비율은 이름에 (%)가 붙은 축뿐이다. 각 카드 근거 줄에 지표 성격을 표기했다.",
        "그룹 비교는 전부 **상품 1개당**이다 — 품목 수가 많은 그룹이 유리한 합계 비교가 "
        "아니다(화이트가 소수 컬러라서 총 하트가 적은 것과, 화이트 상품 하나하나의 하트가 "
        "낮은 것은 다른 말이고, 여기 실린 것은 후자다). 각 카드에 그룹별 상품 수와 "
        "중앙값을 병기했다.",
        "가설을 많이 만들수록 우연한 패턴이 반드시 나온다. 다중비교 보정(BH)을 걸었지만 "
        "강한 주장도 확증이 아니라 **다음 관측으로 재확인할 대상**이다.",
    ]
    pts += eda_res["survivorship"]["notes"]
    if eda_res["small_sample"]:
        pts.append("표본이 작다(n=%d) — 이 리포트의 모든 수치에 그 한계가 붙는다." % g["rows"])
    pts.append("관측 창: %s ~ %s · 상품 %s건 · 플랫폼 %s" % (
        g["observed_from"], g["observed_to"], "{:,}".format(g["rows"]),
        ", ".join(g["sites"])))
    # 어느 단위로 센 숫자인지 모르면 모든 n을 잘못 읽는다 (#7)
    folded = res.get("folded_variants")
    if folded:
        pts.append("그룹 비교와 상관은 **스타일 단위**로 셌다 — 색상 변형 %s건을 접었다"
                   "(무신사는 `(5 COLORS)`로 한 줄, 자사몰은 색상마다 한 줄이라 "
                   "그대로 세면 플랫폼마다 기준이 달라진다). 품절·재고는 상품 단위 그대로다."
                   % "{:,}".format(folded))
    # D42: 표기 그대로 쓰는 축의 한계를 **인사이트 PDF 첫 장에서** 밝힌다 —
    # 카드만 보고 판단하는 사람은 상세 PDF의 각주를 안 읽는다.
    shown = [h for h in res.get("strong", []) + res.get("weak", [])
             if h.get("kind") == "group_compare"]
    if any(h.get("cat_field") == "category" for h in shown):
        sk = res.get("lineage_skipped") or {}
        pts.append(
            "카테고리 값은 **사이트 표기 그대로**다 — 상위·하위 분류가 한 축에 섞여 있다"
            "(예: 「하의」와 「미니」). 대등하지 않은 쌍 %d개를 비교에서 뺐다"
            "(상위-하위 %d · 굵기 차이 %d). **사이트가 계층을 안 밝힌 값은 못 걸렀으니** "
            "두 값이 같은 레벨인지 보고 읽어라."
            % (sum(sk.values()), sk.get("ancestor", 0), sk.get("granularity", 0)))
    if any(h.get("cat_field") == "brand" for h in shown):
        pts.append(
            "브랜드 값도 표기 그대로다 — 같은 브랜드가 플랫폼마다 다르게 적히면"
            "(「로우클래식」 대 「LOW CLASSIC」) 브랜드 차이가 아니라 **플랫폼 차이**를 "
            "보고 있는 것이다.")
    return pts


# 지표의 성격 — **절대값인지 비율인지** 카드가 말한다 (D60 후속, 2026-08-04 사용자
# 지시: "하트나 조회수, 판매량 같은 거 비율인지 아닌지 표기 좀 해"). 하트·누적판매는
# 시간이 쌓는 누적 절대값이라 "요즘 반응"이 아니고, 전환(%)만 비율이다 — 이 구분을
# 모르면 누적값을 비율처럼 읽는다.
METRIC_NATURE = {
    "like_count": "누적 절대값", "review_count": "누적 절대값",
    "purchase_count": "누적 절대값", "view_count": "누적 절대값",
    "viewers_now": "실시간 절대값", "rating": "0~5 척도 절대값",
    "view_band": "구간 하한·누적 절대값", "purchase_band": "구간 하한·누적 절대값",
    "like_band": "구간 하한·누적 절대값",
    "cvr_view_like": "비율(%)", "cvr_view_buy": "비율(%)",
    "cvr_like_buy": "비율(%)", "review_per_buy": "비율(%)",
    "discount_rate": "비율(%)", "price_sale": "원", "price_original": "원",
    "sold_min": "누적 절대값", "opt_out_rate": "비율(%)",
}


def _nature(h):
    """이 카드 지표의 성격. 모르는 지표는 빈 문자열 — 없는 말을 지어내지 않는다."""
    fields = ([h.get("y"), h.get("x")] if h.get("kind") == "correlation"
              else [h.get("metric")])
    parts = []
    for f in fields:
        n = METRIC_NATURE.get(f)
        if n:
            lbl = (h.get("y_label") if f == h.get("y") else
                   h.get("x_label") if f == h.get("x") else h.get("metric_label")) or f
            # 라벨에 이미 (%)·(하한)이 있으면 중복 표기하지 않는다
            parts.append("%s=%s" % (lbl.split("(")[0].strip(), n))
    return " · ".join(parts)


def _n_note(h):
    """근거 줄의 표본 표기 (D60).

    2026-08-04 사용자 지적: "화이트가 원래 소수인 카테고리인데, 품목이 적어서 하트가
    낮은 건지 품목 대비(상품 1개당) 낮은 건지 명시해줘". 그룹 비교의 n을 합계 하나로
    적으면 **총합 비교로 오해된다** — 실제로는 상품 1개당 분포 비교다. 그룹마다
    상품 수와 중앙값을 병기해 "품목 수가 많은 쪽이 이기는 비교가 아니다"를 카드
    자체가 말하게 한다.
    """
    if h.get("kind") == "group_compare" and h.get("n_a") is not None:
        base = "상품 1개당 비교 — %s %s개(중앙값 %s) 대 %s %s개(중앙값 %s)" % (
            h.get("group_a"), "{:,}".format(h.get("n_a") or 0), _fmt(h.get("median_a")),
            h.get("group_b"), "{:,}".format(h.get("n_b") or 0), _fmt(h.get("median_b")))
        nat = _nature(h)
        return base + (" · " + nat if nat else "")
    if h.get("kind") == "paired":
        base = "같은 상품끼리 쌍 비교 — %s쌍" % "{:,}".format(h.get("n") or 0)
    else:
        base = "n=%s" % "{:,}".format(h.get("n") or 0)
    nat = _nature(h)
    return base + (" · " + nat if nat else "")


def build_insight_pdf(res, out_path, target, detail_pages):
    g = res["eda"]["grain"]
    d = Doc("인사이트 — %s" % target,
            subtitle="%s 생성 · 관측 %s ~ %s · 가설 %d개 검정" % (
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                g["observed_from"], g["observed_to"], res["generated"]))
    d.honesty(honesty_points(res))
    # 머리 통계 스트립 — "이 리포트에 뭐가 몇 개 있나"를 첫 화면에서 답한다.
    # **결론(있다+없다)을 한 칸에 합쳐 보여준다** — 나눠 놓으면 "차이 없음"이
    # 부산물처럼 보이는데, 실제로는 그것도 결론이다 (D49).
    nulls = res.get("null_findings") or []
    d.stats([("말할 수 있는 것", len(res["strong"]) + len(nulls)),
             ("판단 보류", len(res["weak"])),
             ("상품(관측 단위)", g["rows"]),
             ("검정한 가설", res["generated"])])

    # ── 레이아웃 축을 바꿨다 (D49) ────────────────────────────────────
    # 전에는 **효과가 크냐 작냐**로 줄을 세워 「차이가 없었다」가 약한 단서보다
    # 아래에 있었다. 그런데 n=500에서 효과가 0.03이면 그건 약한 게 아니라
    # **"여기 신경 쓰지 마라"를 자신 있게 말할 수 있는 결론**이다.
    #
    # 축을 **말할 수 있냐 없냐**로 바꾼다:
    #     ① 차이가 있었다 + ② 차이가 없었다   ← 둘 다 결론. 나란히 둔다
    #     ③ 판단 보류                        ← 아직 말할 수 없는 것
    d.h2("① 차이가 있었다 (%d개)" % len(res["strong"]))
    if not res["strong"]:
        d.para("5관문을 모두 통과한 가설이 없다. **빈자리를 판단 보류로 채우지 않았다** — "
               "그렇게 하면 이 구분이 무의미해진다. 아래 ②·③을 본다.")
    for i, h in enumerate(res["strong"], 1):
        page = detail_pages.get(_anchor(h, "s", i))
        d.card(h["claim"], audience=h["audience"],
               action=_action_for(h, res),     # AI 우선, 없으면 템플릿 (D64)
               evidence="%s %s · %s · p=%s · %s" % (
                   h.get("effect_kind", "효과"), _fmt(h.get("effect")),
                   _n_note(h),
                   ("%.3f" % h["p"]) if h.get("p") is not None else "—",
                   h.get("holdout_note", "")),
               detail_link=("상세 %d쪽" % page) if page else None)

    if nulls:
        d.h2("② 차이가 없었다 (%d개)" % len(nulls))
        d.para("**이것도 결론이다.** 표본이 모자라 못 본 게 아니라 **충분히 보고도 차이를 "
               "찾지 못한** 것들이다 — 여기에는 힘을 쓰지 않아도 된다는 근거로 쓴다. "
               "표본 부족으로 못 본 것은 아래 ③에 있다(그건 '없다'가 아니라 '모른다'다).")
        for h in nulls:
            d.card(_null_claim(h), audience=h.get("audience"),
                   # 카드마다 그 카드의 값으로 쓴다 (D56 3차 피드백 — "이 축은 신경
                   # 쓰지 않아도 된다"가 전 카드에 똑같이 붙어 공간만 차지했다).
                   # "차이가 없다"의 쓸모는 **자유롭게 정해도 된다는 허가**다 — 그걸
                   # 그 축의 말로 적어야 읽는 사람이 자기 일에 대입할 수 있다.
                   action=_action_for(h, res, claim=_null_claim(h)) \
                       if (res.get("ai_actions") or {}).get(_null_claim(h)) \
                       else _null_action(h),
                   evidence="%s %s (차이 없음 수준) · %s" % (
                       h.get("effect_kind", "효과"), _fmt(h.get("effect")),
                       _n_note(h)))

    d.h2("③ 판단 보류 (%d개)" % len(res["weak"]))
    d.para("아래는 **가설이지 결론이 아니다.** ①②와 달리 아직 말할 수 없는 것들이다 — "
           "어느 관문을 통과하지 못했는지와 어떻게 재확인하는지를 적었다.")
    for i, h in enumerate(res["weak"], 1):
        page = detail_pages.get(_anchor(h, "w", i))
        # 약한 단서에도 액션을 준다 (PR #9 리뷰). D47은 "약한 단서는 '아직 정하지
        # 마라'로 시작한다"고 정했는데 렌더가 action=을 안 넘겨 **그 분기가 유닛
        # 테스트에서만 돌고 PDF에는 한 번도 안 나왔다.**
        # 재확인 문구는 액션 줄이 이미 담으므로 evidence에서 뺀다(중복 제거).
        d.card(h["claim"], audience=h["audience"], weak=True,
               action=_action_for(h, res),
               evidence="%s %s · %s · 미통과: %s" % (
                   h.get("effect_kind", "효과"), _fmt(h.get("effect")),
                   _n_note(h),
                   h["fails"][0] if h["fails"] else "—"),
               detail_link=("상세 %d쪽" % page) if page else None)

    if res["rejected"]:
        d.h2("④ 검정했으나 결론에 못 넣은 것 (%d개)" % len(res["rejected"]))
        d.para("데이터가 부정한 가설이다. **같은 막다른 길을 다시 파지 않기 위해 남긴다.**")
        for h in res["rejected"][:10]:
            d.para("· %s — %s" % (h["claim"], h.get("holdout_note", "")), style="small")
    return d.save(out_path)


def _anchor(h, prefix, i):
    return "%s%d" % (prefix, i)


def build_detail_pdf(res, out_path, target):
    """상세 리포트. 가설 하나가 한 섹션이고, 인사이트가 쪽 번호로 가리킨다."""
    g = res["eda"]["grain"]
    d = Doc("상세 리포트 — %s" % target,
            subtitle="인사이트 PDF의 근거 · %s 생성" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    d.honesty(honesty_points(res))

    d.h2("분석 계획 — 어떤 기법을 왜 썼나")
    d.para("**기각된 것도 싣는다.** 어떤 분석을 *안 한 것*과 *못 한 것*은 다르고, "
           "그 구분이 없으면 독자는 빠진 분석을 그냥 없는 것으로 읽는다.")
    rows = []
    for e in res.get("plan", []):
        mark = {"adopted": "채택", "rejected": "기각",
                "adopted_empty": "채택→0건", "ai_suggested": "AI 제안"}.get(e["status"], e["status"])
        if e.get("overridden_by_ai"):
            mark += " (AI)"
        rows.append([mark, e["name"], e.get("reason", ""),
                     str(e.get("produced", "—")) if e["status"].startswith("adopted") else "—"])
    d.table(["판정", "기법", "근거", "가설"], rows,
            widths=[12, 22, 56, 10], align_right=(3,))
    for e in res.get("plan", []):
        if e.get("warning"):
            d.para("**주의 (%s)**: %s" % (e["name"], e["warning"]), style="small")
    d.h3("각 기법이 답하는 질문")
    d.table(["기법", "질문", "방법", "왜 이 기법인가"],
            [[m["name"], m["question"], m["how"], m["why"]] for m in an.METHODS],
            widths=[16, 24, 22, 38])

    d.page_break()
    d.h2("데이터 프로파일 (EDA)")
    d.h3("결측")
    d.para("미노출(값이 없음)과 0은 다르다. 아래 결측률은 **사이트가 보여주지 않은** 비율이다.")
    d.figure(chart.missing_bar(res["eda"]["nulls"]),
             caption="결측률 상위 축. 미노출과 0은 다른 값이라 한 막대에 쌓지 않았다 — "
                     "0 건수는 아래 표에 있다.")
    d.table(["지표", "결측", "결측률", "0인 건수", "판정"],
            [[n["label"], "{:,}".format(n["missing"]), "%.1f%%" % n["missing_pct"],
              "{:,}".format(n["zeros"]), n["status"]] for n in res["eda"]["nulls"]],
            widths=[26, 16, 16, 18, 14], align_right=(1, 2, 3))
    d.h3("분포")
    d.table(["지표", "n", "중위", "평균", "왜도", "이상치(IQR)"],
            [[x["label"], "{:,}".format(x["n"]), _fmt(x["median"]), _fmt(x["mean"]),
              _fmt(x["skew"]), "{:,}".format(x["iqr_outliers"])]
             for x in res["eda"]["distributions"]],
            widths=[26, 14, 16, 16, 12, 16], align_right=(1, 2, 3, 4, 5))
    d.small("이상치는 제거하지 않았다 — 히트 상품일 수 있다.")

    for i, h in enumerate(res["strong"], 1):
        d.page_break()
        d.h2("[강한 주장 %d] %s" % (i, h["claim"]), anchor=_anchor(h, "s", i))
        _detail_body(d, h)
    for i, h in enumerate(res["weak"], 1):
        d.page_break()
        d.h2("[약한 단서 %d] %s" % (i, h["claim"]), anchor=_anchor(h, "w", i))
        _detail_body(d, h)
    d.save(out_path)
    return d._anchors


_AXIS_CAVEAT = {
    "category": "**카테고리 값은 사이트 표기 그대로다.** 상위·하위가 한 축에 섞여 있어서, "
                "DB의 경로 형태 값(`여성의류 > 스커트 > 미디`)에서 배운 포함 관계로 "
                "그런 쌍을 걸러냈다(D42). **경로를 밝히지 않은 쌍은 못 걸렀다** — "
                "위 두 값이 같은 레벨인지 확인하고 읽어라.",
    "brand": "**브랜드 값은 사이트 표기 그대로다.** 같은 브랜드가 플랫폼마다 다르게 적히면"
             "(예: 「로우클래식」과 「LOW CLASSIC」) 두 그룹은 브랜드 차이가 아니라 "
             "**플랫폼 차이**를 보고 있는 것이다 — 표기를 먼저 확인해라.",
}


def _detail_body(d, h):
    d.h3("판정 근거")
    rows = [["효과 크기", "%s %s" % (h.get("effect_kind", ""), _fmt(h.get("effect")))],
            ["표본", "{:,}".format(h.get("n") or 0)],
            ["p (순열검정)", ("%.4f" % h["p"]) if h.get("p") is not None else "—"],
            ["홀드아웃", h.get("holdout_note", "—")],
            ["청중", h.get("audience", "—")]]
    if h["kind"] == "group_compare":
        rows.insert(0, ["비교", "%s: %s (n=%d) 대 %s (n=%d)" % (
            h["cat_label"], h["group_a"], h["n_a"], h["group_b"], h["n_b"])])
        rows.insert(1, ["중앙값", "%s 대 %s" % (_fmt(h["median_a"]), _fmt(h["median_b"]))])
    d.table(["항목", "값"], rows, widths=[24, 76])
    if h["kind"] == "group_compare" and h.get("a") and h.get("b"):
        d.figure(chart.dist_compare(h["a"], h["b"], h["group_a"], h["group_b"],
                                    h.get("metric_label", "")),
                 caption="두 그룹의 분포. 중앙값이 갈려도 상자가 크게 겹치면 차이가 "
                         "약하다는 뜻이다 — 효과 크기와 함께 읽어라.")
    if h["kind"] == "group_compare" and h["cat_field"] in ("category", "brand"):
        # 사이트가 준 문자열을 그대로 쓴다(없는 정규화를 만들지 않는다는 원칙). 그 대가로
        # 카테고리는 상위·하위 레벨이 섞이고, 브랜드는 같은 브랜드가 표기만 다르게
        # 들어온다("로우클래식" 대 "LOW CLASSIC"). 그런 쌍은 대등한 비교가 아니라
        # 사실상 플랫폼 대리 변수다. 코드로는 못 가리므로 읽는 사람에게 넘긴다.
        d.para(_AXIS_CAVEAT[h["cat_field"]], style="small")

    if h.get("fails"):
        d.h3("통과하지 못한 관문")
        for f in h["fails"]:
            d.para("· " + f, style="small")
        d.h3("재확인 방법")
        for line in recheck_lines(h):
            d.para("· " + line, style="small")

    if h["kind"] == "correlation" and h.get("trend"):
        d.h3("구간별 추이")
        d.para("상관계수는 방향만 말한다. **어느 구간인지**는 아래를 본다.")
        d.figure(chart.bins_bar(h["trend"], h["x_label"], "%s 중앙값" % h["y_label"]),
                 caption="구간별 %s. 막대는 0에서 시작한다." % h["y_label"])
        d.table(["%s 구간" % h["x_label"], "n", "%s 중앙값" % h["y_label"]],
                [["%s ~ %s" % (_fmt(t["from"]), _fmt(t["to"])), "{:,}".format(t["n"]),
                  _fmt(t["median"])] for t in h["trend"]],
                widths=[48, 20, 32], align_right=(1, 2))

    if h["kind"] == "dose_response" and h.get("study", {}).get("bins"):
        st_ = h["study"]
        d.figure(chart.bins_bar(st_["bins"], "할인 폭(%)", "하트 증분 중앙값"),
                 caption="할인 폭 구간별 반응. **하트 증분은 판매량이 아니다** — 대리 지표다.")
        d.table(["할인 폭", "n", "증분 중앙값"],
                [["%s~%s%%" % (_fmt(b["from"]), _fmt(b["to"]) if b.get("to") else ""),
                  "{:,}".format(b["n"]), _fmt(b["median"])] for b in st_["bins"]],
                widths=[40, 24, 36], align_right=(1, 2))
    if h["kind"] == "event_study" and h.get("study"):
        s = h["study"]
        d.h3("사건 연구")
        d.table(["항목", "값"],
                [["사건 수", "{:,}".format(s["n_events"])],
                 ["가격 인하", "{:,}".format(s["n_cuts"])],
                 ["가격 인상", "{:,}".format(s["n_rises"])],
                 ["대조군", s.get("control", "—")],
                 ["인하 후 하트 증분(중위)", _fmt(s.get("cut_like_delta_median"))],
                 ["할인 폭 ~ 증분 상관", _fmt(s.get("depth_response_r"))]],
                widths=[40, 60])
        d.para("**하트 증분은 판매량이 아니다** — 판매 대리 지표다. 실제 판매는 "
               "옵션 재고 감소분이 축적돼야 계산된다.", style="small")


def save_to_db(db_path, res, target, contexts, stamp, detail_pdf, pages):
    """인사이트를 DB에 적재한다 — **시트 미러가 실어 나를 유일한 통로**다 (D31 개정).

    팀원은 시트를 읽고 DB는 한 사람이 갖는다(2026-08-03 사용자 확정). PDF는 그 한 사람
    손에서만 나오므로, 결과가 DB를 거치지 않으면 팀에 닿을 길이 없다. 파이프라인
    원칙과 같다 — 무엇도 DB를 건너뛰지 않는다.

    같은 (run_stamp, target)을 다시 쓰면 덮는다. 재실행이 흔하고, 같은 시각의 같은
    대상은 같은 분석이기 때문이다.
    """
    conn = db_connect(db_path)
    ctx = ", ".join(contexts) if contexts else ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for verdict, items in (("strong", res["strong"]), ("weak", res["weak"]),
                           ("rejected", res["rejected"])):
        for i, h in enumerate(items, 1):
            prefix = {"strong": "s", "weak": "w"}.get(verdict)
            page = pages.get("%s%d" % (prefix, i)) if prefix else None
            rows.append((
                stamp, target, ctx, verdict, i,
                h.get("claim"), h.get("audience"),
                h.get("effect"), h.get("effect_kind"), h.get("n"), h.get("p"),
                h.get("holdout_note"),
                " / ".join(h.get("fails") or []) or None,
                recheck_hint(h) if verdict == "weak" else None,
                os.path.basename(detail_pdf), page, now))
    conn.executemany(
        "INSERT OR REPLACE INTO insights (run_stamp, target, context, verdict, idx, "
        "claim, audience, effect, effect_kind, n, p, holdout, fails, recheck, "
        "detail_pdf, detail_page, created_at) VALUES (%s)" % ",".join("?" * 17), rows)
    conn.commit()
    conn.close()
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description="인사이트 엔진 — 강한 주장 5 + 약한 단서 20")
    # D72: INTEL_DB_URL(Turso) > INTEL_DB > data/intel.db. 명시적 --db가 이긴다
    ap.add_argument("--db", default=default_db_target())
    ap.add_argument("--context", action="append", default=[])
    ap.add_argument("--out", default="output", help="PDF를 넣을 디렉터리")
    ap.add_argument("--target", help="리포트 제목에 쓸 대상 이름 (생략하면 문맥에서)")
    ap.add_argument("--ai-notes", help="분석 단에 넘길 AI 예외 판단 JSON (exclude/warn/add)")
    ap.add_argument("--own-brand", action="append", default=[],
                    help="우리 브랜드 이름. 브랜드 축에서 **제3자끼리의 비교를 뺀다**(D56). "
                         "여러 번 줄 수 있다. `brand:X` 문맥은 자동으로 포함된다")
    ap.add_argument("--ai-actions", help="AI가 쓴 액션 플랜 JSON — {주장 문장: [줄, …]}. "
                    "있으면 템플릿 액션 대신 실린다 (D64)")
    ap.add_argument("--no-auto-proxy", action="store_true",
                    help="rule 프록시 자동 생성을 건너뛴다 (기본은 만든다 — D51)")
    ap.add_argument("--signals", help="eda.py --out 산출 JSON — 요청 문맥이 같으면 "
                                      "EDA를 재수행하지 않고 재사용한다 (D74)")
    a = ap.parse_args()

    target = a.target or (", ".join(a.context) if a.context else "전체")
    notes = None
    if a.ai_notes:
        import json
        notes = json.loads(open(a.ai_notes, encoding="utf-8").read())
    signals = None
    if a.signals:
        import json as _j
        signals = _j.loads(open(a.signals, encoding="utf-8").read())
    res, raw = build(a.db, a.context, notes, auto_proxy=not a.no_auto_proxy,
                     own_brands=a.own_brand, signals=signals)
    if res is not None and a.ai_actions:
        import json as _json
        res["ai_actions"] = _json.loads(open(a.ai_actions, encoding="utf-8").read())
    if res is None:
        print("중단: %s" % raw.get("reason"))
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    safe = target.replace("/", "-").replace(":", "-")
    detail_path = os.path.join(a.out, "detail-%s-%s.pdf" % (safe, stamp))
    insight_path = os.path.join(a.out, "insight-%s-%s.pdf" % (safe, stamp))

    # 상세를 먼저 굽는다 — 인사이트가 그 쪽 번호를 가리켜야 하기 때문이다
    pages = build_detail_pdf(res, detail_path, target)
    build_insight_pdf(res, insight_path, target, pages)
    saved = save_to_db(a.db, res, target, a.context, stamp, detail_path, pages)

    print("가설 %d개 검정 → 강한 주장 %d · 약한 단서 %d · 기각 %d" % (
        res["generated"], len(res["strong"]), len(res["weak"]), len(res["rejected"])))
    print("  인사이트: %s" % insight_path)
    print("  상세:     %s" % detail_path)
    print("  DB 적재:  %d행 (insights) — 다음 시트 미러가 팀에 전달한다" % saved)
    print("  후보 %d개 중 선별 · 홀드아웃 재현 확인 %d/%d"
          % (res["strong_pool"], res["verified"], len(res["strong"])))
    if len(res["strong"]) < STRONG_MAX:
        print("  ※ 강한 주장이 %d개다 — 5관문을 통과한 것만 실었고 빈자리를 채우지 않았다."
              % len(res["strong"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
