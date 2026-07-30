#!/usr/bin/env python3
"""무신사·29CM 카테고리 전수조사 범용 수집기 (스토리 B, 스킬 플레이북 준수)

카테고리 코드는 ranking_targets.json(실측 카탈로그)에서 **이름으로** 찾는다.
스토리 C의 snap_ranking_any.py와 같은 카탈로그·같은 이름 해석 규칙을 쓴다 —
전수조사도 "이름만 대면 시작"이 되도록 하는 것이 이 파일의 목적이다.

usage:
    # 규모부터 본다 (범위 협의용 — 좁힐 축의 실제 개수를 뽑아준다)
    scan_market_any.py --site 29cm --target "여성의류>바지" --count-only
    scan_market_any.py --site 29cm --target "여성의류>바지" --keyword "코튼 팬츠" --count-only

    # 수집
    scan_market_any.py --site 29cm --target "여성의류>바지" \
        --keyword "코튼 팬츠" --name-match --label "코튼 팬츠(여성 바지, 검색∩상품명)"
    scan_market_any.py --site musinsa --target "코튼 팬츠" --gender F \
        --min-price 100000 --max-price 200000

    scan_market_any.py --list --site 29cm          # 타겟 목록

사이트에 없는 카테고리를 요청받았을 때 (2026-07-30 사용자 확정):
    **키워드 검색 ∩ 상위 카테고리**를 범위로 삼는다. 예를 들어 29CM에는
    '여성 코튼 팬츠' 카테고리가 없다(여성 바지 소분류 10종에 코튼이 없고
    소재 필터도 없다) — 그래서 검색 '코튼 팬츠' ∩ 여성의류>바지로 잡는다.
    이때 엉터리 키워드가 통과하지 않도록 아래 가드가 자동으로 걸린다.

키워드 가드 (2026-07-30 사용자 확정, 실측 근거는 references/29cm.md):
    G1  교집합 0건        → 수집하지 않고 exit 4. "그런 상품군을 찾지 못했다"
                            (실측: 우주/티타늄 팬츠/라면 팬츠/코튼팬쯔/가나다라마 전부 0)
    G2  교집합 ≥ 카테고리 총계의 90%
                          → 키워드가 사실상 무시된 것으로 보고 exit 5
                            (실측: 무신사 '코튼 팬츠' ∩ 코튼팬츠 카테고리 = 99.8%)
    G3  상품명 실제 매칭률을 계산해 meta.notes와 화면 보고에 남긴다.
        --name-match를 주면 매칭된 것만 남긴다(범위가 그만큼 좁아진다는 뜻이라
        target 이름에 그 사실이 드러나야 한다 — --label로 적어라)

    G2의 임계값은 토큰 변별력 판정에도 그대로 쓴다. 키워드를 토큰으로 쪼개
    토큰별로 교집합을 세고, 카테고리의 90% 이상을 덮는 토큰은 이름매칭에서 버린다
    (실측: 여성 바지에서 '바지' 100.0% · '팬츠' 98.3% → 버림, '코튼' 26.4% → 남김).
    카테고리 이름 목록을 손으로 관리하지 않아도 되는 이유가 이것이다.

새 플랫폼 추가 체크리스트 — 코드 수정 지점은 SITES 레지스트리 하나다:
    1. 어댑터 실측부터 한다. 목록 경로·총계 노출 여부·필드 매핑·품절 기본값을
       확인하고 references/<site>.md 관례로 기록한다
    2. ranking_targets.json에 사이트 섹션이 이미 있으면 그대로 쓴다(랭킹과 공유)
    3. count_<site>(entry, opts) / collect_<site>(entry, opts) 를 쓴다
    4. SITES에 한 줄 등록한다
    5. 검증: 수집 1회 → validate_data.py PASS 확인

exit code: 0 성공 / 1 수집 실패 / 3 타겟 못 찾음·모호함 / 4 가드 G1 / 5 가드 G2
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from snap_ranking_any import load_catalog, resolve_target, curl  # noqa: E402  카탈로그 단일 출처

# 저장소 위치는 스크립트 경로에서 유도한다(data/.tools/ → 루트). 클론해도 그대로 동작하고,
# 다른 위치를 쓰려면 COMMERCE_RESEARCH_HOME으로 덮는다.
REPO = os.environ.get("COMMERCE_RESEARCH_HOME") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NO_DISCRIMINATION = 0.90   # 가드 G2 임계값 (사용자 확정 2026-07-30)
PAGE_SLEEP = 0.5           # 스킬 §지켜야 할 규칙: 페이지 사이 0.5~1.5초
PAGE_CAP = 1000            # 무한 루프 방지 안전판 (상한은 상품 수로 건다)

CM_ITEMS = "https://display-bff-api.29cm.co.kr/api/v1/listing/items"
CM_COUNT = "https://display-bff-api.29cm.co.kr/api/v1/listing/items/count"
MS_PLP = "https://api.musinsa.com/api2/dp/v2/plp/goods"
MS_LIKE = "https://like.musinsa.com/like/api/v2/liketypes/goods/counts"

# 이름 매칭용 동의어. 사이트 검색은 소재·상세까지 훑으므로 상품명 매칭은 따로 필요하다.
# 여기 없는 토큰은 토큰 자신만으로 매칭한다(대소문자 무시).
ALIASES = {
    "코튼": ["코튼", "cotton", "면"],
    "데님": ["데님", "denim", "청"],
    "린넨": ["린넨", "리넨", "linen", "마"],
    "울": ["울", "wool", "모"],
    "캐시미어": ["캐시미어", "cashmere"],
    "코듀로이": ["코듀로이", "corduroy", "골덴"],
    "가죽": ["가죽", "leather"],
    "스웨이드": ["스웨이드", "suede"],
    "나일론": ["나일론", "nylon"],
    "니트": ["니트", "knit"],
    "플리스": ["플리스", "fleece"],
    "시어서커": ["시어서커", "seersucker"],
}


def post_json(url, body):
    return curl(["-H", "Content-Type: application/json", "-X", "POST", url,
                 "-d", json.dumps(body, ensure_ascii=False)])


# ---------------------------------------------------------------- 29CM

def _cm_facets(entry, opts):
    facets = {"categoryFacetInputs": [dict(entry["facet"])]}
    if opts.get("min_price") or opts.get("max_price"):
        # 함정: 필드명을 틀리면 400이 아니라 200 + 총계 그대로가 온다.
        # 호출부에서 총계가 실제로 줄었는지 대조한다.
        facets["priceFacetInput"] = {"min": opts.get("min_price") or 0,
                                     "max": opts.get("max_price") or 100000000}
    return facets


CM_SORT = "NEWEST"   # 전수 순회는 결정적 정렬로만 한다 — 아래 이유


def _cm_body(entry, opts, page=None, size=50):
    kw = opts.get("keyword")
    body = {"pageType": "SRP" if kw else "CATEGORY_PLP",
            # ⚠️ RECOMMENDED(추천순)로 순회하면 깊은 페이지에서 순서가 흔들려 조용히 놓친다.
            # 실측(2026-07-30, 여성 바지 ∩ '코튼 팬츠'): 추천순은 200~230페이지 구간 중복
            # 13.9% · 300~330 구간 9.8%, 342페이지 완주해도 유니크 13,811/17,093(19% 누락).
            # NEWEST·LOWEST_PRICE·MOST_LIKED는 같은 구간에서 중복 0.0%였다.
            "sortType": CM_SORT,
            "facets": _cm_facets(entry, opts)}
    if kw:
        body["keyword"] = kw
    if page is not None:
        body["pageRequest"] = {"page": page, "size": size}
    return body


def count_29cm(entry, opts):
    return post_json(CM_COUNT, _cm_body(entry, opts))["data"]["totalCount"]


def collect_29cm(entry, opts, emit):
    """페이지네이션(100/page)으로 순회한다. 총계·hasNext 둘 다 응답에 있다."""
    page, size, total = 1, 100, None
    while page <= PAGE_CAP:
        data = post_json(CM_ITEMS, _cm_body(entry, opts, page, size))["data"]
        total = data["pagination"]["totalCount"]
        rows = data.get("list") or []
        if not rows:                       # 빈 페이지 = 결정적 종료 판정
            break
        for e in rows:
            info = e["itemInfo"]
            props = (e.get("itemEvent") or {}).get("eventProperties") or {}
            item_id = str(e["itemId"])
            rate = info.get("saleRate")
            if isinstance(rate, float) and rate.is_integer():
                rate = int(rate)
            emit({
                "product_id": item_id,
                "name": info.get("productName"),
                "url": "https://www.29cm.co.kr/products/%s" % item_id,
                "image_url": info.get("thumbnailUrl"),
                "brand": info.get("brandName"),
                # 목록이 카테고리 '이름'을 직접 준다 — meta description 해석이 필요 없다
                "category": (props.get("smallCategoryName") or props.get("middleCategoryName")
                             or props.get("largeCategoryName")),
                "price_original": info.get("originalPrice"),
                "price_sale": info.get("displayPrice"),   # 쿠폰적용가 (sellPrice 아님)
                "discount_rate": rate,                    # saleRate는 displayPrice 기준
                "review_count": info.get("reviewCount"),
                "rating": info.get("reviewScore"),        # 0~5 그대로
                "view_count": None, "view_count_display": None,
                "purchase_count": None, "purchase_count_display": None,
                "like_count": info.get("likeCount"),
                "viewers_now": None, "buyers_now": None,  # 29CM에는 실시간 지표가 없다
                "sold_out": bool(info.get("isSoldOut", False)),
                "rank": None,
                "is_ad": bool(props.get("isAd", False)),
                "attributes": {}, "attributes_basis": "unknown",
            })
        if not data["pagination"].get("hasNext"):
            break
        page += 1
        time.sleep(PAGE_SLEEP)
    notes = [
        "경로 A: display-bff-api /api/v1/listing/items (%s), %d개/페이지, 정렬 %s(결정적), "
        "hasNext + 빈 페이지로 종료 판정"
        % ("SRP 검색" if opts.get("keyword") else "CATEGORY_PLP", size, CM_SORT),
        "품절 상품 포함 — 29CM 목록의 기본값이 품절 포함이다(필터 이름이 '품절상품 제외')",
        "price_sale은 쿠폰적용가(displayPrice), discount_rate(saleRate)도 같은 기준이다",
        "카테고리 이름은 itemEvent.eventProperties에서 직접 왔다(코드 해석 불필요)",
        "view_count·purchase_count·viewers_now·buyers_now는 29CM 미노출 → null",
    ]
    return total, notes


# ---------------------------------------------------------------- 무신사

def _ms_url(entry, opts, size=100):
    kw = opts.get("keyword")
    parts = ["category=%s" % entry["code"], "gf=%s" % (opts.get("gender") or "A"),
             "size=%d" % size, "page=1", "sortCode=POPULAR",
             "caller=%s" % ("SEARCH" if kw else "CATEGORY"),
             "isSoldOut=true"]   # 함정: 없으면 화면·API 모두 품절을 뺀다
    if kw:
        from urllib.parse import quote
        parts.append("keyword=%s" % quote(kw))
    if opts.get("min_price"):
        parts.append("minPrice=%d" % opts["min_price"])
    if opts.get("max_price"):
        parts.append("maxPrice=%d" % opts["max_price"])
    return MS_PLP + "?" + "&".join(parts)


def count_musinsa(entry, opts):
    data = curl([_ms_url(entry, opts, size=1)])["data"]
    return (data.get("pagination") or {}).get("totalCount") or 0


def collect_musinsa(entry, opts, emit):
    """page≥2를 직접 조립하면 403이다 — 응답의 nextPageUrl(hmac 서명)을 그대로 따라간다."""
    url, total, pages, ids = _ms_url(entry, opts), None, 0, []
    while url and pages < PAGE_CAP:
        data = curl([url])["data"]
        total = (data.get("pagination") or {}).get("totalCount") or total
        rows = data.get("list") or []
        if not rows:      # nextPageUrl은 데이터가 소진된 뒤에도 계속 온다 — 빈 결과로 끝낸다
            break
        for g in rows:
            score = g.get("reviewScore")
            ids.append(str(g.get("goodsNo")))
            emit({
                "product_id": str(g.get("goodsNo")),
                "name": g.get("goodsName"),
                "url": g.get("goodsLinkUrl"),
                "image_url": g.get("thumbnail"),
                "brand": g.get("brandName"),
                "category": entry["path"][-1],
                "price_original": g.get("normalPrice"),
                "price_sale": g.get("finalPrice"),      # 쿠폰적용가
                "discount_rate": g.get("finalDiscount"),
                "review_count": g.get("reviewCount"),
                "rating": round(score / 20, 1) if isinstance(score, (int, float)) else None,
                "view_count": None, "view_count_display": None,
                "purchase_count": None, "purchase_count_display": None,
                "like_count": None,                     # 아래 하트 배치 API로 채운다
                "viewers_now": None, "buyers_now": None,
                "sold_out": bool(g.get("isSoldOut", False)),
                "rank": None,
                "is_ad": bool(g.get("isAd", False)),
                "attributes": {}, "attributes_basis": "unknown",
            })
        url = (data.get("pagination") or {}).get("nextPageUrl")
        pages += 1
        time.sleep(PAGE_SLEEP)
    notes = [
        "경로 A: plp/goods caller=%s, gf=%s, isSoldOut=true, nextPageUrl 체인 %d페이지"
        % ("SEARCH" if opts.get("keyword") else "CATEGORY", opts.get("gender") or "A", pages),
        "품절 포함(isSoldOut=true) — 없으면 화면·API 모두 품절을 뺀다",
        "price_sale은 쿠폰적용가(finalPrice), discount_rate는 finalDiscount다",
        "rating은 목록의 0~100 스케일을 20으로 나눠 0~5로 맞췄다",
        "view_count는 구간 표기라 목록에서 담지 않는다 → null",
    ]
    return total, notes


def fill_musinsa_likes(records):
    """상품 하트는 목록에 없다 — 배치 API로 100개씩 채운다(비로그인 200)."""
    filled = 0
    for i in range(0, len(records), 100):
        chunk = records[i:i + 100]
        try:
            data = post_json(MS_LIKE, {"relationIds": [int(r["product_id"]) for r in chunk]})
        except Exception:
            continue
        counts = {str(x["relationId"]): x.get("count")
                  for x in ((data.get("data") or {}).get("contents") or {}).get("items") or []}
        for r in chunk:
            if r["product_id"] in counts:
                r["like_count"] = counts[r["product_id"]]
                filled += 1
        time.sleep(0.3)
    return filled


# ---------------------------------------------------------------- 사이트 레지스트리

SITES = {
    "musinsa": {"count": count_musinsa, "collect": collect_musinsa,
                "post_process": fill_musinsa_likes, "gender_axis": True},
    "29cm": {"count": count_29cm, "collect": collect_29cm,
             "post_process": None, "gender_axis": False},  # 29CM 성별은 카테고리 트리로 갈린다
}


# ---------------------------------------------------------------- 키워드 가드

def token_power(site, entry, opts, keyword, base_total):
    """키워드 토큰별 변별력을 실제 총계로 잰다. 카테고리의 90% 이상을 덮으면 버린다."""
    kept, dropped = [], []
    for tok in [t for t in re.split(r"[\s,/]+", keyword.strip()) if t]:
        probe = dict(opts, keyword=tok)
        try:
            c = SITES[site]["count"](entry, probe)
        except Exception:
            c = None
        ratio = (c / base_total) if (c is not None and base_total) else None
        rec = {"token": tok, "count": c, "ratio": ratio}
        (dropped if (ratio is not None and ratio >= NO_DISCRIMINATION) else kept).append(rec)
        time.sleep(0.3)
    return kept, dropped


def name_matcher(tokens):
    """살아남은 토큰 전부가(동의어 포함) 상품명에 있어야 매칭으로 본다."""
    groups = [ALIASES.get(t.lower(), [t]) for t in tokens]

    def match(name):
        low = (name or "").lower()
        return all(any(a.lower() in low for a in g) for g in groups)
    return match


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description="무신사·29CM 카테고리 전수조사 수집기")
    p.add_argument("--site", choices=list(SITES))
    p.add_argument("--target", help="카테고리 이름 (겹치면 '부모>리프' 경로형)")
    p.add_argument("--keyword", help="사이트에 없는 상품군일 때: 검색 ∩ 카테고리로 범위를 잡는다")
    p.add_argument("--name-match", action="store_true",
                   help="상품명에 키워드가 실제로 있는 것만 남긴다(범위가 좁아진다 — --label에 밝혀라)")
    p.add_argument("--gender", choices=["A", "M", "F"], help="무신사 전용 (29CM은 카테고리로 갈린다)")
    p.add_argument("--min-price", type=int)
    p.add_argument("--max-price", type=int)
    p.add_argument("--limit", type=int, default=30000, help="안전 상한(상품 수)")
    p.add_argument("--label", help="meta.target에 쓸 범위 이름. 좁혔으면 그 사실이 드러나야 한다")
    p.add_argument("--count-only", action="store_true", help="수집하지 않고 규모만 조회한다")
    p.add_argument("--list", action="store_true")
    args = p.parse_args()

    catalog = load_catalog()
    if args.list:
        for site in ([args.site] if args.site else list(SITES)):
            print("== %s (%d개)" % (site, len(catalog[site]["entries"])))
            for e in catalog[site]["entries"]:
                print("  %s%s" % ("  " * (len(e["path"]) - 1), e["path"][-1]))
        return 0
    if not (args.site and args.target):
        p.error("--site와 --target이 필요하다 (--list 제외)")

    entry = resolve_target(catalog, args.site, args.target)
    opts = {"keyword": args.keyword, "gender": args.gender,
            "min_price": args.min_price, "max_price": args.max_price}
    path_name = ">".join(entry["path"])
    print("타겟: %s (%s)" % (path_name, args.site))

    # 기준 총계 — 키워드만 뺀 같은 조건. 가드 G2의 분모다.
    base_opts = dict(opts, keyword=None)
    base_total = SITES[args.site]["count"](entry, base_opts)
    scope_total = SITES[args.site]["count"](entry, opts) if args.keyword else base_total
    print("카테고리 총계 %s / 이 범위 총계 %s" % (f"{base_total:,}", f"{scope_total:,}"))

    if (args.min_price or args.max_price):
        raw = SITES[args.site]["count"](entry, dict(base_opts, min_price=None, max_price=None))
        if raw == base_total:
            print("가격 필터가 총계를 바꾸지 못했다 — 필드명이 무시된 것으로 본다. 중단.")
            return 1

    guard_notes = []
    # --- G1
    if scope_total == 0:
        print("가드 G1: 이 범위에 상품이 0건이다. 그런 상품군을 찾지 못했다 — 수집하지 않는다.")
        return 4
    # --- G2
    kept_tokens = []
    if args.keyword:
        ratio = scope_total / base_total if base_total else 0
        guard_notes.append("가드 G2: 검색∩카테고리 %d / 카테고리 %d = %.1f%% (임계 %.0f%%)"
                           % (scope_total, base_total, ratio * 100, NO_DISCRIMINATION * 100))
        if ratio >= NO_DISCRIMINATION:
            print("가드 G2: 키워드가 카테고리의 %.1f%%를 덮는다 — 사실상 무시된 검색이다. 중단."
                  % (ratio * 100))
            print("        카테고리만으로 조사하거나 다른 키워드를 쓸지 확인이 필요하다.")
            return 5
        kept, dropped = token_power(args.site, entry, base_opts, args.keyword, base_total)
        kept_tokens = [k["token"] for k in kept]
        fmt = lambda r: "%s %s(%.1f%%)" % (r["token"], f"{r['count']:,}" if r["count"] is not None else "?",
                                           (r["ratio"] or 0) * 100)
        guard_notes.append("토큰 변별력 — 남김: %s / 버림(카테고리 %d%% 이상 커버): %s"
                           % (", ".join(fmt(r) for r in kept) or "없음",
                              NO_DISCRIMINATION * 100,
                              ", ".join(fmt(r) for r in dropped) or "없음"))
        print(guard_notes[-1])
        if args.name_match and not kept_tokens:
            print("가드: 변별력 있는 토큰이 없어 상품명 매칭을 걸 수 없다. 중단.")
            return 5

    if args.count_only:
        print("규모 조회만 하고 끝낸다.")
        return 0

    if scope_total > args.limit:
        print("범위 %d건이 안전 상한 %d건을 넘는다 — 좁히거나 --limit을 올려라." % (scope_total, args.limit))
        return 1

    # --- 수집
    records, seen = [], set()
    stat = {"rows": 0, "dups": 0}

    def emit(rec):
        stat["rows"] += 1
        if rec["product_id"] in seen:      # 29CM 목록은 중복이 나올 수 있다
            stat["dups"] += 1
            return
        seen.add(rec["product_id"])
        records.append(rec)

    started = datetime.now()
    reported_total, notes = SITES[args.site]["collect"](entry, opts, emit)
    swept = len(records)
    print("순회 완료: 수신 %d행 / 유니크 %d건 (사이트 총계 %s)"
          % (stat["rows"], swept, reported_total))
    # 완주 판정은 **수신 행 수**로 한다 — 유니크 건수로 재면 사이트 중복이 누락으로 보인다
    missed = (reported_total or 0) - stat["rows"]
    notes.append("완주 대조: 사이트 총계 %s / 수신 %d행 / 중복 %d행 제거 → 유니크 %d건"
                 % (f"{reported_total:,}" if reported_total else "미노출",
                    stat["rows"], stat["dups"], swept))

    # --- G3 이름 매칭률
    match_rate = None
    filtered_out = 0
    if kept_tokens:
        match = name_matcher(kept_tokens)
        hits = [r for r in records if match(r["name"])]
        match_rate = 100 * len(hits) / swept if swept else 0
        print("상품명 실제 매칭률: %d/%d = %.1f%%" % (len(hits), swept, match_rate))
        if args.name_match:
            filtered_out = swept - len(hits)
            records = hits

    post = SITES[args.site].get("post_process")
    if post and records:
        filled = post(records)
        notes.append("좋아요는 하트 배치 API로 %d/%d건 채웠다" % (filled, len(records)))

    if not records:
        print("남은 상품이 0건이다 — 리포트를 만들지 않는다.")
        return 4

    target = args.label or (path_name.split(">")[-1] if not args.keyword else
                            "%s(%s)" % (args.keyword, path_name))
    now = datetime.now()
    safe = target.replace("/", "·")
    out = "%s/data/raw/%s-market-scan-%s-%s.json" % (REPO, args.site, safe, now.strftime("%Y%m%d-%H%M"))

    notes = guard_notes + notes
    if args.keyword:
        notes.insert(0, "범위 = 검색 '%s' ∩ %s. 사이트에 이 이름의 카테고리가 없어 "
                        "교집합으로 잡았다(사용자 확정 방식)" % (args.keyword, path_name))
    if match_rate is not None:
        notes.append("상품명 실제 매칭률 %.1f%% (%d/%d) — 검색은 소재·상세까지 훑으므로 "
                     "이름에 안 드러나는 상품이 섞인다" % (match_rate, swept - filtered_out, swept))
    if args.name_match:
        notes.append("상품명 매칭 필터 적용: 순회 %d건 중 %d건을 제외하고 %d건을 담았다. "
                     "**소재는 코튼이지만 이름에 안 쓴 상품은 빠진다**" % (swept, filtered_out, len(records)))
        notes.append("source_total은 null — 이 범위(검색∩카테고리∩상품명)의 총계를 사이트가 "
                     "노출하지 않는다. 순회 완전성은 검색∩카테고리 총계 %s와 순회 %d건의 "
                     "대조로 확인했다" % (f"{reported_total:,}", swept))
    doc = {
        "meta": {
            "site": args.site, "story": "market-scan", "target": target,
            "collected_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "item_count": len(records),
            "source_total": None if args.name_match else reported_total,
            "incomplete": missed > 0,
            "notes": notes,
        },
        "items": records,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("저장: %s (%d건, %.1f분)" % (out, len(records), (datetime.now() - started).seconds / 60))
    return 0


if __name__ == "__main__":
    sys.exit(main())
