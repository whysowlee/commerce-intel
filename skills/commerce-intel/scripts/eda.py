#!/usr/bin/env python3
"""EDA 고정 세트 — 분석 전에 데이터가 무엇인지부터 본다 (D29 · 2026-08-03).

## 왜 고정인가

우리 도메인은 이미 정해져 있다(패션 커머스 상품·관측). 매번 EDA를 새로 설계할 이유가
없고, 새로 설계하면 10분 SLA를 못 지킨다. 그래서 **무엇을 볼지 미리 못 박고** 그 결과를
인사이트 엔진이 먹는 신호로 낸다.

## 이 산출물은 사람용 리포트가 아니다

`signals[]`는 **인사이트 엔진의 입력**이다. "어느 축에 신호가 있는지"를 판정해 가설 생성
범위를 좁힌다. 사람이 읽을 EDA 요약이 필요하면 그건 리포트 단이 만든다.

절차·임계값은 whysowlee/data-analytics-skills의 `programmatic-eda`에서 차용했다
(2026-08-03). 결측 WARN 5%/FAIL 30%는 우리 `db-contract`가 이미 쓰던 값과 같아 그대로
맞물린다. IQR 1.5배·|z|>3·왜도 구간·|r|≥0.8도 그 문서 값이다.

    python3 eda.py --db data/intel.db --context "brand:로우클래식" --out data/eda.json
"""
import argparse
import json
import math
import os
import statistics as st
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from intel_data import cat_axes, collect, num_axes  # noqa: E402

# ── 임계값 (programmatic-eda/references/quality_thresholds.md 차용) ──────────
NULL_WARN, NULL_FAIL = 5.0, 30.0      # 결측률 % — db-contract와 같은 값

# **노출 여부가 곧 성과인 축** (D50 실측). 무신사는 잘 팔린 상품에만 조회수·누적판매를
# 보여준다(D50 실측: 조회수 표시 있음 235개 하트 중앙 508 대 없음 62개 하트 117).
# 이 축들은 결측률이 높은 게 정상이고, 그 높음 자체가 정보다. 결측률 컷에서 면제하되
# **값이 있는 상품 안에서만** 비교한다는 사실을 리포트가 반드시 밝힌다.
EXPOSURE_SIGNAL_FIELDS = {"purchase_band", "view_band", "like_band",
                          "purchase_count", "view_count",
                          "cvr_view_like", "cvr_view_buy", "cvr_like_buy",
                          "review_per_buy"}
IQR_K = 1.5                            # IQR 이상치 계수
Z_CUT = 3.0                            # z-score 이상치
SKEW_LOG = 1.0                         # |왜도| 이 이상이면 로그 변환 권고
CORR_STRONG = 0.8                      # |r| 이 이상이면 다중공선성 의심
MIN_N = 30                             # 이 미만이면 "표본이 작다" — 분석 근거로 쓰지 않는다
SIMPSON_FLIP = 0.15                    # 세그먼트 상관이 전체와 이만큼 어긋나면 역전 경고

# ── 정의상 연동된 쌍 ────────────────────────────────────────────────────────
# 할인율은 `1 - 판매가/정가`다. 이 셋의 상관은 데이터가 아니라 **산수**이고, 가설로 넘기면
# 인사이트가 동어반복이 된다("할인율이 높을수록 판매가가 낮다"). 신호에서 뺀다.
#
# 뺀다고 해서 분석에서 못 쓰는 게 아니다 — 사건 연구의 처치 변수로는 할인율이 주인공이다.
# 여기서 막는 것은 **횡단면 상관을 발견처럼 보고하는 것**뿐이다.
DEFINITIONAL_PAIRS = {
    frozenset(("price_sale", "price_original")),
    frozenset(("price_sale", "discount_rate")),
    frozenset(("price_original", "discount_rate")),
}
# 정의상 연동은 아니지만 둘 다 "오래 노출된 상품일수록 큰 값"이라 동반 상승한다.
# 허영 지표 문제(분석 리포트 스펙 §4-9)라 신호로는 남기되 경고를 붙인다.
VANITY_FIELDS = {"like_count", "review_count", "purchase_count"}

# 범주 축 — 그룹 비교의 후보다
CAT_AXES = [("site", "플랫폼"), ("category", "카테고리"), ("brand", "브랜드"),
            ("fit", "핏"), ("sold_out", "품절")]


# ── 통계 최소 도구 (외부 의존 없이) ─────────────────────────────────────────
# numpy/pandas를 쓰지 않는 이유는 배포 무게다. reportlab 하나로 이미 약속을 깼고,
# EDA 규모(수천~수만 행)에서는 순수 파이썬으로 충분하다.
def _vals(items, field):
    return [i[field] for i in items if i.get(field) is not None]


def _quartiles(xs):
    s = sorted(xs)
    if len(s) < 4:
        return (s[0], st.median(s), s[-1]) if s else (None, None, None)
    q = st.quantiles(s, n=4, method="inclusive")
    return q[0], q[1], q[2]


def _skew(xs):
    """표본 왜도. 분포가 한쪽으로 쏠렸는지 — 로그 축을 쓸지 판단한다."""
    n = len(xs)
    if n < 3:
        return None
    m = st.fmean(xs)
    sd = st.pstdev(xs)
    if sd == 0:
        return 0.0
    return sum(((x - m) / sd) ** 3 for x in xs) * n / ((n - 1) * (n - 2))


def _pearson(pairs):
    n = len(pairs)
    if n < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = st.fmean(xs), st.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in pairs)
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


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


def _spearman(pairs):
    """순위 상관. 가격·하트처럼 자릿수를 넘나드는 값에는 이쪽이 정직하다."""
    if len(pairs) < 3:
        return None
    xr = _ranks([p[0] for p in pairs])
    yr = _ranks([p[1] for p in pairs])
    return _pearson(list(zip(xr, yr)))


def _cohens_d(a, b):
    """효과 크기. p값만으로는 리오더를 못 정한다 — 크기를 같이 낸다."""
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = st.variance(a), st.variance(b)
    pooled = math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / (len(a) + len(b) - 2))
    if pooled == 0:
        return None
    return (st.fmean(a) - st.fmean(b)) / pooled


# ── 1. 그레인 ───────────────────────────────────────────────────────────────
def check_grain(data):
    """한 행이 무엇인지 못 박는다. 여기가 어긋나면 이후 전부가 어긋난다."""
    items = data["items"]
    return {
        "row_means": "상품 하나의 최신 관측 (site × product_id)",
        "rows": len(items),
        "products": len({(i["site"], i["product_id"]) for i in items}),
        "sites": data["meta"]["sites"],
        "contexts": data["meta"]["contexts"],
        "observed_from": data["meta"]["obs_min"],
        "observed_to": data["meta"]["obs_max"],
    }


# ── 2. 결측 지도 ────────────────────────────────────────────────────────────
def check_nulls(data):
    """미노출(null)과 0을 구분한다. 이 둘을 섞으면 "반응 없음"이라는 거짓말이 만들어진다.

    **결측에는 두 종류가 있다** (D51 — 실측으로 드러났다):

      ① 품질 결측 — 그 사이트가 주는데 우리가 못 받았다. 30% 넘으면 축을 버린다
      ② 구조 결측 — **그 사이트가 아예 안 준다.** 자사몰(Cafe24)은 하트·후기·평점을
         노출하지 않는다(어댑터 실측). 이건 데이터 품질 문제가 아니다

    둘을 섞으면 축이 통째로 사라진다. `brand:2000아카이브스` 실측: 자사몰 502개에
    하트가 없어 전체 결측률 44.5% → 축 탈락. 그런데 **무신사·29CM에는 626개가
    실제로 있었다.** 반응 지표가 하나도 없으니 리포트의 Y가 전부 가격·할인이 됐다.

    그래서 **노출하는 플랫폼 안에서** 결측률을 다시 잰다. 통과하면 축으로 쓰되
    **어느 플랫폼이 빠졌는지 함께 남긴다** — 비교가 그 플랫폼을 안 본다는 사실을
    리포트가 말해야 한다(안 그러면 "전체를 봤다"로 읽힌다).
    """
    items = data["items"]
    n = len(items)
    out = []
    for field, label in num_axes(data):     # 고정 + numeric 프록시
        miss = sum(1 for i in items if i.get(field) is None)
        zeros = sum(1 for i in items if i.get(field) == 0)
        pct = round(miss / n * 100, 1) if n else 100.0
        status = "FAIL" if pct > NULL_FAIL else ("WARN" if pct > NULL_WARN else "OK")

        # 사이트별로 나눠 본다 — **한 값도 없는 사이트**가 구조 결측이다
        by_site = {}
        for i in items:
            st = by_site.setdefault(i.get("site"), [0, 0])
            st[0] += 1
            st[1] += i.get(field) is not None
        blind = sorted(s for s, (tot, got) in by_site.items() if tot and got == 0)
        seen_items = [i for i in items if i.get("site") not in blind]
        pct_seen = pct
        if blind and seen_items:
            m2 = sum(1 for i in seen_items if i.get(field) is None)
            pct_seen = round(m2 / len(seen_items) * 100, 1)

        out.append({"field": field, "label": label, "missing": miss, "missing_pct": pct,
                    "zeros": zeros, "status": status,
                    # 노출 플랫폼 안에서의 결측률과, 아예 안 주는 플랫폼 목록
                    "missing_pct_exposed": pct_seen, "blind_sites": blind,
                    "n_exposed": len(seen_items),
                    # 전부 결측인 축은 존재하지 않는 것과 같다 — 차트로 그리면 안 된다.
                    # **구조 결측은 빼고 판정한다** — 그 사이트를 안 보면 될 뿐이다
                    # **노출 자체가 성과인 축은 결측률로 죽이지 않는다** (D50 실측 기반).
                    # 무신사는 일정 수준 이상 팔린 상품에만 조회수·누적판매를 노출한다
                    # (D50 실측 — MNAR). 그래서 결측률 41%가 나오는데, 이건 데이터
                    # 품질이 나쁜 게 아니라 **그 자체가 신호**다. 30% 컷으로 버리면
                    # 사용자가 1순위 Y로 지목한 누적판매가 영영 축이 되지 못한다.
                    # 대신 **값이 노출된 상품 안에서만** 비교하고 노출률을 함께 싣는다
                    # (D50이 정한 처리 그대로). 표본 하한(MIN_N)은 그대로 건다.
                    "exposure_signal": field in EXPOSURE_SIGNAL_FIELDS,
                    "usable": ((field in EXPOSURE_SIGNAL_FIELDS
                                or pct_seen <= NULL_FAIL)
                               and (n - miss) >= MIN_N and len(seen_items) >= MIN_N)})
    return out


# ── 3. 분포·이상치 ──────────────────────────────────────────────────────────
def check_distributions(data):
    items = data["items"]
    out = []
    for field, label in num_axes(data):     # 고정 + numeric 프록시
        xs = _vals(items, field)
        if len(xs) < 4:
            continue
        q1, med, q3 = _quartiles(xs)
        iqr = q3 - q1
        lo, hi = q1 - IQR_K * iqr, q3 + IQR_K * iqr
        iqr_out = [x for x in xs if x < lo or x > hi]
        sd = st.pstdev(xs)
        mean = st.fmean(xs)
        z_out = [x for x in xs if sd and abs((x - mean) / sd) > Z_CUT]
        skew = _skew(xs)
        out.append({
            "field": field, "label": label, "n": len(xs),
            "min": min(xs), "q1": q1, "median": med, "q3": q3, "max": max(xs),
            "mean": round(mean, 2), "sd": round(sd, 2),
            "skew": round(skew, 2) if skew is not None else None,
            # 로그는 **오른쪽으로 쏠린 음이 아닌** 값에만 쓴다. 왼쪽 쏠림(평점 5점 만점처럼
            # 천장에 붙은 분포)에 로그를 권하면 틀린 조언이다 — 쏠림이 더 심해진다.
            "log_recommended": bool(skew is not None and skew > SKEW_LOG and min(xs) >= 0),
            # 0이 섞였으면 log(x)가 아니라 log(1+x)를 쓴다. 하트·후기 수는 0이 흔하고
            # (아무도 안 눌렀다는 뜻이다) 그 0을 버리면 인기 없는 상품이 통째로 사라진다.
            "log_needs_1p": bool(xs and min(xs) == 0),
            "iqr_outliers": len(iqr_out), "z_outliers": len(z_out),
            # 이상치는 자동으로 지우지 않는다. 진짜 신호일 수 있다(히트 상품)
            "outlier_note": "이상치는 제거하지 않았다 — 히트 상품일 수 있다",
        })
    return out


# ── 4. 축 카디널리티 ────────────────────────────────────────────────────────
def check_cardinality(data):
    """범주 축마다 값이 몇 개고 각 값에 몇 건인지. n<30 값은 그룹 비교에 쓰지 않는다."""
    items = data["items"]
    out = []
    for field, label in cat_axes(data, CAT_AXES):   # 고정 + 범주 프록시
        counts = Counter(i.get(field) for i in items if i.get(field) is not None)
        if not counts:
            continue
        usable = {k: v for k, v in counts.items() if v >= MIN_N}
        out.append({
            "field": field, "label": label,
            "distinct": len(counts),
            "missing": sum(1 for i in items if i.get(field) is None),
            "top": counts.most_common(12),
            "usable_values": len(usable),
            # 값이 하나뿐인 축으로는 비교가 성립하지 않는다
            "comparable": len(usable) >= 2,
        })
    return out


# ── 5. 상관 + 세그먼트 재확인 (심슨의 역설) ─────────────────────────────────
def check_correlations(data, nulls):
    """전체 상관을 구하고 **반드시 세그먼트로 쪼개 다시 본다.**

    전체에서 보이는 상관이 카테고리·플랫폼별로 나누면 사라지거나 뒤집히는 일이 실제로
    있다(심슨의 역설). 예전에는 사람이 대시보드 필터로 확인했는데, HTML을 폐기했으므로
    (D27) 이 확인은 여기서 자동으로 한다.
    """
    items = data["items"]
    num = num_axes(data)                        # 한 번만 계산 (쌍 루프 안에서 재호출 금지)
    cats = cat_axes(data, CAT_AXES)
    usable = [f for f, _ in num
              if next((x for x in nulls if x["field"] == f), {}).get("usable")]
    labels = dict(num)
    # **같은 재료에서 뽑은 수치 프록시끼리는 정의상 연동이다** (D56).
    # `name_length`(문자 수)와 `name_word_count`(단어 수)는 둘 다 상품명의
    # 크기를 재는 자라, 상관 r=+0.78이 나와도 발견이 아니라 산수다. 실측에서
    # 이 쌍이 강한 주장 한 자리를 차지했다. 재료가 같고 둘 다 numeric이면 뺀다 —
    # 프록시 이름을 박아 두지 않고 **재료로** 판정한다(D19: 하드코딩 금지).
    px_material = {"px_" + p["name"]: p.get("material")
                   for p in data["meta"].get("proxies", []) if p.get("numeric")}
    out = []
    for a, bfield in [(usable[i], usable[j])
                      for i in range(len(usable)) for j in range(i + 1, len(usable))]:
        pairs = [(i[a], i[bfield]) for i in items
                 if i.get(a) is not None and i.get(bfield) is not None]
        if len(pairs) < MIN_N:
            continue
        r = _spearman(pairs)
        if r is None:
            continue
        seg_flips = []
        for seg_field, seg_label in cats:
            groups = defaultdict(list)
            for i in items:
                if i.get(a) is not None and i.get(bfield) is not None and i.get(seg_field) is not None:
                    groups[i[seg_field]].append((i[a], i[bfield]))
            for gval, gpairs in groups.items():
                if len(gpairs) < MIN_N:
                    continue
                gr = _spearman(gpairs)
                if gr is None:
                    continue
                # 부호가 뒤집혔거나 상관이 통째로 사라지면 전체 상관을 믿으면 안 된다
                if (r * gr < 0) or (abs(r) > 0.3 and abs(gr) < SIMPSON_FLIP):
                    seg_flips.append({"segment": "%s=%s" % (seg_label, gval),
                                      "n": len(gpairs), "r": round(gr, 3)})
        definitional = (frozenset((a, bfield)) in DEFINITIONAL_PAIRS
                        or (a in px_material and bfield in px_material
                            and px_material[a] == px_material[bfield]))
        out.append({
            "x": a, "y": bfield, "x_label": labels[a], "y_label": labels[bfield],
            "n": len(pairs), "spearman": round(r, 3),
            "strong": abs(r) >= CORR_STRONG,
            "simpson_flips": seg_flips[:6],
            "trustworthy": not seg_flips,
            # 산수로 이미 정해진 관계 — 발견이 아니다
            "definitional": definitional,
            # 둘 다 누적 지표면 "오래된 상품이 유리한 값"이라 동반 상승한다
            "vanity_pair": a in VANITY_FIELDS and bfield in VANITY_FIELDS,
        })
    out.sort(key=lambda d: -abs(d["spearman"]))
    return out


# ── 6. 시계열 커버리지 ──────────────────────────────────────────────────────
def check_timeseries(data):
    """시계열 분석이 성립하는지. 시점이 없으면 '추이'라는 말 자체를 쓰면 안 된다."""
    ts = data.get("time_series")
    events = data.get("price_events") or []
    if not ts:
        return {"available": False, "reason": "시점 2개 이상 관측된 상품이 없다 (단일 스냅샷)",
                "price_events": len(events)}
    per = [sum(1 for v in s["rank"] if v is not None) or
           sum(1 for v in s["price"] if v is not None) for s in ts["series"]]
    return {
        "available": True,
        "stamps": len(ts["stamps"]),
        "from": ts["stamps"][0], "to": ts["stamps"][-1],
        "series": ts["total_series"], "series_shown": ts["shown"],
        "obs_per_series_median": st.median(per) if per else 0,
        "price_events": len(events),
        # 사건이 없으면 "할인 → 판매" 같은 사건 연구가 성립하지 않는다
        "event_study_possible": len(events) >= 10,
    }


# ── 7. 생존편향 진단 ────────────────────────────────────────────────────────
def check_survivorship(data):
    """**데이터에 없는 것**을 적는다. 지금 팔리는 것만 보고 성공 요인을 읽는 오류를 막는다."""
    items = data["items"]
    ts = data.get("time_series")
    dropped = 0
    if ts and ts["stamps"]:
        last = ts["stamps"][-1]
        for s in ts["series"]:
            idx = ts["stamps"].index(last)
            seen_any = any(v is not None for v in s["rank"][:idx] + s["price"][:idx])
            gone = s["rank"][idx] is None and s["price"][idx] is None
            if seen_any and gone:
                dropped += 1
    return {
        "sold_out_in_data": sum(1 for i in items if i.get("sold_out")),
        "dropped_from_last_snapshot": dropped,
        "notes": [
            "판매 종료된 상품은 수집 시점에 이미 목록에 없다 — 이 데이터에 존재하지 않는다.",
            "랭킹 축적은 순위권(top 100) 밖을 관측하지 못한다 — 권외는 결측이 아니라 미관측이다.",
            "품절 필터 상태에 따라 품절 상품이 통째로 빠졌을 수 있다(수집 notes 확인).",
        ],
    }


# ── 신호 추출 — 인사이트 엔진이 먹는 것 ─────────────────────────────────────
def to_signals(grain, nulls, dists, cards, corrs, ts, surv):
    """EDA 결과에서 **가설을 세울 만한 자리**만 뽑는다.

    신호는 가설이 아니다. "여기를 보면 뭔가 있을 수 있다"는 좌표일 뿐이고,
    실제 검정은 인사이트 엔진이 통계 플레이북으로 한다.
    """
    sig = []
    for c in corrs[:20]:
        if abs(c["spearman"]) < 0.2 or c["definitional"]:
            continue        # 정의상 연동된 쌍은 발견이 아니다 — 가설로 넘기지 않는다
        caveats = []
        if not c["trustworthy"]:
            caveats.append("세그먼트에서 역전/소멸 — %s" %
                           ", ".join(f["segment"] for f in c["simpson_flips"][:3]))
        if c["vanity_pair"]:
            caveats.append("둘 다 누적 지표다 — 출시가 오래된 상품이 유리한 값이라 "
                           "동반 상승한다. 증분으로 다시 볼 것")
        sig.append({
            "kind": "correlation", "x": c["x"], "y": c["y"], "n": c["n"],
            "strength": abs(c["spearman"]), "caveats": caveats,
        })
    for c in cards:
        if c["comparable"]:
            sig.append({"kind": "group_compare", "axis": c["field"],
                        "groups": c["usable_values"], "n": grain["rows"],
                        "strength": 0.5, "caveats": []})
    if ts.get("event_study_possible"):
        sig.append({"kind": "event_study", "events": ts["price_events"],
                    "n": ts["price_events"], "strength": 0.9,
                    "caveats": ["대조군(미할인 유사 상품)을 반드시 세울 것"]})
    elif ts.get("available"):
        sig.append({"kind": "trend", "stamps": ts["stamps"], "n": ts["series"],
                    "strength": 0.4,
                    "caveats": ["가격 변경 사건이 %d건뿐 — 사건 연구는 성립하지 않는다"
                                % ts.get("price_events", 0)]})
    if data_has_variants(dists):
        sig.append({"kind": "stock_depletion", "n": grain["rows"], "strength": 0.6,
                    "caveats": ["옵션 개수 기준 품절률이지 수량 비율이 아니다"]})
    sig.sort(key=lambda s: -s["strength"])
    return sig


def data_has_variants(dists):
    """옵션 재고 신호는 **표본이 충분할 때만** 낸다.

    옵션이 7개 상품에만 붙어 있는데 "사이즈별 재고 분석 가능"이라고 신호를 내보내면
    인사이트 엔진이 n=7짜리 가설을 만든다. 축이 존재하는 것과 쓸 만한 것은 다르다.
    """
    return any(d["field"] in ("opt_out_rate", "opt_total") and d["n"] >= MIN_N
               for d in dists)


def run(db_path, contexts):
    data = collect(db_path, contexts)
    if not data["items"]:
        return {"ok": False, "reason": "해당 조건의 관측이 DB에 없다", "contexts": contexts}
    grain = check_grain(data)
    nulls = check_nulls(data)
    dists = check_distributions(data)
    cards = check_cardinality(data)
    corrs = check_correlations(data, nulls)
    ts = check_timeseries(data)
    surv = check_survivorship(data)
    return {
        "ok": True,
        "grain": grain, "nulls": nulls, "distributions": dists,
        "cardinality": cards, "correlations": corrs,
        "timeseries": ts, "survivorship": surv,
        "signals": to_signals(grain, nulls, dists, cards, corrs, ts, surv),
        # 표본이 작으면 이후 전부에 경고가 붙는다
        "small_sample": grain["rows"] < MIN_N,
    }


def main():
    ap = argparse.ArgumentParser(description="EDA 고정 세트 — 인사이트 엔진의 입력을 만든다")
    ap.add_argument("--db", default="data/intel.db")
    ap.add_argument("--context", action="append", default=[],
                    help='관측 문맥. 예: "brand:로우클래식" (여러 번 가능, 생략하면 전체)')
    ap.add_argument("--out", help="결과 JSON 경로 (생략하면 표준출력)")
    a = ap.parse_args()

    res = run(a.db, a.context)
    text = json.dumps(res, ensure_ascii=False, indent=2)
    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        if res["ok"]:
            print("EDA 완료: %s — 상품 %d건, 신호 %d개%s" % (
                a.out, res["grain"]["rows"], len(res["signals"]),
                " ※ 표본이 작다(n<%d)" % MIN_N if res["small_sample"] else ""))
        else:
            print("EDA 중단: %s" % res["reason"])
    else:
        print(text)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
