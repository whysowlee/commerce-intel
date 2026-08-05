#!/usr/bin/env python3
"""브랜드 생존·이탈 — 두 기간의 랭킹을 대조해 **실패자를 찾는다** (D54).

    python3 musinsa_brand_survival.py --category 003002 --gf F \
        --out data/raw/musinsa-brand-survival-여성데님.json

## 왜 필요한가

랭킹만 보면 **생존자만 본다.** "상위 30 중 와이드핏이 20개"에서 "와이드가 대세"를
읽으면 틀린다 — 전체 800개 중 20개면 진입률 2.5%이고, 공급이 적은 스트레이트가
7/200 = 3.5%로 더 높을 수 있다. 그 결론은 비생존자를 봐야 나온다.

전수조사가 있으면 제일 좋지만, **없어도 시간이 비생존자를 만들어 준다** —
기간이 다른 두 랭킹을 대조하면 밀려난 브랜드가 보인다.

## 사용자 정의 (2026-08-04)

    생존자  = 여성 데님 팬츠 브랜드 랭킹 **1주** 상위 30
    이탈자  = **1개월** 상위 30에는 있었는데 **1주** 상위 30에는 없는 브랜드

실측(2026-08-04): 이탈 6개(론론·플리즈노팔로우·달렌·캘빈클라인 진·이투둘·이지노이지)
· 신규 진입 6개. **이탈은 "망한 브랜드"가 아니라 "밀려난 브랜드"다** — 한때
성공이었다가 뒤로 간 것이라 성공→실패 전환에서 무엇이 바뀌었는지 볼 수 있다.

## 한계 — 이것만으로 다 못 본다

이 도구가 잡는 것은 **이탈**뿐이다. 문서(docs/survival-bias-removal.md)가 나눈 4단계 중:

    이탈    랭킹에 있다가 빠짐          ← 이 도구
    미진입  자사 상품 중 랭킹에 못 든 것  ← 브랜드 라인시트 + 랭킹 대조
    침체    랭킹 안이나 순위 하락 중      ← 랭킹 시계열 축적(크론)
    소멸    전 사이즈 품절 후 사라짐      ← variant_observations
    무반응  카테고리에 있으나 반응 없음    ← **전수조사가 있어야 한다**

**두 기간은 겹치는 창이다** — 1개월 안에 1주가 들어 있다. 그래서 이탈은
"최근 1주에 약해졌다"이지 "한 달 내내 나빴다"가 아니다.
"""
import argparse
import collections
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# 브랜드 표기 정규화는 intel_data.brand_key가 정본이다(D51) — 여기 복사하면
# 규칙이 두 벌이 되고, 어긋나는 날 매칭이 조용히 갈린다.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "skills", "commerce-intel", "scripts"))
from intel_data import brand_key  # noqa: E402

UA = "Claude-User/1.0 (+https://anthropic.com/claude-user)"
RANK = "https://client.musinsa.com/api/home/web/v5/pans/ranking/sections/1054"
TOP_N = 30


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            sys.exit("HTTP %d — 사이트가 막았다. 멈춘다." % e.code)
        return None
    except Exception:
        return None


def brand_top(category, gf, period, n=TOP_N):
    """브랜드 랭킹 상위 n. 순위는 `title.rank`, slug는 `onClick.url`에서 뽑는다."""
    q = urllib.parse.urlencode({
        "storeCode": "musinsa", "gf": gf, "ageBand": "AGE_BAND_ALL",
        "categoryCode": category, "page": "1",
        "period": period, "eventPeriod": "BASIC_" + period})
    d = _get(RANK + "?" + q)
    if not d:
        return []
    seen = {}

    def walk(o):
        if isinstance(o, dict):
            if o.get("type") == "RANKING_BRAND":
                t = o.get("title") or {}
                try:
                    rank = int(t.get("rank") or 0)
                except (TypeError, ValueError):
                    rank = 0
                m = re.search(r"/brand/([^/?#]+)",
                              ((t.get("onClick") or {}).get("url") or ""))
                if m and rank and rank not in seen:
                    seen[rank] = {"rank": rank, "slug": m.group(1),
                                  # 오타가 아니다 — 실제 응답이 title 안에 title이
                                  # 또 있다(섹션 title 객체 안에 텍스트 title 객체)
                                  "name": (t.get("title") or {}).get("text")}
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(d)
    return [seen[k] for k in sorted(seen)][:n]


def to_attrs(items, products_json, attr_name="brand_survival"):
    """브랜드 생존 상태를 **상품 속성으로 편다** — 분석이 축으로 쓸 수 있게.

    생존은 브랜드 단위 사실인데 우리 분석은 상품 단위로 돈다. `product_attributes`
    (D35)가 스키마 변경 없이 축을 늘리는 통로라 그쪽에 얹는다.

    **이탈 브랜드의 상품은 카테고리 전수에서 찾는다** — 이탈했으니 경쟁셋(생존
    브랜드로 뽑은 목록)에는 없다. 전수가 없으면 이탈 쪽 표본이 통째로 비어
    "생존자만 보는" 문제가 그대로 남는다.
    """
    # **양쪽 다 brand_key로 정규화한다** (PR #12 리뷰 6). 랭킹의 브랜드명과 상품
    # 목록의 브랜드명은 출처가 달라 표기가 갈릴 수 있고(D51 실측: 2000아카이브스/
    # 2000Archives/2000 Archives), 원문 대조는 그 브랜드 상품을 **조용히 전부
    # 떨어뜨린다** — D54가 막으려는 생존 편향과 같은 방식의 매칭 편향이다.
    state = {}
    for b in items:
        if b.get("name"):
            state[brand_key(b["name"])] = b["survival"]
    out = []
    for x in products_json:
        st = state.get(brand_key(x.get("brand")))
        if not st:
            continue
        out.append({"site": "musinsa", "product_id": str(x["product_id"]),
                    "attr_name": attr_name, "value": st,
                    "basis": "brand-ranking-diff",
                    # 랭킹은 계속 바뀐다 — 오래 들고 있으면 옛 상태로 분석하게 된다
                    "ttl_days": 7})
    return out


def main():
    ap = argparse.ArgumentParser(description="브랜드 생존·이탈 (D54)")
    ap.add_argument("--category", default="003002", help="카테고리 코드 (기본 여성 데님)")
    ap.add_argument("--gf", default="F", choices=["A", "M", "F"])
    ap.add_argument("--survivor-period", default="WEEKLY",
                    help="생존자를 정의하는 기간 (기본 1주)")
    ap.add_argument("--baseline-period", default="MONTHLY",
                    help="비교 기준 기간 (기본 1개월)")
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--out", required=True)
    ap.add_argument("--attrs-from",
                    help="이 상품 목록(raw JSON)에 생존 상태를 붙여 set-attrs 입력을 만든다. "
                         "**카테고리 전수를 줘라** — 경쟁셋만 주면 이탈 브랜드가 안 잡힌다")
    ap.add_argument("--attrs-out", help="set-attrs 입력 JSON 경로")
    a = ap.parse_args()

    sur = brand_top(a.category, a.gf, a.survivor_period, a.top)
    base = brand_top(a.category, a.gf, a.baseline_period, a.top)
    if not sur or not base:
        sys.exit("랭킹을 못 받았다 — 파라미터나 네트워크를 확인해라.")

    ss = {b["slug"] for b in sur}
    bs = {b["slug"] for b in base}
    dropped = [b for b in base if b["slug"] not in ss]     # 이탈 = 실패자
    entered = [b for b in sur if b["slug"] not in bs]      # 신규 진입
    stayed = [b for b in sur if b["slug"] in bs]

    # 남은 브랜드의 **순위 변동** — "살아는 있지만 죽어가는" 후보를 가른다
    base_rank = {b["slug"]: b["rank"] for b in base}
    for b in stayed:
        b["rank_baseline"] = base_rank.get(b["slug"])
        b["rank_delta"] = (b["rank_baseline"] - b["rank"]) if b["rank_baseline"] else None

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    items = []
    for b in sur:
        items.append(dict(b, survival="survivor"))
    for b in dropped:
        # 이탈자는 **1주 순위가 없다** — rank를 지어내지 않는다
        items.append({"slug": b["slug"], "name": b["name"], "rank": None,
                      "rank_baseline": b["rank"], "rank_delta": None,
                      "survival": "dropped"})
    payload = {
        "meta": {"site": "musinsa", "story": "brand-survival",
                 "target": "%s(gf=%s)" % (a.category, a.gf),
                 "collected_at": now, "source": "api",
                 "survivor_period": a.survivor_period,
                 "baseline_period": a.baseline_period, "top_n": a.top,
                 "notes": "생존자=%s 상위%d · 이탈자=%s 상위%d에는 있었으나 생존자에 없음. "
                          "두 기간은 **겹치는 창**이라 이탈은 '최근에 약해졌다'이지 "
                          "'내내 나빴다'가 아니다"
                          % (a.survivor_period, a.top, a.baseline_period, a.top)},
        "items": items,
    }
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)

    print("생존자 %d · 이탈자 %d · 신규 진입 %d" % (len(sur), len(dropped), len(entered)))
    if dropped:
        print("  이탈:", ", ".join("%s(%d위)" % (b["name"], b["rank"]) for b in dropped))
    falling = sorted([b for b in stayed if (b.get("rank_delta") or 0) < 0],
                     key=lambda b: b["rank_delta"])[:5]
    if falling:
        print("  하락 중(생존자이지만 순위가 밀렸다):",
              ", ".join("%s %d→%d위" % (b["name"], b["rank_baseline"], b["rank"])
                        for b in falling))
    print("저장: %s" % a.out)

    if a.attrs_from and a.attrs_out:
        with open(a.attrs_from, encoding="utf-8") as fh:
            src = json.load(fh)
        rows = to_attrs(items, src.get("items") or [])
        os.makedirs(os.path.dirname(a.attrs_out) or ".", exist_ok=True)
        with open(a.attrs_out, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        c = collections.Counter(r["value"] for r in rows)
        print("속성 %s건 — %s" % ("{:,}".format(len(rows)), dict(c)))
        print("  → intel_db.py set-attrs %s" % a.attrs_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
