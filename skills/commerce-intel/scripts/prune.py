#!/usr/bin/env python3
"""오래된 관측을 솎는다 — **값이 바뀐 순간은 무조건 남긴다** (D45).

    python3 prune.py --db data/intel.db --dry-run     # 얼마나 줄지 먼저 본다
    python3 prune.py --db data/intel.db --apply

## 규칙 (사용자 결정 2026-08-04)

기본은 "오래되면 간격을 드물게"인데, 그것만 하면 **가격이 올랐다 내린 순간이 사라진다.**
이 프로젝트의 분석 절반(이중차분·용량반응·가격 변경 사건)이 그 순간을 본다.
그래서 예외를 하나 둔다 — **앞 관측과 값이 다르면 간격과 무관하게 남긴다.**

    ① 최근 N일(기본 30) 관측은 손대지 않는다
    ② 그보다 오래된 것은 **버킷당 1개**만 남긴다 (기본 1시간)
    ③ 단, 아래는 버킷과 무관하게 **무조건 남긴다** — 어떤 지표든 앞 관측과
       다르면 그 순간은 사건이다 (2026-08-06 확장: 가격·순위·품절만 보던 것을
       전 지표 + 비정형(obs_attr)으로)
       · 판매가·할인율 / 순위·품절 / 후기수·하트·구매수 / 조회수·보는중·구매중
         / 정가·평점 — 하나라도 앞 관측과 다르다
       · obs_attr(비정형 지표) 묶음이 앞 관측과 다르다
       · 그 상품×문맥의 **첫 관측과 마지막 관측**

    CASE 분기는 자주 바뀌는 지표 순서다(판매가 → 순위 → 후기 → 조회 → 정가) —
    앞에서 'change'로 판정되면 뒤는 평가하지 않는다.

③ 덕분에 "언제 얼마에서 얼마로 바뀌었나"는 원본 그대로 남는다. 지워지는 것은
**아무것도 안 변한 구간의 중복 관측**뿐이다.

## 지운 것은 돌아오지 않는다

`--dry-run`이 기본이고 `--apply`를 명시해야 실제로 지운다. 지우기 전에 몇 건이
어느 사유로 남는지 찍는다 — 조용히 줄어든 데이터는 나중에 "원래 그만큼이었나"와
구분되지 않는다.
"""
import argparse
import os
import sqlite3
import sys

KEEP_DAYS = 30        # 이보다 최근은 손대지 않는다
BUCKET_SEC = 3600     # 오래된 구간은 이 간격당 1개만


def _has_v2(conn):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='obs_base'").fetchone())


def plan(conn, keep_days=KEEP_DAYS, bucket=BUCKET_SEC):
    """지울 id 목록과 사유별 집계. **남길 이유를 먼저 세고, 나머지를 지운다.**

    반대로 하면(지울 이유를 세면) 규칙을 하나 빠뜨렸을 때 데이터가 사라진다.
    남길 이유를 세는 쪽이 빠뜨렸을 때 안전하다 — 덜 지울 뿐이다.
    """
    cutoff = conn.execute(
        "SELECT CAST(strftime('%%s','now','-%d days') AS INTEGER)" % keep_days).fetchone()[0]

    # 값이 바뀐 지점 · 첫·끝 · 버킷 대표를 한 번에 판정한다.
    # LAG로 앞 관측과 비교하므로 상품×문맥 안에서 시각 순으로 훑어야 한다.
    # obs_attr는 관측당 묶음 서명(이름=값을 이름순으로 이어붙임)으로 접어 비교한다
    # — 행 단위로 비교하면 "지표가 사라진" 변화를 놓친다.
    conn.execute("DROP TABLE IF EXISTS _prune_keep")
    conn.execute("""
        CREATE TEMP TABLE _prune_keep AS
        WITH oa_sig AS (
            SELECT obs_id, GROUP_CONCAT(attr_name || '=' || COALESCE(value,''), '|') AS sig
            FROM (SELECT obs_id, attr_name, value FROM obs_attr
                  ORDER BY obs_id, attr_name)
            GROUP BY obs_id
        ),
        seq AS (
            SELECT o.id, o.pk, o.context_id, o.observed_at,
                   o.price_original, o.price_sale, o.discount_rate, o.rank, o.sold_out,
                   o.review_count, o.rating, o.view_count, o.purchase_count,
                   o.like_count, o.viewers_now, o.buyers_now,
                   s.sig AS oa,
                   LAG(o.price_original) OVER w AS p0,
                   LAG(o.price_sale)     OVER w AS p1,
                   LAG(o.discount_rate)  OVER w AS p2,
                   LAG(o.rank)           OVER w AS p3,
                   LAG(o.sold_out)       OVER w AS p4,
                   LAG(o.review_count)   OVER w AS p5,
                   LAG(o.like_count)     OVER w AS p6,
                   LAG(o.purchase_count) OVER w AS p7,
                   LAG(o.view_count)     OVER w AS p8,
                   LAG(o.viewers_now)    OVER w AS p9,
                   LAG(o.buyers_now)     OVER w AS p10,
                   LAG(o.rating)         OVER w AS p11,
                   LAG(s.sig)            OVER w AS oa0,
                   ROW_NUMBER() OVER w AS rn,
                   COUNT(*)     OVER (PARTITION BY o.pk, o.context_id) AS cnt,
                   ROW_NUMBER() OVER (PARTITION BY o.pk, o.context_id,
                                      o.observed_at / ?) AS in_bucket
            FROM obs_base o LEFT JOIN oa_sig s ON s.obs_id = o.id
            WINDOW w AS (PARTITION BY o.pk, o.context_id ORDER BY o.observed_at)
        )
        SELECT id,
               CASE
                 WHEN observed_at >= ?                       THEN 'recent'
                 WHEN rn = 1 OR rn = cnt                     THEN 'edge'
                 -- 파티션 첫 행은 위 'edge'가 이미 잡으므로 `p1 IS NULL` 분기는
                 -- 도달하지 않는다 (PR #9 리뷰). 아래 분기는 자주 바뀌는 지표
                 -- 순서다 — 앞에서 'change'면 뒤는 평가하지 않는다
                 WHEN price_sale IS NOT p1
                   OR discount_rate IS NOT p2                 THEN 'change'
                 WHEN rank IS NOT p3 OR sold_out IS NOT p4    THEN 'change'
                 WHEN review_count IS NOT p5 OR like_count IS NOT p6
                   OR purchase_count IS NOT p7                THEN 'change'
                 WHEN view_count IS NOT p8 OR viewers_now IS NOT p9
                   OR buyers_now IS NOT p10                   THEN 'change'
                 WHEN price_original IS NOT p0
                   OR rating IS NOT p11                       THEN 'change'
                 WHEN oa IS NOT oa0                           THEN 'change'
                 WHEN in_bucket = 1                           THEN 'bucket'
                 ELSE NULL
               END AS why
        FROM seq
    """, (bucket, cutoff))
    counts = dict(conn.execute(
        "SELECT COALESCE(why,'(지움)'), COUNT(*) FROM _prune_keep GROUP BY 1").fetchall())
    total = conn.execute("SELECT COUNT(*) FROM obs_base").fetchone()[0]
    return counts, total


def apply_prune(conn):
    cur = conn.execute("DELETE FROM obs_base WHERE id IN "
                       "(SELECT id FROM _prune_keep WHERE why IS NULL)")
    conn.commit()
    return cur.rowcount


def main():
    ap = argparse.ArgumentParser(description="오래된 관측 솎기 — 변화 순간은 보존 (D45)")
    ap.add_argument("--db", default=os.environ.get("INTEL_DB", "data/intel.db"))
    ap.add_argument("--keep-days", type=int, default=KEEP_DAYS)
    ap.add_argument("--bucket-minutes", type=int, default=BUCKET_SEC // 60)
    ap.add_argument("--apply", action="store_true", help="실제로 지운다 (없으면 예행)")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    # FK는 커넥션 설정 — 안 켜면 관측 삭제 시 obs_attr CASCADE가 조용히 안 돌아
    # 고아 행이 남는다 (스키마 obs_attr 주석이 전제하는 그 동작이다)
    conn.execute("PRAGMA foreign_keys = ON")
    if not _has_v2(conn):
        print("v2 스키마가 아니다 — migrate_v2.py를 먼저 돌려라.", file=sys.stderr)
        return 3
    before = os.path.getsize(a.db)
    counts, total = plan(conn, a.keep_days, a.bucket_minutes * 60)
    drop = counts.get("(지움)", 0)
    print("관측 %s행 · 최근 %d일 보존 · 그 이전은 %d분당 1개" % (
        "{:,}".format(total), a.keep_days, a.bucket_minutes))
    for k, label in (("recent", "최근이라 그대로"), ("change", "값이 바뀐 관측"),
                     ("edge", "첫·마지막 관측"), ("bucket", "구간 대표")):
        print("  남김 %-14s %s" % (label, "{:,}".format(counts.get(k, 0))))
    print("  지움 %-14s %s (%.1f%%)" % ("변화 없는 중복", "{:,}".format(drop),
                                      100 * drop / (total or 1)))
    if not a.apply:
        print("\n예행이다. 실제로 지우려면 --apply 를 붙여라.")
        return 0
    n = apply_prune(conn)
    conn.execute("VACUUM")
    conn.close()
    after = os.path.getsize(a.db)
    print("\n%s행 삭제 · %.2fMB → %.2fMB" % ("{:,}".format(n),
                                          before / 1048576, after / 1048576))
    return 0


if __name__ == "__main__":
    sys.exit(main())
