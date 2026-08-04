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
# 스토리별 뷰 탭 — 정본이 아니라 파생이다(context 접두사로 걸러 상품별 최신 관측만).
# 스토리 안의 세부 대상은 context 열(앞쪽)로 구분한다 — 시트 필터로 걸러 본다.
VIEWS = (("뷰_라인시트", "brand:", "브랜드 라인시트 수집분"),
         ("뷰_전수조사", "market:", "카테고리 전수조사 수집분 (핏 분류 포함)"),
         ("뷰_랭킹", "ranking:", "랭킹 모니터링 축적분 (카테고리는 context 열로 구분)"))
VIEW_HEADERS = ["썸네일", "site", "context", "product_id", "상품명", "브랜드", "카테고리",
                "핏", "관측시각", "정가", "판매가", "할인율", "후기수", "평점", "하트",
                "누적판매", "보는중", "품절", "순위"]
TABLE_DESC = {  # 안내 탭에 싣는 원본 탭 설명
    "products": "상품 정적 속성(이름·브랜드·카테고리·핏). 상품당 1행, 전체 다시 쓰기",
    "observations": "시점별 관측 원본(가격·하트·순위…). append only, context 열이 출처",
    "variants": "옵션(컬러·사이즈) 구성. 옵션 수집 시 생성",
    "variant_observations": "옵션별 재고 관측. 재고 프로브 시 생성",
    "platforms": "입점처 누적 카탈로그. channel-scout 실행 시 생성",
    "runs": "수집 실행 이력",
    "proxy_defs": "AI 파생 프록시 정의. 프록시 사용 시 생성",
    "proxy_cache": "프록시 판정 캐시. 프록시 사용 시 생성",
}
NOTICE = ("이 스프레드시트는 로컬 정본 DB(data/intel.db)의 단방향 미러입니다. "
          "여기서 고친 값은 정본에 반영되지 않고 다음 동기화 때 덮일 수 있습니다.")


def open_spreadsheet(config_path, creds_path):
    """스프레드시트를 연다. 성공 시 (sh, None), 실패 시 (None, (사유, exit_code)).

    예외를 던지지도 exit하지도 않는다 — 읽기 쪽(팀 커버리지 조회)은 시트가 없어도
    진행해야 하고, 쓰기 쪽(main)은 받은 exit_code로 스스로 끝낸다. exit 3은
    "아직 설정 안 됨"(설치·키·설정파일), exit 1은 "설정은 됐는데 실패"다.
    """
    try:
        import gspread
    except ImportError:
        return None, ("gspread가 없다. 설치: pip3 install gspread  (docs/SHEETS-SETUP.md 참조)", 3)
    if not Path(creds_path).exists():
        return None, (f"서비스 계정 키가 없다: {creds_path} — 발급 절차는 docs/SHEETS-SETUP.md", 3)
    cfg_path = Path(config_path)
    if not cfg_path.exists():
        return None, (f'설정 파일이 없다: {cfg_path} — {{"spreadsheet_id": "..."}} 형태로 만든다. '
                      f"docs/SHEETS-SETUP.md 참조", 3)
    try:
        spreadsheet_id = json.loads(cfg_path.read_text())["spreadsheet_id"]
    except (ValueError, KeyError) as e:
        return None, (f"설정 파일을 읽을 수 없다: {cfg_path} ({e})", 3)
    try:
        gc = gspread.service_account(filename=creds_path)
        return gc.open_by_key(spreadsheet_id), None
    except Exception as e:
        return None, (f"스프레드시트 열기 실패: {e} — 서비스 계정 이메일에 시트가 공유돼 있는지 확인", 1)


def fetch_tab(sh, title):
    """시트 탭 하나를 [{헤더: 값}] 로 읽는다. 읽기 실패는 예외를 올린다.

    탭이 없으면 None을 돌려준다 — 빈 리스트([])와 구분해야 한다. 없는 탭은
    "아직 미러가 안 돌았다"(판정 근거 없음)이고, 빈 탭은 "미러는 돌았고 데이터가
    0행"이다. 빈 탭은 애초에 만들지 않는 규칙이라 실제로는 전자가 대부분이다.

    미러가 단방향이라는 원칙은 그대로다 — 여기서 읽은 값은 정본에 쓰지 않고
    "이미 수집됐나" 판정에만 쓴다(D32).
    """
    try:
        ws = sh.worksheet(title)
    except Exception:
        return None
    values = ws.get_all_values()
    if len(values) < 2:
        return []
    headers = values[0]
    return [dict(zip(headers, row)) for row in values[1:] if any(c.strip() for c in row)]


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


def view_rows(conn, prefix):
    """한 스토리(context 접두사)의 상품별 최신 관측을 사람이 읽을 표로 만든다."""
    rows = conn.execute("""
        SELECT p.site, p.product_id, p.name, p.brand, p.category, p.attributes, p.image_url,
               o.context, o.observed_at, o.price_original, o.price_sale, o.discount_rate,
               o.review_count, o.rating, o.like_count, o.purchase_count,
               o.viewers_now, o.sold_out, o.rank
        FROM products p JOIN observations o
          ON o.site = p.site AND o.product_id = p.product_id
        WHERE o.context LIKE ? AND o.observed_at = (
            SELECT MAX(o2.observed_at) FROM observations o2
            WHERE o2.site = o.site AND o2.product_id = o.product_id
              AND o2.context LIKE ?)
        ORDER BY o.context, o.rank IS NULL, o.rank, p.site, p.product_id
    """, (prefix + "%", prefix + "%")).fetchall()
    data, seen = [], set()
    for r in rows:
        key = (r["site"], r["product_id"], r["context"])
        if key in seen:
            continue
        seen.add(key)
        fit = (json.loads(r["attributes"]) if r["attributes"] else {}).get("핏") or ""
        so = "" if r["sold_out"] is None else ("품절" if r["sold_out"] else "판매중")
        # 썸네일: 시트가 인셀 렌더하는 =IMAGE() 수식 (value_input_option=USER_ENTERED 필요)
        img = f'=IMAGE("{r["image_url"]}")' if r["image_url"] else ""
        vals = [img, r["site"], r["context"], r["product_id"], r["name"], r["brand"],
                r["category"], fit, r["observed_at"], r["price_original"], r["price_sale"],
                r["discount_rate"], r["review_count"], r["rating"], r["like_count"],
                r["purchase_count"], r["viewers_now"], so, r["rank"]]
        data.append(["" if v is None else v for v in vals])
    return data


THUMB_PX = 102   # 썸네일 열 너비·데이터 행 높이(px)


def size_thumbs(sh, ws, n_rows):
    """썸네일 열(A)을 넓히고 데이터 행을 높인다 — =IMAGE(url,1)이 셀에 맞춰 커진다.
    25k행도 요청 2번(열 1 + 행 범위 1)이라 싸다."""
    sid = ws._properties["sheetId"]
    dim = lambda kind, start, end, px: {"updateDimensionProperties": {
        "range": {"sheetId": sid, "dimension": kind, "startIndex": start, "endIndex": end},
        "properties": {"pixelSize": px}, "fields": "pixelSize"}}
    try:
        sh.batch_update({"requests": [
            dim("COLUMNS", 0, 1, THUMB_PX),            # A열 너비
            dim("ROWS", 1, n_rows + 1, THUMB_PX)]})    # 데이터 행(헤더 제외) 높이
    except Exception as e:
        print(f"  썸네일 크기 조정 실패(무해): {e}")


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

    sh, err = open_spreadsheet(args.config, args.creds)
    if err:
        reason, code = err
        if code == 3:
            reason += "\n미러만 밀린 것이고 수집·적재는 유효하다."
        print(reason, file=sys.stderr)
        sys.exit(code)
    conn = connect(args.db)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing = {ws.title: ws for ws in sh.worksheets()}
    tab_rows = []      # 안내 탭에 실을 [탭, 행수, 내용]
    def drop_if_empty(title):
        if title in existing:
            sh.del_worksheet(existing.pop(title))
            print(f"{title}: 데이터 없음 — 탭 삭제")

    for table in FULL_TABLES:
        headers, data, _ = rows_of(conn, table)
        if not data:               # 빈 테이블은 탭을 만들지 않는다 (있으면 지운다)
            drop_if_empty(table)
            continue
        ws = ensure_ws(sh, table)
        ws.clear()
        ws.update(values=[headers] + data, range_name="A1")
        tab_rows.append([table, len(data), TABLE_DESC.get(table, "")])
        print(f"{table}: 전체 {len(data)}행 미러")

    # 스토리별 뷰 탭 — 파생이므로 전체 다시 쓰기. 데이터 없는 스토리는 탭이 없다
    story_rows = []    # 안내 탭에 실을 스토리 현황
    for title, prefix, desc in VIEWS:
        data = view_rows(conn, prefix)
        if not data:
            drop_if_empty(title)
            story_rows.append([title, "아직 데이터 없음", desc])
            continue
        ws = ensure_ws(sh, title, len(VIEW_HEADERS))
        ws.clear()
        # USER_ENTERED: 썸네일 =IMAGE() 수식이 셀에서 렌더된다
        ws.update(values=[VIEW_HEADERS] + data, range_name="A1",
                  value_input_option="USER_ENTERED")
        size_thumbs(sh, ws, len(data))   # 썸네일 열·행을 키운다(=IMAGE는 셀에 맞춰 커짐)
        n_ctx = len({d[2] for d in data})
        story_rows.append([title, f"{len(data)}행 · 대상 {n_ctx}개(context 열로 구분)", desc])
        tab_rows.append([title, len(data), desc + " — 상품별 최신 관측만. 원본은 observations"])
        print(f"{title}: {len(data)}행 (상품별 최신 관측)")
    for t in list(existing):       # 구 명명 규칙의 뷰_ 탭 정리 (다른 탭은 건드리지 않는다)
        if t.startswith("뷰_") and t not in {v[0] for v in VIEWS}:
            drop_if_empty(t)

    # 관측 테이블들은 rowid 기준 증분 append — 빈 테이블은 탭을 만들지 않는다
    for table in INCR_TABLES:
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if total == 0:
            drop_if_empty(table)
            continue
        tab_rows.append([table, total, TABLE_DESC.get(table, "")])
        row = conn.execute(
            "SELECT last_synced_key FROM sync_state WHERE table_name=?", (table,)).fetchone()
        last = int(row["last_synced_key"]) if row and row["last_synced_key"] else 0
        result = rows_of(conn, table, since_rowid=last)
        headers, data, max_rowid = result[0], result[1], result[2]
        full_headers = [d[1] for d in conn.execute(f"PRAGMA table_info({table})")]
        ws = ensure_ws(sh, table, len(full_headers))
        # 빈 열은 셀 한도(워크북 1천만)를 갉아먹는다 — 관측 탭은 계속 자라므로
        # 여기가 제일 먼저 막힌다. 2026-08-04 실측: observations가 156열(실제 20열)로
        # 부풀어 780만 셀을 쓰고 있었고 append가 400으로 거절당했다. 줄이니 100만이 됐다.
        # **값이 있는 열은 건드리지 않는다** — 헤더 수보다 넓을 때만 헤더 폭으로 맞춘다.
        if ws.col_count > len(full_headers):
            ws.resize(rows=ws.row_count, cols=len(full_headers))
        if not ws.get_values("A1:A1"):
            ws.update(values=[full_headers], range_name="A1")
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

    # 안내 탭 — 스토리 현황과 탭 가이드를 사람이 읽게 쓴다
    guide = [[NOTICE], [f"마지막 동기화: {now}"], [""],
             ["■ 스토리 현황", "", ""]] + story_rows + [
             [""], ["■ 탭 안내", "행수", "내용"]] + [[t, str(n), d] for t, n, d in tab_rows]
    ws = ensure_ws(sh, "안내", 4)
    ws.clear()
    ws.update(values=guide, range_name="A1")
    print(f"안내: 스토리 현황 {len(story_rows)}줄 + 탭 가이드 {len(tab_rows)}줄")


if __name__ == "__main__":
    main()
