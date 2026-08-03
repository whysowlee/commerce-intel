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
# 순열 반복. **p의 하한이 1/(iters+1)** 이라는 점이 중요하다 — 가설 수가 많으면
# 다중비교 보정 임계가 이 하한보다 낮아져 구조적으로 아무도 통과 못 한다(2026-08-03).
# 4,000회면 하한이 0.00025로 내려가 가설 400개까지 α=0.10을 감당한다.
PERM_ITER = 4000
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


# ═══════════════════════════════════════════════════════════════════════════
# 리오더 직결 — 소진 속도
# ═══════════════════════════════════════════════════════════════════════════
def _hours(a, b):
    from datetime import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return (datetime.strptime(b[:19], fmt)
                    - datetime.strptime(a[:19], fmt)).total_seconds() / 3600
        except ValueError:
            continue
    return None


def time_to_soldout(series):
    """상품별 **품절까지 걸린 시간**. 리오더 판단의 본체다 (2026-08-03 MD 인터뷰).

    관측 시퀀스에서 재고 있음(sold_out=0)이 처음 보인 시점부터 품절(1)이 처음 보인
    시점까지를 잰다.

    **아직 품절 안 난 상품을 빼면 안 된다.** 빼면 "빨리 팔린 것"만 남아 소진 속도가
    실제보다 빠르게 나온다(생존편향의 교과서적 사례). 그래서 마지막 관측까지의 시간을
    **중도절단(censored)**으로 함께 담고, 중앙값은 Kaplan-Meier로 구한다.

    반환: [{key, hours, event(True=품절 관측 / False=중도절단), restocks}]
    """
    out = []
    for key, obs in series.items():
        seq = [o for o in obs if o.get("sold_out") is not None]
        if len(seq) < 2:
            continue
        start = next((o for o in seq if o["sold_out"] == 0), None)
        if start is None:
            continue                     # 처음부터 품절 — 시작점을 모른다
        after = [o for o in seq if o["observed_at"] > start["observed_at"]]
        hit = next((o for o in after if o["sold_out"] == 1), None)
        end = hit or (after[-1] if after else None)
        if end is None:
            continue
        h = _hours(start["observed_at"], end["observed_at"])
        if h is None or h <= 0:
            continue
        out.append({"key": key, "hours": h, "event": hit is not None,
                    "restocks": count_restocks(seq)})
    return out


def count_restocks(seq):
    """재입고 횟수 — sold_out이 1에서 0으로 돌아온 횟수.

    소진 속도 계산의 전제다. 재입고를 모르면 "다시 팔리기 시작한 것"을 "품절 안 남"으로
    읽는다. 옵션별 판매수량(D17)이 재입고 구간에서 과소 집계되는 것과 같은 문제다.
    """
    n, prev = 0, None
    for o in seq:
        if prev == 1 and o["sold_out"] == 0:
            n += 1
        prev = o["sold_out"]
    return n


def km_median(records):
    """Kaplan-Meier 중앙 생존시간. 중도절단을 반영한 "절반이 품절되는 시점".

    단순 중앙값을 쓰면 중도절단된 상품을 어떻게 세든 틀린다 — 빼면 편향되고,
    관측 종료 시각을 품절 시각으로 치면 실제보다 빠르게 나온다.

    사건이 절반에 못 미치면 중앙값에 도달하지 못한다 — 그때는 None을 돌려주고
    "관측 기간 안에 절반이 품절되지 않았다"고 말한다. 외삽하지 않는다.
    """
    if not records:
        return None, 0.0
    rows = sorted(records, key=lambda r: r["hours"])
    n_at_risk = len(rows)
    surv = 1.0
    median = None
    i = 0
    while i < len(rows):
        t = rows[i]["hours"]
        tied = [r for r in rows if r["hours"] == t]
        events = sum(1 for r in tied if r["event"])
        if events and n_at_risk > 0:
            surv *= (1 - events / n_at_risk)
            if median is None and surv <= 0.5:
                median = t
        n_at_risk -= len(tied)
        i += len(tied)
    censor_rate = sum(1 for r in rows if not r["event"]) / len(rows)
    return median, censor_rate


def compare_depletion(rec_a, rec_b):
    """두 그룹의 소진 속도 비교. 사건(품절)이 양쪽에 충분해야 성립한다."""
    ea = [r for r in rec_a if r["event"]]
    eb = [r for r in rec_b if r["event"]]
    if len(ea) < 5 or len(eb) < 5:
        return None
    ma, ca = km_median(rec_a)
    mb, cb = km_median(rec_b)
    return {
        "km_median_a": ma, "km_median_b": mb,
        "censor_a": round(ca, 3), "censor_b": round(cb, 3),
        "events_a": len(ea), "events_b": len(eb),
        "n_a": len(rec_a), "n_b": len(rec_b),
        # 사건이 난 것들끼리의 시간 비교 — 중도절단을 무시하므로 보조 지표다
        "effect": cliffs_delta([r["hours"] for r in ea], [r["hours"] for r in eb]),
        "p": perm_test_groups([r["hours"] for r in ea], [r["hours"] for r in eb]),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 용량-반응 — "할인 얼마 줬을 때 얼마나"
# ═══════════════════════════════════════════════════════════════════════════
DEPTH_BINS = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 101)]


def dose_response(events, metric="like_delta"):
    """할인 **폭 구간별** 반응. MD의 1순위 질문에 직접 답하는 형태다.

    상관계수 하나로는 "얼마나 주면 되나"에 답할 수 없다 — 방향만 말하기 때문이다.
    구간으로 끊어야 "20%까지는 반응이 커지는데 그 위로는 안 커진다" 같은 걸 본다.

    한계는 그대로 밝힌다: **하트 증분은 판매량이 아니다.** 판매 대리 지표일 뿐이고,
    실제 판매는 옵션 재고 감소분이 축적돼야 계산된다(D17).
    """
    cuts = []
    for e in events:
        if e.get(metric) is None or not e.get("price_from"):
            continue
        depth = (e["price_from"] - e["price_to"]) / e["price_from"] * 100
        if depth <= 0:
            continue
        cuts.append((depth, e[metric]))
    if len(cuts) < 20:
        return None
    bins = []
    for lo, hi in DEPTH_BINS:
        vals = [v for d, v in cuts if lo <= d < hi]
        if len(vals) >= 5:
            bins.append({"from": lo, "to": hi if hi <= 100 else None,
                         "n": len(vals), "median": st.median(vals),
                         "mean": round(st.fmean(vals), 2)})
    if len(bins) < 2:
        return None
    # 구간이 올라갈수록 반응도 커지는가 — 구간 대표값의 순위 상관
    trend = spearman([(i, b["median"]) for i, b in enumerate(bins)])
    return {"bins": bins, "n": len(cuts), "monotonic_r": trend,
            "metric": metric,
            "note": "하트 증분은 판매량이 아니다 — 판매 대리 지표다",
            "_cuts": cuts}


def dose_monotonic(cuts):
    """이미 뽑아둔 (할인폭, 반응) 쌍으로 단조성만 다시 구한다 — 홀드아웃 재확인용."""
    bins = []
    for lo, hi in DEPTH_BINS:
        vals = [v for d, v in cuts if lo <= d < hi]
        if len(vals) >= 3:
            bins.append(st.median(vals))
    if len(bins) < 2:
        return None
    return spearman([(i, m) for i, m in enumerate(bins)])


# ═══════════════════════════════════════════════════════════════════════════
# 쌍체 비교 — 같은 상품을 두 플랫폼에서
# ═══════════════════════════════════════════════════════════════════════════
def paired_test(pairs, iters=PERM_ITER):
    """**같은 상품**의 두 플랫폼 값을 짝지어 비교한다.

    독립 두 집단 비교는 상품 구성 차이가 섞인다 — 한쪽에 비싼 아우터가 많으면
    "그 플랫폼이 비싸다"가 나온다. 짝을 지으면 그 교란이 통째로 사라진다.

    순열은 **부호 뒤집기**다. 쌍체 자료에서 귀무가설은 "차이의 부호가 무작위"이므로,
    라벨을 섞는 게 아니라 각 쌍의 부호를 무작위로 뒤집는 것이 정확한 순열이다.
    """
    diffs = [a - b for a, b, _ in pairs if a is not None and b is not None]
    if len(diffs) < 10:
        return None
    rng = random.Random(SEED)
    diffs = _sample(diffs, PERM_MAX_N, rng)
    obs = abs(st.median(diffs))
    hits = 0
    for _ in range(iters):
        flipped = [d if rng.random() < 0.5 else -d for d in diffs]
        if abs(st.median(flipped)) >= obs:
            hits += 1
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    return {
        "n_pairs": len(diffs),
        "median_diff": st.median(diffs),
        "wins_a": pos, "wins_b": neg, "ties": len(diffs) - pos - neg,
        # 효과 크기 = 한쪽이 이기는 비율의 쏠림 (0.5면 무승부)
        "effect": (pos / (pos + neg) - 0.5) * 2 if (pos + neg) else None,
        "p": (hits + 1) / (iters + 1),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 이중차분 — 가격 변경의 진짜 효과
# ═══════════════════════════════════════════════════════════════════════════
def did_within(series, min_events=10):
    """이중차분(DiD). 가격을 바꾼 상품의 **자기 직전 구간**을 기준선으로 쓴다.

    구 `event_study`는 인하군과 인상군의 증분 **수준**을 비교했다. 그건 전후 비교이지
    이중차분이 아니고, 시즌·행사처럼 두 군에 공통으로 걸리는 요인을 못 걷어낸다.

    여기서는 상품마다:
        처치 효과 = (가격 변경 구간의 증분) − (직전 구간의 증분)
    를 구한다. 상품 고유의 인기도·노출량이 두 구간에 똑같이 깔려 있으므로 빼면 사라진다.

    그 다음 **대조군**(같은 기간에 가격을 바꾸지 않은 상품)의 같은 값과 비교한다.
    대조군도 (구간 증분 − 직전 구간 증분)이므로, 남는 것은 가격 변경에 기인한 몫이다.

    한계: 관측 간격이 상품마다 다르면 구간 길이가 달라 비교가 흔들린다. 간격 편차를
    같이 낸다 — 크면 해석에 주의를 붙인다.
    """
    treated, control = [], []
    gaps = []
    for key, obs in series.items():
        seq = [o for o in obs if o.get("like_count") is not None
               and o.get("price_sale") is not None]
        if len(seq) < 3:
            continue
        for i in range(2, len(seq)):
            prev2, prev1, cur = seq[i - 2], seq[i - 1], seq[i]
            post = cur["like_count"] - prev1["like_count"]
            pre = prev1["like_count"] - prev2["like_count"]
            within = post - pre
            changed_now = cur["price_sale"] != prev1["price_sale"]
            changed_before = prev1["price_sale"] != prev2["price_sale"]
            if changed_before:
                continue          # 기준선 구간이 오염됐다 — 쓰지 않는다
            g = _hours(prev1["observed_at"], cur["observed_at"])
            if g:
                gaps.append(g)
            if changed_now and cur["price_sale"] < prev1["price_sale"]:
                treated.append(within)
            elif not changed_now:
                control.append(within)
    if len(treated) < min_events or len(control) < min_events:
        return None
    did = st.median(treated) - st.median(control)
    return {
        "n_treated": len(treated), "n_control": len(control),
        "treated_within_median": st.median(treated),
        "control_within_median": st.median(control),
        "did": did,
        "effect": cliffs_delta(treated, control),
        "p": perm_test_groups(treated, control),
        "gap_hours_median": round(st.median(gaps), 1) if gaps else None,
        "gap_hours_iqr": (round(st.quantiles(gaps, n=4)[0], 1),
                          round(st.quantiles(gaps, n=4)[2], 1)) if len(gaps) >= 4 else None,
        "control_def": "같은 기간에 가격을 바꾸지 않은 상품의 (구간 증분 − 직전 구간 증분)",
        # 홀드아웃이 절반으로 나눠 재확인할 재료. 리포트에는 싣지 않는다(strip_payload)
        "_treated": treated, "_control": control,
    }


def did_from(treated, control):
    """이미 뽑아둔 처치·대조 배열로 이중차분만 다시 구한다 — 홀드아웃 재확인용."""
    if len(treated) < 5 or len(control) < 5:
        return None
    return {"did": st.median(treated) - st.median(control),
            "effect": cliffs_delta(treated, control)}
