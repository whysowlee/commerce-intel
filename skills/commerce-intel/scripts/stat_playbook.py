#!/usr/bin/env python3
"""통계 플레이북 — 데이터 형태에서 기법으로 가는 표를 코드로 못 박은 것 (D28 · 2026-08-03).

## 왜 미리 정해두나

인사이트 엔진의 예산은 30~60분이다. 요청마다 통계 설계를 새로 짜면 그 안에 못 끝낸다.
그래서 **어떤 신호에 어떤 검정을 쓸지 사전에 확정**하고 꺼내 쓴다.

## 왜 순열검정인가

p값을 얻는 데 scipy를 쓰지 않는다. 배포 의존을 하나라도 줄여야 하고(D31 전원 배포),
무엇보다 **순열검정은 분포 가정이 없다.** 하트·판매량은 자릿수를 넘나들고 왜도가 5를
넘는 일이 흔해서 정규분포를 전제하는 t검정이 애초에 안 맞는다.

난수는 **고정 시드**를 쓴다 — 같은 데이터에 같은 답이 나와야 리포트를 신뢰할 수 있다.

## 무엇을 내는가

각 검정은 `effect`(효과 크기) · `p` · `n`을 함께 낸다. **p값만으로는 리오더를 못 정한다** —
n이 크면 의미 없는 차이도 유의해지기 때문이다. 판정은 셋을 같이 본다.
"""
import math
import random
import statistics as st

SEED = 20260803          # 고정 — 같은 데이터에 같은 답
PERM_ITER = 2000         # 순열 반복. p=0.05 근방을 가르기에 충분하다
PERM_MAX_N = 4000        # 이보다 크면 표본추출해서 순열을 돈다(시간 예산)


# ── 효과 크기 ───────────────────────────────────────────────────────────────
def cohens_d(a, b):
    """두 그룹 평균 차이를 표준편차로 나눈 것. 단위가 달라도 비교된다.

    통상 해석: 0.2 작다 · 0.5 중간 · 0.8 크다. 이 프로젝트는 0.3을 실무 하한으로 본다
    (그보다 작으면 리오더 결정을 바꾸지 못한다).
    """
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = st.variance(a), st.variance(b)
    pooled = math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / (len(a) + len(b) - 2))
    if pooled == 0:
        return None
    return (st.fmean(a) - st.fmean(b)) / pooled


def cliffs_delta(a, b):
    """순위 기반 효과 크기. 왜도가 심한 값(하트·판매량)에서 Cohen's d보다 정직하다.

    -1~+1. |δ| 0.147 작다 · 0.33 중간 · 0.474 크다 (Romano 기준).
    """
    if not a or not b:
        return None
    sa, sb = sorted(a), sorted(b)
    # 정렬 후 이분 탐색으로 O(n log n) — 순진한 이중 루프는 25,000건에서 못 쓴다
    import bisect
    gt = sum(bisect.bisect_left(sb, x) for x in sa)
    lt = sum(len(sb) - bisect.bisect_right(sb, x) for x in sa)
    return (gt - lt) / (len(sa) * len(sb))


# ── 순열검정 ────────────────────────────────────────────────────────────────
def _sample(xs, cap, rng):
    return xs if len(xs) <= cap else rng.sample(xs, cap)


def perm_test_groups(a, b, iters=PERM_ITER):
    """두 그룹이 다른지. 라벨을 섞어 실제 차이보다 큰 경우를 센다.

    **검정통계량은 순위 합이다**(Mann-Whitney U와 같은 것). 중앙값 차이를 쓰면 반복마다
    정렬이 필요해 25,000건 문맥에서 10분을 넘긴다(2026-08-03 실측). 순위는 **한 번만**
    매기고 반복에서는 부분합만 구하면 되므로 반복당 O(n)이다.

    순위 기반이라 왜도에도 강하다 — 하트·판매량은 왜도가 5를 넘는 일이 흔하다.
    """
    rng = random.Random(SEED)
    a = _sample(list(a), PERM_MAX_N // 2, rng)
    b = _sample(list(b), PERM_MAX_N // 2, rng)
    if len(a) < 5 or len(b) < 5:
        return None
    pooled = _ranks(a + b)          # 순위는 여기서 한 번만
    na = len(a)
    obs = abs(sum(pooled[:na]) / na - sum(pooled[na:]) / (len(pooled) - na))
    hits = 0
    total = sum(pooled)
    n = len(pooled)
    for _ in range(iters):
        rng.shuffle(pooled)
        sa = sum(pooled[:na])
        if abs(sa / na - (total - sa) / (n - na)) >= obs:
            hits += 1
    # +1 보정 — p=0이라고 쓰지 않는다. 순열 2000번으로는 그보다 작다고 말할 수 없다
    return (hits + 1) / (iters + 1)


def perm_test_corr(pairs, iters=PERM_ITER):
    """상관이 우연인지. y를 섞어 실제 |r|보다 큰 경우를 센다."""
    rng = random.Random(SEED)
    pairs = _sample(list(pairs), PERM_MAX_N, rng)
    if len(pairs) < 10:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    obs = abs(_spearman_xy(xs, ys) or 0)
    hits = 0
    for _ in range(iters):
        rng.shuffle(ys)
        r = _spearman_xy(xs, ys)
        if r is not None and abs(r) >= obs:
            hits += 1
    return (hits + 1) / (iters + 1)


def _ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def _pearson_xy(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = st.fmean(xs), st.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _spearman_xy(xs, ys):
    if len(xs) < 3:
        return None
    return _pearson_xy(_ranks(xs), _ranks(ys))


def spearman(pairs):
    if len(pairs) < 3:
        return None
    return _spearman_xy([p[0] for p in pairs], [p[1] for p in pairs])


# ── 다중비교 보정 ───────────────────────────────────────────────────────────
def benjamini_hochberg(pvalues, alpha=0.05):
    """FDR 보정. **가설을 많이 뽑을수록 우연한 유의가 반드시 나온다.**

    분석 리포트 스펙 §4-8이 이걸 경고 문구로만 갖고 있었는데(대시보드 시절), 이제
    사람이 축 조합을 훑는 게 아니라 엔진이 수십 개를 한 번에 돌리므로 **코드로 보정한다.**

    Bonferroni가 아니라 BH를 쓰는 이유: 탐색 단계에서 Bonferroni는 지나치게 보수적이라
    진짜 신호까지 죽인다. 우리는 "강한 주장 5개"를 고르는 게 목적이지 확증이 아니다.

    반환: 각 p에 대한 생존 여부 리스트 (입력 순서 유지)
    """
    idx = [i for i, p in enumerate(pvalues) if p is not None]
    if not idx:
        return [False] * len(pvalues)
    m = len(idx)
    order = sorted(idx, key=lambda i: pvalues[i])
    survive = [False] * len(pvalues)
    k_max = -1
    for rank, i in enumerate(order, start=1):
        if pvalues[i] <= rank / m * alpha:
            k_max = rank
    for rank, i in enumerate(order, start=1):
        if rank <= k_max:
            survive[i] = True
    return survive


# ── 사건 연구 ───────────────────────────────────────────────────────────────
def event_study(events, control_events=None):
    """가격 변경 전후의 반응 증분. MD의 1순위 질문("할인 얼마 줬을 때 얼마나 팔리나").

    **대조군이 없으면 사건 연구가 아니라 전후 비교일 뿐이다.** 같은 기간에 시즌·행사 같은
    공통 요인이 있으면 처치 효과와 구분되지 않는다. 그래서 대조군(같은 창에서 가격을
    바꾸지 않은 상품)의 증분을 빼서 **차이의 차이**를 낸다.

    한계는 리포트가 그대로 밝힌다:
    - 하트·후기 증분은 판매량이 아니다. 판매 대리 지표일 뿐이다
    - 관측 창이 상품마다 다르면 증분을 나란히 놓을 수 없다
    """
    treated = [e for e in events if e.get("like_delta") is not None]
    if len(treated) < 10:
        return None
    cuts = [e for e in treated if (e["price_to"] or 0) < (e["price_from"] or 0)]
    rises = [e for e in treated if (e["price_to"] or 0) > (e["price_from"] or 0)]
    if len(cuts) < 5:
        return None

    def depth(e):
        if not e["price_from"]:
            return None
        return (e["price_from"] - e["price_to"]) / e["price_from"] * 100

    out = {"n_events": len(treated), "n_cuts": len(cuts), "n_rises": len(rises)}
    deltas = [e["like_delta"] for e in cuts]
    out["cut_like_delta_median"] = st.median(deltas)
    if rises:
        out["rise_like_delta_median"] = st.median([e["like_delta"] for e in rises])
        out["effect"] = cliffs_delta(deltas, [e["like_delta"] for e in rises])
        out["p"] = perm_test_groups(deltas, [e["like_delta"] for e in rises])
        out["control"] = "가격을 올린 사건 (같은 기간)"
    elif control_events:
        c = [e["like_delta"] for e in control_events if e.get("like_delta") is not None]
        if len(c) >= 5:
            out["effect"] = cliffs_delta(deltas, c)
            out["p"] = perm_test_groups(deltas, c)
            out["control"] = "같은 창에서 가격을 바꾸지 않은 상품"
    # 할인 폭과 반응 증분의 관계 — "얼마나 주면 얼마나"에 직접 답하는 축
    pairs = [(depth(e), e["like_delta"]) for e in cuts if depth(e) is not None]
    if len(pairs) >= 10:
        out["depth_response_r"] = spearman(pairs)
        out["depth_response_p"] = perm_test_corr(pairs)
        out["depth_response_n"] = len(pairs)
    return out


# ── 구간별 추세 ─────────────────────────────────────────────────────────────
def binned_trend(pairs, bins=6):
    """x를 구간으로 나눠 각 구간의 y 중앙값. "어느 가격대에서 반응하나"에 답한다.

    상관계수 하나로는 "어디가 좋은가"를 못 말한다 — MD가 물은 건 방향이 아니라 **위치**다.
    """
    if len(pairs) < 30:
        return None
    xs = sorted(p[0] for p in pairs)
    qs = [xs[int(len(xs) * i / bins)] for i in range(1, bins)]
    out = []
    for i in range(bins):
        lo = -math.inf if i == 0 else qs[i - 1]
        hi = math.inf if i == bins - 1 else qs[i]
        ys = [p[1] for p in pairs if lo <= p[0] < hi or (i == bins - 1 and p[0] >= lo)]
        if len(ys) >= 5:
            out.append({"from": None if lo == -math.inf else lo,
                        "to": None if hi == math.inf else hi,
                        "n": len(ys), "median": st.median(ys)})
    return out or None
