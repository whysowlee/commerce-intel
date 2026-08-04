#!/usr/bin/env python3
"""무신사 수집 — **robots가 허용한 호스트만** 쓴다 (D48).

    python3 musinsa_collect.py plp --category 003002 --gf F --out data/raw/여성데님.json
    python3 musinsa_collect.py plp --brand 2000archives --out data/raw/2000archives.json
    python3 musinsa_collect.py likes --from-db "brand:2000아카이브스"

## 어느 호스트를 쓰나

2026-08-04 실측으로 **robots가 호스트마다 다르다**는 것이 드러났다. `www`만 보고
API 호스트를 쓰면 안 된다.

    api.musinsa.com      robots 없음 → PLP (정가·할인·후기·평점·isAd)   ○
    like.musinsa.com     robots 없음 → 하트 벌크                        ○
    client.musinsa.com   robots 없음 → 랭킹 (연령·기간·보는 중)          ○
    goods.musinsa.com    robots 없음 → 리뷰                             ○
    goods-detail...      Disallow: / → 조회수·누적판매 원시 정수         ✗ 안 쓴다

**조회수·누적판매는 원시 정수를 못 얻는다.** 허용된 `www` 상품 페이지에 구간 표기
("1.2만 회 이상")로 표시되므로 브라우저 경로로 그것만 받고, 분석은 순서형 축으로
쓴다(D48 — 하한은 정확하므로 순위는 오차가 없다).

## 페이지 넘김 함정

**page≥2 URL을 직접 조립하면 403이다**(어댑터 §2 함정 — 실제로 오늘 다시 밟았다).
응답의 `pagination.nextPageUrl`을 **그대로 따라간다**. 빈 결과가 나오면 끝낸다 —
`hasNext`를 믿지 않는다.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

UA = "Claude-User/1.0 (+https://anthropic.com/claude-user)"
PLP = "https://api.musinsa.com/api2/dp/v2/plp/goods"
LIKES = "https://like.musinsa.com/like/api/v2/liketypes/goods/counts"
REVIEW = ("https://goods.musinsa.com/api2/review/v1/view/list"
          "?goodsNo=%s&page=%d&pageSize=20&sort=up_cnt_desc"
          "&myFilter=false&hasPhoto=false&isExperience=false")
REVIEW_PAGE_MAX = 20      # pageSize 상한이 20이다(실측 400: "pageSize는 20 이하")
LIKE_BATCH = 100
SLEEP = 0.4
# 무한 루프 방지 상한. 어댑터 §2 실측: **데이터가 소진된 뒤에도 nextPageUrl이 계속 온다.**
# 다만 고정 200은 큰 카테고리를 조용히 자른다 — 여성 데님 팬츠는 246페이지인데
# 200에서 끊겨 81.5%만 받고도 "수집 완료"로 보였다(2026-08-04). **사이트가 주는
# totalPages에 여유를 붙여 상한을 잡고, 총계 대비 커버리지를 반드시 보고한다.**
MAX_PAGES = 200          # totalPages를 못 읽었을 때만 쓰는 폴백
PAGE_SLACK = 20          # totalPages + 이만큼까지 (마지막 페이지 경계 여유)


def _req(url, data=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, body, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            sys.exit("HTTP %d — 사이트가 막았다. 멈춘다(우회하지 않는다)." % e.code)
        return e.code, None
    except Exception as e:
        return None, {"_error": str(e)}


def plp(params, max_pages=None):
    """PLP 전수. **nextPageUrl을 따라간다** — page를 직접 올리면 403이다.

    돌려주는 것: (항목, 페이지 수, 사이트 총계). **총계를 함께 돌려주는 이유**는
    "몇 건 받았다"만으로는 다 받았는지 알 수 없어서다 — 상한에 걸려 잘려도
    똑같이 "수집 완료"로 보인다(2026-08-04에 실제로 81.5%만 받았다).
    """
    url = PLP + "?" + "&".join("%s=%s" % kv for kv in params.items())
    out, seen, pages, total, cap = [], set(), 0, None, max_pages
    while url and (cap is None or pages < cap):
        code, d = _req(url)
        if code != 200 or not d:
            print("  [%s] 중단 (page %d)" % (code, pages + 1), file=sys.stderr)
            break
        data = d.get("data") or {}
        pg = data.get("pagination") or {}
        if total is None:
            total = pg.get("totalCount")
            if cap is None:      # 첫 응답의 totalPages로 상한을 잡는다
                cap = (pg.get("totalPages") or MAX_PAGES) + PAGE_SLACK
        lst = data.get("list") or []
        if not lst:
            break                     # 빈 결과 = 끝. hasNext를 믿지 않는다
        for x in lst:
            g = str(x.get("goodsNo") or "")
            if g and g not in seen:
                seen.add(g)
                out.append(_plp_item(x))
        pages += 1
        url = pg.get("nextPageUrl")
        time.sleep(SLEEP)
    return out, pages, total


def _plp_item(x):
    """PLP 카드 → 데이터 계약. **없는 값은 null로 둔다** (0으로 채우지 않는다)."""
    score = x.get("reviewScore")
    return {
        "product_id": str(x.get("goodsNo")),
        "name": x.get("goodsName"),
        "url": x.get("goodsLinkUrl"),
        "image_url": x.get("thumbnail"),
        "brand": x.get("brandName"),
        "price_original": x.get("normalPrice"),
        "price_sale": x.get("finalPrice"),
        "discount_rate": x.get("finalDiscount"),
        "review_count": x.get("reviewCount"),
        # reviewScore는 100점 척도다(94 = 4.7점) — 20으로 나눈다. 어댑터 §12 실측
        "rating": round(score / 20.0, 2) if isinstance(score, (int, float)) else None,
        "sold_out": bool(x.get("isSoldOut")),
        "raw_extras": {
            # **광고 슬롯은 반드시 남긴다** — 노출을 돈으로 산 트래픽이라
            # 오가닉 분석에서 분리해야 한다(인벤토리 PART B-1)
            "is_ad": x.get("isAd"),
            "gender_text": x.get("displayGenderText"),
            "is_lowest_price": x.get("isLowestPrice"),
            "coupon_price": x.get("couponPrice"),
            "has_option_price": x.get("hasOptionPrice"),
        },
    }


def likes(goods_nos):
    """하트 벌크. 실패한 배치는 **0으로 채우지 않고 빠뜨린다**."""
    out = {}
    for i in range(0, len(goods_nos), LIKE_BATCH):
        chunk = [int(g) for g in goods_nos[i:i + LIKE_BATCH]]
        code, d = _req(LIKES, {"relationIds": chunk})
        if code != 200 or not d:
            print("  하트 배치 실패 [%s] — %d건 null" % (code, len(chunk)), file=sys.stderr)
            continue
        for it in (((d.get("data") or {}).get("contents") or {}).get("items") or []):
            out[str(it.get("relationId"))] = it.get("count")
        time.sleep(SLEEP)
    return out


def reviews(goods_no, max_pages=REVIEW_PAGE_MAX):
    """상품 하나의 리뷰를 훑어 **만족도 분포와 구매 사이즈를 집계**한다.

    화면의 "조금 커요 39%"는 이 응답들의 집계인데 **분포 API가 없다**(2026-08-04:
    summary·statistics·aggregate 전부 404/400). 그래서 우리가 센다.

    **표본이지 전수가 아니다.** 후기가 353건이면 20페이지 × 20 = 400까지만 훑고,
    실제로 몇 건을 봤는지(`sampled`)를 총계(`total`)와 함께 남긴다 — 비율만 적으면
    20건에서 나온 39%와 300건에서 나온 39%가 구분되지 않는다.
    """
    from collections import Counter
    survey, sizes, grades = {}, Counter(), Counter()
    created, first_date, sampled, total = None, None, 0, None
    for page in range(max_pages):
        code, d = _req(REVIEW % (goods_no, page))
        if code != 200 or not d or not d.get("data"):
            break
        data = d["data"]
        total = data.get("total") if total is None else total
        lst = data.get("list") or []
        if not lst:
            break
        for r in lst:
            sampled += 1
            for q in ((r.get("reviewSurveySatisfaction") or {}).get("questions") or []):
                bucket = survey.setdefault(q.get("attribute"), Counter())
                for a in (q.get("answers") or []):
                    bucket[a.get("answerShortText")] += 1
            if r.get("goodsOption"):
                sizes[r["goodsOption"]] += 1
            if r.get("grade") is not None:
                grades[r["grade"]] += 1
            created = created or ((r.get("goods") or {}).get("goodsCreateDate"))
            cd = r.get("createDate")
            if cd and (first_date is None or cd < first_date):
                first_date = cd
        if len(lst) < 20:
            break
        time.sleep(SLEEP)
    return {"review_total": total, "review_sampled": sampled,
            # **비율이 아니라 건수로 남긴다** — 분모를 잃지 않는다
            "satisfaction": {k: dict(v) for k, v in survey.items()},
            "size_bought": dict(sizes), "grade_dist": dict(grades),
            "goods_create_date": created,      # 상품 등록일 → 나이
            "earliest_review_at": first_date}


def save(path, meta, items):
    payload = {"meta": meta, "items": items}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print("저장: %s (%d건)  →  intel_db.py load %s" % (path, len(items), path))


def main():
    ap = argparse.ArgumentParser(description="무신사 수집 (허용 호스트만 · D48)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("plp", help="카테고리·브랜드 전수")
    p1.add_argument("--category")
    p1.add_argument("--brand")
    p1.add_argument("--gf", default="A", choices=["A", "M", "F"])
    p1.add_argument("--sort", default="POPULAR", help="sortCode (판매수량순 등)")
    p1.add_argument("--with-likes", action="store_true", help="하트도 함께 받는다")
    p1.add_argument("--out", required=True)

    p3 = sub.add_parser("reviews", help="리뷰 만족도 분포·사이즈·상품 나이")
    p3.add_argument("--goods", nargs="+")
    p3.add_argument("--from-db")
    p3.add_argument("--db", default=os.environ.get("INTEL_DB", "data/intel.db"))
    p3.add_argument("--limit", type=int)
    p3.add_argument("--out", required=True)

    p2 = sub.add_parser("likes", help="하트만 갱신")
    p2.add_argument("--from-db", required=True)
    p2.add_argument("--db", default=os.environ.get("INTEL_DB", "data/intel.db"))
    p2.add_argument("--out", required=True)
    a = ap.parse_args()

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if a.cmd == "plp":
        if not (a.category or a.brand):
            sys.exit("--category 나 --brand 중 하나는 필요하다.")
        params = {"gf": a.gf, "sortCode": a.sort, "size": "60", "page": "1"}
        if a.brand:
            params.update(brand=a.brand, caller="BRAND")
            target, story = "brand:%s" % a.brand, "brand-linesheet"
        else:
            params.update(category=a.category, caller="CATEGORY")
            target, story = "market:%s(gf=%s)" % (a.category, a.gf), "market-scan"
        items, pages, total = plp(params)
        cov = (100.0 * len(items) / total) if total else None
        print("PLP %s — %d건 / %d페이지%s" % (
            target, len(items), pages,
            ("" if total is None else
             " · 사이트 총계 %s (커버리지 %.1f%%)%s" % (
                 "{:,}".format(total), cov,
                 "" if cov >= 99.0 else "  ← 완주가 아니다"))))
        if a.with_likes:
            lk = likes([i["product_id"] for i in items])
            for i in items:
                i["like_count"] = lk.get(i["product_id"])
            got = sum(1 for i in items if i.get("like_count") is not None)
            print("  하트 %d건 (%.0f%%)" % (got, 100 * got / (len(items) or 1)))
        save(a.out, {"site": "musinsa", "story": story, "target": target,
                     "collected_at": now, "source": "api",
                     "source_total": total,
                     "notes": "PLP(api.musinsa.com) · 조회수·누적판매는 이 경로에 없다"
                              " — goods-detail은 robots Disallow (D48)"}, items)
    elif a.cmd == "reviews":
        if a.goods:
            gs = list(a.goods)
        else:
            conn = sqlite3.connect(a.db)
            gs = [r[0] for r in conn.execute(
                "SELECT DISTINCT p.product_id FROM products p JOIN observations o"
                " ON o.site=p.site AND o.product_id=p.product_id"
                " WHERE p.site='musinsa' AND o.context=?", (a.from_db,))]
        if a.limit:
            gs = gs[:a.limit]
        print("리뷰 수집 — 상품 %d개" % len(gs))
        items, with_survey = [], 0
        for i, g in enumerate(gs, 1):
            r = reviews(g)
            r["product_id"] = str(g)
            items.append(r)
            with_survey += bool(r["satisfaction"])
            if i % 20 == 0 or i == len(gs):
                print("  %d/%d" % (i, len(gs)), file=sys.stderr)
        print("  만족도 설문이 있는 상품 %d/%d" % (with_survey, len(gs)))
        save(a.out, {"site": "musinsa", "story": "reviews",
                     "target": a.from_db or "goods:%d개" % len(gs),
                     "collected_at": now, "source": "api",
                     "notes": "goods.musinsa.com 리뷰 · 만족도는 표본 집계"
                              "(review_sampled/review_total를 함께 본다)"}, items)
    else:
        conn = sqlite3.connect(a.db)
        gs = [r[0] for r in conn.execute(
            "SELECT DISTINCT p.product_id FROM products p JOIN observations o"
            " ON o.site=p.site AND o.product_id=p.product_id"
            " WHERE p.site='musinsa' AND o.context=?", (a.from_db,))]
        print("DB %s — 무신사 상품 %d개" % (a.from_db, len(gs)))
        lk = likes(gs)
        items = [{"product_id": g, "like_count": lk.get(g)} for g in gs]
        save(a.out, {"site": "musinsa", "story": "likes", "target": a.from_db,
                     "collected_at": now, "source": "api",
                     "notes": "하트 갱신만 (like.musinsa.com)"}, items)
    return 0


if __name__ == "__main__":
    sys.exit(main())
