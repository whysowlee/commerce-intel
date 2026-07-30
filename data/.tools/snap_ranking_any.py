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
               "?storeCode=musinsa&gf=A&ageBand=AGE_BAND_ALL&period=REALTIME"
               "&categoryCode={code}&page=1")
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

def collect_musinsa(entry):
    data = curl([MUSINSA_URL.format(code=entry["code"])])
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
    items = items[:TOP_N]
    if not items:
        raise RuntimeError("랭킹 항목 0건 — categoryCode·응답 구조를 의심할 것")

    updated_at = None
    for m in (data.get("data") or {}).get("modules") or []:
        if m.get("type") == "QUERY_UPDATEDAT":
            updated_at = (m.get("information") or {}).get("updatedAt")

    records = []
    for it in items:
        info, image = it["info"], it["image"]
        final = info.get("finalPrice")
        ratio = info.get("discountRatio") or 0
        # 랭킹 목록은 정가 미노출 — 노출된 판매가·할인율에서 산술 복원
        original = round(final / (1 - ratio / 100)) if (final and ratio) else final
        viewers = buyers = None
        for ai in info.get("additionalInformation") or []:
            text = ai.get("text", "")
            if "보는 중" in text:
                viewers = parse_korean_number(text)
            elif "구매 중" in text:
                buyers = parse_korean_number(text)
        # 축약 표기는 정수로 파싱해 담되(SPEC v15) 원문도 남긴다 — 반올림 노출값이라는
        # 사실이 리포트 각주와 검증에서 살아 있어야 한다.
        purchase = purchase_display = None
        for lb in image.get("labels") or []:
            if lb.get("text", "").startswith("판매"):
                purchase_display = lb["text"]
                purchase = parse_korean_number(lb["text"])
        records.append({
            "product_id": str(it["id"]),
            "name": info.get("productName"),
            "url": (it.get("onClick") or {}).get("url"),
            "image_url": image.get("url"),
            "brand": info.get("brandName"),
            "category": entry["path"][-1],
            "price_original": original,
            "price_sale": final,
            "discount_rate": ratio,
            "review_count": None, "rating": None,
            "view_count": None, "view_count_display": None,
            "purchase_count": purchase, "purchase_count_display": purchase_display,
            "like_count": None,
            "viewers_now": viewers, "buyers_now": buyers,
            "sold_out": bool(info.get("isSoldOut", False)),
            "rank": image["rank"],
        })
    notes = [
        "updatedAt(원본 갱신 시각, epoch ms): %s" % updated_at,
        "경로 A: sections/199 + categoryCode=%s, period=REALTIME (요청 1회, top %d)"
        % (entry["code"], TOP_N),
        "price_original은 랭킹 목록 미노출 — 노출된 finalPrice·discountRatio에서 산술 복원",
        "purchase_count는 '판매 N개' 배지의 반올림 노출값(천/만 변환)",
        "review_count·rating·like_count는 랭킹 목록 미노출 → null",
    ]
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
