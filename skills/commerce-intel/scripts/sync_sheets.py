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
from schema_v3 import (proxy_connect, proxy_db_exists,  # noqa: E402
                       proxy_db_path, rowid_parts)

# 프록시 표 2종은 별도 DB(proxy.db — D65-8)에서 읽는다. 탭 구성은 그대로 —
# 팀원이 보는 창구가 파일 분리 때문에 달라질 이유는 없다.
FULL_TABLES = ("products", "variants", "platforms", "runs",
               "brand_aliases", "brand_platforms", "proxy_defs")
PROXY_TABLES = ("proxy_defs", "proxy_cache")
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
    "products": "상품 정적 속성(이름·브랜드·카테고리). 상품당 1행, 전체 다시 쓰기",
    "observations": "시점별 관측 원본(가격·하트·순위…). append only, context 열이 출처",
    "variants": "옵션(컬러·사이즈) 구성. 옵션 수집 시 생성",
    "variant_observations": "옵션별 재고 관측. 재고 프로브 시 생성",
    "platforms": "입점처 누적 카탈로그. channel-scout 실행 시 생성",
    "brand_aliases": "브랜드 표기 별명(플랫폼별 변형). 수집이 자동 등록(candidate)",
    "brand_platforms": "브랜드-입점처 매핑. channel-scout·수집이 채운다",
    "runs": "수집 실행 이력",
    "proxy_defs": "AI 파생 프록시 정의 (proxy.db). 프록시 사용 시 생성",
    "proxy_cache": "프록시 판정 캐시 (proxy.db). 프록시 사용 시 생성",
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
    # v2는 옛 이름이 뷰라 rowid가 없다 — 뷰가 `_rowid`로 물리 키를 내준다 (D45).
    # **WHERE·ORDER에는 별칭이 아니라 그 테이블에서 진짜로 참조 가능한 표현식**을
    # 쓴다 — 물리 테이블이면 `rowid`, 뷰면 `_rowid` (PR #9 리뷰)
    sel, key = rowid_parts(conn, table)
    q = f"{sel} FROM {table}"
    if since_rowid is not None:
        q += f" WHERE {key} > {int(since_rowid)}"
    rows = conn.execute(q + f" ORDER BY {key}").fetchall()
    if not rows:
        return [], [], None
    headers = [k for k in rows[0].keys() if k != "_rowid"]
    data = [["" if r[h] is None else r[h] for h in headers] for r in rows]
    return headers, data, rows[-1]["_rowid"] if rows else None


def view_rows(conn, prefix):
    """한 스토리(context 접두사)의 상품별 최신 관측을 사람이 읽을 표로 만든다.

    핏은 v3부터 product_attributes가 유일한 정본이다(D65-2) — 옛 attributes(JSON)
    컬럼은 없다.
    """
    rows = conn.execute("""
        SELECT p.site, p.product_id, p.name, p.brand, p.category, p.image_url,
               (SELECT a.value FROM product_attributes a
                WHERE a.site = p.site AND a.product_id = p.product_id
                  AND a.attr_name = '핏') AS fit,
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
        fit = r["fit"] or ""
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


def sheet_rows(ws):
    """시트 탭의 **값이 있는** 데이터 행 수 (헤더 제외).

    `row_count`(격자 크기)를 쓰면 안 된다 — 격자는 데이터보다 크고, 중간에 빈 행이
    섞여 있을 수도 있다(2026-08-04 실측: 격자 49,907행인데 실제 데이터는 11,727행).
    A열을 실제로 읽어 빈 칸을 빼고 센다.
    """
    try:
        col = ws.col_values(1)
    except Exception:
        return None                 # 못 읽으면 "모른다" — 0과 구분해야 한다
    return sum(1 for v in col[1:] if str(v).strip())


def trailing_is_empty(ws, keep_cols):
    """`keep_cols` 오른쪽이 전부 비었나. 못 읽으면 **비었다고 단정하지 않는다**(False).

    시트 축소는 되돌릴 수 없다. 확인에 실패했을 때 "아마 비었겠지"로 자르면
    그 순간 데이터가 사라지고 아무도 모른다 — 모르면 안 자르는 쪽으로 간다.
    """
    if ws.col_count <= keep_cols:
        return True
    try:
        import gspread.utils as u
        rng = "%s:%s" % (u.rowcol_to_a1(1, keep_cols + 1),
                         u.rowcol_to_a1(ws.row_count, ws.col_count))
        return not any(str(c).strip() for row in ws.get(rng) for c in row)
    except Exception:
        return False


def rebuild_tab(ws, headers, data, chunk=5000):
    """탭을 통째로 다시 쓴다. 증분이 어긋났을 때의 복구 경로다.

    부분 보정을 하지 않는 이유: 어긋난 시트는 **어디가 비었는지 알 수 없다.**
    중간에 빈 행이 흩어져 있으면 "몇 행부터 이어 붙일까"가 성립하지 않는다.
    """
    ws.clear()
    ws.resize(rows=max(len(data) + 1, 2), cols=len(headers))
    ws.update(values=[headers], range_name="A1")
    for i in range(0, len(data), chunk):
        ws.update(values=data[i:i + chunk], range_name="A%d" % (i + 2),
                  value_input_option="RAW")


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
    p.add_argument("--repair", action="store_true",
                   help="시트 행 수가 DB와 다르면 그 탭을 통째로 다시 쓴다")
    args = p.parse_args()

    sh, err = open_spreadsheet(args.config, args.creds)
    if err:
        reason, code = err
        if code == 3:
            reason += "\n미러만 밀린 것이고 수집·적재는 유효하다."
        print(reason, file=sys.stderr)
        sys.exit(code)
    conn = connect(args.db)
    # 프록시 표는 별도 DB에 산다 (D65-8). 파일이 없으면(아직 프록시 미사용) 그 탭은
    # 빈 테이블과 같게 다룬다 — 진행점(sync_state)은 여전히 정본 쪽에 남는다.
    ppath = proxy_db_path(args.db)
    pconn = proxy_connect(ppath) if proxy_db_exists(ppath) else None

    def src_of(table):
        return pconn if table in PROXY_TABLES else conn

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing = {ws.title: ws for ws in sh.worksheets()}
    tab_rows = []      # 안내 탭에 실을 [탭, 행수, 내용]
    mismatch, audit, repaired = [], [], []   # 미러 무결성 (D46)
    def drop_if_empty(title):
        if title in existing:
            sh.del_worksheet(existing.pop(title))
            print(f"{title}: 데이터 없음 — 탭 삭제")

    for table in FULL_TABLES:
        c = src_of(table)
        if c is None:              # proxy.db가 아직 없다 — 빈 테이블과 같다
            drop_if_empty(table)
            continue
        headers, data, _ = rows_of(c, table)
        if not data:               # 빈 테이블은 탭을 만들지 않는다 (있으면 지운다)
            drop_if_empty(table)
            continue
        ws = ensure_ws(sh, table)
        ws.clear()
        ws.update(values=[headers] + data, range_name="A1")
        tab_rows.append([table, len(data), TABLE_DESC.get(table, "")])
        got = sheet_rows(ws)
        if got is not None and got != len(data):
            audit.append((table, len(data), got))
        print(f"{table}: 전체 {len(data)}행 미러"
              + ("" if got in (None, len(data)) else f" !! 시트는 {got}행"))

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

    # 관측 테이블들은 rowid 기준 증분 append — 빈 테이블은 탭을 만들지 않는다.
    # proxy_cache는 proxy.db에서 읽지만 **진행점은 정본의 sync_state**에 남는다 —
    # 미러 상태는 미러를 도는 쪽(정본)의 것이다.
    for table in INCR_TABLES:
        c = src_of(table)
        total = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if c else 0
        if total == 0:
            drop_if_empty(table)
            continue
        tab_rows.append([table, total, TABLE_DESC.get(table, "")])
        row = conn.execute(
            "SELECT last_synced_key FROM sync_state WHERE table_name=?", (table,)).fetchone()
        last = int(row["last_synced_key"]) if row and row["last_synced_key"] else 0
        result = rows_of(c, table, since_rowid=last)
        headers, data, max_rowid = result[0], result[1], result[2]
        # `_rowid`는 증분 키일 뿐 데이터가 아니다 — 옛 이름은 뷰라
        # PRAGMA가 이것까지 돌려준다. 빼지 않으면 헤더가 데이터보다 한 칸 길어진다.
        full_headers = [d[1] for d in c.execute(f"PRAGMA table_info({table})")
                        if d[1] != "_rowid"]
        ws = ensure_ws(sh, table, len(full_headers))
        # 빈 열은 셀 한도(워크북 1천만)를 갉아먹는다 — 관측 탭은 계속 자라므로
        # 여기가 제일 먼저 막힌다. 2026-08-04 실측: observations가 156열(실제 20열)로
        # 부풀어 780만 셀을 쓰고 있었고 append가 400으로 거절당했다. 줄이니 100만이 됐다.
        #
        # **자르기 전에 정말 비었는지 읽어 본다** (PR #9 리뷰). 시트 축소는 잘린 열의
        # 데이터를 영구 삭제한다 — 팀원이 오른쪽에 메모를 적어 뒀다면 그대로 사라지고
        # 에러도 안 난다. D46이 막으려던 "조용한 손실"과 정확히 같은 종류다.
        # 값이 하나라도 있으면 **자르지 않고 그 사실을 알린다.**
        if ws.col_count > len(full_headers):
            if trailing_is_empty(ws, len(full_headers)):
                ws.resize(rows=ws.row_count, cols=len(full_headers))
            else:
                print(f"{table}: {len(full_headers)}열 뒤에 값이 있어 열을 줄이지 않았다 "
                      f"(현재 {ws.col_count}열) — 셀 한도가 걱정되면 사람이 확인하고 지워라")
        if not ws.get_values("A1:A1"):
            ws.update(values=[full_headers], range_name="A1")
        # ── 올린 뒤 실제로 늘었는지 보고, 그때만 진행점을 옮긴다 ──────────
        # 이 검사가 없어서 2026-08-04에 **시트에 4분의 1만 올라간 채 sync_state는
        # 완료를 주장**하고 있었다(DB 51,034행 / 시트 11,727행). 진행점이 앞서 나가면
        # 다음 동기화는 "이미 다 했네" 하고 넘어가고 빠진 행은 영영 안 올라간다.
        # 에러도 안 나는 종류라, 세어 보지 않으면 아무도 모른다.
        if data:
            before = sheet_rows(ws)
            # 통짜 append는 payload가 커지면 구글이 500을 낸다 — 2026-08-05 실측:
            # proxy_cache 484,216행 한 호출에 Internal error. rebuild_tab과 같은
            # 5,000행 단위로 끊는다. 중간 실패 시 진행점은 안 움직이고, 부분 반영은
            # 다음 실행의 누적 대조가 잡아 --repair로 복구한다.
            for i in range(0, len(data), 5000):
                ws.append_rows(data[i:i + 5000], value_input_option="RAW")
            after = sheet_rows(ws)
            landed = None if (before is None or after is None) else after - before
            if landed is not None and landed != len(data):
                # **진행점을 옮기지 않는다.** 다음 실행이 같은 구간을 다시 시도한다.
                mismatch.append((table, len(data), landed))
                print(f"{table}: !! {len(data)}행을 올렸는데 시트는 {landed}행 늘었다 "
                      f"— 진행점을 옮기지 않는다 (--repair 로 재구축)")
            else:
                conn.execute(
                    "INSERT INTO sync_state VALUES (?, ?, ?) "
                    "ON CONFLICT(table_name) DO UPDATE SET "
                    "last_synced_key=excluded.last_synced_key, "
                    "updated_at=excluded.updated_at", (table, str(max_rowid), now))
                conn.commit()
                print(f"{table}: 증분 {len(data)}행 append (rowid ≤ {max_rowid})")
        else:
            print(f"{table}: 새 관측 없음")

        # 총계 대조 — 증분이 아니라 **누적**이 맞는지 본다. 과거에 어긋난 것도 여기서 걸린다
        got = sheet_rows(ws)
        if got is not None and got != total:
            audit.append((table, total, got))
            if args.repair:
                hdr, alldata, maxr = rows_of(c, table)
                rebuild_tab(ws, hdr, alldata)
                conn.execute(
                    "INSERT INTO sync_state VALUES (?, ?, ?) ON CONFLICT(table_name) "
                    "DO UPDATE SET last_synced_key=excluded.last_synced_key, "
                    "updated_at=excluded.updated_at", (table, str(maxr), now))
                conn.commit()
                repaired.append((table, total, got))
                audit.pop()
                print(f"{table}: 재구축 {total}행 (시트에 {got}행뿐이었다)")

    # 안내 탭 — 스토리 현황과 탭 가이드를 사람이 읽게 쓴다
    guide = [[NOTICE], [f"마지막 동기화: {now}"], [""],
             ["■ 스토리 현황", "", ""]] + story_rows + [
             [""], ["■ 탭 안내", "행수", "내용"]] + [[t, str(n), d] for t, n, d in tab_rows]
    ws = ensure_ws(sh, "안내", 4)
    ws.clear()
    ws.update(values=guide, range_name="A1")
    print(f"안내: 스토리 현황 {len(story_rows)}줄 + 탭 가이드 {len(tab_rows)}줄")

    # ── 무결성 판정 (D46) ─────────────────────────────────────────────────
    # **어긋난 채로 조용히 끝나지 않는다.** 미러가 "됐다"고 말하면 팀원은 시트를
    # 믿는다. 실제로 안 올라간 4만 행이 있어도 에러가 안 나면 아무도 모른다.
    for t, want, got in repaired:
        print(f"복구: {t} — 시트 {got}행 → DB와 같은 {want}행으로 재구축")
    if mismatch or audit:
        print("\n!! 시트와 DB가 어긋난다 — 시트를 믿지 마라", file=sys.stderr)
        for t, want, got in mismatch:
            print(f"   {t}: {want}행을 올렸는데 {got}행만 늘었다", file=sys.stderr)
        for t, want, got in audit:
            print(f"   {t}: DB {want:,}행 / 시트 {got:,}행 (차이 {want-got:+,})",
                  file=sys.stderr)
        print("   고치려면: python3 sync_sheets.py --repair", file=sys.stderr)
        sys.exit(1)
    print("무결성 확인: 미러한 전 탭의 행 수가 DB와 일치한다")


if __name__ == "__main__":
    main()
