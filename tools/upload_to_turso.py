#!/usr/bin/env python3
"""로컬 v3 SQLite → Turso 데이터 이관 (D67).

    # 정본
    python3 tools/upload_to_turso.py --src data/intel.db \
        --url libsql://commerce-intel-xxx.turso.io --token $INTEL_DB_TOKEN
    # D69: 프록시는 본 DB에 통합 — --proxy 옵션 삭제됨
    # 검산만
    python3 tools/upload_to_turso.py --src data/intel.db --url ... --token ... --verify-only

절차: 스키마 생성(정본 DDL은 schema_v3.py가 정본) → FK 순서대로 5,000행 배치
INSERT → 테이블별 행 수 검산. **비어 있지 않은 원격에는 올리지 않는다**(--force로
무시) — 재실행이 중복 위에 쌓이면 검산이 영영 안 맞는다.

migrate_v3와 같은 원칙: 검산이 통과 조건이다. 행 수가 하나라도 어긋나면 exit 1.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "commerce-intel" / "scripts"))
from schema_v3 import SCHEMA_V3, VIEWS_V3, open_db  # noqa: E402

BATCH = 5000

# FK 순서 — 참조되는 쪽이 먼저다. categories는 self-FK라 category_id 오름차순이면
# 부모가 먼저 온다(ensure_category_path가 부모부터 만들어 id가 항상 작다).
INTEL_ORDER = [
    ("sites", "site_id"), ("contexts", "context_id"), ("brands", "brand_id"),
    ("hosts", "host_id"), ("categories", "category_id"),
    ("brand_aliases", "alias_id"), ("runs", "id"), ("platforms", "rowid"),
    ("brand_platforms", "rowid"),
    ("product_base", "pk"), ("product_categories", "rowid"),
    ("obs_base", "id"), ("obs_attr", "id"),
    ("variant_base", "vk"), ("variant_obs_base", "id"),
    ("attr_base", "rowid"), ("insights", "rowid"), ("sync_state", "rowid"),
    ("product_history", "id"), ("attr_history", "id"),
    ("proxy_defs", "rowid"), ("proxy_cache", "rowid"),
    ("proxy_history", "id"),
]
# D69: PROXY_ORDER 삭제 — 프록시가 본 DB에 통합됨


def upload(src_path, url, token, force=False):
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = open_db(url, token=token)
    order = INTEL_ORDER

    # 스키마 — 정본 DDL로 짓는다(로컬에서 추출하지 않는다: schema_v3.py가 정본이고,
    # 로컬 파일과 어긋났다면 그건 로컬이 낡은 것이다)
    dst.executescript(SCHEMA_V3)
    dst.executescript(VIEWS_V3)
    dst.execute("PRAGMA foreign_keys = ON")

    # 비어 있지 않은 원격 방어 — 이중 업로드는 검산 불능 상태를 만든다
    if not force:
        for t, _ in order:
            if not _exists(src, t):
                continue
            n = dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if n:
                raise SystemExit(
                    f"원격 {t}에 이미 {n:,}행이 있다 — 빈 DB에만 올린다. "
                    f"덮어쓰려면 원격을 비우고 다시, 정말 이어 올리려면 --force")

    report = {}
    for table, order_col in order:
        if not _exists(src, table):
            continue
        cols = [d[1] for d in src.execute(f"PRAGMA table_info({table})")]
        sel = ",".join(cols)
        ins = "INSERT INTO %s (%s) VALUES (%s)" % (table, sel, ",".join("?" * len(cols)))
        n, batch = 0, []
        for r in src.execute(f"SELECT {sel} FROM {table} ORDER BY {order_col}"):
            batch.append(tuple(r[c] for c in cols))
            if len(batch) >= BATCH:
                dst.executemany(ins, batch)
                dst.commit()          # 배치마다 커밋 — 네트워크 실패 시 재개 지점이 남는다
                n += len(batch)
                batch = []
        if batch:
            dst.executemany(ins, batch)
            dst.commit()
            n += len(batch)
        report[table] = n
        print(f"  {table:22} {n:>9,}행 업로드")
    src.close()
    return dst, report


def verify(src_path, dst):
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    order = INTEL_ORDER
    ok = True
    for table, _ in order:
        if not _exists(src, table):
            continue
        a = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        b = dst.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        mark = "OK" if a == b else "!! 불일치"
        if a != b:
            ok = False
        print(f"  {table:22} 로컬 {a:>9,} → Turso {b:>9,}  {mark}")
    src.close()
    return ok


def _exists(conn, table):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", required=True, help="로컬 v3 SQLite 파일")
    ap.add_argument("--url", default=os.environ.get("INTEL_DB_URL"),
                    help="libsql:// URL (기본 $INTEL_DB_URL)")
    ap.add_argument("--token", default=os.environ.get("INTEL_DB_TOKEN"),
                    help="쓰기 토큰 (기본 $INTEL_DB_TOKEN)")
    ap.add_argument("--force", action="store_true",
                    help="원격이 비어 있지 않아도 이어서 INSERT (권장하지 않음)")
    ap.add_argument("--verify-only", action="store_true")
    a = ap.parse_args()
    if not a.url or not a.url.startswith("libsql://"):
        raise SystemExit("--url(libsql://...)이 필요하다 — turso db show <이름> --url")

    if a.verify_only:
        dst = open_db(a.url, token=a.token)
    else:
        print("── 업로드 ──")
        dst, _ = upload(a.src, a.url, a.token, force=a.force)
    print("── 검산 ──")
    ok = verify(a.src, dst)
    print("검산 %s" % ("통과" if ok else "실패 — 원격을 믿지 마라"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
