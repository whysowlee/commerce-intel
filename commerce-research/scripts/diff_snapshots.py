#!/usr/bin/env python3
"""랭킹 스냅샷들을 기간으로 골라 비교하고, 변화 리포트용 diff JSON을 만든다.

usage:
    python3 diff_snapshots.py data/snapshots --from 2026-03-01 --to 2026-03-31 --out data/diff.json
    python3 diff_snapshots.py data/snapshots --site musinsa --target 바지 --out data/diff.json

기간 경계는 --from 00:00, --to 23:59:59 를 포함한다(끝날 포함).
--from/--to를 생략하면 조건에 맞는 모든 스냅샷을 쓴다("전체 구간").

exit code:
    0  diff 생성됨 (스냅샷 2개 이상)
    1  비교 대상 부족 — 스냅샷이 1개 이하다. 현재 스냅샷 리포트만 만들 것
    2  입력 오류
"""

import argparse
import json
import os
import sys
from datetime import datetime

# 급상승/급하락 강조 기준: 랭킹 길이의 10%, 최소 3계단 (2026-07-29 확정).
# top100이면 10계단. 강조 표시에만 쓰이고 진입/이탈·전체 변동 표는 임계값과 무관하다.
BIG_MOVE_RATIO = 0.10
BIG_MOVE_MIN = 3


def big_move_threshold(ranking_size):
    return max(BIG_MOVE_MIN, round(ranking_size * BIG_MOVE_RATIO))


def parse_dt(value):
    """수집 시각 문자열을 naive datetime으로 읽는다.

    스냅샷은 같은 환경에서 쌓이므로 벽시계 기준으로 비교한다.
    타임존 표기가 있으면 떼어내고 표기된 지역 시각 그대로 쓴다.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "").replace("/", "-")
    if "+" in text[10:]:
        text = text[:10] + text[10:].split("+")[0]
    elif text[10:].count("-") > 0 and "T" in text:
        head, tail = text.split("T", 1)
        tail = tail.split("-")[0]
        text = head + "T" + tail
    text = text.replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d-%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_bound(value, end_of_day):
    if not value:
        return None
    dt = parse_dt(value)
    if dt is None:
        raise ValueError("날짜를 해석할 수 없다: %r (YYYY-MM-DD 형식을 쓸 것)" % value)
    if end_of_day and dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt


def collect_files(path):
    if os.path.isfile(path):
        return [path]
    if not os.path.isdir(path):
        raise ValueError("경로가 없다: %s" % path)
    found = []
    for name in sorted(os.listdir(path)):
        if name.endswith(".json"):
            found.append(os.path.join(path, name))
    return found


def load_snapshots(paths, site, target, start, end):
    snapshots = []
    skipped = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            skipped.append((path, "읽기 실패: %s" % exc))
            continue
        meta = data.get("meta") or {}
        if meta.get("story") != "ranking-snapshot":
            skipped.append((path, "story가 ranking-snapshot이 아니다"))
            continue
        if site and meta.get("site") != site:
            skipped.append((path, "사이트 불일치"))
            continue
        if target and str(meta.get("target")) != str(target):
            skipped.append((path, "대상 불일치"))
            continue
        when = parse_dt(meta.get("collected_at"))
        if when is None:
            skipped.append((path, "collected_at 해석 불가"))
            continue
        if start and when < start:
            skipped.append((path, "기간 이전"))
            continue
        if end and when > end:
            skipped.append((path, "기간 이후"))
            continue
        items = data.get("items") or []
        snapshots.append(
            {
                "path": path,
                "at": when,
                "meta": meta,
                "items": [i for i in items if isinstance(i, dict)],
            }
        )
    snapshots.sort(key=lambda s: s["at"])
    return snapshots, skipped


def index_by_id(items):
    table = {}
    for item in items:
        pid = item.get("product_id")
        if pid is None:
            continue
        table[str(pid)] = item
    return table


def brief(item, rank=None):
    return {
        "product_id": str(item.get("product_id")),
        "name": item.get("name"),
        "brand": item.get("brand"),
        "url": item.get("url"),
        "image_url": item.get("image_url"),
        "price_sale": item.get("price_sale"),
        "price_original": item.get("price_original"),
        "discount_rate": item.get("discount_rate"),
        "rank": item.get("rank") if rank is None else rank,
    }


def as_number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def classify_price_change(before, after):
    """두 시점의 가격/할인 상태를 보고 변화 종류를 정한다."""
    d_before = as_number(before.get("discount_rate")) or 0
    d_after = as_number(after.get("discount_rate")) or 0
    p_before = as_number(before.get("price_sale"))
    p_after = as_number(after.get("price_sale"))

    if d_before == 0 and d_after > 0:
        return "discount_started"
    if d_before > 0 and d_after == 0:
        return "discount_ended"
    if d_after > d_before:
        return "discount_deepened"
    if d_before > d_after > 0:
        return "discount_reduced"
    if p_before is not None and p_after is not None and p_after != p_before:
        return "price_down" if p_after < p_before else "price_up"
    return None


def build_diff(snapshots):
    first, last = snapshots[0], snapshots[-1]
    first_idx = index_by_id(first["items"])
    last_idx = index_by_id(last["items"])

    entered = [brief(item) for pid, item in last_idx.items() if pid not in first_idx]
    entered.sort(key=lambda x: (x["rank"] is None, x["rank"]))
    exited = [brief(item) for pid, item in first_idx.items() if pid not in last_idx]
    exited.sort(key=lambda x: (x["rank"] is None, x["rank"]))

    movers = []
    for pid, item in last_idx.items():
        if pid not in first_idx:
            continue
        rank_first = as_number(first_idx[pid].get("rank"))
        rank_last = as_number(item.get("rank"))
        if rank_first is None or rank_last is None:
            continue
        record = brief(item)
        record["rank_first"] = int(rank_first)
        record["rank_last"] = int(rank_last)
        record["delta"] = int(rank_first - rank_last)  # 양수 = 상승
        movers.append(record)
    movers.sort(key=lambda m: -abs(m["delta"]))

    # 연속 스냅샷 쌍을 훑어 가격·할인 변화를 잡는다
    price_changes = []
    for older, newer in zip(snapshots, snapshots[1:]):
        older_idx = index_by_id(older["items"])
        for pid, item in index_by_id(newer["items"]).items():
            if pid not in older_idx:
                continue
            kind = classify_price_change(older_idx[pid], item)
            if not kind:
                continue
            price_changes.append(
                {
                    "product_id": pid,
                    "name": item.get("name"),
                    "brand": item.get("brand"),
                    "url": item.get("url"),
                    "image_url": item.get("image_url"),
                    "kind": kind,
                    "at": newer["at"].isoformat(sep=" "),
                    "from": {
                        "price_sale": older_idx[pid].get("price_sale"),
                        "discount_rate": older_idx[pid].get("discount_rate"),
                    },
                    "to": {
                        "price_sale": item.get("price_sale"),
                        "discount_rate": item.get("discount_rate"),
                    },
                }
            )
    price_changes.sort(key=lambda c: c["at"])

    # 상품별 순위 추이
    trends = {}
    for snap in snapshots:
        stamp = snap["at"].isoformat(sep=" ")
        for item in snap["items"]:
            pid = item.get("product_id")
            if pid is None:
                continue
            pid = str(pid)
            entry = trends.setdefault(
                pid,
                {
                    "product_id": pid,
                    "name": item.get("name"),
                    "brand": item.get("brand"),
                    "url": item.get("url"),
                    "series": [],
                },
            )
            entry["name"] = entry["name"] or item.get("name")
            entry["series"].append(
                {
                    "at": stamp,
                    "rank": as_number(item.get("rank")),
                    "price_sale": item.get("price_sale"),
                    "discount_rate": item.get("discount_rate"),
                    "viewers_now": as_number(item.get("viewers_now")),
                    "buyers_now": as_number(item.get("buyers_now")),
                }
            )

    threshold = big_move_threshold(max(len(first["items"]), len(last["items"])))
    big_risers = [m for m in movers if m["delta"] >= threshold]
    big_fallers = [m for m in movers if m["delta"] <= -threshold]

    return {
        "meta": {
            "story": "ranking-diff",
            "site": last["meta"].get("site"),
            "target": last["meta"].get("target"),
            "period": {
                "start": first["at"].isoformat(sep=" "),
                "end": last["at"].isoformat(sep=" "),
            },
            "snapshots": [
                {
                    "path": s["path"],
                    "collected_at": s["at"].isoformat(sep=" "),
                    "item_count": len(s["items"]),
                    "incomplete": bool(s["meta"].get("incomplete")),
                }
                for s in snapshots
            ],
        },
        "summary": {
            "snapshot_count": len(snapshots),
            "first_count": len(first["items"]),
            "last_count": len(last["items"]),
            "entered": len(entered),
            "exited": len(exited),
            "stayed": len(movers),
            "big_risers": len(big_risers),
            "big_fallers": len(big_fallers),
            "big_move_threshold": threshold,
            "price_change_events": len(price_changes),
            "discount_started": len([c for c in price_changes if c["kind"] == "discount_started"]),
        },
        "entered": entered,
        "exited": exited,
        "movers": movers,
        "price_changes": price_changes,
        "trends": sorted(trends.values(), key=lambda t: t["product_id"]),
    }


def main():
    parser = argparse.ArgumentParser(description="랭킹 스냅샷 기간 비교")
    parser.add_argument("path", help="스냅샷 폴더 또는 파일")
    parser.add_argument("--from", dest="start", help="기간 시작 YYYY-MM-DD (포함)")
    parser.add_argument("--to", dest="end", help="기간 끝 YYYY-MM-DD (포함)")
    parser.add_argument("--site", help="사이트로 거른다 (예: musinsa)")
    parser.add_argument("--target", help="카테고리로 거른다 (예: 바지)")
    parser.add_argument("--out", required=True, help="diff JSON 저장 경로")
    args = parser.parse_args()

    try:
        start = parse_bound(args.start, end_of_day=False)
        end = parse_bound(args.end, end_of_day=True)
        paths = collect_files(args.path)
    except ValueError as exc:
        print("입력 오류 — %s" % exc)
        return 2

    if start and end and start > end:
        print("입력 오류 — 기간 시작이 끝보다 늦다")
        return 2

    snapshots, skipped = load_snapshots(paths, args.site, args.target, start, end)

    period_label = "%s ~ %s" % (args.start or "처음", args.end or "마지막")
    print("기간 %s / 후보 파일 %d개 중 스냅샷 %d개 선택" % (period_label, len(paths), len(snapshots)))
    for path, reason in skipped[:10]:
        print("  건너뜀: %s (%s)" % (os.path.basename(path), reason))
    if len(skipped) > 10:
        print("  건너뜀: ... 외 %d개" % (len(skipped) - 10))

    if len(snapshots) < 2:
        print(
            "비교 대상 부족 — 기간 내 스냅샷이 %d개다. "
            "빈 diff를 만들지 않는다. 현재 스냅샷 리포트만 생성할 것." % len(snapshots)
        )
        return 1

    diff = build_diff(snapshots)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(diff, handle, ensure_ascii=False, indent=2)

    s = diff["summary"]
    print(
        "스냅샷 %d개 (%s ~ %s): 신규 %d · 이탈 %d · 유지 %d · 급상승 %d · 급하락 %d · 가격변화 %d건"
        % (
            s["snapshot_count"],
            diff["meta"]["period"]["start"],
            diff["meta"]["period"]["end"],
            s["entered"],
            s["exited"],
            s["stayed"],
            s["big_risers"],
            s["big_fallers"],
            s["price_change_events"],
        )
    )
    print("저장: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
