#!/usr/bin/env python3
"""인사이트 엔진 — 가설을 만들고 검정해서 강한 주장 5 + 약한 단서 20을 낸다 (D28).

## 이 도구가 하는 일

EDA가 낸 신호(`eda.py`)를 받아 **가설을 대량으로 만들고**, 통계 플레이북
(`stat_playbook.py`)으로 검정한 뒤, 통과 수준에 따라 셋으로 가른다.

    강한 주장  5개  — 아래 5관문을 **전부** 통과
    약한 단서 20개  — 하나 이상 미통과. 어느 것을 미통과했는지 명시
    기각       기록  — 데이터가 부정한 가설. 같은 막다른 길을 다시 파지 않게 남긴다

5관문 (D28):
  ① 효과 크기 임계 이상    ② n 임계 이상    ③ 다중비교 보정 생존
  ④ 세그먼트 분해에서 역전 없음            ⑤ 홀드아웃 재현

**강한 주장이 5개가 안 나오면 억지로 채우지 않는다.** 빈자리를 약한 것으로 승격시키면
이 구분 자체가 무의미해진다.

## 어조

MD가 "참고용·정성적"이라고 못박았다(2026-08-03 인터뷰). 그래서 강한 주장도 실행 지시가
아니라 **관측 진술**로 쓴다 — "20% 할인하라"가 아니라 "20% 구간에서 증분이 3배였다".

    python3 insight.py --db data/intel.db --context "ranking:스커트" --out output/
"""
import argparse
import json
import os
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stat_playbook as sp                                  # noqa: E402
from eda import CAT_AXES, MIN_N, run as run_eda             # noqa: E402
from intel_data import AXES, collect                        # noqa: E402
from pdf_doc import Doc                                     # noqa: E402

LABELS = dict(AXES)
CAT_LABELS = dict(CAT_AXES)

# ── 관문 임계 ───────────────────────────────────────────────────────────────
EFFECT_MIN = 0.33        # |Cliff's δ| 중간 이상 (Romano 기준). 이보다 작으면 결정을 못 바꾼다
CORR_MIN = 0.30          # |순위 상관| 하한
N_MIN = MIN_N            # 표본 하한 — EDA와 같은 값을 쓴다
ALPHA = 0.05
STRONG_MAX = 5
WEAK_MAX = 20
# 범주축 하나에서 볼 그룹 수. 쌍은 제곱으로 늘어나므로 여기서 막는다
GROUPS_PER_AXIS = 8

# ── 청중 배정 (MD 요구: 판매전략/디자인/마케팅으로 나눠서) ──────────────────
# 가설이 "누가 무엇을 할 수 있는 발견인가"로 갈린다. 축을 보고 정한다.
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
    return "판매전략"


# ── 가설 생성 ───────────────────────────────────────────────────────────────
# "가설은 많을수록 좋다"(피드백 1). 넓게 만들고 검정으로 거른다 — 생성 단계에서
# 품질을 걱정하면 진짜 발견까지 미리 죽인다.
def gen_group_hypotheses(items, nulls):
    """범주축 × 수치축 × 그룹쌍. 여기서 물량이 나온다."""
    usable_metrics = [f for f, _ in AXES
                      if next((x for x in nulls if x["field"] == f), {}).get("usable")]
    out = []
    for cat_field, cat_label in CAT_AXES:
        groups = defaultdict(list)
        for it in items:
            if it.get(cat_field) is not None:
                groups[it[cat_field]].append(it)
        big = [(k, v) for k, v in groups.items() if len(v) >= N_MIN]
        if len(big) < 2:
            continue
        big.sort(key=lambda kv: -len(kv[1]))
        # 그룹이 많으면 쌍이 제곱으로 는다 — 20개면 190쌍이고 지표 12개를 곱하면
        # 2,280개다. 큰 그룹 상위 몇 개만 본다(작은 그룹끼리의 비교는 어차피 표본
        # 관문에서 죽는다). 잘린 그룹 수는 아래 truncated로 리포트에 남는다.
        h_truncated = max(0, len(big) - GROUPS_PER_AXIS)
        big = big[:GROUPS_PER_AXIS]
        for metric in usable_metrics:
            for i in range(len(big)):
                for j in range(i + 1, len(big)):
                    ka, va = big[i]
                    kb, vb = big[j]
                    a = [x[metric] for x in va if x.get(metric) is not None]
                    b = [x[metric] for x in vb if x.get(metric) is not None]
                    if len(a) < N_MIN or len(b) < N_MIN:
                        continue
                    out.append({
                        "kind": "group_compare",
                        "cat_field": cat_field, "cat_label": cat_label,
                        "group_a": str(ka), "group_b": str(kb),
                        "metric": metric, "metric_label": LABELS[metric],
                        "a": a, "b": b, "groups_truncated": h_truncated,
                    })
    return out


def gen_corr_hypotheses(eda_res, items):
    out = []
    for c in eda_res["correlations"]:
        if c["definitional"] or abs(c["spearman"]) < 0.15:
            continue
        pairs = [(i[c["x"]], i[c["y"]]) for i in items
                 if i.get(c["x"]) is not None and i.get(c["y"]) is not None]
        if len(pairs) < N_MIN:
            continue
        out.append({"kind": "correlation", "x": c["x"], "y": c["y"],
                    "x_label": c["x_label"], "y_label": c["y_label"],
                    "pairs": pairs, "eda": c, "vanity": c.get("vanity_pair")})
    return out


def gen_event_hypothesis(data):
    events = data.get("price_events") or []
    if len(events) < 10:
        return []
    return [{"kind": "event_study", "events": events}]


# ── 검정 ────────────────────────────────────────────────────────────────────
def test_group(h):
    a, b = h["a"], h["b"]
    eff = sp.cliffs_delta(a, b)
    p = sp.perm_test_groups(a, b)
    ma, mb = st.median(a), st.median(b)
    h.update({
        "effect": eff, "effect_kind": "Cliff δ", "p": p,
        "n": len(a) + len(b), "n_a": len(a), "n_b": len(b),
        "median_a": ma, "median_b": mb,
        "claim": "%s에서 %s는 %s보다 %s가 %s (중앙값 %s 대 %s)" % (
            h["cat_label"], h["group_a"], h["group_b"], h["metric_label"],
            "높다" if ma > mb else "낮다", _fmt(ma), _fmt(mb)),
        "audience": audience_of(h["metric"], h["cat_field"]),
    })
    return h


def test_corr(h):
    r = sp.spearman(h["pairs"])
    p = sp.perm_test_corr(h["pairs"])
    h.update({
        "effect": r, "effect_kind": "순위 상관", "p": p, "n": len(h["pairs"]),
        "trend": sp.binned_trend(h["pairs"]),
        "claim": "%s가 높을수록 %s가 %s (순위 상관 %+.2f)" % (
            h["x_label"], h["y_label"], "높다" if (r or 0) > 0 else "낮다", r or 0),
        "audience": audience_of(h["y"], h["x"]),
    })
    return h


def test_event(h):
    res = sp.event_study(h["events"])
    if not res:
        h.update({"effect": None, "p": None, "n": len(h["events"]),
                  "claim": "가격 변경 사건이 관측됐으나 대조군을 세울 수 없어 검정하지 못했다",
                  "audience": "판매전략", "failed": True})
        return h
    h.update({
        "effect": res.get("effect"), "effect_kind": "Cliff δ", "p": res.get("p"),
        "n": res["n_events"], "study": res,
        "claim": "가격을 내린 사건(%d건)의 하트 증분 중앙값은 %s로, %s보다 %s" % (
            res["n_cuts"], _fmt(res["cut_like_delta_median"]),
            res.get("control", "대조군"),
            "높다" if (res.get("effect") or 0) > 0 else "낮다"),
        "audience": "판매전략",
    })
    return h


# ── 관문 ────────────────────────────────────────────────────────────────────
def check_segments(h, items):
    """④ 세그먼트 분해에서 역전되지 않는가.

    전체에서 보이던 차이가 카테고리·플랫폼별로 쪼개면 뒤집히는 일이 실제로 있다.
    HTML 대시보드 시절에는 사람이 필터로 확인했는데(D26 폐기), 이제 여기서 자동으로 한다.
    """
    if h["kind"] != "correlation":
        return True, []
    flips = h.get("eda", {}).get("simpson_flips") or []
    return (not flips), ["%s에서 관계가 사라지거나 뒤집힌다 (n=%d, r=%+.2f)"
                         % (f["segment"], f["n"], f["r"]) for f in flips[:3]]


def check_holdout(h, items, data):
    """⑤ 홀드아웃에서 재현되는가.

    시계열이 있으면 **시간 분할**(앞 절반에서 발견 → 뒤 절반에서 재확인)이 맞다. MD가
    쓸 방식이 "다음 주 관측으로 확인"이기 때문이다. 단일 스냅샷이면 시간 분할이 불가능해
    상품을 무작위로 반씩 갈라 **안정성**만 본다 — 재현과 다른 것이므로 라벨을 달리 쓴다.
    """
    if h["kind"] == "group_compare":
        a, b = h["a"], h["b"]
        if len(a) < N_MIN * 2 or len(b) < N_MIN * 2:
            return None, "표본이 작아 홀드아웃을 나눌 수 없다"
        ha, hb = a[len(a) // 2:], b[len(b) // 2:]
        eff = sp.cliffs_delta(ha, hb)
        if eff is None:
            return None, "홀드아웃 효과를 계산하지 못했다"
        same_dir = (eff or 0) * (h["effect"] or 0) > 0
        return (same_dir and abs(eff) >= EFFECT_MIN * 0.6), \
               "홀드아웃 δ=%+.2f (원 δ=%+.2f)" % (eff, h["effect"] or 0)
    if h["kind"] == "correlation":
        pairs = h["pairs"]
        if len(pairs) < N_MIN * 2:
            return None, "표본이 작아 홀드아웃을 나눌 수 없다"
        half = pairs[len(pairs) // 2:]
        r = sp.spearman(half)
        if r is None:
            return None, "홀드아웃 상관을 계산하지 못했다"
        same_dir = r * (h["effect"] or 0) > 0
        return (same_dir and abs(r) >= CORR_MIN * 0.6), \
               "홀드아웃 r=%+.2f (원 r=%+.2f)" % (r, h["effect"] or 0)
    return None, "이 유형은 홀드아웃 분할이 성립하지 않는다"


def gate(h, items, data, fdr_survive):
    """5관문. 통과 못 한 관문을 전부 기록한다 — 약한 단서에 이유를 적어야 하기 때문이다."""
    fails = []
    eff = abs(h.get("effect") or 0)
    threshold = CORR_MIN if h["kind"] == "correlation" else EFFECT_MIN
    if h.get("effect") is None:
        fails.append("효과 크기를 계산하지 못했다")
    elif eff < threshold:
        fails.append("효과 크기가 작다 (%.2f < %.2f) — 결정을 바꿀 만한 차이가 아니다"
                     % (eff, threshold))
    if (h.get("n") or 0) < N_MIN:
        fails.append("표본이 작다 (n=%d < %d)" % (h.get("n") or 0, N_MIN))
    if not fdr_survive:
        fails.append("다중비교 보정(BH, α=%.2f)을 통과하지 못했다 — 여러 조합을 훑으면 "
                     "우연한 유의가 반드시 나온다" % ALPHA)
    seg_ok, seg_notes = check_segments(h, items)
    if not seg_ok:
        fails.extend(seg_notes)
    hold_ok, hold_note = check_holdout(h, items, data)
    h["holdout_note"] = hold_note
    if hold_ok is False:
        fails.append("홀드아웃에서 재현되지 않는다 — %s" % hold_note)
    elif hold_ok is None:
        fails.append("홀드아웃 확인 불가 — %s" % hold_note)
    # 허영 지표 쌍은 강한 주장이 될 수 없다. 하트·후기 수·누적판매는 **둘 다 시간에 따라
    # 쌓이는 값**이라 오래 노출된 상품에서 동반 상승한다 — 상관이 높은 게 당연하고,
    # "하트가 높으면 후기도 많다"는 발견이 아니라 노출 기간의 그림자다(스펙 §4-9).
    if h.get("vanity"):
        fails.append("둘 다 누적 지표다 — 출시가 오래된 상품일수록 함께 커진다. "
                     "관측 간 증분으로 다시 보기 전에는 발견으로 볼 수 없다")
    h["fails"] = fails
    h["verdict"] = "strong" if not fails else "weak"
    # 방향이 반대로 재현되면 기각이다. 약한 단서로도 남기지 않는다
    if hold_ok is False and h.get("effect") is not None:
        h["verdict"] = "rejected"
    return h


# ── 선별 ────────────────────────────────────────────────────────────────────
# MD의 결정은 리오더이고, 1순위 질문은 "할인 얼마 줬을 때 얼마나 팔리나"다
# (2026-08-03 인터뷰). 효과 크기만으로 줄을 세우면 이 질문과 무관한 가설이 앞자리를
# 차지한다 — 통계적으로 센 것과 **결정에 쓸모 있는 것**은 다르다.
DECISION_FIELDS = {"discount_rate": 3.0, "price_sale": 2.0, "price_original": 1.5,
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

    ※ 실제로 이 데이터에는 `하의`(상위 카테고리)와 `미니`(하위)가 같은 축에 섞여 있어
    비교 자체가 성립하지 않는 쌍이 만들어진다. 계층 정보가 없어 코드로는 못 가리므로,
    지문을 굵게 잡아 **한 축에서 하나만** 내보내는 것으로 피해를 줄인다.
    """
    if h["kind"] == "group_compare":
        return ("g", h["cat_field"], h["metric"])
    if h["kind"] == "correlation":
        return ("c", h["x"], h["y"])
    return ("e",)


# 한 청중이 요약을 독점하지 못하게 한다. MD가 원한 건 판매전략·디자인·마케팅으로
# 나뉘어 나오는 것이지(2026-08-03 인터뷰), 마케팅 발견 5개가 아니다.
AUDIENCE_CAP = 2


def _select(hyps, cap):
    """관련성 × 효과 크기로 정렬하고, 같은 지문·같은 청중 편중을 걷어낸다."""
    hyps.sort(key=lambda h: -(_relevance(h) * abs(h.get("effect") or 0)))
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
    # 빈칸을 만들라는 규칙이 아니다
    for h in hyps:
        if len(out) >= cap:
            break
        if h in out or _signature(h) in seen:
            continue
        seen.add(_signature(h))
        out.append(h)
    return out


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".") if abs(v) < 100 else "{:,.0f}".format(v)
    return "{:,}".format(v)


def recheck_hint(h):
    """약한 단서마다 "어떻게 재확인하나"를 한 줄로. 없으면 단서가 아니라 잡음이다."""
    if any("표본이 작다" in f for f in h["fails"]):
        return "표본이 쌓이면 다시 본다 — 다음 수집 후 재확인"
    if any("홀드아웃" in f for f in h["fails"]):
        return "다음 관측 주기에 같은 방향이 나오는지 확인"
    if any("세그먼트" in f for f in h["fails"]):
        return "해당 세그먼트만 따로 수집해 표본을 키운 뒤 재검정"
    if any("다중비교" in f for f in h["fails"]):
        return "이 가설만 단독으로 검정하면 유의할 수 있다 — 목적을 정하고 재검정"
    return "다음 관측으로 재확인"


# ── 실행 ────────────────────────────────────────────────────────────────────
def analyze(db_path, contexts):
    eda_res = run_eda(db_path, contexts)
    if not eda_res["ok"]:
        return None, eda_res
    data = collect(db_path, contexts)
    items = data["items"]

    hyps = []
    hyps += [test_group(h) for h in gen_group_hypotheses(items, eda_res["nulls"])]
    hyps += [test_corr(h) for h in gen_corr_hypotheses(eda_res, items)]
    hyps += [test_event(h) for h in gen_event_hypothesis(data)]

    # ③ 다중비교 보정 — 전체 가설 집합에 한 번에 건다. 개별로 걸면 의미가 없다
    survive = sp.benjamini_hochberg([h.get("p") for h in hyps], alpha=ALPHA)
    for h, s in zip(hyps, survive):
        gate(h, items, data, s)

    strong = _select([h for h in hyps if h["verdict"] == "strong"], STRONG_MAX)
    weak = _select([h for h in hyps if h["verdict"] == "weak"], WEAK_MAX)
    rejected = [h for h in hyps if h["verdict"] == "rejected"]
    return {
        "generated": len(hyps),
        # 5개가 안 나오면 억지로 채우지 않는다 (D28)
        "strong": strong[:STRONG_MAX],
        "weak": weak[:WEAK_MAX],
        "rejected": rejected,
        "eda": eda_res, "data": data,
    }, eda_res


# ── PDF ─────────────────────────────────────────────────────────────────────
def honesty_points(res):
    eda_res = res["eda"]
    g = eda_res["grain"]
    pts = [
        "이 리포트는 **관측된 상관**을 보여준다. 인과가 아니다 — 왜 그런지는 데이터가 답하지 않는다.",
        "가설을 많이 만들수록 우연한 패턴이 반드시 나온다. 다중비교 보정(BH)을 걸었지만 "
        "강한 주장도 확증이 아니라 **다음 관측으로 재확인할 대상**이다.",
    ]
    pts += eda_res["survivorship"]["notes"]
    if eda_res["small_sample"]:
        pts.append("표본이 작다(n=%d) — 이 리포트의 모든 수치에 그 한계가 붙는다." % g["rows"])
    pts.append("관측 창: %s ~ %s · 상품 %s건 · 플랫폼 %s" % (
        g["observed_from"], g["observed_to"], "{:,}".format(g["rows"]),
        ", ".join(g["sites"])))
    return pts


def build_insight_pdf(res, out_path, target, detail_pages):
    g = res["eda"]["grain"]
    d = Doc("인사이트 — %s" % target,
            subtitle="%s 생성 · 관측 %s ~ %s · 가설 %d개 검정" % (
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                g["observed_from"], g["observed_to"], res["generated"]))
    d.honesty(honesty_points(res))

    d.h2("강한 주장 (%d개)" % len(res["strong"]))
    if not res["strong"]:
        d.para("5관문을 모두 통과한 가설이 없다. **빈자리를 약한 단서로 채우지 않았다** — "
               "그렇게 하면 이 구분이 무의미해진다. 아래 약한 단서를 재확인 대상으로 본다.")
    for i, h in enumerate(res["strong"], 1):
        page = detail_pages.get(_anchor(h, "s", i))
        d.card(h["claim"], audience=h["audience"],
               evidence="%s %s · n=%s · p=%s · %s" % (
                   h.get("effect_kind", "효과"), _fmt(h.get("effect")),
                   "{:,}".format(h.get("n") or 0),
                   ("%.3f" % h["p"]) if h.get("p") is not None else "—",
                   h.get("holdout_note", "")),
               detail_link=("상세 %d쪽" % page) if page else None)

    d.h2("약한 단서 (%d개)" % len(res["weak"]))
    d.para("아래는 **가설이지 결론이 아니다.** 각 항목에 어느 관문을 통과하지 못했는지와 "
           "어떻게 재확인하는지를 적었다.")
    for i, h in enumerate(res["weak"], 1):
        page = detail_pages.get(_anchor(h, "w", i))
        d.card(h["claim"], audience=h["audience"], weak=True,
               evidence="%s %s · n=%s · 미통과: %s · 재확인: %s" % (
                   h.get("effect_kind", "효과"), _fmt(h.get("effect")),
                   "{:,}".format(h.get("n") or 0),
                   h["fails"][0] if h["fails"] else "—", recheck_hint(h)),
               detail_link=("상세 %d쪽" % page) if page else None)

    if res["rejected"]:
        d.h2("기각된 가설 (%d개)" % len(res["rejected"]))
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

    d.h2("데이터 프로파일 (EDA)")
    d.h3("결측")
    d.para("미노출(값이 없음)과 0은 다르다. 아래 결측률은 **사이트가 보여주지 않은** 비율이다.")
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
    "category": "**카테고리 값은 사이트 표기 그대로다.** 상위 분류와 하위 분류가 같은 축에 "
                "섞여 있을 수 있고, 그런 쌍은 대등한 비교가 아니다 — 위 두 값이 같은 "
                "레벨인지 확인하고 읽어라.",
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
        d.para("**재확인 방법**: " + recheck_hint(h), style="small")

    if h["kind"] == "correlation" and h.get("trend"):
        d.h3("구간별 추이")
        d.para("상관계수는 방향만 말한다. **어느 구간인지**는 아래를 본다.")
        d.table(["%s 구간" % h["x_label"], "n", "%s 중앙값" % h["y_label"]],
                [["%s ~ %s" % (_fmt(t["from"]), _fmt(t["to"])), "{:,}".format(t["n"]),
                  _fmt(t["median"])] for t in h["trend"]],
                widths=[48, 20, 32], align_right=(1, 2))

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


def main():
    ap = argparse.ArgumentParser(description="인사이트 엔진 — 강한 주장 5 + 약한 단서 20")
    ap.add_argument("--db", default="data/intel.db")
    ap.add_argument("--context", action="append", default=[])
    ap.add_argument("--out", default="output", help="PDF를 넣을 디렉터리")
    ap.add_argument("--target", help="리포트 제목에 쓸 대상 이름 (생략하면 문맥에서)")
    a = ap.parse_args()

    target = a.target or (", ".join(a.context) if a.context else "전체")
    res, eda_res = analyze(a.db, a.context)
    if res is None:
        print("중단: %s" % eda_res.get("reason"))
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    safe = target.replace("/", "-").replace(":", "-")
    detail_path = os.path.join(a.out, "detail-%s-%s.pdf" % (safe, stamp))
    insight_path = os.path.join(a.out, "insight-%s-%s.pdf" % (safe, stamp))

    # 상세를 먼저 굽는다 — 인사이트가 그 쪽 번호를 가리켜야 하기 때문이다
    pages = build_detail_pdf(res, detail_path, target)
    build_insight_pdf(res, insight_path, target, pages)

    print("가설 %d개 검정 → 강한 주장 %d · 약한 단서 %d · 기각 %d" % (
        res["generated"], len(res["strong"]), len(res["weak"]), len(res["rejected"])))
    print("  인사이트: %s" % insight_path)
    print("  상세:     %s" % detail_path)
    if len(res["strong"]) < STRONG_MAX:
        print("  ※ 강한 주장이 %d개다 — 5관문을 통과한 것만 실었고 빈자리를 채우지 않았다."
              % len(res["strong"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
