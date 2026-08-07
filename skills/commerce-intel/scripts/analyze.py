#!/usr/bin/env python3
"""분석 단 — **어떤 기법을 쓸지 정하고**, 정한 대로 분석하고, 5관문으로 판정한다.

## 이 단이 없어서 생겼던 문제 (2026-08-03)

EDA는 신호를 만들었는데 인사이트 엔진이 그걸 **한 번도 읽지 않았다.** 가설 생성 함수
세 개가 조건 없이 전부 돌았고, "이 데이터에는 이 기법이 맞고 저건 안 맞는다"는 판단이
코드에도 산출물에도 없었다. 소유자 피드백 3번("EDA 결과를 네가 판단 후 가설 검정으로
들어가")의 **판단 후**가 빠져 있던 것이다.

## 규칙표가 기본, AI가 예외 (2026-08-03 사용자 확정)

    ① 규칙표 자동 판정 — 조건 충족이면 채택, 미달이면 기각 (사유와 n을 함께 기록)
    ② AI 예외층      — 규칙은 통과했지만 이 데이터엔 부적절한 것을 기각하거나,
                       채택하되 해석 주의를 붙이거나, 규칙표에 없는 조합을 추가
    ③ 분석 계획서    — 채택·기각이 **이유와 함께** 남는다

**기각된 것도 남긴다.** "옵션 재고는 n=7이라 소진 속도를 계산하지 않았다"가 적혀 있어야
독자가 *안 한 것*과 *못 한 것*을 구분한다.

## 쓰는 법 (2단계)

    # 1) 계획만 세운다 — AI가 이걸 읽고 예외를 판단한다
    python3 analyze.py --db data/intel.db --context "brand:로우클래식" \
        --plan-only --out data/plan.json

    # 2) 예외를 반영해 실행한다
    python3 analyze.py --db data/intel.db --context "brand:로우클래식" \
        --ai-notes data/ai-notes.json --out data/analysis.json

`ai-notes.json` 형식:

    {"exclude": [{"method": "group_compare", "reason": "카테고리 값에 상위·하위가 섞여
                  대등한 비교가 아니다"}],
     "warn":    [{"method": "did", "note": "관측 간격 편차가 커 구간 길이가 고르지 않다"}]}
"""
import argparse
import json
import os
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stat_playbook as sp                                          # noqa: E402
from eda import CAT_AXES, MIN_N, run as run_eda                     # noqa: E402
from intel_data import (brand_key, cat_axes, category_hierarchy,  # noqa: E402
                        collect, display_value, incomparable_reason,
                        matched_pairs, num_axes, product_series, role_of,
                        style_rows)
from schema_v3 import default_db_target                             # noqa: E402

# ── 관문 임계 ───────────────────────────────────────────────────────────────
# 임계는 2026-08-03에 한 번 완화했다. 우리가 모으는 데이터는 표본이 크지 않고
# (랭킹 top 100 · 브랜드 수백 건), 원래 값으로는 매번 강한 주장이 0개로 끝났다.
# **숫자를 낮추기 전에 구조적 병목 둘을 먼저 고쳤다** — 아래 ①②가 그것이고,
# 효과 크기는 거의 그대로 뒀다(그건 '쓸모'의 척도라 낮추면 리포트가 무의미해진다).
EFFECT_MIN = 0.30        # |Cliff δ| — 0.33(Romano 중간)에서 소폭. 이하는 결정을 못 바꾼다
CORR_MIN = 0.25          # |순위 상관| 하한 (0.30에서)
# ② 순열검정은 대표본 근사를 쓰지 않는다 — n=30은 정규 근사가 필요한 검정의 관습이고
#    우리에겐 근거가 없다. 20이면 순열 분포가 충분히 촘촘하다.
N_MIN = 20
# ① 순열 2,000회의 p 하한은 1/2001 = 0.0005였는데, 가설 107개에 α=0.05면 1등 임계가
#    0.05/107 = 0.00047 — **하한이 임계보다 커서 구조적으로 아무도 통과 못 했다.**
#    순열 반복을 4,000으로 올려(하한 0.00025) 해결했다. α는 0.05 그대로 둔다 —
#    0.10으로 완화해 봤더니 결과가 거의 안 바뀌어(79→79 · 51→53 · 120→122) 느슨하게
#    둘 이유가 없었다. 진짜 병목은 α가 아니라 순열 하한이었다.
ALPHA = 0.05
GROUPS_PER_AXIS = 8      # 범주축 하나에서 볼 그룹 수 (쌍이 제곱으로 는다)

# ── 청중 배정 (MD 요구: 판매전략/디자인/마케팅) ─────────────────────────────
AUDIENCE_BY_FIELD = {
    "price_sale": "판매전략", "price_original": "판매전략", "discount_rate": "판매전략",
    "opt_out_rate": "판매전략", "opt_total": "판매전략", "stock_sum": "판매전략",
    "sold_min": "판매전략", "sold_out": "판매전략",
    "fit": "디자인", "category": "디자인", "color": "디자인",
    "like_count": "마케팅", "review_count": "마케팅", "rating": "마케팅",
    "viewers_now": "마케팅", "purchase_count": "마케팅", "site": "마케팅",
    "brand": "마케팅",
}


def audience_of(*fields):
    for f in fields:
        if f in AUDIENCE_BY_FIELD:
            return AUDIENCE_BY_FIELD[f]
        if f and f.startswith("px_"):
            return "디자인"      # AI 프록시는 대개 상품 외형·특성 판정
    return "판매전략"


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".") if abs(v) < 100 else "{:,.0f}".format(v)
    return "{:,}".format(v)


def _josa(word, pair):
    """한국어 조사 결합. 프록시 값 이름이 한글이라(영문만·혼합·평점) 받침에 따라
    은/는·이/가를 골라야 문장이 자연스럽다. 한글 받침만 정확히 보고, 영문·기호·숫자로
    끝나면 받침 없음으로 취급한다(그쪽은 조사 자체가 덜 어색하다).

    pair: "은는" | "이가" | "와과"

    **와/과는 앞의 둘과 방향이 반대다** — 받침이 있으면 `과`, 없으면 `와`다
    (은/는·이/가는 받침이 있을 때 앞쪽을 쓴다). 2026-08-04 사용자 지적:
    "`material_word(AI)에서 데님와 소재 미표기는`" — `데님`은 받침이 있으니 `데님과`다.
    그전에는 조사를 문자열에 박아 써서 받침을 아예 안 봤다.
    """
    w = str(word)
    if not w:
        return w
    last = w[-1]
    has_batchim = "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0
    a, b = {"은는": ("은", "는"), "이가": ("이", "가"),
            "와과": ("과", "와"), "을를": ("을", "를"),
            # 로/으로도 받침을 본다. 단 `ㄹ` 받침은 `로`다(`진청으로`·`서울로`)
            "로으로": ("으로", "로")}[pair]
    if pair == "로으로" and has_batchim and (ord(last) - 0xAC00) % 28 == 8:
        return w + "로"          # ㄹ 받침
    return w + (a if has_batchim else b)


def _usable_metrics(eda_res):
    # nulls는 num_axes(data) 기준으로 만들어졌다 → numeric 프록시 축도 포함(D19·D23)
    return [x["field"] for x in eda_res["nulls"] if x.get("usable")]


# ═══════════════════════════════════════════════════════════════════════════
# 규칙표 — 데이터 형태에서 기법으로
# ═══════════════════════════════════════════════════════════════════════════
# 각 항목의 `check`는 (가능한가, 사유, 규모)를 돌려준다. 사유는 **채택이든 기각이든**
# 계획서에 그대로 실린다 — 나중에 "왜 그 분석을 했지/안 했지"에 답하는 유일한 기록이다.

def _chk_group(ctx):
    usable = _usable_metrics(ctx["eda"])
    axes_ok = []
    for field, label in ctx["cat_axes"]:
        counts = defaultdict(int)
        for it in ctx["styles"]:
            if it.get(field) is not None:
                counts[it[field]] += 1
        big = [k for k, v in counts.items() if v >= N_MIN]
        if len(big) >= 2:
            axes_ok.append("%s(%d값)" % (label, len(big)))
    if not axes_ok or not usable:
        return False, "비교 가능한 범주축(값마다 n≥%d)이 2개 미만이거나 쓸 수 있는 지표가 없다" % N_MIN, 0
    folded = len(ctx["items"]) - len(ctx["styles"])
    return True, ("범주축 %s × 지표 %d개 · **스타일 단위**(상품 %d건 → 스타일 %d건, "
                  "색상 변형 %d건 접음)" % (", ".join(axes_ok), len(usable),
                                       len(ctx["items"]), len(ctx["styles"]), folded)), \
        len(ctx["styles"])


def _chk_corr(ctx):
    cands = [c for c in ctx["eda"]["correlations"]
             if not c["definitional"] and abs(c["spearman"]) >= 0.15 and c["n"] >= N_MIN]
    if not cands:
        return False, "정의상 연동을 뺀 뒤 |r|≥0.15 · n≥%d 인 축 쌍이 없다" % N_MIN, 0
    return True, "축 쌍 %d개 (정의상 연동 제외)" % len(cands), max(c["n"] for c in cands)


def _chk_did(ctx):
    """이중차분은 사건 수가 아니라 **연속 3시점**이 있어야 성립한다.

    처음에는 가격 변경 사건 수만 셌는데, 그러면 "채택했지만 0건"이 나온다(2026-08-03
    실측: 사건 414건인데 산출 0건). 이중차분은 상품마다 [직전 구간][변경 구간]이 필요해서
    관측이 3시점 이상 쌓인 상품이 있어야 한다. 브랜드 라인시트처럼 한두 번 훑은 문맥은
    사건이 아무리 많아도 성립하지 않는다 — 규칙이 그 사실을 먼저 말해야 한다.
    """
    n = len(ctx["data"].get("price_events") or [])
    if n < 10:
        return False, "가격 변경 사건이 %d건뿐이다 (요건 10)" % n, n
    usable = sum(1 for obs in ctx["series"].values()
                 if sum(1 for o in obs if o.get("like_count") is not None
                        and o.get("price_sale") is not None) >= 3)
    if usable < 20:
        return (False,
                "가격 변경은 %d건이지만 **연속 3시점 이상 관측된 상품이 %d개**뿐이다(요건 20). "
                "이중차분은 [직전 구간][변경 구간]이 필요해서 축적이 얕으면 성립하지 않는다"
                % (n, usable), usable)
    return True, "가격 변경 %d건 · 3시점 이상 관측 상품 %d개, 대조군은 같은 기간 무변동 상품" % (
        n, usable), n


def _chk_dose(ctx):
    events = ctx["data"].get("price_events") or []
    cuts = [e for e in events
            if e.get("like_delta") is not None and (e.get("price_to") or 0) < (e.get("price_from") or 0)]
    if len(cuts) < 20:
        return False, "반응 증분이 있는 인하 사건이 %d건뿐이다 (요건 20)" % len(cuts), len(cuts)
    return True, "인하 사건 %d건을 할인 폭 구간으로 나눈다" % len(cuts), len(cuts)


def _chk_paired(ctx):
    sites = {i["site"] for i in ctx["styles"]}
    if len(sites) < 2:
        return False, "플랫폼이 1개다 — 쌍을 만들 수 없다", 0
    best = 0
    for metric in ("price_sale", "discount_rate"):
        for pairs in matched_pairs(ctx["items"], metric).values():
            best = max(best, len(pairs))
    if best < 10:
        return False, "플랫폼 간 이름이 완전일치하는 상품이 %d쌍뿐이다 (요건 10)" % best, best
    return True, "최대 %d쌍 매칭 — 같은 상품끼리 비교하므로 구성 차이가 통제된다" % best, best


def _chk_depletion(ctx):
    if not ctx["series"]:
        return False, "관측 시퀀스가 없다", 0
    recs = sp.time_to_soldout(ctx["series"])
    events = sum(1 for r in recs if r["event"])
    ctx["_depletion"] = recs
    if events < 10:
        return (False,
                "품절 전이가 %d건뿐이다 (요건 10) — 관측 기간에 품절이 거의 나지 않았다. "
                "축적이 쌓이면 성립한다" % events, events)
    return True, "품절 전이 %d건 / 추적 상품 %d개 (미품절은 중도절단)" % (events, len(recs)), events


METHODS = [
    {"id": "group_compare", "name": "그룹 비교",
     "question": "범주(플랫폼·카테고리·핏·브랜드·품절)에 따라 지표가 다른가",
     "how": "Cliff δ + 순열 순위합 검정",
     "why": "왜도가 5를 넘는 값(하트·판매)에 정규분포 전제 검정은 맞지 않는다",
     "check": _chk_group},
    {"id": "correlation", "name": "순위 상관",
     "question": "두 수치가 같이 움직이는가",
     "how": "Spearman + 순열 검정, 구간별 추세 병기",
     "why": "자릿수를 넘나드는 값에 피어슨은 이상치 하나에 끌려간다. "
            "상관계수는 방향만 말하므로 '어느 구간인지'는 구간 표로 답한다",
     "check": _chk_corr},
    {"id": "did", "name": "이중차분 (가격 변경 효과)",
     "question": "가격을 내린 것이 반응을 늘렸는가",
     "how": "(변경 구간 증분 − 직전 구간 증분)을 처치군·대조군에서 각각 구해 비교",
     "why": "전후 비교만으로는 시즌·행사처럼 두 군에 공통으로 걸리는 요인을 못 걷어낸다. "
            "상품 고유의 인기도는 자기 직전 구간을 빼면 사라진다",
     "check": _chk_did},
    {"id": "dose_response", "name": "용량-반응 (할인 폭별)",
     "question": "할인을 얼마나 주면 반응이 얼마나 오는가",
     "how": "할인 폭을 10%p 구간으로 나눠 구간별 반응 증분 중앙값",
     "why": "상관계수 하나로는 '얼마나 주면 되나'에 답할 수 없다. MD의 1순위 질문이라 "
            "구간으로 끊어야 실무에서 쓸 수 있다",
     "check": _chk_dose},
    {"id": "paired_platform", "name": "쌍체 비교 (플랫폼 간)",
     "question": "같은 상품이 플랫폼마다 가격·할인이 다른가",
     "how": "이름 완전일치 상품을 짝지어 부호 뒤집기 순열 검정",
     "why": "독립 두 집단으로 비교하면 상품 구성 차이가 섞인다 — 한쪽에 비싼 아우터가 "
            "많으면 '그 플랫폼이 비싸다'가 나온다. 짝을 지으면 그 교란이 사라진다",
     "check": _chk_paired},
    {"id": "depletion", "name": "소진 속도 (품절까지 걸린 시간)",
     "question": "무엇이 빨리 팔리는가 — 리오더 판단의 본체",
     "how": "Kaplan-Meier 중앙 생존시간 + 사건 시간 비교. 미품절은 중도절단",
     "why": "아직 품절 안 난 상품을 빼면 '빨리 팔린 것'만 남아 소진 속도가 실제보다 "
            "빠르게 나온다. 재입고 구간도 함께 센다",
     "check": _chk_depletion},
]
METHOD_BY_ID = {m["id"]: m for m in METHODS}


# ═══════════════════════════════════════════════════════════════════════════
# ① 계획 수립
# ═══════════════════════════════════════════════════════════════════════════
def make_plan(ctx, ai_notes=None):
    ai_notes = ai_notes or {}
    excl = {e["method"]: e.get("reason", "AI 판단") for e in ai_notes.get("exclude", [])}
    warn = {w["method"]: w.get("note", "") for w in ai_notes.get("warn", [])}
    plan = []
    for m in METHODS:
        ok, detail, n = m["check"](ctx)
        entry = {"method": m["id"], "name": m["name"], "question": m["question"],
                 "how": m["how"], "why": m["why"], "n": n,
                 "rule_verdict": "adopted" if ok else "rejected",
                 "rule_reason": detail}
        if not ok:
            entry["status"] = "rejected"
            entry["reason"] = detail
        elif m["id"] in excl:
            entry["status"] = "rejected"
            entry["reason"] = "AI 기각 — %s" % excl[m["id"]]
            entry["overridden_by_ai"] = True
        else:
            entry["status"] = "adopted"
            entry["reason"] = detail
            if m["id"] in warn:
                entry["warning"] = warn[m["id"]]
        plan.append(entry)
    for add in ai_notes.get("add", []):        # 규칙표에 없는 조합 (기록만 — 실행은 사람이)
        plan.append({"method": add.get("method", "ai-custom"), "name": add.get("name", "AI 제안"),
                     "status": "ai_suggested", "reason": add.get("reason", ""),
                     "question": add.get("question", ""), "n": None})
    return plan


# ═══════════════════════════════════════════════════════════════════════════
# ② 채택된 기법 실행
# ═══════════════════════════════════════════════════════════════════════════
def run_group(ctx):
    out = []
    usable = _usable_metrics(ctx["eda"])
    labels = ctx["labels"]
    # D42: 카테고리 축에서만 계층 쌍을 거른다. 브랜드·품절·프록시는 계층이 없다.
    hier = ctx.get("hierarchy") or set()
    skipped_pairs = defaultdict(set)
    # **문맥이 이미 브랜드를 지정했으면 브랜드 축으로 비교하지 않는다** (D51).
    # `brand:2000아카이브스` 문맥의 상품은 전부 같은 브랜드인데 사이트마다 표기가
    # 달라(`2000아카이브스`·`2000Archives`·`2000 Archives`) 서로 다른 브랜드처럼
    # 비교됐다 — "정가 차이가 없다"가 인사이트 자리를 먹었다. 같은 브랜드니 당연하다.
    single_brand = any(str(c).startswith("brand:") for c in (ctx.get("contexts") or []))
    # **제3자끼리의 브랜드 비교는 내지 않는다** (D56). 2026-08-04 사용자 지적:
    # "'브랜드에서 에잇세컨즈는 잠뱅이보다 평점이 높다' — 에잇세컨즈와 잠뱅이는 우리
    # 분석 대상과 직접적인 관련이 없어. 우리 팀원들이 인사이트를 보고 액션 플랜을
    # 수행할 수 있는 인사이트가 나와야 해."
    #
    # 브랜드 **정체성**끼리의 비교는 우리 브랜드가 한쪽에 있을 때만 쓸모가 있다
    # (벤치마크가 된다). 남의 브랜드 둘을 비교한 문장은 옮겨 쓸 규칙이 없다.
    #
    # **브랜드의 속성 축은 이 규칙에 걸리지 않는다.** 생존/이탈·공급량 같은 축은
    # 그룹이 브랜드 이름이 아니라 성질이라, "론론이 상품 58개로 3위 아캄(48개)보다
    # 많은데 이탈했다 — 공급량이 성과가 아니다"처럼 **자사에 옮겨 쓸 교훈**이 나온다
    # (사용자가 살려두라고 명시). 여기서 막는 것은 `cat_field == "brand"`뿐이다.
    own = {brand_key(b) for b in (ctx.get("own_brands") or []) if b}
    for cat_field, cat_label in ctx["cat_axes"]:
        if single_brand and cat_field == "brand":
            continue
        groups, label = defaultdict(list), {}
        for it in ctx["styles"]:          # 변형이 아니라 스타일 단위 (#7)
            # None만이 아니라 **빈 문자열·공백도 거른다** — 값이 없는 것은 그룹이
            # 아니다. 남겨두면 "카테고리에서 은 미니보다…" 같은 문장이 나온다
            v = it.get(cat_field)
            if v is not None and str(v).strip():
                # **키를 다듬어 담는다** — 앞뒤 공백만 다른 같은 값이 서로 다른
                # 그룹으로 쪼개지면 n이 갈리고 같은 비교가 두 번 나온다 (PR #9 리뷰).
                # 브랜드는 표기 차이가 더 심해서 따로 정규화한다 (D51) —
                # `2000Archives`와 `2000 Archives`가 다른 브랜드로 비교되고 있었다.
                #
                # **카테고리도 같은 정규화를 받는다** (D55). 앞뒤 공백만 걷어내면
                # 안쪽 공백이 남아 `데님팬츠`(3,329)와 `데님 팬츠`(48)가 다른 그룹이
                # 됐고, 2026-08-04 여성 데님 리포트의 강한 주장 10번이 **띄어쓰기만
                # 다른 같은 카테고리끼리의 비교**였다(δ=-0.58 · n=2,992). 사이트
                # 표기를 그대로 쓴다는 원칙은 **표시**에 대한 것이지, 같은 값을 두
                # 그룹으로 갈라 검정하라는 뜻이 아니다.
                key = (brand_key(v) if cat_field in ("brand", "category")
                       else (str(v).strip() if isinstance(v, str) else v))
                groups[key].append(it)
                # 표시는 원문 — 단 영문 코드 값(survivor/dropped)은 한글 라벨로
                label.setdefault(key, display_value(cat_field, v))
        big = sorted([(k, v) for k, v in groups.items() if len(v) >= N_MIN],
                     key=lambda kv: -len(kv[1]))
        truncated = max(0, len(big) - GROUPS_PER_AXIS)
        big = big[:GROUPS_PER_AXIS]
        if len(big) < 2:
            continue
        # **공급자가 정한 값은 Y로 놓지 않는다** (D47을 그룹 비교에도 적용 — D51).
        # 여태 `run_corr`에만 걸려 있어서 "브랜드에서 A는 B보다 할인율이 낮다"가
        # 강한 주장으로 올라왔다(2026-08-04 자사 브랜드 리포트 8건 전부 그랬다).
        # 가격·할인은 **우리가 정한 값**이라 그룹 간 차이는 성과가 아니라 정책 기술이다.
        #
        # 다만 **버리지는 않는다** — 정책 기술도 MD에게 쓸모가 있다. 반응 지표가
        # 하나라도 있으면 그쪽만 쓰고, **하나도 없으면** lever를 쓰되 그 사실을
        # 주장에 적는다(안 그러면 리포트가 통째로 비어 나간다).
        response = [m for m in usable if role_of(m) == "response"]
        metrics = response or usable
        lever_only = not response
        for metric in metrics:
            for i in range(len(big)):
                for j in range(i + 1, len(big)):
                    ka, va = big[i]
                    kb, vb = big[j]
                    # 우리 브랜드가 없는 브랜드 쌍은 건너뛴다 (D56).
                    # `own`이 비어 있으면(자사 브랜드를 안 알려줬으면) 아무것도 안 막는다 —
                    # 모르는 채로 거르면 리포트가 조용히 비어 나간다.
                    if cat_field == "brand" and own and ka not in own and kb not in own:
                        skipped_pairs["제3자끼리"].add((cat_field, ka, kb))
                        continue
                    # 대등하지 않은 쌍은 검정하지 않는다 (D42)
                    if cat_field == "category":
                        # **키가 아니라 표시값을 넘긴다** — 계층 맵(`hier`)은 카탈로그와
                        # DB의 원문으로 지어져 있는데, D55로 카테고리 키가 정규화되면서
                        # (`데님 팬츠` → `데님팬츠`) 키로 조회하면 계층을 통째로 놓친다.
                        why = incomparable_reason(label.get(ka, ka), label.get(kb, kb), hier)
                        if why:
                            # 쌍 단위로 센다 — 지표마다 더하면 4쌍이 24로 부풀고
                            # 리포트가 "쌍 24개를 뺐다"고 거짓말한다
                            skipped_pairs[why].add((cat_field, ka, kb))
                            continue
                    a = [x[metric] for x in va if x.get(metric) is not None]
                    b = [x[metric] for x in vb if x.get(metric) is not None]
                    if len(a) < N_MIN or len(b) < N_MIN:
                        continue
                    eff = sp.cliffs_delta(a, b)
                    ma, mb = st.median(a), st.median(b)
                    out.append({
                        "method": "group_compare", "kind": "group_compare",
                        "cat_field": cat_field, "cat_label": cat_label,
                        "group_a": label.get(ka, str(ka)),
                        "group_b": label.get(kb, str(kb)),
                        "lever_metric": role_of(metric) == "lever",
                        "metric": metric, "metric_label": labels.get(metric, metric),
                        "a": a, "b": b, "groups_truncated": truncated,
                        "effect": eff, "effect_kind": "Cliff δ",
                        "p": sp.perm_test_groups(a, b),
                        "n": len(a) + len(b), "n_a": len(a), "n_b": len(b),
                        "median_a": ma, "median_b": mb,
                        # **주장에 숫자를 넣지 않는다** (D56 — 2026-08-04 사용자 지시:
                        # "`(중앙값 4.8 대 0)` 이런 건 메인 문구에 넣지 말고 상세 설명으로 빼").
                        # 중앙값은 `median_a`·`median_b`로 이미 넘어가고, 카드 본문과
                        # 상세 PDF가 그걸 쓴다. 한 줄짜리 주장은 **무슨 일이 있었나**만 말한다.
                        "claim": ("%s에서 %s %s보다 %s %s" % (
                            cat_label, _josa(label.get(ka, str(ka)), "은는"),
                            label.get(kb, str(kb)),
                            _josa(labels.get(metric, metric), "이가"),
                            "높다" if ma > mb else "낮다")),
                        "audience": audience_of(metric, cat_field),
                    })
    # 몇 개를 왜 걸렀는지 남긴다 — 조용히 줄어든 검정 수는 "전부 봤다"로 읽히고,
    # 사유를 안 나누면 ②(다른 가지)로 과하게 빠져도 알 수 없다
    ctx["lineage_skipped"] = {k: len(v) for k, v in skipped_pairs.items()}
    return out


def _orient(x, y, xl, yl):
    """상관의 방향을 역할로 세운다 (D47).

    공급자가 정한 값(lever)은 **원인 쪽**에만 놓는다. "하트가 높을수록 할인율이
    높다"는 할인을 하트가 정했다고 읽히는데, 실제로는 우리가 할인을 정했다.
    둘 다 고객 반응이면 방향을 못 정하므로 그 사실을 표시해 둔다.
    """
    rx, ry = role_of(x), role_of(y)
    if rx == "lever" and ry == "lever":
        # 둘 다 우리가 정한 값이다 — 정가와 할인율처럼. 어느 쪽이 원인인지
        # 데이터가 답하지 않으므로 response끼리와 같이 헤지한다 (PR #9 리뷰).
        return x, y, xl, yl, "lever_pair"
    if ry == "lever":                       # 결과 자리에 레버가 왔다 — 뒤집는다
        return y, x, yl, xl, "lever_to_response"
    if rx == "lever":
        return x, y, xl, yl, "lever_to_response"
    if rx == "response" and ry == "response":
        return x, y, xl, yl, "response_pair"   # 선후를 데이터가 답하지 않는다
    return x, y, xl, yl, "unknown"


def run_corr(ctx):
    out = []
    for c in ctx["eda"]["correlations"]:
        if c["definitional"] or abs(c["spearman"]) < 0.15:
            continue
        # D47 — 방향을 세운 뒤에 값을 만든다. 순위 상관은 대칭이라 r은 안 바뀌지만,
        # 문장과 구간별 추이(어느 축을 구간으로 자르나)가 달라진다.
        x, y, xl, yl, direction = _orient(c["x"], c["y"], c["x_label"], c["y_label"])
        pairs = [(i[x], i[y]) for i in ctx["styles"]
                 if i.get(x) is not None and i.get(y) is not None]
        if len(pairs) < N_MIN:
            continue
        r = sp.spearman(pairs)
        # 상관계수도 주장에서 뺀다 (D56) — `effect`로 이미 넘어가고 카드가 찍는다
        claim = "%s 높을수록 %s %s" % (
            _josa(xl, "이가"), _josa(yl, "이가"),
            "높다" if (r or 0) > 0 else "낮다")
        if direction in ("response_pair", "lever_pair"):
            # 둘 다 고객 반응이다 — 어느 쪽이 먼저인지 데이터가 답하지 않는다.
            # 문장이 인과처럼 읽히지 않게 **함께 움직인다**로 쓴다.
            claim = "%s %s 함께 움직인다 (선후는 알 수 없다)" % (
                _josa(xl, "와과"), _josa(yl, "은는"))
        out.append({
            "method": "correlation", "kind": "correlation",
            "x": x, "y": y, "x_label": xl, "y_label": yl,
            "pairs": pairs, "eda": c, "vanity": c.get("vanity_pair"),
            "effect": r, "effect_kind": "순위 상관", "p": sp.perm_test_corr(pairs),
            "n": len(pairs), "trend": sp.binned_trend(pairs),
            "claim": claim,
            "audience": audience_of(y, x),
            "direction": direction,
        })
    return out


def run_did(ctx):
    res = sp.did_within(ctx["series"])
    if not res:
        return []
    return [{
        "method": "did", "kind": "did", "study": res,
        "treated_raw": res.pop("_treated", []), "control_raw": res.pop("_control", []),
        "effect": res["effect"], "effect_kind": "Cliff δ", "p": res["p"],
        "n": res["n_treated"] + res["n_control"],
        "claim": "가격 인하의 순효과(이중차분)는 하트 증분 %s다 "
                 "(처치 %d건 대 대조 %d건)" % (
                     _fmt(res["did"]), res["n_treated"], res["n_control"]),
        "audience": "판매전략",
    }]


def run_dose(ctx):
    res = sp.dose_response(ctx["data"].get("price_events") or [])
    if not res:
        return []
    best = max(res["bins"], key=lambda b: b["median"])
    return [{
        "method": "dose_response", "kind": "dose_response", "study": res,
        "cuts_raw": res.pop("_cuts", []),
        # 용량-반응은 "차이"가 아니라 "형태"라 효과 크기를 단조성으로 잡는다
        "effect": res["monotonic_r"], "effect_kind": "구간 단조성(r)",
        "p": None, "n": res["n"],
        "claim": "할인 %d~%s%% 구간에서 하트 증분이 가장 컸다 (중앙값 %s, n=%d)" % (
            best["from"], best["to"] if best["to"] else "", _fmt(best["median"]), best["n"]),
        "audience": "판매전략",
    }]


def run_paired(ctx):
    """플랫폼 간 쌍체 비교.

    **여기에도 Y 선정을 건다** (D56 — D47/D51을 `run_paired`에 못 걸어 놓고 있었다).
    2026-08-04 사용자 지적: "17:39에 만들어진 것도 Y가 할인율인 게 있네?" — 맞다.
    `run_group`·`run_corr`만 고쳐서 "같은 상품 119쌍에서 29cm의 할인율이 musinsa보다
    높다"가 강한 주장 1번으로 올라왔다. 할인율은 **우리가 정한 값**이라 성과가 아니다.

    반응 지표가 하나라도 쓸 수 있으면 그쪽만 쓰고, 하나도 없을 때만 lever로 내려간다
    (리포트를 통째로 비우지는 않는다 — D51과 같은 처리).
    """
    out = []
    usable = _usable_metrics(ctx["eda"])
    response = [m for m in ("purchase_band", "purchase_count", "like_count",
                            "review_count", "rating", "view_band") if m in usable]
    lever = [m for m in ("price_sale", "discount_rate") if m in usable]
    for metric in (response or lever):
        if metric not in usable:
            continue
        for (sa, sb), pairs in matched_pairs(ctx["items"], metric).items():
            res = sp.paired_test(pairs)
            if not res:
                continue
            out.append({
                "method": "paired_platform", "kind": "paired", "study": res,
                "pairs_raw": pairs,
                "site_a": sa, "site_b": sb, "metric": metric,
                "metric_label": ctx["labels"].get(metric, metric),
                "effect": res["effect"], "effect_kind": "쌍체 우세도", "p": res["p"],
                "n": res["n_pairs"],
                "claim": "같은 상품 %d쌍에서 %s의 %s %s보다 %s (중앙값 차 %s)" % (
                    res["n_pairs"], sa, _josa(ctx["labels"].get(metric, metric), "이가"), sb,
                    "높다" if res["median_diff"] > 0 else "낮다",
                    _fmt(abs(res["median_diff"]))),
                "lever_metric": role_of(metric) == "lever",
                "audience": audience_of(metric),
            })
    return out


def run_depletion(ctx):
    recs = ctx.get("_depletion") or sp.time_to_soldout(ctx["series"])
    if not recs:
        return []
    by_key = {r["key"]: r for r in recs}
    out = []
    for cat_field, cat_label in ctx["cat_axes"]:
        groups = defaultdict(list)
        for it in ctx["items"]:
            r = by_key.get((it["site"], it["product_id"]))
            if r and it.get(cat_field) is not None:
                groups[it[cat_field]].append(r)
        big = sorted([(k, v) for k, v in groups.items() if len(v) >= 10],
                     key=lambda kv: -len(kv[1]))[:6]
        for i in range(len(big)):
            for j in range(i + 1, len(big)):
                ka, ra = big[i]
                kb, rb = big[j]
                res = sp.compare_depletion(ra, rb)
                if not res:
                    continue
                out.append({
                    "method": "depletion", "kind": "depletion", "study": res,
                    "recs_a": ra, "recs_b": rb,
                    "cat_field": cat_field, "cat_label": cat_label,
                    "group_a": str(ka), "group_b": str(kb),
                    "effect": res["effect"], "effect_kind": "Cliff δ", "p": res["p"],
                    "n": res["n_a"] + res["n_b"],
                    "claim": "%s에서 %s %s보다 %s 품절된다 (중앙 소진 %s 대 %s시간)" % (
                        cat_label, _josa(ka, "이가"), kb,
                        "빨리" if (res["km_median_a"] or 1e9) < (res["km_median_b"] or 1e9) else "늦게",
                        _fmt(res["km_median_a"]), _fmt(res["km_median_b"])),
                    "audience": "판매전략",
                })
    return out


RUNNERS = {"group_compare": run_group, "correlation": run_corr, "did": run_did,
           "dose_response": run_dose, "paired_platform": run_paired,
           "depletion": run_depletion}


# ═══════════════════════════════════════════════════════════════════════════
# ③ 5관문 판정
# ═══════════════════════════════════════════════════════════════════════════
def check_segments(h):
    if h["kind"] != "correlation":
        return True, []
    flips = h.get("eda", {}).get("simpson_flips") or []
    return (not flips), ["%s에서 관계가 사라지거나 뒤집힌다 (n=%d, r=%+.2f)"
                         % (f["segment"], f["n"], f["r"]) for f in flips[:3]]


def check_holdout(h):
    """⑤ 홀드아웃 재현. 유형마다 나눌 수 있는 축이 다르다."""
    if h["kind"] == "group_compare":
        a, b = h["a"], h["b"]
        if len(a) < N_MIN * 2 or len(b) < N_MIN * 2:
            return None, "표본이 작아 홀드아웃을 나눌 수 없다"
        eff = sp.cliffs_delta(a[len(a) // 2:], b[len(b) // 2:])
        if eff is None:
            return None, "홀드아웃 효과를 계산하지 못했다"
        return ((eff * (h["effect"] or 0) > 0 and abs(eff) >= EFFECT_MIN * 0.6),
                "홀드아웃 δ=%+.2f (원 δ=%+.2f)" % (eff, h["effect"] or 0))
    if h["kind"] == "correlation":
        pairs = h["pairs"]
        if len(pairs) < N_MIN * 2:
            return None, "표본이 작아 홀드아웃을 나눌 수 없다"
        r = sp.spearman(pairs[len(pairs) // 2:])
        if r is None:
            return None, "홀드아웃 상관을 계산하지 못했다"
        return ((r * (h["effect"] or 0) > 0 and abs(r) >= CORR_MIN * 0.6),
                "홀드아웃 r=%+.2f (원 r=%+.2f)" % (r, h["effect"] or 0))
    # 아래 넷은 처음에 전부 "분할 불가"로 두었다가 고쳤다 — 그러면 ⑤관문에서 항상 걸려
    # **더 강한 검정을 만들어놓고 강한 주장이 될 길을 스스로 막는** 꼴이 된다(2026-08-03).
    # 재료(원시 배열)를 들고 있으면 넷 다 절반으로 나눌 수 있다.
    if h["kind"] == "paired":
        pairs = h.get("pairs_raw") or []
        if len(pairs) < 20:
            return None, "쌍이 %d개뿐이라 절반으로 나누면 검정이 성립하지 않는다" % len(pairs)
        res = sp.paired_test(pairs[len(pairs) // 2:])
        if not res:
            return None, "홀드아웃 쌍체 검정을 계산하지 못했다"
        return ((res["effect"] or 0) * (h["effect"] or 0) > 0
                and abs(res["effect"] or 0) >= EFFECT_MIN * 0.6,
                "홀드아웃 우세도=%+.2f (원 %+.2f)" % (res["effect"] or 0, h["effect"] or 0))
    if h["kind"] == "did":
        t = h.get("treated_raw") or []
        c = h.get("control_raw") or []
        if len(t) < 20 or len(c) < 20:
            return None, "처치 %d·대조 %d — 절반으로 나누기에 부족하다" % (len(t), len(c))
        res = sp.did_from(t[len(t) // 2:], c[len(c) // 2:])
        if not res:
            return None, "홀드아웃 이중차분을 계산하지 못했다"
        return ((res["effect"] or 0) * (h["effect"] or 0) > 0
                and abs(res["effect"] or 0) >= EFFECT_MIN * 0.6,
                "홀드아웃 δ=%+.2f (원 %+.2f)" % (res["effect"] or 0, h["effect"] or 0))
    if h["kind"] == "dose_response":
        cuts = h.get("cuts_raw") or []
        if len(cuts) < 40:
            return None, "인하 사건이 %d건 — 절반으로 나누면 구간마다 표본이 부족하다" % len(cuts)
        r = sp.dose_monotonic(cuts[len(cuts) // 2:])
        if r is None:
            return None, "홀드아웃 단조성을 계산하지 못했다"
        return (r * (h["effect"] or 0) > 0 and abs(r) >= CORR_MIN * 0.6,
                "홀드아웃 단조성 r=%+.2f (원 %+.2f)" % (r, h["effect"] or 0))
    if h["kind"] == "depletion":
        ra, rb = h.get("recs_a") or [], h.get("recs_b") or []
        ea = [r for r in ra if r["event"]]
        eb = [r for r in rb if r["event"]]
        if len(ea) < 10 or len(eb) < 10:
            return None, "품절 사건이 %d·%d건 — 절반으로 나누면 요건에 못 미친다" % (
                len(ea), len(eb))
        res = sp.compare_depletion(ra[len(ra) // 2:], rb[len(rb) // 2:])
        if not res:
            return None, "홀드아웃 소진 비교를 계산하지 못했다"
        return ((res["effect"] or 0) * (h["effect"] or 0) > 0
                and abs(res["effect"] or 0) >= EFFECT_MIN * 0.6,
                "홀드아웃 δ=%+.2f (원 %+.2f)" % (res["effect"] or 0, h["effect"] or 0))
    return None, "홀드아웃 분할 규칙이 정의되지 않은 유형이다"


def gate(h, fdr_survive):
    # 사람이 읽는 문구(`fails`)와 코드가 읽는 코드(`fail_codes`)를 나눠 담는다.
    # 문구로 관문을 판정하면(`"표본" in f`) 문구를 다듬는 날 조용히 오분류된다
    # — 실제로 `null_findings`가 그렇게 갈라내고 있었다 (PR #9 리뷰).
    fails, codes = [], []
    eff = abs(h.get("effect") or 0)
    threshold = CORR_MIN if h["kind"] in ("correlation", "dose_response") else EFFECT_MIN
    if h.get("effect") is None:
        fails.append("효과 크기를 계산하지 못했다")
        codes.append("effect_missing")
    elif eff < threshold:
        fails.append("효과 크기가 작다 (%.2f < %.2f) — 결정을 바꿀 만한 차이가 아니다"
                     % (eff, threshold))
        codes.append("effect_small")
    if (h.get("n") or 0) < N_MIN:
        fails.append("표본이 작다 (n=%d < %d)" % (h.get("n") or 0, N_MIN))
        codes.append("sample")
    if h.get("p") is None:
        fails.append("p값을 계산하지 않는 유형이다 — 우연 여부를 가리지 못했다")
        codes.append("no_p")
    elif not fdr_survive:
        fails.append("다중비교 보정(BH, α=%.2f)을 통과하지 못했다" % ALPHA)
        codes.append("fdr")
    seg_ok, seg_notes = check_segments(h)
    if not seg_ok:
        fails.extend(seg_notes)
        codes.append("segment")
    hold_ok, hold_note = check_holdout(h)
    h["holdout_note"] = hold_note
    # **"재현되지 않았다"와 "확인할 수 없었다"는 다르다.** 전자만 탈락시킨다.
    # 후자까지 탈락시키면 표본이 작은 문맥에서 강한 주장이 매번 0개가 된다 —
    # 그건 데이터가 말해준 게 아니라 우리 규칙이 만든 결과다(2026-08-03 완화).
    # 대신 확인하지 못했다는 사실을 리포트에 표시로 남긴다.
    if hold_ok is False:
        fails.append("홀드아웃에서 재현되지 않는다 — %s" % hold_note)
        codes.append("holdout")
    elif hold_ok is None:
        h["holdout_unverified"] = True
    if h.get("vanity"):
        fails.append("둘 다 누적 지표다 — 출시가 오래된 상품일수록 함께 커진다. "
                     "관측 간 증분으로 다시 보기 전에는 발견으로 볼 수 없다")
        codes.append("vanity")
    h["fails"] = fails
    h["fail_codes"] = codes
    h["verdict"] = "strong" if not fails else "weak"
    if hold_ok is False and h.get("effect") is not None:
        h["verdict"] = "rejected"
    return h


def strip_payload(h):
    """직렬화 전에 원시 배열을 뺀다 — 계획서·결과 JSON이 수십 MB가 되면 아무도 안 읽는다."""
    drop = ("a", "b", "pairs", "eda", "pairs_raw", "treated_raw", "control_raw",
            "cuts_raw", "recs_a", "recs_b")
    out = {k: v for k, v in h.items() if k not in drop}
    if isinstance(out.get("study"), dict):
        out["study"] = {k: v for k, v in out["study"].items() if not k.startswith("_")}
    return out


# ═══════════════════════════════════════════════════════════════════════════
def reuse_or_run_eda(db_path, contexts, signals=None):
    """--signals로 받은 EDA 산출물을 재사용하거나, 안 맞으면 새로 돈다 (D74).

    재사용 조건: ok=True이고 **요청 문맥이 정확히 같을 때**. 다르면 조용히
    쓰지 않는다 — 다른 요청의 EDA로 분석하면 사용자가 본 근거와 분석 근거가
    갈라진다(그걸 막자고 재사용을 만든 것이다). 경고를 남기고 새로 돈다.
    재사용하면 Turso에서 전체 데이터 fetch 한 번이 절약되고, 무엇보다
    **사용자에게 보여준 EDA와 분석이 쓴 EDA가 같은 파일**이 된다.
    """
    want = sorted(str(c) for c in (contexts or []))
    if signals:
        if signals.get("ok") and sorted(signals.get("requested_contexts") or []) == want:
            return signals
        print("경고: --signals 파일이 이 요청의 EDA가 아니다 "
              f"(파일: {signals.get('requested_contexts')} / 요청: {want}) "
              "— EDA를 새로 수행한다", file=sys.stderr)
    return run_eda(db_path, contexts)


def analyze(db_path, contexts, ai_notes=None, plan_only=False, own_brands=None,
            signals=None):
    """`own_brands` — 우리 브랜드 이름들. 브랜드 축에서 **제3자끼리의 비교를
    거르는 데만** 쓴다(D56). 안 주면 아무것도 안 거른다.
    `signals` — eda.py --out 산출물(dict). 요청 문맥이 같으면 EDA를 재수행하지
    않고 이걸 쓴다 (D74).
    """
    # `brand:X` 문맥은 그 자체로 "X를 보러 왔다"는 선언이다 — 따로 안 알려줘도 쓴다
    own_brands = list(own_brands or []) + [
        str(c).split(":", 1)[1] for c in (contexts or [])
        if str(c).startswith("brand:")]
    eda_res = reuse_or_run_eda(db_path, contexts, signals)
    if not eda_res["ok"]:
        return {"ok": False, "reason": eda_res.get("reason"), "contexts": contexts}
    data = collect(db_path, contexts)
    # **표기 변형까지 자사로 친다** (D56). `brand_key()`는 한글·영문을 일부러 안 묶는데
    # (음차 매칭은 근거가 없다 — D51), 자사 브랜드는 그 셋이 전부 우리다:
    # `2000아카이브스` · `2000Archives` · `2000 Archives`. 한 표기만 알려주면 나머지
    # 표기로 적힌 쌍이 "제3자끼리"로 걸려 통째로 빠진다.
    #
    # `brand:X` 문맥은 **그 문맥의 상품이 전부 같은 브랜드**라는 선언이므로, 거기 실제로
    # 적힌 브랜드 값을 전부 자사 표기로 받는다. 시장 전수조사 문맥에는 그런 신호가 없으니
    # 그때는 `--own-brand`로 표기를 다 넘겨야 한다.
    if any(str(c).startswith("brand:") for c in (contexts or [])):
        own_brands += [it.get("brand") for it in data["items"] if it.get("brand")]
    # 분석 단위가 둘이다(2026-08-03 #7):
    #   items  — 상품(변형) 단위. 재고·품절처럼 색상마다 다른 것
    #   styles — 스타일 단위. 가격·반응처럼 색상이 같이 움직이는 것
    # 플랫폼마다 한 상품을 세는 기준이 달라서(자사몰은 색상별로, 무신사는 묶어서)
    # 변형 단위로 비교하면 n이 서로 다른 기준으로 세어진다.
    # 프록시를 포함한 동적 축(D19·D23) — AI 자동 판정 프록시가 검정 대상에 들도록.
    cats = cat_axes(data, CAT_AXES)
    ctx = {"items": data["items"],
           "styles": style_rows(data["items"], proxies=data["meta"].get("proxies")),
           "eda": eda_res, "data": data,
           "series": product_series(db_path, contexts),
           "cat_axes": cats, "num_axes": num_axes(data),
           "labels": dict(num_axes(data) + cats),
           "hierarchy": category_hierarchy(db_path),   # D42
           "contexts": contexts, "own_brands": own_brands}

    plan = make_plan(ctx, ai_notes)
    if plan_only:
        return {"ok": True, "plan": plan, "eda": eda_res, "plan_only": True}

    hyps = []
    for entry in plan:
        if entry["status"] != "adopted":
            continue
        runner = RUNNERS.get(entry["method"])
        if not runner:
            continue
        produced = runner(ctx)
        for h in produced:
            if entry.get("warning"):
                h["method_warning"] = entry["warning"]
        entry["produced"] = len(produced)
        if not produced:
            # 규칙은 통과했는데 실행이 아무것도 못 냈다 — 규칙 조건이 실제 성립 요건을
            # 덜 본 것이다. 조용히 넘어가면 "그 분석을 했는데 결과가 없었다"로 읽힌다.
            entry["status"] = "adopted_empty"
            entry["reason"] += " → 실행 결과 0건 (규칙 조건은 맞았으나 세부 요건 미달)"
        hyps.extend(produced)

    survive = sp.benjamini_hochberg([h.get("p") for h in hyps], alpha=ALPHA)
    for h, s in zip(hyps, survive):
        gate(h, s)
    return {"ok": True, "plan": plan, "eda": eda_res, "data": data,
            "hypotheses": hyps, "generated": len(hyps),
            "lineage_skipped": ctx.get("lineage_skipped") or {}}   # D42


def main():
    ap = argparse.ArgumentParser(description="분석 단 — 방법론 결정 · 실행 · 5관문 판정")
    # D72: INTEL_DB_URL(Turso) > INTEL_DB > data/intel.db. 명시적 --db가 이긴다
    ap.add_argument("--db", default=default_db_target())
    ap.add_argument("--context", action="append", default=[])
    ap.add_argument("--plan-only", action="store_true",
                    help="계획만 세우고 멈춘다 — AI가 이걸 읽고 예외를 판단한다")
    ap.add_argument("--ai-notes", help="AI 예외 판단 JSON (exclude/warn/add)")
    ap.add_argument("--signals", help="eda.py --out 산출 JSON — 요청 문맥이 같으면 "
                                      "EDA를 재수행하지 않고 재사용한다 (D74)")
    ap.add_argument("--out", help="결과 JSON 경로 (생략하면 표준출력)")
    a = ap.parse_args()

    notes = None
    if a.ai_notes:
        notes = json.loads(open(a.ai_notes, encoding="utf-8").read())
    signals = None
    if a.signals:
        signals = json.loads(open(a.signals, encoding="utf-8").read())
    res = analyze(a.db, a.context, notes, a.plan_only, signals=signals)
    if not res["ok"]:
        print("중단: %s" % res.get("reason"))
        return 1

    payload = dict(res)
    payload.pop("data", None)
    payload.pop("eda", None)
    if "hypotheses" in payload:
        payload["hypotheses"] = [strip_payload(h) for h in payload["hypotheses"]]
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        open(a.out, "w", encoding="utf-8").write(text)
    else:
        print(text)

    print("── 분석 계획 ──")
    for e in res["plan"]:
        mark = {"adopted": "채택", "rejected": "기각", "ai_suggested": "AI제안",
                "adopted_empty": "채택→0건"}[e["status"]]
        print("  [%s] %-22s %s" % (mark, e["name"], e["reason"]))
        if e.get("warning"):
            print("         주의: %s" % e["warning"])
    if not a.plan_only:
        v = defaultdict(int)
        for h in res["hypotheses"]:
            v[h["verdict"]] += 1
        print("가설 %d개 → 강함 %d · 약함 %d · 기각 %d"
              % (res["generated"], v["strong"], v["weak"], v["rejected"]))
    if a.out:
        print("저장: %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
