#!/usr/bin/env python3
"""Turso 연결·드라이버 호환 래퍼 검증 (D67).

    python3 tests/test_turso_connect.py

3층으로 나뉜다 — 환경이 안 되는 층은 **스킵을 찍고** 넘어간다(조용히 통과 아님):

  [A] 래퍼 오프라인 — libsql_experimental이 있으면 **로컬 파일**로 호환 계약 검증
      (row 이름 접근·IntegrityError 번역·lastrowid·executescript).
      드라이버는 파이썬 3.10+에서만 설치된다 — 시스템 3.9면 스킵된다
  [B] 로컬 폴백 — 환경변수 없이 open_db가 로컬 SQLite로 도는지 (항상 돈다)
  [C] Turso 실연결 — INTEL_DB_URL·INTEL_DB_TOKEN이 있을 때만:
      연결·쓰기/읽기 왕복·(TURSO_RO_TOKEN이 있으면) 읽기 전용 토큰의 INSERT 거부
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "commerce-intel" / "scripts"))

from schema_v3 import open_db  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def wrapper_tests():
    """[A] libsql 드라이버 호환 래퍼 — 로컬 파일로 네 가지 실측 지뢰를 고정한다."""
    try:
        import libsql_experimental  # noqa: F401
    except ImportError:
        print("  SKIP  [A] libsql_experimental 미설치 (py3.10+ 필요 — docs/TURSO-SETUP.md)")
        return
    from schema_v3 import LibsqlConnection
    import libsql_experimental as libsql
    path = os.path.join(tempfile.mkdtemp(prefix="lsql-"), "w.db")
    conn = LibsqlConnection(libsql.connect(path))

    conn.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)")
    conn.execute("INSERT INTO t (b) VALUES ('x')")
    row = conn.execute("SELECT a, b FROM t").fetchone()
    check("A1 행 이름 접근 (드라이버는 row_factory가 없다 — 래퍼가 흡수)",
          row["b"] == "x" and row[0] == 1 and row.keys() == ["a", "b"], row)
    check("A2 dict(row) 캐스팅", dict(row) == {"a": 1, "b": "x"})
    cur = conn.execute("INSERT INTO t (b) VALUES ('y')")
    check("A3 lastrowid (드라이버 값이 불안정 — last_insert_rowid()로 대행)",
          cur.lastrowid == 2, cur.lastrowid)
    dup = False
    try:
        conn.execute("INSERT INTO t (a, b) VALUES (1, 'z')")
    except sqlite3.IntegrityError:
        dup = True   # 드라이버는 ValueError를 던진다 — 번역이 없으면 여기 안 온다
    check("A4 제약 위반이 sqlite3.IntegrityError로 온다 (중복 카운팅 계약)", dup)
    conn.executescript("CREATE TABLE u (k INTEGER); INSERT INTO u VALUES (7);")
    check("A5 executescript", conn.execute("SELECT k FROM u").fetchone()[0] == 7)
    check("A6 total_changes 대행", isinstance(conn.total_changes, int))

    # 래퍼 위에 v3 스키마 전체가 올라가는지 — connect()가 하는 그대로
    import intel_db
    db2 = os.path.join(tempfile.mkdtemp(prefix="lsql-"), "v3.db")
    c2 = LibsqlConnection(libsql.connect(db2))
    from schema_v3 import SCHEMA_V3, TRIGGERS_V3, VIEWS_V3
    c2.executescript(SCHEMA_V3)
    c2.executescript(VIEWS_V3)
    c2.executescript(TRIGGERS_V3)
    c2.execute("INSERT INTO runs (run_id, site, story, target, collected_at) "
               "VALUES ('R1','29cm','brand-linesheet','T','2026-08-05 10:00:00')")
    c2.execute("INSERT INTO products (site, product_id, name, brand, "
               "static_verified_at) VALUES ('29cm','P1','상품','브랜드','2026-08-05 10:00:00')")
    c2.execute("INSERT INTO observations (site, product_id, observed_at, context, "
               "price_sale, run_id) VALUES ('29cm','P1','2026-08-05 10:00:00','brand:t',1000,'R1')")
    o = c2.execute("SELECT price_sale, run_id, context FROM observations").fetchone()
    check("A7 v3 뷰·트리거가 libsql 위에서 돈다 (INSTEAD OF INSERT 왕복)",
          tuple(o) == (1000, "R1", "brand:t"), tuple(o))
    assert intel_db is not None


def local_fallback_tests():
    """[B] 환경변수 없이 open_db → 로컬 SQLite. 항상 도는 하위호환 계약."""
    work = tempfile.mkdtemp(prefix="odb-")
    conn = open_db(os.path.join(work, "l.db"))
    check("B1 로컬 경로는 sqlite3 커넥션이다", isinstance(conn, sqlite3.Connection))
    conn.execute("CREATE TABLE t (a)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    check("B2 row_factory=Row가 걸려 있다",
          conn.execute("SELECT a FROM t").fetchone()["a"] == 1)
    conn2 = open_db("file:" + os.path.join(work, "l.db"))
    check("B3 file: 접두사도 같은 파일을 연다",
          conn2.execute("SELECT a FROM t").fetchone()[0] == 1)


def turso_live_tests():
    """[C] 실 Turso — 자격이 있을 때만. CI·로컬 기본 실행에서는 스킵된다."""
    url = os.environ.get("INTEL_DB_URL", "")
    if not url.startswith("libsql://"):
        print("  SKIP  [C] INTEL_DB_URL 미설정 — 실연결 검증 생략")
        return
    conn = open_db(url)
    one = conn.execute("SELECT 1").fetchone()
    check("C1 Turso 연결·SELECT", one[0] == 1, one)
    conn.execute("CREATE TABLE IF NOT EXISTS _connect_test (k INTEGER, v TEXT)")
    conn.execute("INSERT INTO _connect_test VALUES (1, 'ok')")
    conn.commit()
    got = conn.execute("SELECT v FROM _connect_test WHERE k=1").fetchone()
    check("C2 쓰기 토큰 INSERT/SELECT 왕복", got and got["v"] == "ok", got)
    conn.execute("DROP TABLE _connect_test")
    conn.commit()

    ro = os.environ.get("TURSO_RO_TOKEN")
    if not ro:
        print("  SKIP  [C3] TURSO_RO_TOKEN 미설정 — 읽기 전용 거부 검증 생략")
        return
    rconn = open_db(url, token=ro)
    denied = False
    try:
        rconn.execute("CREATE TABLE _ro_test (k INTEGER)")
        rconn.commit()
    except Exception:
        denied = True   # 읽기 전용 토큰은 서버가 쓰기를 거부한다 — 3중 안전의 최종층
    check("C3 읽기 전용 토큰은 쓰기가 거부된다", denied)


def main():
    print("[A] libsql 드라이버 호환 래퍼 (로컬 파일)")
    wrapper_tests()
    print("[B] 로컬 SQLite 폴백")
    local_fallback_tests()
    print("[C] Turso 실연결")
    turso_live_tests()
    print("-" * 56)
    print(f"통과 {passed} · 실패 {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()


