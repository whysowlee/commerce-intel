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
from pdf_doc import Doc                                     # noqa: E402

STRONG_MAX = 5
WEAK_MAX = 20

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
def build(db_path, contexts, ai_notes=None):
    """분석 단을 돌리고 그 판정 결과에서 실을 것을 고른다."""
    res = an.analyze(db_path, contexts, ai_notes)
    if not res["ok"]:
        return None, res
    hyps = res["hypotheses"]
    strong = _select([h for h in hyps if h["verdict"] == "strong"], STRONG_MAX)
    weak = _select([h for h in hyps if h["verdict"] == "weak"], WEAK_MAX)
    rejected = [h for h in hyps if h["verdict"] == "rejected"]
    return {"generated": res["generated"], "plan": res["plan"],
            "strong": strong, "weak": weak, "rejected": rejected,
            "eda": res["eda"], "data": res["data"],
            "strong_pool": sum(1 for h in hyps if h["verdict"] == "strong"),
            "verified": sum(1 for h in strong if not h.get("holdout_unverified"))}, res

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
    ap.add_argument("--db", default="data/intel.db")
    ap.add_argument("--context", action="append", default=[])
    ap.add_argument("--out", default="output", help="PDF를 넣을 디렉터리")
    ap.add_argument("--target", help="리포트 제목에 쓸 대상 이름 (생략하면 문맥에서)")
    ap.add_argument("--ai-notes", help="분석 단에 넘길 AI 예외 판단 JSON (exclude/warn/add)")
    a = ap.parse_args()

    target = a.target or (", ".join(a.context) if a.context else "전체")
    notes = None
    if a.ai_notes:
        import json
        notes = json.loads(open(a.ai_notes, encoding="utf-8").read())
    res, raw = build(a.db, a.context, notes)
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
