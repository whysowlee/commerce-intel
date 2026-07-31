#!/usr/bin/env python3
"""정본 DB → 구글 시트 단방향 미러. SPEC-INTEL §3 구현.

    python3 sync_sheets.py                     # data/intel.db → 설정된 스프레드시트
    python3 sync_sheets.py --db data/intel.db --config data/sheets_config.json

설정:
  - 서비스 계정 키: $INTEL_SHEETS_CREDENTIALS (기본 ~/.config/intel/service-account.json)
  - data/sheets_config.json: {"spreadsheet_id": "..."}
  - 최초 발급 절차는 docs/SHEETS-SETUP.md

동작:
  - products·variants·platforms·runs: 탭 전체 다시 쓰기 (행 수가 작다)
  - observations·variant_observations: rowid 기준 증분 append (sync_state가 진행점 기억)
  - 시트는 보는 창구다 — 시트에서 손으로 고친 값은 다음 미러에서 덮일 수 있다
  - 실패해도 수집·적재는 유효하다. exit 3 = 인증/설정 없음, exit 1 = 동기화 실패
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from intel_db import connect  # noqa: E402

FULL_TABLES = ("products", "variants", "platforms", "runs", "proxy_defs")
INCR_TABLES = ("observations", "variant_observations", "proxy_cache")
NOTICE = ("이 스프레드시트는 로컬 정본 DB(data/intel.db)의 단방향 미러입니다. "
          "여기서 고친 값은 정본에 반영되지 않고 다음 동기화 때 덮일 수 있습니다.")


def load_gspread(creds_path):
    try:
        import gspread
    except ImportError:
        print("gspread가 없다. 설치: pip3 install gspread  (docs/SHEETS-SETUP.md 참조)",
              file=sys.stderr)
        sys.exit(3)
    if not Path(creds_path).exists():
        print(f"서비스 계정 키가 없다: {creds_path}\n"
              f"발급 절차는 docs/SHEETS-SETUP.md — 미러만 밀린 것이고 수집·적재는 유효하다.",
              file=sys.stderr)
        sys.exit(3)
    return gspread.service_account(filename=creds_path)


def rows_of(conn, table, since_rowid=None):
    q = f"SELECT rowid AS _rowid, * FROM {table}"
    if since_rowid is not None:
        q += f" WHERE rowid > {int(since_rowid)}"
    rows = conn.execute(q + " ORDER BY rowid").fetchall()
    if not rows:
        return [], [], None
    headers = [k for k in rows[0].keys() if k != "_rowid"]
    data = [["" if r[h] is None else r[h] for h in headers] for r in rows]
    return headers, data, rows[-1]["_rowid"] if rows else None


def ensure_ws(sh, title, cols=26):
    try:
        return sh.worksheet(title)
    except Exception:
        return sh.add_worksheet(title=title, rows=1000, cols=cols)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=os.environ.get("INTEL_DB", "data/intel.db"))
    p.add_argument("--config", default="data/sheets_config.json")
    p.add_argument("--creds", default=os.environ.get(
        "INTEL_SHEETS_CREDENTIALS", str(Path.home() / ".config/intel/service-account.json")))
    args = p.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"설정 파일이 없다: {cfg_path} — {{\"spreadsheet_id\": \"...\"}} 형태로 만든다. "
              f"docs/SHEETS-SETUP.md 참조", file=sys.stderr)
        sys.exit(3)
    spreadsheet_id = json.loads(cfg_path.read_text())["spreadsheet_id"]

    gc = load_gspread(args.creds)
    conn = connect(args.db)
    try:
        sh = gc.open_by_key(spreadsheet_id)
    except Exception as e:
        print(f"스프레드시트 열기 실패: {e}\n서비스 계정 이메일에 시트가 공유돼 있는지 확인",
              file=sys.stderr)
        sys.exit(1)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ensure_ws(sh, "안내", 2).update("A1:A2", [[NOTICE], [f"마지막 동기화: {now}"]])

    for table in FULL_TABLES:
        headers, data, _ = rows_of(conn, table)
        ws = ensure_ws(sh, table)
        ws.clear()
        if headers:
            ws.update("A1", [headers] + data)
        print(f"{table}: 전체 {len(data)}행 미러")

    # 관측 테이블들은 rowid 기준 증분 append
    for table in INCR_TABLES:
        row = conn.execute(
            "SELECT last_synced_key FROM sync_state WHERE table_name=?", (table,)).fetchone()
        last = int(row["last_synced_key"]) if row and row["last_synced_key"] else 0
        result = rows_of(conn, table, since_rowid=last)
        headers, data, max_rowid = result[0], result[1], result[2]
        ws = ensure_ws(sh, table, 30)
        if not ws.get_values("A1:A1"):
            full_headers = [d[1] for d in conn.execute(f"PRAGMA table_info({table})")]
            ws.update("A1", [full_headers])
        if data:
            ws.append_rows(data, value_input_option="RAW")
            conn.execute(
                "INSERT INTO sync_state VALUES (?, ?, ?) "
                "ON CONFLICT(table_name) DO UPDATE SET last_synced_key=excluded.last_synced_key, "
                "updated_at=excluded.updated_at", (table, str(max_rowid), now))
            conn.commit()
            print(f"{table}: 증분 {len(data)}행 append (rowid ≤ {max_rowid})")
        else:
            print(f"{table}: 새 관측 없음")


if __name__ == "__main__":
    main()
