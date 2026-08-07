#!/usr/bin/env python3
"""로컬 v3 SQLite → Turso 데이터 이관 (D67).

    # 정본
    python3 tools/upload_to_turso.py --src data/intel.db \
        --url libsql://commerce-intel-xxx.turso.io --token $INTEL_DB_TOKEN
    # D69: 프록시는 본 DB에 통합 — --proxy 옵션 삭제됨
    # 검산만
    python3 tools/upload_to_turso.py --src data/intel.db --url ... --token ... --verify-only

절차: 표 스키마 생성(정본 DDL은 schema_v3.py) → FK 순서대로 5,000행 배치
INSERT → **뷰·트리거는 데이터 뒤에** (INSTEAD OF 트리거가 미리 있으면 INSERT를
가로챈다) → 테이블별 행 수 검산. **비어 있지 않은 원격에는 올리지 않는다.**
`--force`는 원격 표를 전부 DROP하고 이 로컬본으로 **교체**한다 — 이어 붙이기가
아니다(중복 위에 쌓이면 검산이 영영 안 맞는다).

migrate_v3와 같은 원칙: 검산이 통과 조건이다. 행 수가 하나라도 어긋나면 exit 1.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "commerce-intel" / "scripts"))
from schema_v3 import SCHEMA_V3, TRIGGERS_V3, VIEWS_V3, open_db  # noqa: E402

BATCH = 5000

# FK 순서 — 참조되는 쪽이 먼저다. categories는 self-FK라 category_id 오름차순이면
# 부모가 먼저 온다(ensure_category_path가 부모부터 만들어 id가 항상 작다).
INTEL_ORDER = [
    ("sites", "site_id"), ("contexts", "context_id"), ("brands", "brand_id"),
    ("categories", "category_id"),
    ("brand_aliases", "alias_id"), ("runs", "id"), ("platforms", "rowid"),
    ("brand_platforms", "rowid"),
    ("product_base", "pk"), ("product_categories", "rowid"),
    ("obs_base", "id"), ("obs_attr", "id"),
    ("variant_base", "vk"), ("variant_obs_base", "id"),
    ("attr_base", "rowid"), ("insights", "rowid"), ("sync_state", "rowid"),
    ("product_history", "id"), ("attr_history", "id"),
    ("proxy_defs", "rowid"), ("proxy_cache", "rowid"),
    ("proxy_history", "id"),
    # D72: 시트에 아직 반영 안 된 편집·삭제 신호 — 빼먹으면 이관 시점의 미반영
    # 변경이 조용히 사라져 시트에 옛 값이 영구히 남는다 (PR #21 2R 리뷰)
    ("mirror_dirty", "rowid"),
]
# D69: PROXY_ORDER 삭제 — 프록시가 본 DB에 통합됨


def upload(src_path, url, token, force=False):
    # D70: intel_db.connect()를 먼저 거쳐 URL 이관을 적용한 뒤 읽기 전용으로 다시 연다.
    # connect()가 hosts→url 전환을 해야 새 스키마의 dst와 컬럼이 맞는다.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                          / "skills" / "commerce-intel" / "scripts"))
    import intel_db as _idb
    _pre = _idb.connect(str(src_path))
    _pre.close()
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    dst = open_db(url, token=token)
    order = INTEL_ORDER

    if force:
        # --force = **원격을 이 로컬본으로 교체한다.** 이어 붙이기가 아니다 —
        # 이어 붙이면 중복 위에 쌓여 검산이 영영 안 맞는다(원 --force의 버그).
        # 뷰가 표를 참조하므로 뷰 먼저, 표는 FK 역순으로 지운다. 뷰 목록은
        # 원격의 sqlite_master에서 읽는다 — 하드코딩하면 뷰가 늘 때마다 여기가
        # 낡는다 (1R 리뷰).
        for (v,) in dst.execute(
                "SELECT name FROM sqlite_master WHERE type='view'").fetchall():
            dst.execute(f"DROP VIEW IF EXISTS {v}")
        for t, _ in reversed(order):
            dst.execute(f"DROP TABLE IF EXISTS {t}")
        dst.commit()
        print("  --force: 원격 표를 전부 비웠다 (재생성 후 다시 올린다)")

    # 스키마(표·인덱스만) — 정본 DDL로 짓는다(로컬에서 추출하지 않는다:
    # schema_v3.py가 정본이고, 로컬 파일과 어긋났다면 그건 로컬이 낡은 것이다).
    # **뷰·트리거는 데이터를 다 넣은 뒤에 얹는다** — INSTEAD OF 트리거가 미리
    # 있으면 이름이 같은 경로로 들어오는 INSERT를 가로채 UNIQUE 충돌을 낸다.
    dst.executescript(SCHEMA_V3)
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
                    f"원격을 이 로컬본으로 교체하려면 --force")

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
    # 데이터가 다 들어간 뒤에 뷰·트리거 — 조회 계약(intel-query 등)과 이후
    # connect() 없이 쓰는 클라이언트를 위해 원격에도 얹는다
    dst.executescript(VIEWS_V3)
    dst.executescript(TRIGGERS_V3)
    dst.commit()
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
                    help="원격 표를 전부 DROP하고 이 로컬본으로 교체한다")
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
