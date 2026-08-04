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
import chart                                                # noqa: E402
from pdf_doc import Doc                                     # noqa: E402

# **판정 기준이 아니라 표시 개수다** — 자격은 5관문이 정한다. 축이 적은 문맥은
# 지문 중복 제거 때문에 다 안 채워지는데, **빈자리를 약한 단서로 메우지 않는다**.
STRONG_MAX = 10
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

    계층이 안 맞는 쌍은 D42가 따로 거른다. 여기 지문은 그것과 무관하게 굵게 잡는다 —
    대등한 형제끼리라도 같은 축·같은 지표면 독자에게는 발견 하나다.
    """
    if h["kind"] == "group_compare":
        return ("g", h["cat_field"], h["metric"])
    if h["kind"] == "correlation":
        return ("c", h["x"], h["y"])
    return ("e",)


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


# ── 액션 제안 (D47) ─────────────────────────────────────────────────────────
# 2026-08-04 피드백: "인사이트 이후 **액션 제안**이 필요 — 읽었을 때 리턴이 있어야
# 함". 관측 진술만 실으면 읽는 사람이 "그래서 뭘 하지"를 매번 스스로 번역해야 한다.
#
# **지시하지 않는다.** MD가 "참고용·정성적"이라고 못박았고(2026-08-03 인터뷰) 우리는
# 상관만 봤다. 그래서 "무엇을 해라"가 아니라 **"이 숫자로 무엇을 정할 수 있나"**를 쓴다.
# 문장이 확정적일수록 근거보다 세 보인다 — 그 간극이 이 리포트가 제일 조심할 것이다.

def action_hint(h):
    """이 발견으로 **무엇을 하면 되나**. 여러 줄을 돌려준다 (D51).

    2026-08-04 사용자 요청: "액션플랜 좀 더 쉽고 구체적인 걸로 여러 가지가
    적히면 좋겠다". 전에는 한 줄이었고 문장이 추상적이었다("참고선으로 쓴다").

    쓰는 법 세 가지를 지킨다:
      1. **동사로 시작한다** — "확인한다"·"비교한다"·"정한다". 명사로 끝내지 않는다
      2. **숫자를 문장에 넣는다** — 그 카드의 값을 그대로. 다시 안 찾아봐도 되게
      3. **지시하지 않는다** — 우리는 상관만 봤다. "무엇을 확인할지"까지가 우리 몫이다
    """
    v = lambda x: _fmt(x)
    metric = h.get("metric_label") or h.get("y_label") or "이 지표"
    ga, gb = h.get("group_a"), h.get("group_b")
    ma, mb = h.get("median_a"), h.get("median_b")
    n = "{:,}".format(h.get("n") or 0)

    if h.get("verdict") == "weak":
        return ["**아직 정하지 마라** — %s" % recheck_hint(h),
                "지금 값으로 기획을 바꾸면 다음 관측에서 뒤집힐 수 있다"]

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
            out += ["**%s와 %s를 나란히 놓고** %s 차이%s가 어디서 오는지 본다"
                    % (ga, gb, metric, gap),
                    "낮은 쪽(%s)의 상품 몇 개를 **직접 열어** 상세·썸네일·가격대를 "
                    "높은 쪽과 비교한다" % (gb if (ma or 0) > (mb or 0) else ga),
                    "다음 기획에서 **높은 쪽 조건을 의도적으로 골라** 재확인한다 "
                    "(지금은 관측이지 실험이 아니다)"]
            if h.get("cat_field") == "category":
                out.append("**구성비를 어디에 둘지**의 근거로 쓴다 — 다만 카테고리 값은 "
                           "사이트 표기라 같은 레벨인지 먼저 본다")
            if h.get("cat_field") == "brand":
                out.append("**우리 위치가 이 분포의 어디인지** 표시해 두고 다음 분기에 다시 본다")
    elif kind == "correlation":
        d = h.get("direction")
        if d in ("response_pair", "lever_pair"):
            out += ["**둘 다 %s라 선후를 모른다.**" % (
                        "고객 반응" if d == "response_pair" else "우리가 정한 값"),
                    "한쪽을 올리면 다른 쪽이 따라온다고 읽지 마라",
                    "선후를 보려면 **시점을 나눠** 앞선 변화가 뒤선 변화를 예고하는지 확인한다"]
        else:
            out += ["%s를 움직였을 때 %s가 따라 움직인 관측이다 (n=%s)"
                    % (h.get("x_label"), h.get("y_label"), n),
                    "**폭을 정할 때 참고**하되, 같은 시기에 다른 조건(노출·시즌·경쟁)이 "
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
        return "%s에서 %s와 %s는 %s 차이가 없다" % (
            h.get("cat_label"), h.get("group_a"), h.get("group_b"),
            h.get("metric_label", ""))
    if h.get("kind") == "correlation":
        return "%s와 %s는 함께 움직이지 않는다" % (h.get("x_label"), h.get("y_label"))
    return "이 조건에서는 차이가 나타나지 않았다 — %s" % h.get("claim", "")


def recheck_hint(h):
    """약한 단서마다 "어떻게 재확인하나"를 한 줄로. 없으면 단서가 아니라 잡음이다.

    **관문은 코드로 가른다**(`fail_codes`) — 한글 문구 매칭은 gate()의 문구를 다듬는
    날 이 함수만 조용히 오분류된다(PR #9 리뷰). `null_findings`는 이미 코드로 갔는데
    여기만 남아 있었다. 코드가 없는 옛 항목은 문구로 폴백한다.
    """
    codes = h.get("fail_codes") or []
    fails = h.get("fails") or []
    has = lambda code, word: (code in codes) if codes else any(word in f for f in fails)
    if has("sample", "표본이 작다"):
        return "표본이 쌓이면 다시 본다 — 다음 수집 후 재확인"
    if has("holdout", "홀드아웃"):
        return "다음 관측 주기에 같은 방향이 나오는지 확인"
    if has("segment", "세그먼트"):
        return "해당 세그먼트만 따로 수집해 표본을 키운 뒤 재검정"
    if has("fdr", "다중비교"):
        return "이 가설만 단독으로 검정하면 유의할 수 있다 — 목적을 정하고 재검정"
    return "다음 관측으로 재확인"


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
    out = os.path.join(os.path.dirname(db_path) or ".", ".px-auto.json")
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


def build(db_path, contexts, ai_notes=None, auto_proxy=True):
    """분석 단을 돌리고 그 판정 결과에서 실을 것을 고른다."""
    if auto_proxy:
        ensure_rule_proxies(db_path, contexts)
    res = an.analyze(db_path, contexts, ai_notes)
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
               action=action_hint(h),          # D47 — 읽고 무엇을 정할 수 있나
               evidence="%s %s · n=%s · p=%s · %s" % (
                   h.get("effect_kind", "효과"), _fmt(h.get("effect")),
                   "{:,}".format(h.get("n") or 0),
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
                   action=["**이 축은 신경 쓰지 않아도 된다** — 다른 축에 힘을 쓴다",
                           "여기에 기획·마케팅 자원을 더 넣어도 차이가 안 났다는 뜻이다"],
                   evidence="%s %s (차이 없음 수준) · n=%s" % (
                       h.get("effect_kind", "효과"), _fmt(h.get("effect")),
                       "{:,}".format(h.get("n") or 0)))

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
               action=action_hint(h),
               evidence="%s %s · n=%s · 미통과: %s" % (
                   h.get("effect_kind", "효과"), _fmt(h.get("effect")),
                   "{:,}".format(h.get("n") or 0),
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
        d.para("**재확인 방법**: " + recheck_hint(h), style="small")

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
    ap.add_argument("--db", default="data/intel.db")
    ap.add_argument("--context", action="append", default=[])
    ap.add_argument("--out", default="output", help="PDF를 넣을 디렉터리")
    ap.add_argument("--target", help="리포트 제목에 쓸 대상 이름 (생략하면 문맥에서)")
    ap.add_argument("--ai-notes", help="분석 단에 넘길 AI 예외 판단 JSON (exclude/warn/add)")
    ap.add_argument("--no-auto-proxy", action="store_true",
                    help="rule 프록시 자동 생성을 건너뛴다 (기본은 만든다 — D51)")
    a = ap.parse_args()

    target = a.target or (", ".join(a.context) if a.context else "전체")
    notes = None
    if a.ai_notes:
        import json
        notes = json.loads(open(a.ai_notes, encoding="utf-8").read())
    res, raw = build(a.db, a.context, notes, auto_proxy=not a.no_auto_proxy)
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
