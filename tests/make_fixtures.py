#!/usr/bin/env python3
"""테스트용 수집 JSON 픽스처를 만든다. 실제 사이트에 붙지 않고 스크립트만 검증하기 위한 것이다.

usage: python3 tests/make_fixtures.py [출력폴더]   (기본: tests/fixtures)
"""

import json
import os
import random
import sys

RNG = random.Random(7)  # 실행마다 같은 픽스처가 나오도록 고정한다

BRANDS = ["인사일런스", "토피", "포터리"]
CATEGORIES = ["코트", "니트", "셔츠", "팬츠"]
FITS = ["부츠컷", "와이드", "스트레이트", "슬림", "테이퍼드", "unknown"]


def product(idx, site="musinsa", brand="인사일런스", category=None, expose_views=True):
    original = RNG.choice([59000, 89000, 129000, 189000, 249000])
    rate = RNG.choice([0, 0, 10, 20, 30, 50])
    sale = round(original * (100 - rate) / 100)
    # 무신사는 조회수를 정수가 아니라 구간으로 보여준다("300회 이상 (최근 1개월)").
    # 정수 칸은 비우고 원문을 display 칸에 담는 게 계약이다.
    view_display = (
        "%s회 이상 (최근 1개월)" % format(RNG.choice([100, 300, 500, 1000, 5000]), ",")
        if expose_views
        else None
    )
    return {
        "product_id": "%s-%05d" % (site, idx),
        "name": "%s %s %02d" % (brand, category or RNG.choice(CATEGORIES), idx),
        "url": "https://example.test/%s/goods/%05d" % (site, idx),
        "image_url": "https://example.test/img/%05d.jpg" % idx,
        "brand": brand,
        "category": category or RNG.choice(CATEGORIES),
        "price_original": original,
        "price_sale": sale,
        "discount_rate": rate,
        "review_count": RNG.choice([0, 3, 41, 260, 1502]),
        "rating": round(RNG.uniform(3.8, 5.0), 1),
        "view_count": None,
        "view_count_display": view_display,
        "purchase_count": RNG.choice([None, RNG.randint(10, 4000)]),
        "like_count": RNG.randint(0, 12000),
        "sold_out": RNG.random() < 0.15,
    }


def write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def build(out_dir):
    made = []

    # F1-1 정상 브랜드 라인시트 (무신사)
    items = [product(i, "musinsa") for i in range(1, 25)]
    items[3]["sold_out"] = True
    items[7]["view_count_display"] = None  # F1-4 완전 미노출 값
    # E-OUT-1: 사이트 텍스트에 HTML이 섞여 와도 그대로 실행되면 안 된다
    items[5]["name"] = '인사일런스 니트 <script>alert("xss")</script> 06'
    made.append(
        write(
            os.path.join(out_dir, "musinsa-brand-linesheet-good.json"),
            {
                "meta": {
                    "site": "musinsa",
                    "story": "brand-linesheet",
                    "target": "인사일런스",
                    "collected_at": "2026-07-29 14:00:00",
                    "item_count": len(items),
                    "source_total": 24,
                    "incomplete": False,
                    "notes": [],
                },
                "items": items,
            },
        )
    )

    # F1-5 크로스 플랫폼용 29cm (조회수 미노출 사이트를 가정)
    items29 = [product(i, "29cm", expose_views=False) for i in range(100, 114)]
    made.append(
        write(
            os.path.join(out_dir, "29cm-brand-linesheet-good.json"),
            {
                "meta": {
                    "site": "29cm",
                    "story": "brand-linesheet",
                    "target": "인사일런스",
                    "collected_at": "2026-07-29 14:20:00",
                    "item_count": len(items29),
                    "source_total": 14,
                    "incomplete": False,
                    "notes": [],
                },
                "items": items29,
            },
        )
    )

    # R3 레이아웃 변경 모사 — 필수 필드를 대량으로 비운다
    broken = [product(i, "musinsa") for i in range(1, 21)]
    for item in broken[:14]:
        item["name"] = None
        item["image_url"] = ""
        item["price_original"] = None
        item["price_sale"] = None
        item["discount_rate"] = None
    made.append(
        write(
            os.path.join(out_dir, "musinsa-brand-linesheet-broken.json"),
            {
                "meta": {
                    "site": "musinsa",
                    "story": "brand-linesheet",
                    "target": "인사일런스",
                    "collected_at": "2026-07-29 15:00:00",
                    "item_count": len(broken),
                    "incomplete": False,
                },
                "items": broken,
            },
        )
    )

    # R1 차단으로 중단된 부분 수집
    partial = [product(i, "musinsa") for i in range(1, 8)]
    # 구간 표기를 정수로 바꿔 담은 실수를 심어둔다 — 검증기가 잡아야 한다
    partial[1]["view_count"] = 300
    made.append(
        write(
            os.path.join(out_dir, "musinsa-brand-linesheet-partial.json"),
            {
                "meta": {
                    "site": "musinsa",
                    "story": "brand-linesheet",
                    "target": "인사일런스",
                    "collected_at": "2026-07-29 15:10:00",
                    "item_count": len(partial),
                    "source_total": 24,
                    "incomplete": True,
                    "notes": ["3페이지에서 429 응답 — 우회하지 않고 중단"],
                },
                "items": partial,
            },
        )
    )

    # F1-2 검색 0건
    made.append(
        write(
            os.path.join(out_dir, "musinsa-brand-linesheet-empty.json"),
            {
                "meta": {
                    "site": "musinsa",
                    "story": "brand-linesheet",
                    "target": "아사드프qwe",
                    "collected_at": "2026-07-29 15:20:00",
                    "item_count": 0,
                    "source_total": 0,
                    "incomplete": False,
                    "notes": ["브랜드 검색 결과 0건"],
                },
                "items": [],
            },
        )
    )

    # F2-1 시장 전수조사 — 리뷰 본문 없이 노출된 후기 수·평점만 담는다(2026-07-29 결정)
    scan = []
    for i in range(200, 218):
        item = product(i, "musinsa", brand=RNG.choice(BRANDS), category="데님팬츠")
        fit = FITS[i % len(FITS)]
        item["attributes"] = {"핏": fit}
        item["attributes_basis"] = "image" if fit != "unknown" else "unknown"
        scan.append(item)
    scan[2]["attributes"] = {"핏": "unknown"}  # F2-3 분류 불가
    scan[2]["attributes_basis"] = "unknown"
    scan[4]["review_count"] = 0        # F2-2 후기 0건 상품
    scan[6]["rating"] = None           # 평점 미노출 상품
    made.append(
        write(
            os.path.join(out_dir, "musinsa-market-scan.json"),
            {
                "meta": {
                    "site": "musinsa",
                    "story": "market-scan",
                    "target": "데님팬츠",
                    "collected_at": "2026-07-29 16:00:00",
                    "item_count": len(scan),
                    "source_total": 18,
                    "incomplete": False,
                    "notes": [],
                },
                "items": scan,
            },
        )
    )

    # F3 랭킹 스냅샷 3개 — 진입/이탈/급등/할인 시작을 심어둔다
    snap_dir = os.path.join(out_dir, "snapshots")
    base = [product(i, "musinsa", category="팬츠") for i in range(300, 330)]
    for item in base:
        item["discount_rate"] = 0
        item["price_sale"] = item["price_original"]

    # 심어둔 변화: 30·31번 신규 진입, 2번 이탈, 24번 급상승(25위→1위),
    # 5번 급하락(6위→29위), 3번 할인 시작. 0·1번은 중간 스냅샷에만 빠져 추이에 결측이 생긴다.
    plans = [
        ("2026-03-01 09:00:00", list(range(30))),
        ("2026-03-15 09:00:00", list(range(2, 30)) + [30, 31]),
        (
            "2026-03-31 09:00:00",
            [24, 0, 1] + list(range(3, 5)) + list(range(6, 24)) + list(range(25, 30)) + [5, 30, 31],
        ),
    ]
    extra = [product(i, "musinsa", category="팬츠") for i in (330, 331)]
    for item in extra:
        item["discount_rate"] = 0
        item["price_sale"] = item["price_original"]
    pool = base + extra

    for stamp, order in plans:
        items = []
        for rank, pool_idx in enumerate(order, start=1):
            item = json.loads(json.dumps(pool[pool_idx]))
            item["rank"] = rank
            # 랭킹 목록에만 붙는 실시간 지표. 일부 항목은 아예 안 뜬다(미노출 = null).
            item["viewers_now"] = RNG.randint(20, 1800) if rank % 5 else None
            item["buyers_now"] = RNG.randint(3, 90) if rank % 7 == 0 else None
            # 마지막 스냅샷에서 3번 상품에 할인이 들어간다
            if stamp.startswith("2026-03-31") and pool_idx == 3:
                item["discount_rate"] = 30
                item["price_sale"] = round(item["price_original"] * 0.7)
            items.append(item)
        made.append(
            write(
                os.path.join(snap_dir, "musinsa-ranking-바지-%s.json" % stamp[:10].replace("-", "")),
                {
                    "meta": {
                        "site": "musinsa",
                        "story": "ranking-snapshot",
                        "target": "바지",
                        "collected_at": stamp,
                        "item_count": len(items),
                        "source_total": len(items),
                        "incomplete": False,
                        "notes": [],
                    },
                    "items": items,
                },
            )
        )

    # 기간 밖 스냅샷 — 기간 필터가 실제로 거르는지 보려고 둔다
    outside = json.loads(json.dumps(pool[0]))
    outside["rank"] = 1
    made.append(
        write(
            os.path.join(snap_dir, "musinsa-ranking-바지-20260601.json"),
            {
                "meta": {
                    "site": "musinsa",
                    "story": "ranking-snapshot",
                    "target": "바지",
                    "collected_at": "2026-06-01 09:00:00",
                    "item_count": 1,
                    "incomplete": False,
                },
                "items": [outside],
            },
        )
    )

    return made


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "fixtures")
    paths = build(target)
    print("픽스처 %d개 생성: %s" % (len(paths), target))
