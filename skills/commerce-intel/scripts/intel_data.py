#!/usr/bin/env python3
"""데이터 층 — 정본 DB에서 분석용 데이터를 뽑는다 (D29 파이프라인 5단 분리).

`build_analysis_report.py`(HTML 대시보드)에서 **계산 로직만** 떼어 왔다. 렌더 층은
D27로 폐기됐지만 이 부분은 산출 형식과 무관하다 — 상품별 최신 관측, 가격 변경 사건,
시계열, 옵션 재고, 플랫폼 합집합을 만드는 일은 HTML이든 PDF든 똑같다.

이 모듈은 **DB만 안다.** EDA·분석·인사이트가 이걸 공유해서 먹는다. 리포트를 모르고,
어떤 통계를 돌릴지도 모른다.

    from intel_data import collect
    data = collect("data/intel.db", ["brand:로우클래식"])
    data["items"]         # 상품별 최신 관측 + 정적 속성
    data["price_events"]  # 가격 변경 사건 (관측 창 증분 포함)
    data["time_series"]   # 시점 2개 이상 관측된 상품
    data["union"]         # 플랫폼 합집합 (2개 이상일 때만)
    data["meta"]["stock"] # 옵션(사이즈) 재고 요약
"""
import json
import os
import re
import sqlite3
import statistics as st
from collections import Counter
from datetime import datetime

# ── 동일 상품 매칭 ──────────────────────────────────────────────────────────
# 규칙은 하나뿐이다: **정규화 상품명 완전일치.** 유사도·가격 보조 매칭은 금지다 —
# 인사일런스 실측에서 유사도 0.82↑ 후보 11건 중 대부분이 다른 상품이었고,
# `레터링 그래픽 티셔츠`와 `스탠실 그래픽 티셔츠`는 정가까지 66,000원으로 같아
# **가격이 오탐을 확증**했다(EVIDENCE §5). 그래서 가격도 신호로 못 쓴다.
#
# 원래 build_report.py가 갖고 있던 것을 여기로 옮겼다 — 배포 패키지에서 HTML
# 생성기를 빼면서 데이터 층이 렌더 층에 의존할 수 없게 됐기 때문이다(D27).

# 괄호/대괄호 안은 매칭에서 뺀다 — "[2 PACK]", "(단독)", "(5 COLORS)" 같은 유통·옵션
# 표기가 사이트마다 다르게 붙는다. 무신사 `REYA LACE TOP (5 COLORS)`와 자사몰
# `REYA LACE TOP (PINK)`가 같은 상품으로 묶이는 것이 이 규칙 덕분이다(MD 인터뷰 확인).
BRACKETS_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
# 한글·영숫자만 남긴다. 공백·`_`·`/`·하이픈은 사이트마다 다르게 넣는다.
NON_WORD_RE = re.compile(r"[^0-9a-z가-힣]")

# ── 괄호 안이 시즌이면 지우지 않는다 (2026-08-03 실측) ─────────────────────
# 괄호 제거는 색상·유통 표기를 걷어내려는 규칙인데, 시즌 표기까지 지워서
# `(FW23) T-Logo LT Hoodie`와 `(FW24) T-Logo LT Hoodie`가 **같은 상품이 됐다.**
# 서로 다른 시즌 상품은 다른 상품이다.
#
# FW/SS/SP/AW 문자를 **필수**로 요구한다 — 숫자만 보면 상품코드 `[0157]`을 시즌으로
# 오인한다(실측에서 실제로 걸렸다).
#
# 실측 효과: 플랫폼 간 매칭 688건 → 688건(손실 0) · 사이트 내 잘못된 병합 40건 감소.
# 공짜다.
SEASON_RE = re.compile(r"^(?:(?:FW|SS|SP|AW)\s?\d{2,4}|\d{2,4}\s?(?:FW|SS|SP|AW))$", re.I)


def match_key(name):
    """동일 상품 판정에 쓰는 정규화 상품명. 이 키가 같을 때만 같은 상품이다.

    괄호 안은 지우되 **시즌 표기는 키에 남긴다.** 색상 변형은 묶이고(무신사
    `REYA LACE TOP (5 COLORS)` = 자사몰 `REYA LACE TOP (PINK)`), 시즌 변형은
    갈라진다(`(FW23) X` ≠ `(FW24) X`).
    """
    if not name:
        return ""
    kept = []

    def _repl(m):
        inner = m.group(0).strip("[]()").strip()
        if SEASON_RE.match(inner):
            kept.append(inner.upper().replace(" ", ""))
        return " "

    base = NON_WORD_RE.sub("", BRACKETS_RE.sub(_repl, str(name).lower()))
    return base + ("|" + "|".join(sorted(kept)) if kept else "")


AXES = [
    ("price_sale", "판매가"), ("price_original", "정가"), ("discount_rate", "할인율(%)"),
    ("like_count", "하트"), ("review_count", "후기 수"), ("rating", "평점"),
    ("purchase_count", "누적판매*"), ("viewers_now", "보는 중(랭킹)"),
    ("view_count", "조회수"),
    # 옵션(사이즈) — 값이 있는 문맥에서만 축 목록에 뜬다
    ("opt_total", "옵션 수"), ("opt_out_rate", "옵션 품절률(%)"), ("stock_sum", "재고 수량 합"),
    ("sold_min", "최소 판매량*"),
    # 구간 표기에서 뽑은 순서형 축 (D48) — **순위로만 쓴다**
    ("view_band", "조회수 구간(하한)"), ("purchase_band", "누적판매 구간(하한)"),
    ("like_band", "하트 구간(하한)"),
    # 퍼널 비율 (D47) — 아래 FUNNEL이 계산해 붙인다
    ("cvr_view_like", "조회→하트 전환(%)"), ("cvr_view_buy", "조회→구매 전환(%)"),
    ("cvr_like_buy", "하트→구매 전환(%)"), ("review_per_buy", "구매 대비 후기(%)"),
]

# ── 구간 표기를 순서형 축으로 (D48) ────────────────────────────────────────
# 무신사는 조회수·누적판매를 **구간으로만** 보여준다("1.2만 회 이상", "2.7천 개 이상").
# 원시 정수를 주는 API가 있지만 그 호스트는 robots가 `Disallow: /`라 쓰지 않는다
# (2026-08-04 실측 — SPEC D48). 그래서 화면에 표시된 구간 표기를 쓴다.
#
# **정수로 담지 않는다**(어댑터 함정 1은 그대로다). "1.2만 이상"을 12000으로 담으면
# 실제 90,000인 상품과 12,001인 상품이 같은 값이 되고, 그 값으로 평균이나 비율을
# 내면 조용히 틀린다.
#
# **대신 순서로 쓴다.** 구간 표기의 **하한은 정확하다** — "1.2만 이상"은 값은 몰라도
# 12,000보다 크다는 것은 틀림이 없다. 그래서 상품 사이의 **순서**는 오차 없이 매겨진다
# (같은 구간끼리는 동순위). 그리고 이 프로젝트의 통계는 **Cliff δ와 Spearman —
# 둘 다 순위 기반**이라 순서만 맞으면 그대로 성립한다.
#
#     못 하는 것: 평균·합계·비율(퍼널 전환율의 분모로 쓸 수 없다)
#     되는 것:   그룹 비교(Cliff δ) · 순위 상관(Spearman) · 구간별 분포

_BAND_UNIT = {"": 1, "천": 1000, "만": 10000, "억": 100000000}
_BAND_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(천|만|억)?\s*(?:회|개|건)?\s*이상")


def band_floor(display):
    """구간 표기의 **하한**을 정수로. 못 읽으면 None.

    돌려주는 값은 "이 상품의 조회수"가 아니라 **"적어도 이만큼"**이다.
    순서를 매기는 데만 쓰고 크기 계산에는 쓰지 마라 — 이름에 floor를 박아 둔 이유다.
    """
    if not display:
        return None
    m = _BAND_RE.search(str(display))
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(n * _BAND_UNIT.get(m.group(2) or "", 1))


# 구간 표기에서 뽑는 순서형 축. 값은 하한(정수)이지만 **순위로만 쓴다**.
BAND_AXES = [("view_band", "view_count_display", "조회수 구간(하한)"),
             ("purchase_band", "purchase_count_display", "누적판매 구간(하한)"),
             ("like_band", "like_count_display", "하트 구간(하한)")]


def add_bands(item):
    """구간 표기 → 순서형 축. 원문(`*_display`)은 그대로 둔다."""
    for name, src, _ in BAND_AXES:
        item[name] = band_floor(item.get(src))
    return item


# ── 축의 역할 — 무엇이 Y가 될 수 있나 (D47) ────────────────────────────────
# 2026-08-04 피드백: "할인율·판매가 등 **사람이 설정하는 값은 공급자 입장에서 Y가
# 될 수 없다**". 맞다 — 우리가 정한 값을 결과로 놓고 원인을 찾으면 인과가 거꾸로다.
# "하트가 높을수록 할인율이 높다"는 문장은 **할인을 하트가 정했다**고 읽힌다.
#
#   lever    공급자가 정한다 — 가격·할인·재고·옵션. **원인 쪽(X)에만 놓는다**
#   response 고객이 만든다 — 조회·하트·후기·구매·순위. **결과 쪽(Y)이 된다**
#
# 상관을 만들 때 이 역할로 **방향을 세운다**. 둘 다 response면 방향을 못 정하므로
# 그 사실을 주장에 적는다(선후를 데이터가 답하지 않는다).
LEVER = {"price_sale", "price_original", "discount_rate",
         "opt_total", "stock_sum"}
RESPONSE = {"view_count", "viewers_now", "like_count", "review_count", "rating",
            "view_band", "purchase_band", "like_band",
            "purchase_count", "sold_min", "sold_out", "opt_out_rate", "rank",
            "cvr_view_like", "cvr_view_buy", "cvr_like_buy", "review_per_buy"}


# ── 브랜드 표기 정규화 (D51) ───────────────────────────────────────────────
# 같은 브랜드가 사이트마다 다르게 적힌다. 실측(2000아카이브스 문맥):
#   무신사 `2000아카이브스` · 29CM `2000아카이브스` · 자사몰 `2000Archives`(354)
#   그리고 **같은 자사몰 안에서** `2000 Archives`(148) — 자기 안에서도 갈렸다
# 그대로 두면 "2000아카이브스와 2000Archives는 정가 차이가 없다"가 인사이트로
# 올라온다. 같은 브랜드니 당연한 말이고, 그게 진짜 발견의 자리를 먹는다.
#
# **표기만 걷어낸다** — 대소문자·공백·하이픈. 뜻이 다른 이름은 건드리지 않는다.
# 한글과 영문은 서로 다른 키가 된다(음차 매칭은 하지 않는다 — 근거가 없다).

_BRAND_STRIP = re.compile(r"[\s\-_·.]+")


def brand_key(name):
    """브랜드 비교용 키. **표시는 원문을 쓰고 묶을 때만 이걸 쓴다.**"""
    if not name:
        return None
    return _BRAND_STRIP.sub("", str(name)).lower()


def role_of(field):
    return "lever" if field in LEVER else ("response" if field in RESPONSE else "context")


# ── 퍼널 비율 (D47) ────────────────────────────────────────────────────────
# 피드백: "절대값이 아니라 **비율**로 판단 (노출 대비 하트 등)". 절대값은 노출량에
# 끌려간다 — 많이 노출된 상품이 하트도 많은 건 당연하고, 그건 상품의 힘이 아니다.
#
#     조회 → 하트 → 구매        (무신사가 세 값을 다 준다)
#     stat.pageViewTotal · like_count · stat.purchaseTotal
#
# **분모가 없거나 0이면 만들지 않는다.** 0으로 나눈 자리를 0이나 100으로 채우면
# 없는 성과를 만들어내는 것이고, 그건 이 프로젝트의 제1원칙 위반이다.
FUNNEL = [
    ("cvr_view_like", "like_count", "view_count"),
    ("cvr_view_buy", "purchase_count", "view_count"),
    ("cvr_like_buy", "purchase_count", "like_count"),
    ("review_per_buy", "review_count", "purchase_count"),
]


def add_funnel(item):
    """상품 하나에 퍼널 비율을 붙인다. 재료가 없으면 그 축은 만들지 않는다(None).

    **구간 밴드(`*_band`)는 절대 쓰지 않는다** — 하한일 뿐이라 나누면 전환율이
    부풀려진다("1.2만 이상"인 상품이 실제 9만이면 분모가 7배 작게 들어간다).
    비율은 원시 정수가 있을 때만 만든다. 없으면 그 축은 없는 것이다 (D48).
    """
    for name, num, den in FUNNEL:
        a, b = item.get(num), item.get(den)
        item[name] = round(100.0 * a / b, 3) if (
            a is not None and b is not None and b > 0) else None
    return item

# ── 모듈 ────────────────────────────────────────────────────────────────────
# 리포트는 종류가 아니라 **모듈의 조합**이다. 필요한 것만 골라 한 리포트로 만든다.
# `need`는 그 모듈이 성립할 데이터가 있는지 보는 함수다 — 없으면 넣지 않고, 뺐다는 사실을
# 리포트에 적는다(조용히 빠지면 독자는 그 분석을 안 한 게 아니라 못 한 걸 모른다).
MODULES = [
    ("kpi", "요약 KPI", lambda d: True),
    ("scatter", "산점도 (축 선택)", lambda d: bool(numeric_axes(d))),
    ("dist", "분포 (히스토그램)", lambda d: bool(numeric_axes(d))),
    ("group", "그룹 비교 (박스 플롯)", lambda d: bool(numeric_axes(d))),
    ("variants", "사이즈별 재고", lambda d: bool(d["meta"].get("stock"))),
    ("timeseries", "시계열 추이", lambda d: bool(d.get("time_series"))),
    ("events", "가격 변경 사건", lambda d: bool(d.get("price_events"))),
    ("linesheet", "라인시트 (플랫폼 합집합)", lambda d: bool(d.get("union"))),
    ("table", "상품 표", lambda d: True),
]
MODULE_IDS = [m[0] for m in MODULES]


MAX_TREND_POINTS = 48   # 시계열에 그릴 최대 시점 수 (D24)


def downsample_indices(count, limit=MAX_TREND_POINTS):
    """시점이 많으면 **균등 구간 대표만** 남긴다 — 평균 내지 않는다(D24).

    평균을 내면 없는 관측을 만들어내는 셈이고, 순위 축에서는 순위권 밖 구간이
    보간돼 "없는 순위"를 주장하게 된다. 대표 시점만 뽑고 **첫·끝은 반드시 포함**한다.

    build_report.py(D27 폐기)에 있던 함수를 데이터 층으로 옮겼다 — 규칙이 사라지면
    안 되고, 인라인으로 묻혀 있으면 검증도 참조도 되지 않는다.
    """
    if count <= limit:
        return list(range(count))
    idx = [round(i * (count - 1) / (limit - 1)) for i in range(limit)]
    return sorted(set(idx))


# ── 카테고리 계층 (D42) ────────────────────────────────────────────────────
# 사이트가 깊이 다른 카테고리를 한 필드에 섞어 줘서 "미니가 하의보다 크다" 같은
# 부분-전체 비교가 만들어진다. 근거는 실측 카탈로그(사이트가 밝힌 트리)다.
#
# **집합 포함으로는 판정할 수 없다** — 상품마다 카테고리가 하나뿐이라 하의와 미니의
# 상품 집합이 안 겹친다. 기각한 대안과 실측 수치는 SPEC-INTEL D42에 있다.

# 배포본은 `skills/`만 나가므로 카탈로그를 references/에도 둔다. 저장소 루트만 보면
# 팀원 환경에서 **예외 없이 조용히** 필터가 죽는다. package.sh가 두 사본을 대조한다.
_HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_CANDIDATES = [
    os.path.join(os.path.dirname(_HERE), "references", "ranking_targets.json"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(_HERE))),
                 "data", ".tools", "ranking_targets.json"),
]


def find_catalog():
    """실측 카탈로그 위치. 없으면 None — 그때는 DB 경로 값만으로 계층을 배운다."""
    for p in CATALOG_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _feed_path(parts, anc, parent):
    for i, node in enumerate(parts):
        if i:
            parent.setdefault(node, set()).add(parts[i - 1])
        for j in range(i + 1, len(parts)):
            anc.add((node, parts[j]))


def category_hierarchy(db_path, catalog_path=None):
    """카테고리 계층 — {"anc": {(상위,하위)}, "parent": {값: {부모들}}, "umbrella": {값}}.

    한 값이 부모를 여럿 가질 수 있다(`하의`는 단독·홈웨어·남성의류 밑에 다 있다).

    `umbrella`는 **트리 어딘가에서 자식을 거느린 값**이다. `하의`는 `남성의류 > 하의 >
    데님 팬츠`에서 부모 노릇을 하므로 우산이고, `미니`는 어디서도 부모가 아니라
    리프 전용이다. 이 구분이 "겉보기 형제"와 "굵기가 다른 값"을 가른다.
    """
    anc, parent = set(), {}
    path = catalog_path or find_catalog()
    try:
        with open(path, encoding="utf-8") as fh:
            cat = json.load(fh)
        for site in ("musinsa", "29cm"):
            for e in cat.get(site, {}).get("entries", []):
                _feed_path([p.strip() for p in e.get("path", []) if p.strip()], anc, parent)
    except (OSError, TypeError, ValueError, KeyError):
        pass    # 카탈로그가 없어도 DB 경로만으로 돌아간다 — 덜 걸러질 뿐이다
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT DISTINCT category FROM products WHERE category LIKE '%>%'").fetchall()
        conn.close()
        for (c,) in rows:
            _feed_path([p.strip() for p in c.split(">") if p.strip()], anc, parent)
    except sqlite3.Error:
        pass
    # 자식을 거느린 적이 있는 값 = 우산. parent 맵의 값들이 곧 그 목록이다.
    umbrella = {p for ps in parent.values() for p in ps}
    return {"anc": anc, "parent": parent, "umbrella": umbrella}


def incomparable(a, b, hier):
    """두 카테고리 값을 대등하게 비교할 수 없나 — 참이면 그 쌍을 검정하지 않는다.

    거르는 경우는 둘이다:
      ① **조상-자손** — `스커트` 대 `미니`. 부분과 전체를 겨루는 문장이 된다
      ② **굵기가 다르다** — `미니`(리프 전용) 대 `하의`(다른 가지에서 자식을 거느린
         우산). 트리에서 직접 이어지진 않지만 한쪽이 다른 쪽을 포함할 수 있는 폭이다

    **다른 가지라는 것만으로는 거르지 않는다**(2026-08-04 리뷰로 좁혔다). `미니`와
    `후드`는 부모가 갈리지만 **둘 다 리프**라 대등한 비교다 — 이걸 거르면
    "스커트 계열이 아우터 계열보다 할인율이 높다" 같은 정상 비교까지 사라진다.

    **모르면 거르지 않는다.** 둘 중 하나라도 트리에 없으면 판단 근거가 없고,
    없는 근거로 검정을 지우면 "안 나온 것"과 "없는 것"이 섞인다 — 그건 리포트의
    정직성 규칙 위반이다. 대신 리포트 서두 경고가 그 몫을 진다.

    남는 한계: 우산끼리는 깊이가 달라도 통과한다(`여성의류` 대 `하의`). 한 값이
    트리마다 다른 깊이에 있어 깊이로는 못 가른다 — 서두 경고가 그 몫을 진다.
    """
    return incomparable_reason(a, b, hier) is not None


def incomparable_reason(a, b, hier):
    """`incomparable`과 같은 판정에 **사유**를 붙여 돌려준다.

    "same"(같은 값) | "ancestor" | "granularity" | None(대등하다·판단 불가).

    사유를 나눠 세야 리포트가 "몇 개를 왜 뺐는지"를 말할 수 있다. 합계만 찍으면
    한쪽 사유로 과하게 빠져도 독자가 알 수 없다.
    """
    if not hier or a == b:
        return "same" if a == b else None
    anc = hier["anc"]
    umbrella = hier.get("umbrella") or set()
    pa = [p.strip() for p in str(a).split(">") if p.strip()]
    pb = [p.strip() for p in str(b).split(">") if p.strip()]
    # 빈 문자열·공백·`>`만 있는 값은 조각이 하나도 안 남는다. 그대로 두면 아래
    # `pa[-1]`이 IndexError를 내고 **리포트 생성 전체가 죽는다**(PR #8 리뷰 발견).
    # 판정 불가는 "거르지 않는다"로 간다 — 모르면 안 거르는 이 함수의 원칙 그대로다.
    if not pa or not pb:
        return None
    for x in pa:
        for y in pb:
            if x == y or (x, y) in anc or (y, x) in anc:
                return "ancestor"      # ① 같은 것이거나 조상-자손
    # ② 한쪽만 우산이면 굵기가 다르다. 둘 다 우산이거나 둘 다 리프면 대등하다.
    ua, ub = pa[-1] in umbrella, pb[-1] in umbrella
    if ua != ub:
        return "granularity"
    return None


def numeric_axes(data):
    """값이 하나라도 있는 수치 축. 전부 null인 축으로는 차트를 만들지 않는다."""
    fields = [f for f, _ in AXES] + [
        "px_" + p["name"] for p in data["meta"].get("proxies", []) if p.get("numeric")]
    return [f for f in fields if any(i.get(f) is not None for i in data["items"])]


# ── 프록시를 포함한 동적 축 (D19·D23) ──────────────────────────────────────
# AI가 자동 정의·판정한 파생 프록시(proxy_defs)를 collect()가 상품에 px_{name}으로
# 붙이고 meta.proxies에 목록을 담는다. **EDA·분석은 이 두 헬퍼로 축을 만들어야
# 프록시가 검정 대상에 든다** — 고정 AXES/CAT_AXES만 순회하면 프록시가 통째로 빠진다
# (실측: name_lang·thumb_cut·logo_presence가 판정돼 있는데 리포트에서 검정 안 됨).
# 프록시는 사용자가 "제일 중요"하다고 한 축이다.

def num_axes(data):
    """수치축 (field, label) — 고정 AXES + numeric 프록시. judged 있는 것만."""
    out = list(AXES)
    for p in data["meta"].get("proxies", []):
        if p.get("numeric") and p.get("judged"):
            out.append(("px_" + p["name"], p["name"] + "(AI)"))
    return out


def attr_axes(data):
    """`product_attributes`에서 온 동적 속성 축 (D54).

    값이 **두 종류 이상**일 때만 축이 된다 — 전부 같은 값이면 비교할 상대가 없다.
    수치처럼 보여도 범주로 둔다: 이 표는 TEXT라 크기 비교의 근거가 없다.
    """
    seen = {}
    for it in data["items"]:
        for k, v in it.items():
            if k.startswith("attr_") and v not in (None, ""):
                seen.setdefault(k, set()).add(v)
    return [(k, k[5:]) for k in sorted(seen) if len(seen[k]) >= 2]


def cat_axes(data, base):
    """범주축 (field, label) — base(호출자의 CAT_AXES) + 동적 속성 + 범주형 프록시.

    프록시 대부분이 범주형이다(착용컷/제품컷·영문/한글·로고 유무). 이것들이 그룹 비교
    축이 되어야 "착용컷 상품이 제품컷보다 하트가 높은가" 같은 검정이 성립한다.
    """
    out = list(base) + attr_axes(data)
    for p in data["meta"].get("proxies", []):
        if not p.get("numeric") and p.get("judged"):
            out.append(("px_" + p["name"], p["name"] + "(AI)"))
    return out


def collect(db_path, contexts):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ctx_where, params = "", []
    if contexts:
        ctx_where = " AND o.context IN (%s)" % ",".join("?" * len(contexts))
        params = list(contexts)
    # 상품별 최신 관측 + 정적 속성
    rows = conn.execute(f"""
        SELECT p.site, p.product_id, p.name, p.url, p.image_url, p.brand, p.category,
               p.attributes, o.observed_at, o.context,
               o.price_original, o.price_sale, o.discount_rate, o.review_count, o.rating,
               o.purchase_count, o.like_count, o.viewers_now, o.sold_out, o.rank
        FROM products p
        JOIN observations o ON o.site = p.site AND o.product_id = p.product_id
        WHERE o.observed_at = (
            SELECT MAX(o2.observed_at) FROM observations o2
            WHERE o2.site = o.site AND o2.product_id = o.product_id{ctx_where.replace('o.context','o2.context')}
        ){ctx_where}
    """, params + params).fetchall()
    # D35: 동적 속성 표. 축이 스키마 변경 없이 는다(컬러·소재·넥라인). 없으면 JSON 폴백.
    dyn_attrs = {}
    try:
        for a in conn.execute(
                "SELECT site, product_id, attr_name, value FROM product_attributes "
                "WHERE value IS NOT NULL"):
            dyn_attrs.setdefault((a["site"], a["product_id"]), {})[a["attr_name"]] = a["value"]
    except sqlite3.OperationalError:
        pass          # 구버전 DB — 표가 없다
    items, seen = [], set()
    for r in rows:
        key = (r["site"], r["product_id"])
        if key in seen:
            continue
        seen.add(key)
        d = dict(r)
        attrs = json.loads(d.pop("attributes") or "{}")
        attrs.update(dyn_attrs.get(key, {}))   # 표가 JSON을 덮는다(더 최신)
        d["fit"] = attrs.get("핏")
        d["color"] = attrs.get("컬러")         # 컬러 축은 인프라만 — 채우는 건 8번(보류)
        # **동적 속성을 전부 필드로 편다** (D54). D35가 "스키마 변경 없이 축을
        # 늘린다"고 했는데 정작 축 목록(CAT_AXES)이 고정이라, 새 속성을 넣어도
        # 분석이 안 봤다 — `brand_survival`을 넣고서야 드러났다.
        for k, v in attrs.items():
            if k in ("핏", "컬러"):
                continue                       # 위에서 고정 이름으로 이미 넣었다
            d["attr_" + k] = v
        d["_attrs"] = attrs
        add_bands(d)           # D48 — 구간 표기 → 순서형 축 (순위로만)
        add_funnel(d)          # D47 — 조회→하트→구매 비율. 재료 없으면 None
        items.append(d)

    # 가격 변경 사건 — 상품별 마지막 관측 상태 대비(기존 diff 규칙과 동일 철학)
    events = []
    obs = conn.execute(f"""
        SELECT o.site, o.product_id, o.observed_at, o.price_sale, o.like_count, o.review_count
        FROM observations o WHERE o.price_sale IS NOT NULL{ctx_where}
        ORDER BY o.site, o.product_id, o.observed_at
    """, params).fetchall()
    prev = {}
    names = {(i["site"], i["product_id"]): i["name"] for i in items}
    for r in obs:
        key = (r["site"], r["product_id"])
        p = prev.get(key)
        if p and p["price_sale"] != r["price_sale"]:
            events.append({
                "site": r["site"], "product_id": r["product_id"],
                "name": names.get(key), "from_at": p["observed_at"], "to_at": r["observed_at"],
                "price_from": p["price_sale"], "price_to": r["price_sale"],
                "like_delta": (r["like_count"] - p["like_count"])
                              if r["like_count"] is not None and p["like_count"] is not None else None,
                "review_delta": (r["review_count"] - p["review_count"])
                                if r["review_count"] is not None and p["review_count"] is not None else None,
            })
        prev[key] = dict(r)

    # 파생 프록시 주입 (D19) — 재료 지문이 현재 값과 맞는 캐시만 유효하다
    proxies = []
    try:
        defs = conn.execute("SELECT * FROM proxy_defs").fetchall()
    except sqlite3.OperationalError:
        defs = []
    for d in defs:
        pn, mat = d["proxy_name"], d["material"]
        try:
            space = json.loads(d["value_space"]) if d["value_space"] else None
        except (TypeError, ValueError):
            space = d["value_space"]
        is_numeric = space == "numeric"
        cache = {(r["site"], r["product_id"]): (r["fingerprint"], r["value"])
                 for r in conn.execute(
                     "SELECT site, product_id, fingerprint, value FROM proxy_cache "
                     "WHERE proxy_name=? ORDER BY judged_at", (pn,))}
        fp_field = {"image": "image_url", "name": "name"}.get(mat)
        judged = 0
        for it in items:
            hit = cache.get((it["site"], it["product_id"]))
            fp_now = it.get(fp_field) if fp_field else None
            valid = hit and (fp_field is None or hit[0] == fp_now)
            val = hit[1] if valid else None
            # proxy_cache.value는 TEXT다. 수치 프록시는 float로 되살려야 산술이 죽지 않는다.
            # proxy-load가 이미 캐스팅을 강제하지만, 구버전 DB에 문자열이 남았을 수 있어 방어한다.
            if is_numeric and val is not None:
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    val = None
            it["px_" + pn] = val
            judged += 1 if val is not None else 0
        # **값 공간 밖 값이 섞여 있으면 그 축을 쓰지 않는다** (D53 안전망).
        # 같은 이름으로 값 공간이 바뀌면 옛 판정이 남아 축이 갈린다 — 실측:
        # `name_lang`에 `영문`(1,120)과 `영문만`(945)이 공존해 같은 개념이 두
        # 그룹으로 검정됐다. 적재 가드가 앞으로를 막지만, **이미 갈린 DB에서도
        # 분석이 조용히 틀리면 안 된다.** 축을 빼고 그 사실을 남긴다.
        off = []
        if isinstance(space, list):
            off = sorted({it["px_" + pn] for it in items
                          if it.get("px_" + pn) is not None
                          and it["px_" + pn] not in space})
        if off:
            for it in items:
                it["px_" + pn] = None
            judged = 0
        proxies.append({"name": pn, "question": d["question"], "method": d["method"],
                        "numeric": is_numeric,
                        "judged": judged, "unjudged": len(items) - judged,
                        # 리포트가 "왜 이 축이 없나"에 답할 수 있어야 한다
                        "off_space": off})

    # 시계열 — 축적 관측이 있는 상품의 시점별 지표. 시점이 2개 이상인 상품만.
    ts_rows = conn.execute(f"""
        SELECT site, product_id, observed_at, rank, price_sale, like_count, review_count
        FROM observations o WHERE 1=1{ctx_where} ORDER BY observed_at
    """, params).fetchall()
    stamps_set, per = set(), {}
    for r in ts_rows:
        key = (r["site"], r["product_id"])
        stamps_set.add(r["observed_at"])
        per.setdefault(key, {})[r["observed_at"]] = r
    stamps = sorted(stamps_set)
    # 시점이 많으면 균등 구간 대표만 그린다(평균 금지 — D24, 첫·끝 포함)
    if len(stamps) > MAX_TREND_POINTS:
        stamps = [stamps[i] for i in downsample_indices(len(stamps))]
    series = []
    for key, byt in per.items():
        pts = [byt.get(s) for s in stamps]
        if sum(1 for p in pts if p is not None) < 2:
            continue   # 시계열이 성립하려면 2시점 이상
        it0 = next((i for i in items if (i["site"], i["product_id"]) == key), None)
        series.append({
            "site": key[0], "product_id": key[1],
            "name": (it0 or {}).get("name") or key[1],
            "rank": [p["rank"] if p else None for p in pts],
            "price": [p["price_sale"] if p else None for p in pts],
            "like": [p["like_count"] if p else None for p in pts],
            "review": [p["review_count"] if p else None for p in pts],
        })
    # 페이로드 상한: 관측 시점 많은 순 120계열 (초과분은 리포트에 건수 명시).
    # 순위만 세면 랭킹이 아닌 문맥(전수조사 축적 등)에서는 전부 0이라 아무 순서로나 잘린다 —
    # 네 지표의 관측 수를 합쳐 센다.
    series.sort(key=lambda s: -sum(
        1 for k in ("rank", "price", "like", "review") for v in s[k] if v is not None))
    ts_capped = len(series)
    series = series[:120]
    time_series = {"stamps": stamps, "series": series, "total_series": ts_capped,
                   "shown": len(series)} if series else None

    stock = attach_variants(conn, items, ctx_where, params)
    union = build_union_rows(items)

    times = [i["observed_at"] for i in items if i["observed_at"]]
    meta = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "obs_min": min(times) if times else None, "obs_max": max(times) if times else None,
        "contexts": sorted({i["context"] for i in items}),
        "sites": sorted({i["site"] for i in items}),
        "proxies": proxies,
        "stock": stock,
    }
    return {"meta": meta, "items": items, "price_events": events,
            "time_series": time_series, "union": union}


def attach_variants(conn, items, ctx_where, params):
    """옵션(사이즈)별 재고를 상품에 붙인다.

    `variants`(정적 구성) + `variant_observations`(시변 재고)에서 **옵션마다 마지막 관측**을
    가져온다. 수집이 L1(사이트가 이미 준 품절 상태)까지만 돌면 `stock_qty`는 전부 null이고
    **품절 여부만** 있는 것이다 — 그때 재고 '수량'을 만들어내지 않는다. SPEC-INTEL §6-4의
    옵션별 판매수량(재고 감소분)도 수량 축적이 있어야 성립하므로 여기서 계산하지 않는다.
    """
    try:
        rows = conn.execute(f"""
            SELECT v.site, v.product_id, v.option_id, v.size, v.option_name, v.color,
                   o.sold_out, o.stock_qty, o.stock_display, o.stock_basis, o.observed_at
            FROM variants v
            JOIN variant_observations o
              ON o.site = v.site AND o.product_id = v.product_id AND o.option_id = v.option_id
            WHERE o.observed_at = (
                SELECT MAX(o2.observed_at) FROM variant_observations o2
                WHERE o2.site = v.site AND o2.product_id = v.product_id
                  AND o2.option_id = v.option_id)
        """).fetchall()
    except sqlite3.OperationalError:
        return None
    by_product = {}
    for r in rows:
        by_product.setdefault((r["site"], r["product_id"]), []).append(r)

    has_any, qty_seen, bases = False, False, set()
    for it in items:
        vs = by_product.get((it["site"], it["product_id"]))
        if not vs:
            continue
        has_any = True
        out = sum(1 for v in vs if v["sold_out"])
        qty = [v["stock_qty"] for v in vs if v["stock_qty"] is not None]
        qty_seen = qty_seen or bool(qty)
        bases.update(v["stock_basis"] for v in vs if v["stock_basis"])
        it["opt_total"] = len(vs)
        it["opt_out"] = out
        it["opt_live"] = len(vs) - out
        # 품절률은 옵션 **개수** 비율이다 — 수량 비율이 아니다(수량은 대개 없다)
        it["opt_out_rate"] = round(out / len(vs) * 100, 1)
        if qty:
            it["stock_sum"] = sum(qty)
        # 사이즈별 집계용 — 이름은 size가 없으면 option_name을 그대로 쓴다(축을 지어내지 않는다)
        it["sizes"] = [[v["size"] or v["option_name"] or "?", 1 if v["sold_out"] else 0,
                        v["stock_qty"]] for v in vs]
    if not has_any:
        return None
    sold = min_sold(conn, items)
    return {"products": sum(1 for i in items if i.get("opt_total")),
            "options": sum(i.get("opt_total") or 0 for i in items),
            "has_qty": qty_seen, "basis": sorted(bases), "sold": sold}


def min_sold(conn, items):
    """옵션별 **최소 판매량** — 관측된 재고의 감소분이다 (SPEC-INTEL §6-4).

    관측 두 개의 차이이므로 추정이 아니다. 다만 한계가 둘 있고 리포트가 그대로 밝힌다:
    ① 재입고가 섞인 구간은 **과소 집계**된다(재입고 전에 팔린 수량이 가려진다)
    ② 같은 옵션을 두 번 이상 수량까지 관측해야 성립한다 — 축적 전에는 값이 없다.
    "판매량"이라고만 쓰면 실제보다 작은 수를 진짜 판매량으로 읽는다.
    """
    rows = conn.execute("""
        SELECT site, product_id, option_id, observed_at, stock_qty
        FROM variant_observations WHERE stock_qty IS NOT NULL
        ORDER BY site, product_id, option_id, observed_at
    """).fetchall()
    per, prev = {}, {}
    restock, windows = 0, []
    for r in rows:
        key = (r["site"], r["product_id"], r["option_id"])
        p = prev.get(key)
        if p is not None:
            diff = p[1] - r["stock_qty"]
            if diff > 0:
                per[key] = per.get(key, 0) + diff
            elif diff < 0:
                restock += 1
            windows.append((p[0], r["observed_at"]))
        prev[key] = (r["observed_at"], r["stock_qty"])
    if not per:
        return None
    by_product = {}
    for (site, pid, _opt), qty in per.items():
        by_product[(site, pid)] = by_product.get((site, pid), 0) + qty
    for it in items:
        v = by_product.get((it["site"], it["product_id"]))
        if v:
            it["sold_min"] = v
    return {"options": len(per), "total": sum(per.values()), "restock": restock,
            "from_at": min(w[0] for w in windows), "to_at": max(w[1] for w in windows)}


def build_union_rows(items):
    """플랫폼 합집합 — 같은 상품을 한 행으로 묶는다.

    매칭 규칙은 스토리 리포트와 **같은 것 하나**다(정규화 상품명 완전일치, build_report.match_key).
    두 곳에 규칙을 각각 적으면 같은 데이터에서 다른 합집합이 나온다.
    행에는 상품 인덱스만 담는다 — 상품 값을 복사하면 페이로드가 두 배가 된다.
    """
    sites = {i["site"] for i in items}
    if len(sites) < 2:
        return None
    rows, order = {}, []
    for idx, it in enumerate(items):
        key = match_key(it.get("name")) or "\x00%s\x00%s" % (it["site"], it["product_id"])
        row = rows.get(key)
        if row is None:
            row = {"n": it.get("name") or it["product_id"], "c": it.get("category"),
                   "b": it.get("brand"), "i": []}
            rows[key] = row
            order.append(key)
        row["i"].append(idx)
    return [rows[k] for k in order]


def product_series(db_path, contexts):
    """상품별 **전체** 관측 시퀀스. collect()의 time_series와 다르다.

    collect()는 화면에 그릴 목적이라 48시점으로 다운샘플하고 120계열로 자른다.
    이 함수는 **검정용**이라 자르지 않는다 — 사건 전후 구간을 세려면 실제 관측이
    빠짐없이 있어야 하고, 다운샘플된 계열로 이중차분을 하면 없는 구간을 만들어낸다.

    반환: {(site, product_id): [{observed_at, price_sale, like_count, review_count,
                                 sold_out, rank}, ...]}  — 시각 오름차순
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where, params = "", []
    if contexts:
        where = " WHERE context IN (%s)" % ",".join("?" * len(contexts))
        params = list(contexts)
    rows = conn.execute(
        "SELECT site, product_id, observed_at, price_sale, like_count, review_count, "
        "sold_out, rank FROM observations%s ORDER BY site, product_id, observed_at"
        % where, params).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out.setdefault((r["site"], r["product_id"]), []).append(dict(r))
    return out


# ══════════════════════════════════════════════════════════════════════════
# 스타일 : 변형 2계층 (2026-08-03 — 사용자 지적 #7)
# ══════════════════════════════════════════════════════════════════════════
# 플랫폼마다 **한 상품을 세는 단위가 다르다.** 무신사는 `REYA LACE TOP (5 COLORS)`
# 한 줄로 올리고 자사몰은 색상마다 따로 올린다. 그대로 세면 자사몰 카탈로그가 몇 배로
# 부풀고, 플랫폼 비교의 n이 서로 다른 기준으로 세어진다.
#
# 실측(2026-08-03): 사이트 안에서 같은 스타일로 묶이는 상품이 4,350건.
# 그중 **843개 그룹은 변형끼리 가격이 다르다** — 대표 하나만 쓰면 그 차이가 사라진다.
#
# 그래서 단위를 **명시적으로 고른다**:
#   variant(상품) — 재고·품절·개별 가격. 기본값
#   style(스타일)  — 플랫폼 비교·카탈로그 폭. 대표값은 **중앙값**(첫 건이 아니다)


def style_groups(items):
    """(site, style_key) → 그 스타일의 변형 목록. 같은 사이트 안에서만 묶는다."""
    groups = {}
    for it in items:
        key = match_key(it.get("name"))
        if not key:
            key = "\x00%s\x00%s" % (it["site"], it["product_id"])
        groups.setdefault((it["site"], key), []).append(it)
    return groups


STYLE_NUMERIC_BASE = [
    "price_original", "price_sale", "discount_rate", "review_count", "rating",
    "purchase_count", "like_count", "viewers_now", "opt_out_rate", "opt_total"]


def style_rows(items, numeric_fields=None, proxies=None):
    """상품 목록을 **스타일 단위**로 접는다.

    수치는 변형들의 **중앙값**을 쓴다. 첫 건을 쓰면 어느 색이 먼저 수집됐느냐에 따라
    값이 달라져 재현되지 않고, 평균을 쓰면 한 색만 세일 중일 때 끌려간다.

    **수치형 프록시(px_*)도 중앙값 집계 대상이다** — 넣지 않으면 첫 변형(members[0])
    값을 그대로 써서, 첫 변형이 미판정(None)이면 스타일 전체가 결측이 되고 DB 반환
    순서에 따라 값이 달라진다(이 docstring이 경계하는 바로 그 문제의 재발). 그래서
    호출부는 `proxies=data["meta"]["proxies"]`를 넘겨 수치 프록시를 목록에 더한다.

    `variant_n`(변형 수)과 `variant_price_spread`(변형 간 가격 폭)를 함께 남긴다 —
    접었다는 사실과 접으면서 버린 정보량을 리포트가 밝힐 수 있어야 한다.
    """
    if numeric_fields is None:
        numeric_fields = list(STYLE_NUMERIC_BASE)
        for p in (proxies or []):
            if p.get("numeric"):
                numeric_fields.append("px_" + p["name"])
    # 범주형 프록시(px_thumb_cut 등)는 첫 변형이 아니라 **최빈값**으로 접는다.
    # 같은 스타일이라도 색상 변형마다 대표 이미지가 달라 판정이 갈릴 수 있어서,
    # members[0]을 그대로 쓰면 DB 반환 순서에 따라 값이 달라진다(수치 중앙값과 같은 이유).
    cat_proxy_fields = ["px_" + p["name"] for p in (proxies or []) if not p.get("numeric")]
    out = []
    for (site, key), members in style_groups(items).items():
        rep = dict(members[0])
        rep["style_key"] = key
        rep["variant_n"] = len(members)
        for f in numeric_fields:
            vals = [m[f] for m in members if m.get(f) is not None]
            rep[f] = st.median(vals) if vals else None
        for f in cat_proxy_fields:
            vals = [m[f] for m in members if m.get(f) is not None]
            # 최빈값. 동률이면 값 오름차순 첫 값 — 결정적이라 members 순서와 무관하게 재현된다
            if vals:
                c = Counter(vals)
                rep[f] = min(c, key=lambda v: (-c[v], str(v)))
            else:
                rep[f] = None
        prices = [m["price_sale"] for m in members if m.get("price_sale") is not None]
        rep["variant_price_spread"] = (max(prices) - min(prices)) if len(prices) > 1 else 0
        # 하나라도 재고가 있으면 그 스타일은 살아 있다 — 전부 품절일 때만 품절이다
        so = [m["sold_out"] for m in members if m.get("sold_out") is not None]
        rep["sold_out"] = (1 if all(so) else 0) if so else None
        out.append(rep)
    return out


def matched_pairs(items, metric, unit="style"):
    """플랫폼 간 **같은 상품** 쌍. 쌍체 비교용.

    독립 두 집단으로 플랫폼을 비교하면 상품 구성 차이가 섞인다 — 한쪽에 비싼 아우터가
    많으면 "그 플랫폼이 비싸다"가 나온다. 같은 상품끼리 짝지으면 그 교란이 사라진다.

    매칭은 정규화 상품명 완전일치(match_key)뿐이다 — 리포트 전체가 쓰는 규칙 하나.

    **기본 단위는 스타일이다.** 상품 단위로 짝지으면 색상 변형이 많은 쪽이 같은 쌍을
    여러 번 밀어 넣어 쌍이 부풀고, 순열검정의 n이 실제 독립 관측 수보다 커진다
    (유사 반복 — p값이 실제보다 작게 나온다).

    반환: {(siteA, siteB): [(valueA, valueB, name), ...]}
    """
    rows = style_rows(items) if unit == "style" else items
    by_key = {}
    for it in rows:
        if it.get(metric) is None:
            continue
        key = it.get("style_key") or match_key(it.get("name"))
        if not key:
            continue
        by_key.setdefault(key, {}).setdefault(it["site"], it)
    pairs = {}
    for key, per_site in by_key.items():
        sites = sorted(per_site)
        for i in range(len(sites)):
            for j in range(i + 1, len(sites)):
                a, b = sites[i], sites[j]
                pairs.setdefault((a, b), []).append(
                    (per_site[a][metric], per_site[b][metric],
                     per_site[a].get("name") or key))
    return pairs
