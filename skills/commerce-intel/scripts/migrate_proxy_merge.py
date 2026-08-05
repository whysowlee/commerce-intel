#!/usr/bin/env python3
"""proxy.db의 데이터를 본 DB(intel.db)로 이관한다 (D69).

D65-8에서 분리했던 proxy_defs·proxy_cache·proxy_history를 본 DB로 되돌린다.
lazy 판정 전환 이후 캐시가 천천히 쌓여 분리의 이점이 약해졌고,
FK·자동동기화를 못 하는 대가가 더 커졌기 때문이다.

사용법:
    python3 skills/commerce-intel/scripts/migrate_proxy_merge.py --db data/intel.db

동작:
    1. proxy.db에서 proxy_defs, proxy_cache, proxy_history 읽기
    2. 본 DB에 해당 테이블이 없으면 생성 (schema_v3의 SCHEMA_V3로)
    3. 데이터 복사 (INSERT OR IGNORE — 이미 있으면 스킵)
    4. 행 수 검산 (proxy.db vs 본 DB 일치 확인)
    5. 성공 시 proxy.db → proxy.db.merged-backup으로 이름 변경
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from schema_v3 import open_db  # noqa: E402


PROXY_TABLES = [
    ("proxy_defs",
     "proxy_name, question, material, value_space, method, created_at, label, rules"),
    ("proxy_cache",
     "proxy_name, site, product_id, fingerprint, value, basis, judged_at"),
    ("proxy_history",
     "id, proxy_name, site, product_id, old_value, new_value, "
     "old_fingerprint, new_fingerprint, changed_at"),
]


def _resolve_proxy_source(db_path_str: str):
    """프록시 DB 소스를 찾는다.

    우선순위: PROXY_DB_URL(터소) > INTEL_PROXY_DB(로컬 격리) > 정본 옆 proxy.db.
    터소 URL이면 open_db로 연결하고, 로컬이면 파일 존재 확인.
    """
    env = os.environ.get("PROXY_DB_URL") or os.environ.get("INTEL_PROXY_DB")
    if env:
        if env.startswith("libsql://"):
            return env, "turso"
        return Path(env), "local"
    db_path = Path(db_path_str).resolve()
    return db_path.parent / "proxy.db", "local"


def migrate(db_path: str, dry_run: bool = False) -> bool:
    proxy_source, source_type = _resolve_proxy_source(db_path)

    if source_type == "local":
        proxy_path = Path(proxy_source)
        if not proxy_path.exists():
            print(f"proxy.db가 없다: {proxy_path}")
            print("이미 이관됐거나 프록시를 사용한 적이 없다 — 할 일 없음.")
            return True

    db_path_resolved = Path(db_path).resolve()

    # 본 DB 열기
    conn = open_db(str(db_path_resolved))
    conn.execute("PRAGMA foreign_keys = ON")

    # 프록시 DB 열기
    if source_type == "turso":
        pconn = open_db(str(proxy_source),
                        token=os.environ.get("PROXY_DB_TOKEN"))
    else:
        pconn = sqlite3.connect(f"file:{proxy_source}?mode=ro", uri=True)
        pconn.row_factory = sqlite3.Row

    print(f"── proxy.db 이관 시작 ──")
    print(f"  원본: {proxy_source}")
    print(f"  대상: {db_path}")

    # 원본 행 수 확인
    src_counts = {}
    for table, _ in PROXY_TABLES:
        try:
            n = pconn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            src_counts[table] = n
            print(f"  {table:20} {n:>8} 행 (원본)")
        except sqlite3.OperationalError:
            src_counts[table] = 0
            print(f"  {table:20}    없음 (원본)")

    if all(v == 0 for v in src_counts.values()):
        print("proxy.db에 데이터가 없다 — 할 일 없음.")
        pconn.close()
        return True

    if dry_run:
        print("\n[dry-run] 실제 이관하지 않음.")
        pconn.close()
        return True

    # 데이터 복사
    print("\n── 데이터 복사 ──")
    for table, cols in PROXY_TABLES:
        if src_counts.get(table, 0) == 0:
            continue
        rows = pconn.execute(f"SELECT {cols} FROM {table}").fetchall()
        placeholders = ", ".join(["?"] * len(cols.split(",")))
        inserted = 0
        for row in rows:
            try:
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})",
                    tuple(row))
                inserted += 1
            except Exception as e:
                print(f"  경고: {table} 행 삽입 실패 — {e}")
        conn.commit()
        print(f"  {table:20} {inserted:>8} 행 삽입 시도")

    # 검산
    print("\n── 검산 ──")
    ok = True
    for table, _ in PROXY_TABLES:
        src_n = src_counts.get(table, 0)
        try:
            dst_n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            dst_n = 0
        mark = "OK" if dst_n >= src_n else "!! 불일치"
        print(f"  {table:20} 원본 {src_n:>8} → 본DB {dst_n:>8}  {mark}")
        if dst_n < src_n:
            ok = False

    pconn.close()

    if not ok:
        print("\n검산 실패 — proxy.db를 보존한다.")
        return False

    # 성공 시 백업
    if source_type == "local":
        backup = Path(proxy_source).with_suffix(".db.merged-backup")
        Path(proxy_source).rename(backup)
        print(f"\n성공! proxy.db → {backup.name} 으로 이름 변경.")
    else:
        print(f"\n성공! Turso 프록시 DB에서 데이터를 본 DB로 복사했다.")
        print(f"Turso의 commerce-intel-proxy DB는 수동으로 삭제하라: {proxy_source}")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/intel.db", help="본 DB 경로")
    ap.add_argument("--dry-run", action="store_true", help="실제 이관 없이 행 수만 확인")
    a = ap.parse_args()

    success = migrate(a.db, dry_run=a.dry_run)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
