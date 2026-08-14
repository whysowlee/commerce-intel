#!/usr/bin/env python3
"""⚠️ 폐기됨 (2026-08-13) — 이 스크립트는 시트가 아니라 Turso를 손대간다.

정본이 Google Sheets로 옮겨서(intel-query SKILL.md v2.0) 여기서 아무리 실행해도
정본에는 아무 효과가 없다. 시트 쪽 정리는 Aside 루틴 `vlWHt4HN0nYwK0vj`가
계산해 보고하고, 삭제는 사용자 승인 후에만 한다.
자세한 배경과 대체 경로는 `legacy_guard.py` 문서 주석을 보라.
내부 함수(plan / apply_prune)는 회귀 테스트가 그대로 쓰므로 남겨둔다 —
막힌 것은 main()이다.

--- 이하 원문 ---

오래된 관측을 솎는다 — **값이 바뀐 순간은 무조건 남긴다** (D45).

    python3 prune.py --dry-run                        # 얼마나 줄지 먼저 본다
    python3 prune.py --apply                          # 실제로 지운다
    python3 prune.py --cron                           # 주간 자동 실행 crontab 줄 출력

대상 DB는 INTEL_DB_URL(Turso) > INTEL_DB > data/intel.db 순이다 (D72와 같은
규칙). Turso 정본에도 직접 돈다 (D75) — 연결은 schema_v3.open_db() 단일 통로,
삭제는 id 목록을 클라이언트로 가져와 청크 DELETE로 나눠 보낸다(임시 테이블
없음 — 원격 커넥션의 세션 상태에 기대지 않는다).

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

시트 미러: obs_base 삭제는 mirror_dirty 트리거(D72)에 잡혀 다음 동기화 때
해당 탭이 전체 재구축된다 — 솎은 결과가 시트에도 따라간다.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_v3
from legacy_guard import block_if_legacy

KEEP_DAYS = 30        # 이보다 최근은 손대지 않는다
BUCKET_SEC = 3600     # 오래된 구간은 이 간격당 1개만
DELETE_CHUNK = 400    # 청크당 id 수 — Turso 왕복 한 번의 문장 크기 상한


def _has_v2(conn):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='obs_base'").fetchone())


def plan(conn, keep_days=KEEP_DAYS, bucket=BUCKET_SEC):
    """(사유별 집계, 전체 행 수, 지울 id 목록)을 돌려준다.
    **남길 이유를 먼저 세고, 나머지를 지운다.**

    반대로 하면(지울 이유를 세면) 규칙을 하나 빠뜨렸을 때 데이터가 사라진다.
    남길 이유를 세는 쪽이 빠뜨렸을 때 안전하다 — 덜 지울 뿐이다.

    임시 테이블을 쓰지 않는다 — 판정 전체가 SELECT 한 문장이고, 결과(id·사유)를
    클라이언트에서 접는다. 로컬 파일과 Turso 원격이 같은 경로를 탄다 (D75).
    """
    cutoff = conn.execute(
        "SELECT CAST(strftime('%%s','now','-%d days') AS INTEGER)" % keep_days).fetchone()[0]

    # 값이 바뀐 지점 · 첫·끝 · 버킷 대표를 한 번에 판정한다.
    # LAG로 앞 관측과 비교하므로 상품×문맥 안에서 시각 순으로 훑어야 한다.
    # obs_attr는 관측당 묶음 서명(이름=값을 이름순으로 이어붙임)으로 접어 비교한다
    # — 행 단위로 비교하면 "지표가 사라진" 변화를 놓친다.
    rows = conn.execute("""
        WITH oa_sig AS (
            -- 구분자는 제어문자(RS/US)다 — '|'나 '='를 쓰면 값에 그 문자가 든
            -- 다른 속성 집합이 같은 서명이 되어 진짜 변화가 지워질 수 있다
            -- (1R 리뷰 실증: {a:'1|b=2'} == {a:'1', b:'2'}). 서브쿼리 정렬이
            -- GROUP_CONCAT 순서를 보장하진 않지만, 순서가 어긋나면 서명이
            -- 달라져 **더 남길 뿐**이다 — 안전한 방향이라 그대로 둔다.
            SELECT obs_id, GROUP_CONCAT(attr_name || char(31) || COALESCE(value,''),
                                        char(30)) AS sig
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
    """, (bucket, cutoff)).fetchall()

    counts, drop_ids = {}, []
    for r in rows:
        why = r["why"]
        counts["(지움)" if why is None else why] = \
            counts.get("(지움)" if why is None else why, 0) + 1
        if why is None:
            drop_ids.append(r["id"])
    return counts, len(rows), drop_ids


def apply_prune(conn, drop_ids):
    """청크로 나눠 지운다. obs_attr를 먼저 명시 삭제한다 — FK CASCADE가 받쳐
    주지만, 원격 백엔드에서 pragma가 다르게 굴러도 고아 행이 안 남게 양쪽에서
    지운다 (명시 삭제가 정본, CASCADE는 보험)."""
    n = 0
    for i in range(0, len(drop_ids), DELETE_CHUNK):
        chunk = drop_ids[i:i + DELETE_CHUNK]
        ph = ",".join("?" * len(chunk))
        conn.execute("DELETE FROM obs_attr WHERE obs_id IN (%s)" % ph, tuple(chunk))
        conn.execute("DELETE FROM obs_base WHERE id IN (%s)" % ph, tuple(chunk))
        n += len(chunk)
    conn.commit()
    return n


def cron_line():
    """주간 자동 솎기 crontab 등록 줄 (D75). 일요일 04:10 — 랭킹 스냅샷(:07·:37)과
    분 단위가 겹치지 않는다. env를 소싱해 Turso 정본을 대상으로 돈다."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    return ("10 4 * * 0 /bin/zsh -c 'source ~/.config/intel/env && "
            "cd %s && .venv/bin/python3 skills/commerce-intel/scripts/prune.py --apply' "
            ">> %s/data/prune-cron.log 2>&1 # commerce-intel-prune-weekly"
            % (repo, repo))


def main():
    # 폐기된 경로다 (2026-08-13). --cron으로 crontab 줄만 뽑는 것까지 막는다 —
    # 그 줄을 등록하면 폐기된 경로가 다시 주간로 실행된다.
    ap = argparse.ArgumentParser(description="오래된 관측 솎기 — 변화 순간은 보존 (D45)")
    ap.add_argument("--db", default=schema_v3.default_db_target(),
                    help="경로 또는 libsql:// URL (기본: INTEL_DB_URL > INTEL_DB > data/intel.db)")
    ap.add_argument("--keep-days", type=int, default=KEEP_DAYS)
    ap.add_argument("--bucket-minutes", type=int, default=BUCKET_SEC // 60)
    ap.add_argument("--apply", action="store_true", help="실제로 지운다 (없으면 예행)")
    ap.add_argument("--cron", action="store_true", help="주간 자동 실행 crontab 줄만 출력")
    a = ap.parse_args()

    # Turso를 가리키면 막는다. --cron은 대상과 무관하게 막는다 — 그 줄을
    # crontab에 넣으면 폐기된 경로가 다시 주간으로 돌아온다.
    block_if_legacy("prune.py", None if a.cron else a.db)

    if a.cron:
        print(cron_line())
        return 0

    local = not schema_v3.is_libsql_url(a.db)
    conn = schema_v3.open_db(a.db)   # FK pragma는 open_db가 켠다 (D69)
    if not _has_v2(conn):
        print("v2 스키마가 아니다 — migrate_v2.py를 먼저 돌려라.", file=sys.stderr)
        return 3
    before = os.path.getsize(a.db) if local else None
    counts, total, drop_ids = plan(conn, a.keep_days, a.bucket_minutes * 60)
    drop = len(drop_ids)
    print("대상 %s · 관측 %s행 · 최근 %d일 보존 · 그 이전은 %d분당 1개" % (
        a.db if not local else os.path.abspath(a.db),
        "{:,}".format(total), a.keep_days, a.bucket_minutes))
    for k, label in (("recent", "최근이라 그대로"), ("change", "값이 바뀐 관측"),
                     ("edge", "첫·마지막 관측"), ("bucket", "구간 대표")):
        print("  남김 %-14s %s" % (label, "{:,}".format(counts.get(k, 0))))
    print("  지움 %-14s %s (%.1f%%)" % ("변화 없는 중복", "{:,}".format(drop),
                                      100 * drop / (total or 1)))
    if not a.apply:
        print("\n예행이다. 실제로 지우려면 --apply 를 붙여라.")
        return 0
    n = apply_prune(conn, drop_ids)
    if local:
        conn.execute("VACUUM")   # 원격(Turso)은 서버가 공간을 관리한다 — 안 보낸다
        conn.close()
        after = os.path.getsize(a.db)
        print("\n%s행 삭제 · %.2fMB → %.2fMB" % ("{:,}".format(n),
                                              before / 1048576, after / 1048576))
    else:
        conn.close()
        print("\n%s행 삭제 (원격 — VACUUM은 서버 몫)" % "{:,}".format(n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
