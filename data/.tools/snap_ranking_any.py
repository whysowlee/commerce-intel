#!/usr/bin/env python3
"""무신사·29CM 랭킹 top 100 스냅샷 범용 수집기 (스토리 C, 스킬 플레이북 준수)

카테고리 코드는 ranking_targets.json(실측 카탈로그)에서 이름으로 찾는다.
새 카테고리를 모니터링할 때 코드 조사가 필요 없다 — 이름만 대면 된다.

usage:
    snap_ranking_any.py --site 29cm --target 여성슈즈
    snap_ranking_any.py --site musinsa --target "데님 팬츠"
    snap_ranking_any.py --site 29cm --target "여성슈즈>스니커즈"   # 이름이 겹칠 때 경로형
    snap_ranking_any.py --list --site musinsa                      # 타겟 목록
    snap_ranking_any.py --site musinsa --target 바지 --cron        # crontab 등록 줄 출력
    snap_ranking_any.py --site 29cm --target 남성슈즈 \
        --until "2026-07-31 10:00" --cron                          # 기한부 모니터링

기한부 모니터링(--until): 기한이 지난 실행은 수집하지 않고 **자기 crontab 줄을
스스로 지우고** 종료한다. 마감 후에도 빈 실행이 계속되던 하드코딩 마감의 안티패턴
(구 snap_ranking.py)을 대체한다. 정리 실행 시각을 놓쳐도(맥 수면 등) 다음 실행이
정리하므로 죽은 잡이 남지 않는다.

수집 기준(사용자 컨펌·어댑터 확정값):
    무신사  sections/199 + period=REALTIME + gf=A            (30분 갱신)
    29CM   BEST API + HOURLY + POPULARITY + gender/age=ALL   (1시간 갱신)

새 플랫폼 추가 체크리스트 — 코드 수정 지점은 SITES 레지스트리 하나다:
    1. 어댑터 실측부터 한다(commerce-research 스킬 원칙). 랭킹 데이터 출처·필드 매핑·
       갱신 주기·함정을 확인하고 references/<site>.md 관례로 기록한다. 이 단계는
       구조로 없앨 수 없다 — 사이트마다 화면과 API가 다르기 때문이다
    2. ranking_targets.json에 사이트 섹션을 추가한다:
       {"entries": [{"path": ["대","중",...], "target": "정규이름", ...코드 필드}]}
       path·target은 공통 스키마이고 코드 필드(code/facet 등)는 사이트 자유다
    3. collect_<site>(entry) 함수를 쓴다 — (records, notes)를 반환하며
       records는 스킬 데이터 계약 필드 전부, 미노출 지표는 null
    4. SITES에 {"collect": 함수, "cron": "분 시 * * *"} 한 줄을 등록한다
       (cron 분(minute)은 기존 사이트와 겹치지 않게, 갱신 주기는 어댑터 실측값)
    5. 검증: 스냅샷 1개 → validate_data.py PASS 확인 후에 스케줄에 건다

exit code: 0 저장 성공 / 1 수집 실패 / 3 타겟 못 찾음·모호함
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

# 저장소 위치는 스크립트 경로에서 유도한다(data/.tools/ → 루트). 클론해도 그대로 동작하고,
# 다른 위치를 쓰려면 COMMERCE_RESEARCH_HOME으로 덮는다.
REPO = os.environ.get("COMMERCE_RESEARCH_HOME") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ranking_targets.json")
TOP_N = 100

MUSINSA_URL = ("https://api.musinsa.com/api2/hm/web/v5/pans/ranking/sections/199"
               "?storeCode=musinsa&gf=A&ageBand=AGE_BAND_ALL&period={period}"
               "&categoryCode={code}&page=1")
# 원시값 보강 엔드포인트 — references/musinsa.md §4·§4-1 실측 확정 (전부 비로그인 200)
MUSINSA_DETAIL = "https://goods-detail.musinsa.com/api2/goods/{no}"           # 성별·시즌·리뷰·정가
MUSINSA_STAT = "https://goods-detail.musinsa.com/api2/goods/{no}/stat"        # 누적판매·조회수 원시
MUSINSA_PAGEVIEW = "https://goods-detail.musinsa.com/api2/goods/{no}/page-view"  # "보는 중" 원시
MUSINSA_LIKE = "https://like.musinsa.com/like/api/v2/liketypes/{kind}/counts"    # goods·brand 배치
MUSINSA_PERIODS = ("DAILY", "WEEKLY", "MONTHLY")  # REALTIME 외 기간별 순위도 기록한다
# 후기 목록(최신순). pageSize를 바꾸면 다른 시점의 캐시가 온다(2026-07-30 실측:
# ps=20은 7/13까지, ps=5는 7/25까지) — 반드시 같은 형태의 URL만 쓴다
MUSINSA_REVIEW = ("https://goods.musinsa.com/api2/review/v1/view/list"
                  "?goodsNo={no}&page={page}&pageSize=20&sort=new")
REVIEW_MAX_PAGES = 5          # 증분 판정용 상한 — 30분에 100건 넘게 달리면 초과분은 노트로 남긴다
REVIEW_NEGATIVE_MAX = 3       # 2026-07-30 사용자 기준: 0~3점 부정, 3점 초과~5점 긍정
CM_API = "https://display-bff-api.29cm.co.kr/api/v1/plp/best/items"


def curl(args_list):
    """실패하면 간격을 늘리며 2번까지 다시 시도한다(스킬 §지켜야 할 규칙)."""
    last = None
    for attempt in range(3):
        if attempt:
            time.sleep(2 * attempt)
        try:
            out = subprocess.run(["curl", "-s", "-m", "20", "-A", "Claude-User"] + args_list,
                                 capture_output=True, text=True, check=True)
            return json.loads(out.stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            last = exc
    raise last


def parse_korean_number(text):
    m = re.search(r"([\d.]+)(천|만)?", text)
    if not m:
        return None
    value = float(m.group(1))
    if m.group(2) == "천":
        value *= 1000
    elif m.group(2) == "만":
        value *= 10000
    return int(value)


# ---------------------------------------------------------------- 타겟 해석

def load_catalog():
    with open(CATALOG, encoding="utf-8") as f:
        return json.load(f)


def norm(s):
    return re.sub(r"\s+", "", s)


def resolve_target(catalog, site, query):
    """이름 → 카탈로그 항목. 리프 이름이 얕은 깊이 우선으로 유일하면 그걸 쓰고,
    겹치면 후보를 보여주며 경로형("부모>리프")을 요구한다."""
    entries = catalog[site]["entries"]
    q = [norm(p) for p in re.split(r"\s*>\s*", query.strip())]

    if len(q) > 1:  # 경로형: 경로의 뒷부분이 일치하면 된다
        hits = [e for e in entries
                if len(e["path"]) >= len(q)
                and [norm(p) for p in e["path"][-len(q):]] == q]
    else:
        hits = [e for e in entries if norm(e["path"][-1]) == q[0]]
        if len(hits) > 1:  # 얕은 깊이 우선 (예: '상의'는 1depth가 이긴다)
            min_d = min(len(e["path"]) for e in hits)
            shallow = [e for e in hits if len(e["path"]) == min_d]
            if len(shallow) == 1:
                hits = shallow

    if len(hits) == 1:
        return hits[0]
    if not hits:
        # 부분 일치 후보 제시
        similar = [e for e in entries if q[-1] in norm(e["path"][-1])][:8]
        print("타겟을 찾지 못했다: %r (사이트 %s)" % (query, site))
        if similar:
            print("비슷한 후보:")
            for e in similar:
                print("  - %s" % ">".join(e["path"]))
        sys.exit(3)
    print("이름이 여러 카테고리에 있다: %r — 경로형으로 지정해라:" % query)
    for e in hits[:10]:
        print("  - %s" % ">".join(e["path"]))
    sys.exit(3)


# ---------------------------------------------------------------- 무신사

def musinsa_ranking(code, period):
    """sections/199 랭킹 한 기간 조회 → (순위순 items, updatedAt epoch ms)."""
    data = curl([MUSINSA_URL.format(code=code, period=period)])
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            if "info" in obj and "image" in obj and "id" in obj:
                found.append(obj)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    # 어댑터 함정: BANNER_COLUMN(광고)은 rank가 없다 — PRODUCT_COLUMN + rank만
    items = [i for i in found
             if isinstance(i.get("image"), dict) and i["image"].get("rank")
             and i.get("type", "PRODUCT_COLUMN") == "PRODUCT_COLUMN"]
    items.sort(key=lambda x: x["image"]["rank"])

    updated_at = None
    for m in (data.get("data") or {}).get("modules") or []:
        if m.get("type") == "QUERY_UPDATEDAT":
            updated_at = (m.get("information") or {}).get("updatedAt")
    return items, updated_at


def prev_review_watermarks(site, target):
    """직전 스냅샷에서 상품별 후기 워터마크(본 것 중 최대 createDate)를 읽는다.

    스냅샷 파일이 곧 상태 저장소다 — 별도 상태 파일을 두면 스냅샷과 어긋날 수 있다.
    직전 파일이 없거나(첫 실행) 워터마크 필드가 없으면(구형 스냅샷) 빈 dict를 준다.
    """
    import glob
    safe = target.replace("/", "·")
    files = sorted(glob.glob("%s/data/snapshots/%s-ranking-%s-*.json" % (REPO, site, safe)))
    if not files:
        return {}
    try:
        with open(files[-1], encoding="utf-8") as f:
            items = json.load(f).get("items") or []
        return {i["product_id"]: i["review_watermark"] for i in items
                if i.get("review_watermark")}
    except (json.JSONDecodeError, KeyError, OSError):
        return {}


def musinsa_new_reviews(goods_no, watermark):
    """최신순 후기에서 워터마크(createDate) 이후 신규만 긍/부정으로 센다.

    반환: (positive, negative, new_watermark, truncated, list_total)
    워터마크가 없으면(첫 관측) 세지 않고 기준선만 잡는다 — 처음부터 전량을 훑지 않는다
    (2026-07-30 사용자 지시). 후기 no는 날짜와 단조가 아니라서(실측) 날짜로만 판정한다.
    같은 초에 걸린 후기는 중복 집계 방지(strict >)를 위해 다음 주기로 넘어갈 수 있다.
    """
    positive = negative = 0
    newest = watermark
    truncated = False
    list_total = None
    for page in range(1, REVIEW_MAX_PAGES + 1):
        data = (curl([MUSINSA_REVIEW.format(no=goods_no, page=page)]) or {}).get("data") or {}
        if list_total is None:
            list_total = data.get("total")
        rows = data.get("list") or []
        if not rows:
            if newest is None:
                # 후기 0건 상품: 날짜 워터마크를 잡을 후기가 없다. 수집 시각을 합성
                # 워터마크로 둬야 첫 후기가 달렸을 때 다음 주기가 셀 수 있다
                newest = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            break
        dates = [r.get("createDate") or "" for r in rows]
        if dates != sorted(dates, reverse=True):  # sort=new 폴백 의심 — 판정 불가로 처리
            raise RuntimeError("후기 목록이 최신순이 아니다 — sort 폴백 의심")
        if newest is None or dates[0] > newest:
            newest = dates[0]
        if watermark is None:  # 첫 관측: 기준선만
            return None, None, newest, False, list_total
        older_reached = False
        for r in rows:
            if (r.get("createDate") or "") > watermark:
                # 목록은 색상 변형 패밀리 합산이다(2026-07-30 실측: 변형별 상세
                # totalCount 합 = 목록 total 1017 정확 일치). 자기 상품 행만 센다 —
                # 안 거르면 다른 색상 후기가 섞이고 변형 동시 랭크인 시 이중 집계된다
                if str((r.get("goods") or {}).get("goodsNo")) != str(goods_no):
                    continue
                if int(r.get("grade") or 0) <= REVIEW_NEGATIVE_MAX:
                    negative += 1
                else:
                    positive += 1
            else:
                older_reached = True
        if older_reached:
            break
    else:
        truncated = True  # 상한 페이지까지 전부 신규 — 그 아래는 못 셌다
    if watermark is None:  # 첫 관측(후기 0건 포함): 규약대로 기준선만 — 0/0과 구분한다
        return None, None, newest, False, list_total
    return positive, negative, newest, truncated, list_total


def musinsa_like_counts(kind, relation_ids):
    """하트 배치 조회 → {relationId(str): count}. kind는 goods/brand 단수형."""
    data = curl(["-H", "Content-Type: application/json", "-X", "POST",
                 MUSINSA_LIKE.format(kind=kind),
                 "-d", json.dumps({"relationIds": relation_ids})])
    return {str(i["relationId"]): i.get("count")
            for i in ((data.get("data") or {}).get("contents") or {}).get("items") or []}


def collect_musinsa(entry):
    items, updated_at = musinsa_ranking(entry["code"], "REALTIME")
    items = items[:TOP_N]
    if not items:
        raise RuntimeError("랭킹 항목 0건 — categoryCode·응답 구조를 의심할 것")

    ids = [str(it["id"]) for it in items]
    enrich_fail = {"period": [], "detail": 0, "stat": 0, "page-view": 0,
                   "goods-like": False, "brand-like": False, "review": 0}
    watermarks = prev_review_watermarks("musinsa", entry["target"])

    # ① 기간별 순위 — DAILY/WEEKLY/MONTHLY는 하루 1회(04:4x) 배치라 요청 3번으로 끝난다
    period_rank, period_updated = {}, {}
    for period in MUSINSA_PERIODS:
        try:
            p_items, p_up = musinsa_ranking(entry["code"], period)
            period_rank[period] = {str(i["id"]): i["image"]["rank"] for i in p_items}
            period_updated[period] = p_up
        except Exception:
            enrich_fail["period"].append(period)
            period_rank[period] = {}

    # ② 상품 하트 배치 (100개 한 요청 실측 확인)
    goods_like = {}
    try:
        goods_like = musinsa_like_counts("goods", [int(i) for i in ids])
    except Exception:
        enrich_fail["goods-like"] = True

    # ③ 상품별 원시값 — 상세(성별·시즌·리뷰·정가) + stat(누적판매·조회수) + page-view(보는 중)
    #    + 후기 증분(직전 스냅샷 워터마크 이후 신규만 긍/부정 분류)
    detail_map, stat_map, pv_map, review_map = {}, {}, {}, {}
    review_truncated = []
    for gid in ids:
        for name, url, store in (("detail", MUSINSA_DETAIL, detail_map),
                                 ("stat", MUSINSA_STAT, stat_map),
                                 ("page-view", MUSINSA_PAGEVIEW, pv_map)):
            try:
                store[gid] = (curl([url.format(no=gid)]) or {}).get("data") or {}
            except Exception:
                enrich_fail[name] += 1
        try:
            pos, neg, wm, truncated, list_total = musinsa_new_reviews(gid, watermarks.get(gid))
            review_map[gid] = {"pos": pos, "neg": neg, "wm": wm, "total": list_total}
            if truncated:
                review_truncated.append(gid)
        except Exception:
            enrich_fail["review"] += 1
            # 판정 실패 시 워터마크를 이월해 다음 주기가 이어서 셀 수 있게 한다
            review_map[gid] = {"pos": None, "neg": None,
                               "wm": watermarks.get(gid), "total": None}
        time.sleep(0.1)  # 400여 요청이라 간격을 둔다 (30분 창 대비 충분히 짧다)

    # ④ 브랜드 하트 배치 — slug는 랭킹 항목의 onClickBrandName.url에서 얻는다
    brand_like = {}
    slug_of = {}
    for it in items:
        url = ((it["info"].get("onClickBrandName") or {}).get("url")) or ""
        m = re.search(r"/brand/([^/?#]+)", url)
        if m:
            slug_of[str(it["id"])] = m.group(1)
    try:
        slugs = sorted(set(slug_of.values()))
        if slugs:
            brand_like = musinsa_like_counts("brand", slugs)
    except Exception:
        enrich_fail["brand-like"] = True

    records = []
    for it in items:
        gid = str(it["id"])
        info, image = it["info"], it["image"]
        det = detail_map.get(gid) or {}
        stat = stat_map.get(gid) or {}
        final = info.get("finalPrice")
        ratio = info.get("discountRatio") or 0
        # 정가는 상세 원시값(goodsPrice.normalPrice) 우선, 실패 시 산술 복원 폴백
        original = (det.get("goodsPrice") or {}).get("normalPrice")
        if original is None:
            original = round(final / (1 - ratio / 100)) if (final and ratio) else final
        viewers_text = buyers = None
        for ai in info.get("additionalInformation") or []:
            text = ai.get("text", "")
            if "보는 중" in text:
                viewers_text = parse_korean_number(text)
            elif "구매 중" in text:
                buyers = parse_korean_number(text)
        # "보는 중"은 page-view 원시값 우선(문구와 정확 일치 검증됨). 어댑터 함정:
        # 원시 0은 저값 마스킹일 수 있어 문구 파싱으로 폴백한다
        viewers_raw = pv_map.get(gid, {}).get("goodsPageViewCount")
        viewers = viewers_raw if viewers_raw else viewers_text
        # 축약 표기는 정수로 파싱해 담되(SPEC v15) 원문도 남긴다 — 반올림 노출값이라는
        # 사실이 리포트 각주와 검증에서 살아 있어야 한다.
        purchase = purchase_display = None
        for lb in image.get("labels") or []:
            if lb.get("text", "").startswith("판매"):
                purchase_display = lb["text"]
                purchase = parse_korean_number(lb["text"])
        review = det.get("goodsReview") or {}
        category = det.get("baseCategoryFullPath") or entry["path"][-1]
        slug = slug_of.get(gid)
        records.append({
            "product_id": gid,
            "name": info.get("productName"),
            "url": (it.get("onClick") or {}).get("url"),
            "image_url": image.get("url"),
            "brand": info.get("brandName"),
            "category": category,
            "price_original": original,
            "price_sale": final,
            "discount_rate": ratio,
            "review_count": review.get("totalCount"),
            "rating": review.get("satisfactionScore"),   # 상세는 0~5 스케일이다
            "view_count": None, "view_count_display": None,  # SPEC 소관 — 원시는 page_view_total
            "purchase_count": purchase, "purchase_count_display": purchase_display,
            "like_count": goods_like.get(gid),
            "viewers_now": viewers, "buyers_now": buyers,
            "sold_out": bool(info.get("isSoldOut", False)),
            "rank": image["rank"],
            # ---- 계약 외 보강 필드 (2026-07-30 사용자 지시: 판매 추이 해석용 원시값 전부) ----
            "purchase_total": stat.get("purchaseTotal"),      # 누적판매 원시(배지의 원천)
            "page_view_total": stat.get("pageViewTotal"),     # 조회수 원시(최근 1개월 추정 윈도우)
            "brand_slug": slug,
            "brand_like_count": brand_like.get(slug),
            "sex": det.get("sex"),                            # 예: ["남성","여성"]
            "season": det.get("season"), "season_year": det.get("seasonYear"),
            "rank_daily": period_rank["DAILY"].get(gid),
            "rank_weekly": period_rank["WEEKLY"].get(gid),
            "rank_monthly": period_rank["MONTHLY"].get(gid),
            # 후기 증분(2026-07-30 지시): 직전 스냅샷 이후 신규 후기만 분류.
            # 0~3점 부정 / 3점 초과 긍정. null = 첫 관측이라 기준선만 잡은 것(0과 다르다)
            "review_new_positive": (review_map.get(gid) or {}).get("pos"),
            "review_new_negative": (review_map.get(gid) or {}).get("neg"),
            "review_watermark": (review_map.get(gid) or {}).get("wm"),
            "review_list_total": (review_map.get(gid) or {}).get("total"),
        })
    fails = {k: v for k, v in enrich_fail.items() if v}
    notes = [
        "updatedAt(원본 갱신 시각, epoch ms): %s" % updated_at,
        "기간별 랭킹 updatedAt: %s" % json.dumps(period_updated),
        "경로 A: sections/199 + categoryCode=%s, period=REALTIME top %d"
        % (entry["code"], TOP_N),
        "보강 수집(2026-07-30 지시): 상품별 detail·stat·page-view + 하트 배치(goods·brand) "
        "+ DAILY/WEEKLY/MONTHLY 순위 — 계약 외 필드 purchase_total·page_view_total·"
        "brand_slug·brand_like_count·sex·season·season_year·rank_daily/weekly/monthly",
        "price_original은 상세 goodsPrice.normalPrice 원시값(실패 시 finalPrice·discountRatio 산술 복원)",
        "like_count는 하트 배치 API 원시값, viewers_now는 page-view 원시값(0이면 문구 파싱 폴백)",
        "purchase_count는 '판매 N개' 배지 반올림 파싱 유지 — 원시는 purchase_total",
        "view_count는 SPEC 지시 전까지 null 유지 — 원시는 page_view_total(stat.pageViewTotal)",
        "rank_daily/weekly/monthly는 해당 기간 top102 밖이면 null (하루 1회 04:4x 배치 값)",
        "review_new_positive/negative는 직전 스냅샷 워터마크(createDate) 이후 신규 후기만 "
        "센 증분값 — 0~3점 부정, 3점 초과 긍정(2026-07-30 기준). null은 첫 관측(기준선만). "
        "후기 목록 캐시가 pageSize별로 시점이 달라 반영이 한 주기 늦을 수 있다",
        "review_list_total은 후기 목록 API 총계 = 색상 변형 패밀리 합산값(2026-07-30 원인 확정: "
        "변형별 상세 totalCount 합과 정확 일치). 상품 단독 총계는 review_count(상세) 쪽이다. "
        "증분 집계는 goods.goodsNo 필터로 자기 상품 후기만 센다",
    ]
    if review_truncated:
        notes.append("후기 증분 페이지 상한(%d) 도달 %d건(%s) — 그 아래 신규는 다음 주기로"
                     % (REVIEW_MAX_PAGES, len(review_truncated), ",".join(review_truncated)))
    if fails:
        notes.append("보강 실패 카운트: %s — 해당 필드는 null" % json.dumps(fails, ensure_ascii=False))
    return records, notes


# ---------------------------------------------------------------- 29CM

def collect_29cm(entry):
    body = {
        "pageRequest": {"page": 1, "size": TOP_N},
        "userSegment": {"gender": "ALL", "age": "ALL"},
        "facets": {
            "categoryFacetInputs": [entry["facet"]],
            "periodFacetInput": {"type": "HOURLY", "order": "DESC"},
            "rankingFacetInput": {"type": "POPULARITY"},
        },
    }
    last = None
    for attempt in range(3):
        if attempt:
            time.sleep(2 * attempt)
        try:
            data = curl(["-H", "Content-Type: application/json",
                         "-X", "POST", CM_API, "-d", json.dumps(body)])
        except Exception as exc:
            last = exc
            continue
        if (data.get("data") or {}).get("list"):
            break
        # 함정: 필드명을 틀리면 400이 아니라 200 + 빈 목록이 온다
        last = RuntimeError("빈 목록 — facet·필드명을 의심할 것")
    else:
        raise last

    records = []
    seen = set()
    off_category = 0
    own_code = list(entry["facet"].values())[-1]  # 가장 깊은 지정 코드
    for e in data["data"]["list"]:
        info = e["itemInfo"]
        props = (e.get("itemEvent") or {}).get("eventProperties") or {}
        item_id = str(e["itemId"])
        if item_id in seen:  # 29CM 목록은 중복이 나올 수 있다(어댑터)
            continue
        seen.add(item_id)
        if own_code not in (props.get("largeCategoryNo"), props.get("middleCategoryNo"),
                            props.get("smallCategoryNo")):
            off_category += 1
        rate = info.get("saleRate")
        if isinstance(rate, float) and rate.is_integer():
            rate = int(rate)
        category = (props.get("smallCategoryName") or props.get("middleCategoryName")
                    or props.get("largeCategoryName"))
        records.append({
            "product_id": item_id,
            "name": info.get("productName"),
            "url": "https://www.29cm.co.kr/products/%s" % item_id,
            "image_url": info.get("thumbnailUrl"),
            "brand": info.get("brandName"),
            "category": category,
            "price_original": info.get("originalPrice"),
            "price_sale": info.get("displayPrice"),  # 쿠폰적용가 — sellPrice가 아니다
            "discount_rate": rate,                    # saleRate는 displayPrice 기준
            "review_count": info.get("reviewCount"),
            "rating": info.get("reviewScore"),        # 0~5 그대로
            "view_count": None, "view_count_display": None,
            "purchase_count": None, "purchase_count_display": None,
            "like_count": info.get("likeCount"),
            "viewers_now": None, "buyers_now": None,  # 29CM에는 실시간 지표가 없다
            "sold_out": bool(info.get("isSoldOut", False)),
            "rank": len(records) + 1,
        })
    notes = [
        "경로 A: BEST API(display-bff-api) categoryFacetInputs=%s, "
        "HOURLY + POPULARITY, gender=ALL age=ALL (요청 1회, size=%d)"
        % (json.dumps(entry["facet"]), TOP_N),
        "source_total은 null — 29CM BEST API는 총계를 주지 않는다(hasNext만). "
        "랭킹 top 100이 대상이라 카탈로그 전량이 기준이 아니다",
        "카테고리 이름은 itemEvent.eventProperties에서 직접 왔다 "
        "(small 미노출이면 middle → large 순으로 폴백)",
        "주 카테고리가 지정 카테고리 밖인 상품 %d건 포함 — 유니섹스 등 교차 노출이며 "
        "사이트가 이 랭킹에 실제로 올린 것이다" % off_category,
        "price_sale은 쿠폰적용가(displayPrice), discount_rate(saleRate)도 같은 기준이다",
        "view_count·purchase_count·viewers_now·buyers_now는 29CM 미노출 → null",
        "화면 기본값은 개인화(gender=F&age=30) — 축적 기준은 gender=ALL·age=ALL이다",
    ]
    return records, notes


# ---------------------------------------------------------------- 사이트 레지스트리
# 새 플랫폼은 여기 한 줄 + collect_<site>() + 카탈로그 섹션으로 추가한다(모듈 docstring).
# cron 주기는 사이트의 원본 갱신 주기를 따르고(어댑터 실측), 분(minute)은
# 정각·30분과 다른 사이트를 피해 잡는다.

SITES = {
    "musinsa": {"collect": collect_musinsa, "cron": "7,37 * * * *"},  # 실시간 30분 갱신
    "29cm": {"collect": collect_29cm, "cron": "17 * * * *"},          # HOURLY 1시간 갱신
}


# ---------------------------------------------------------------- 기한부 모니터링

def cron_tag(site, target):
    """crontab 줄 끝 주석 태그. 등록(--cron)과 자기 제거가 같은 값을 써야 한다."""
    return "commerce-research-snap-%s-%s" % (site, target.replace("/", "·").replace(" ", ""))


def remove_own_cron_line(tag):
    """태그가 줄 끝 주석과 정확히 일치하는 crontab 줄만 지운다.

    부분 문자열로 지우면 안 된다 — '남성슈즈' 태그가 '남성슈즈>스니커즈' 잡까지
    지워버린다. endswith로 판정해 다른 잡을 보존한다.
    """
    listed = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if listed.returncode != 0:  # crontab 자체가 없다
        return False
    lines = listed.stdout.splitlines()
    kept = [l for l in lines if not l.rstrip().endswith("# " + tag)]
    if len(kept) == len(lines):
        return False
    subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n", text=True, check=True)
    return True


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="무신사·29CM 랭킹 top 100 스냅샷")
    parser.add_argument("--site", choices=list(SITES))
    parser.add_argument("--target", help="카테고리 이름 (겹치면 '부모>리프' 경로형)")
    parser.add_argument("--list", action="store_true", help="타겟 목록을 출력")
    parser.add_argument("--cron", action="store_true", help="crontab 등록 줄만 출력")
    parser.add_argument("--until", metavar="'YYYY-MM-DD HH:MM'",
                        help="이 시각이 지난 실행은 수집 대신 자기 crontab 줄을 지우고 끝낸다")
    args = parser.parse_args()

    until = None
    if args.until:
        try:
            until = datetime.strptime(args.until.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            parser.error("--until 형식은 'YYYY-MM-DD HH:MM'이다: %r" % args.until)

    catalog = load_catalog()

    if args.list:
        sites = [args.site] if args.site else list(SITES)
        for site in sites:
            print("== %s (%d개)" % (site, len(catalog[site]["entries"])))
            for e in catalog[site]["entries"]:
                depth = len(e["path"])
                print("  %s%s" % ("  " * (depth - 1), e["path"][-1])
                      + ("   → target: %s" % e["target"] if ">" in e["target"] else ""))
        return 0

    if not (args.site and args.target):
        parser.error("--site와 --target이 필요하다 (--list 제외)")

    entry = resolve_target(catalog, args.site, args.target)
    target = entry["target"]
    tag = cron_tag(args.site, target)

    if until and datetime.now() >= until:
        removed = remove_own_cron_line(tag)
        print("마감(%s) 경과 — 수집하지 않음. crontab 정리: %s"
              % (args.until, "제거함" if removed else "등록된 줄 없음"))
        return 0

    if args.cron:
        safe = target.replace("/", "·").replace(" ", "")
        script = os.path.abspath(__file__)
        log = os.path.join(os.path.dirname(script), "snap-%s-%s.log" % (args.site, safe))
        until_part = " --until '%s'" % args.until.strip() if until else ""
        line = ("%s /usr/bin/python3 %s --site %s --target '%s'%s >> %s 2>&1 # %s"
                % (SITES[args.site]["cron"], script, args.site, target, until_part, log, tag))
        print("( crontab -l 2>/dev/null; echo \"%s\" ) | crontab -" % line.replace('"', '\\"'))
        return 0

    records, notes = SITES[args.site]["collect"](entry)

    now = datetime.now()
    safe = target.replace("/", "·")
    path = "%s/data/snapshots/%s-ranking-%s-%s.json" % (
        REPO, args.site, safe, now.strftime("%Y%m%d-%H%M"))
    doc = {
        "meta": {
            "site": args.site, "story": "ranking-snapshot", "target": target,
            "collected_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "item_count": len(records), "source_total": None, "incomplete": False,
            "notes": notes,
        },
        "items": records,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)   # 클론 직후엔 data/가 비어 있다
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("저장: %s (%d건)" % (path, len(records)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
