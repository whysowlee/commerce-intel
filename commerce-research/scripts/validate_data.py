#!/usr/bin/env python3
"""수집 JSON이 데이터 계약을 지키는지 검증하고 결측 리포트를 출력한다.

usage:
    python3 validate_data.py data/raw/musinsa-brand-linesheet-인사일런스-20260729-1500.json
    python3 validate_data.py <file> --json validation.json

exit code:
    0  PASS  — 그대로 리포트 생성 가능
    1  WARN  — 생성은 가능하나 리포트에 경고를 명시해야 함 (결측 5~30%, 총계 오차 등)
    2  FAIL  — 스키마가 깨졌거나 결측 30% 초과. 수집을 다시 하거나 원인을 먼저 확인할 것
"""

import argparse
import json
import sys

STORIES = ("brand-linesheet", "market-scan", "ranking-snapshot")

META_REQUIRED = ("site", "story", "target", "collected_at")

# 결측률 <5% 기준의 대상이 되는 필수 필드
REQUIRED_FIELDS = (
    "product_id",
    "name",
    "url",
    "image_url",
    "brand",
    "category",
    "price_original",
    "price_sale",
    "discount_rate",
    "sold_out",
)

# 사이트가 노출할 때만 값이 있는 지표. null = "미노출"이며 결측이 아니다.
# viewers_now/buyers_now는 랭킹 목록에만 있는 실시간 지표다("N명이 보는 중").
OPTIONAL_METRICS = (
    "review_count",
    "rating",
    "view_count",
    "purchase_count",
    "like_count",
    "viewers_now",
    "buyers_now",
)

# 사이트가 정확한 수 대신 구간으로만 보여줄 때("300회 이상 (최근 1개월)") 원문을 담는 칸.
# 짝이 되는 수치 필드는 null로 둔다 — 문구를 정수로 바꾸면 없는 정밀도를 만드는 것이다.
DISPLAY_FIELDS = {
    "view_count_display": "view_count",
    "purchase_count_display": "purchase_count",
}

MISSING_WARN = 5.0     # % — 이 위로는 WARN
MISSING_FAIL = 30.0    # % — 이 위로는 FAIL(사이트 구조 변경 의심)
TOTAL_TOLERANCE = 5.0  # % — 사이트 노출 총계 대비 허용 오차

# 사람이 읽는 문자열 — 인코딩이 깨지면 리포트가 통째로 못 읽게 되는 칸들
TEXT_FIELDS = ("name", "brand", "category", "view_count_display", "purchase_count_display")


def looks_mojibake(text):
    """UTF-8 바이트를 latin-1로 디코드한 흔적을 찾는다.

    2026-07-29 라인시트 수집이 상품 상세 HTML을 latin-1로 읽어 category 564행이
    전부 깨진 채 리포트까지 나갔다. 눈으로 봐야만 알 수 있는 실패였으므로 검증기가 잡는다.

    제대로 디코드된 한글은 latin-1로 인코딩이 아예 안 되므로 여기서 걸리지 않는다.
    """
    if not isinstance(text, str) or text.isascii():
        return False
    try:
        raw = text.encode("latin-1")
    except UnicodeEncodeError:
        return False
    # UTF-8 선두 바이트(0xC2~0xF4) 뒤에 연속 바이트(0x80~0xBF)가 붙어 있으면 그 흔적이다
    for idx in range(len(raw) - 1):
        if 0xC2 <= raw[idx] <= 0xF4 and 0x80 <= raw[idx + 1] <= 0xBF:
            return True
    return False


def is_missing(value):
    """필수 필드 기준의 결측 판정. null과 빈 문자열을 결측으로 본다."""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class Report:
    def __init__(self):
        self.errors = []    # FAIL 사유
        self.warnings = []  # WARN 사유
        self.infos = []     # 참고 정보

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    def info(self, msg):
        self.infos.append(msg)

    @property
    def verdict(self):
        if self.errors:
            return "FAIL"
        if self.warnings:
            return "WARN"
        return "PASS"

    @property
    def exit_code(self):
        return {"PASS": 0, "WARN": 1, "FAIL": 2}[self.verdict]


def validate_meta(meta, rep):
    if not isinstance(meta, dict):
        rep.error("meta가 객체가 아니다")
        return
    for key in META_REQUIRED:
        if is_missing(meta.get(key)):
            rep.error("meta.%s 누락" % key)
    story = meta.get("story")
    if story is not None and story not in STORIES:
        rep.error("meta.story가 알 수 없는 값이다: %r (허용: %s)" % (story, ", ".join(STORIES)))
    if meta.get("incomplete"):
        rep.warn("meta.incomplete=true — 수집이 중단된 부분 데이터다. 리포트 상단에 명시할 것")


def validate_items(items, story, rep):
    """항목별 검증. 필드별 결측 카운트를 돌려준다."""
    missing_counts = {f: 0 for f in REQUIRED_FIELDS}
    exposed_counts = {f: 0 for f in tuple(OPTIONAL_METRICS) + tuple(DISPLAY_FIELDS)}
    mojibake_counts = {f: 0 for f in TEXT_FIELDS}
    mojibake_samples = {}
    seen_ids = {}
    ranks = []
    review_total = 0

    for idx, item in enumerate(items):
        where = "items[%d]" % idx
        if not isinstance(item, dict):
            rep.error("%s가 객체가 아니다" % where)
            continue

        for field in REQUIRED_FIELDS:
            if is_missing(item.get(field)):
                missing_counts[field] += 1

        for field in TEXT_FIELDS:
            if looks_mojibake(item.get(field)):
                mojibake_counts[field] += 1
                mojibake_samples.setdefault(field, item.get(field))

        pid = item.get("product_id")
        if not is_missing(pid):
            seen_ids.setdefault(str(pid), []).append(idx)

        for field in ("url", "image_url"):
            value = item.get(field)
            if isinstance(value, str) and value.strip() and not value.startswith(("http://", "https://")):
                rep.warn("%s.%s가 절대 URL이 아니다: %r" % (where, field, value[:60]))

        original = item.get("price_original")
        sale = item.get("price_sale")
        rate = item.get("discount_rate")
        if is_number(original) and is_number(sale):
            if sale > original:
                rep.warn("%s 판매가(%s)가 정가(%s)보다 크다" % (where, sale, original))
            elif is_number(rate) and original > 0:
                expected = round((original - sale) / original * 100)
                if abs(expected - rate) > 1:
                    rep.warn(
                        "%s 할인율 불일치: 기록 %s%%, 가격에서 계산하면 %s%%"
                        % (where, rate, expected)
                    )

        rating = item.get("rating")
        if is_number(rating) and not (0 <= rating <= 5):
            rep.warn("%s 평점이 0~5 범위를 벗어난다: %s" % (where, rating))

        for field in OPTIONAL_METRICS:
            if not is_missing(item.get(field)):
                exposed_counts[field] += 1

        for display_field, exact_field in DISPLAY_FIELDS.items():
            shown = item.get(display_field)
            if is_missing(shown):
                continue
            exposed_counts[display_field] += 1
            if not isinstance(shown, str):
                rep.warn("%s.%s는 사이트 표기 문자열이어야 한다: %r" % (where, display_field, shown))
            elif is_number(item.get(exact_field)):
                rep.warn(
                    "%s에 %s와 %s가 같이 있다 — 구간 표기를 정수로 바꿔 담았는지 확인할 것"
                    % (where, exact_field, display_field)
                )

        if story == "ranking-snapshot":
            rank = item.get("rank")
            if not is_number(rank):
                rep.error("%s rank 누락 — ranking-snapshot에는 필수다" % where)
            else:
                ranks.append(int(rank))

        if story == "market-scan":
            attrs = item.get("attributes")
            if attrs is None:
                rep.warn("%s attributes 누락 — market-scan은 속성 분류가 필요하다" % where)
            # 리뷰 본문은 수집하지 않는다(2026-07-29 결정). 실수로 담겨 있으면 알린다.
            if item.get("reviews"):
                review_total += len(item["reviews"]) if isinstance(item["reviews"], list) else 0

    for pid, positions in seen_ids.items():
        if len(positions) > 1:
            msg = "product_id 중복: %s (%d회, items[%s])" % (
                pid,
                len(positions),
                ", ".join(str(p) for p in positions[:5]),
            )
            if story == "ranking-snapshot":
                rep.error(msg + " — 랭킹 스냅샷에 중복은 있을 수 없다")
            else:
                rep.warn(msg)

    if ranks:
        if len(set(ranks)) != len(ranks):
            rep.error("rank 중복이 있다")
        expected = set(range(1, len(ranks) + 1))
        if set(ranks) != expected:
            missing_ranks = sorted(expected - set(ranks))[:10]
            rep.warn(
                "rank가 1..%d로 연속되지 않는다 (빠진 순위 예: %s)"
                % (len(ranks), ", ".join(str(r) for r in missing_ranks) or "없음")
            )

    if story == "market-scan" and review_total:
        rep.warn(
            "reviews[]에 리뷰 본문 %d건이 담겨 있다 — 스토리2는 리뷰 본문을 수집하지 않는다"
            % review_total
        )

    for field, hits in mojibake_counts.items():
        if not hits:
            continue
        rep.error(
            "%s %d건이 인코딩이 깨졌다 (예: %r) — 응답 바이트를 latin-1로 읽었는지 확인할 것. "
            "UTF-8로 다시 디코드해야 하고, mojibake 문자열에 strip()을 걸면 끝 바이트가 날아간다"
            % (field, hits, mojibake_samples[field][:24])
        )

    return missing_counts, exposed_counts, review_total, mojibake_counts


def attribute_coverage(items):
    """market-scan 속성 분류율 — unknown과 결측을 제외한 비율."""
    classified = 0
    total = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        total += 1
        attrs = item.get("attributes")
        if attrs is None:
            continue
        values = attrs.values() if isinstance(attrs, dict) else [attrs]
        if any(v not in (None, "", "unknown") for v in values):
            classified += 1
    if total == 0:
        return 0.0, 0, 0
    return classified / total * 100, classified, total


def main():
    parser = argparse.ArgumentParser(description="수집 JSON 스키마 검증 및 결측 리포트")
    parser.add_argument("path", help="검증할 수집 JSON 경로")
    parser.add_argument("--json", dest="json_out", help="검증 요약을 JSON으로 저장할 경로")
    parser.add_argument(
        "--quiet", action="store_true", help="판정 줄만 출력한다"
    )
    args = parser.parse_args()

    rep = Report()

    try:
        with open(args.path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        print("FAIL — 파일이 없다: %s" % args.path)
        return 2
    except json.JSONDecodeError as exc:
        print("FAIL — JSON 파싱 실패: %s" % exc)
        return 2

    if not isinstance(data, dict):
        print("FAIL — 최상위가 객체가 아니다")
        return 2

    meta = data.get("meta", {})
    validate_meta(meta, rep)

    items = data.get("items")
    if not isinstance(items, list):
        print("FAIL — items가 배열이 아니다")
        return 2
    if not items:
        rep.error("items가 비어 있다 — 빈 리포트를 만들지 말고 수집 0건 사유를 먼저 확인할 것")

    story = meta.get("story")
    missing_counts, exposed_counts, review_total, mojibake_counts = validate_items(
        items, story, rep
    )

    count = len(items)
    declared = meta.get("item_count")
    if is_number(declared) and int(declared) != count:
        rep.warn("meta.item_count(%s)와 실제 항목 수(%d)가 다르다" % (declared, count))

    source_total = meta.get("source_total")
    total_gap = None
    if is_number(source_total) and source_total > 0:
        total_gap = abs(count - source_total) / source_total * 100
        if total_gap > TOTAL_TOLERANCE:
            rep.warn(
                "사이트 노출 총계 %s건 대비 %d건 수집 — 오차 %.1f%% (기준 ±%.0f%%)"
                % (source_total, count, total_gap, TOTAL_TOLERANCE)
            )
        else:
            rep.info("총계 대비 오차 %.1f%% — 기준 통과" % total_gap)
    else:
        rep.info("meta.source_total 미기록 — 수집 완전성을 자동 검증할 수 없다")

    # 필수 필드 결측률
    total_cells = count * len(REQUIRED_FIELDS)
    missing_cells = sum(missing_counts.values())
    overall_missing = (missing_cells / total_cells * 100) if total_cells else 0.0

    field_rates = {}
    for field, missing in missing_counts.items():
        field_rates[field] = (missing / count * 100) if count else 0.0

    if overall_missing > MISSING_FAIL:
        rep.error(
            "필수 필드 결측률 %.1f%% — %.0f%% 초과. 사이트 구조 변경을 의심할 것"
            % (overall_missing, MISSING_FAIL)
        )
    elif overall_missing > MISSING_WARN:
        rep.warn(
            "필수 필드 결측률 %.1f%% — 기준 %.0f%% 초과" % (overall_missing, MISSING_WARN)
        )

    for field, rate in field_rates.items():
        if rate > MISSING_FAIL:
            rep.error("%s 결측 %.1f%% — 해당 필드 수집 로직 점검 필요" % (field, rate))
        elif rate > MISSING_WARN:
            rep.warn("%s 결측 %.1f%%" % (field, rate))

    exposure_rates = {
        field: (exposed_counts[field] / count * 100) if count else 0.0
        for field in exposed_counts
    }

    attr_rate = None
    if story == "market-scan":
        attr_rate, classified, attr_total = attribute_coverage(items)
        if attr_rate < 80.0:
            rep.warn(
                "속성 분류율 %.1f%% (%d/%d) — 기준 80%% 미달"
                % (attr_rate, classified, attr_total)
            )
        else:
            rep.info("속성 분류율 %.1f%% (%d/%d)" % (attr_rate, classified, attr_total))

    summary = {
        "path": args.path,
        "verdict": rep.verdict,
        "site": meta.get("site"),
        "story": story,
        "target": meta.get("target"),
        "collected_at": meta.get("collected_at"),
        "item_count": count,
        "source_total": source_total if is_number(source_total) else None,
        "total_gap_pct": round(total_gap, 1) if total_gap is not None else None,
        "incomplete": bool(meta.get("incomplete")),
        "missing_overall_pct": round(overall_missing, 1),
        "missing_by_field_pct": {k: round(v, 1) for k, v in field_rates.items() if v > 0},
        "exposure_by_field_pct": {k: round(v, 1) for k, v in exposure_rates.items()},
        "attribute_coverage_pct": round(attr_rate, 1) if attr_rate is not None else None,
        "unexpected_review_bodies": review_total,
        "mojibake_by_field": {k: v for k, v in mojibake_counts.items() if v},
        "errors": rep.errors,
        "warnings": rep.warnings,
        "infos": rep.infos,
    }

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)

    if not args.quiet:
        print("=" * 60)
        print("검증 대상: %s" % args.path)
        print("사이트=%s  스토리=%s  대상=%s" % (meta.get("site"), story, meta.get("target")))
        print("항목 수: %d" % count)
        print("필수 필드 결측률: %.1f%%" % overall_missing)
        worst = sorted(field_rates.items(), key=lambda kv: -kv[1])[:5]
        for field, rate in worst:
            if rate > 0:
                print("  - %-16s %.1f%%" % (field, rate))
        print("지표 노출률:")
        for field in exposure_rates:
            print("  - %-22s %.1f%%" % (field, exposure_rates[field]))
        if attr_rate is not None:
            print("속성 분류율: %.1f%%" % attr_rate)
        print("-" * 60)
        for msg in rep.errors:
            print("[ERROR] %s" % msg)
        for msg in rep.warnings[:30]:
            print("[WARN ] %s" % msg)
        if len(rep.warnings) > 30:
            print("[WARN ] ... 외 %d건" % (len(rep.warnings) - 30))
        for msg in rep.infos:
            print("[INFO ] %s" % msg)
        print("-" * 60)

    print("판정: %s (errors=%d warnings=%d)" % (rep.verdict, len(rep.errors), len(rep.warnings)))
    return rep.exit_code


if __name__ == "__main__":
    sys.exit(main())
