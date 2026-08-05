#!/usr/bin/env python3
"""intel 정본 DB — SQLite. SPEC-INTEL §2 구현.

수집 파이프라인에서의 위치: 수집 → raw JSON → 검증 → [여기] DB 적재 → 시트 미러

    python3 intel_db.py init
    python3 intel_db.py load data/raw/musinsa-brand-linesheet-인사일런스-20260731-1400.json
    python3 intel_db.py check --site musinsa --context "ranking:바지" --cycle-minutes 30
    python3 intel_db.py reuse-attrs data/raw/<수집>.json --out data/raw/<수집>-reused.json
    python3 intel_db.py stale-static --site musinsa
    python3 intel_db.py import-snapshots data/snapshots
    python3 intel_db.py export --table products --format csv
    python3 intel_db.py stats
    python3 intel_db.py merge <팀원의 intel.db>     # 각자 모은 것을 합친다 (D31)

재사용 규칙(SPEC-INTEL §2-2):
  정적 속성 TTL 90일 · 시변 값 스킵 창 = 사이트 갱신 주기(미상 24시간).
  사용자가 명시적으로 재수집을 요구하면 이 판정을 건너뛴다 — 그 판단은 스킬이 한다.
"""
import argparse
import csv
import io
import json
import os
import sqlite3
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# 작업 폴더 기준이다(스킬 §파일 규약). 스크립트 위치 기준이 아니므로
# 스킬을 어디에 설치하든 DB는 그 작업의 data/ 아래에 생긴다.
DEFAULT_DB = os.environ.get("INTEL_DB", "data/intel.db")
STATIC_TTL_DAYS = 90          # D7
DEFAULT_CYCLE_MINUTES = 1440  # D8 — 갱신 주기 미상일 때 24시간

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from schema_v2 import (SCHEMA_V2, TRIGGERS_V2, VIEWS_V2,   # noqa: E402
                       rowid_parts)

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    site TEXT NOT NULL,
    product_id TEXT NOT NULL,
    name TEXT, url TEXT, image_url TEXT, brand TEXT, category TEXT,
    attributes TEXT,          -- JSON 문자열 (예: {"핏": "와이드"})
    attributes_basis TEXT,    -- name/detail/image/group/unknown
    static_verified_at TEXT,  -- 정적 속성 TTL 기준 시각
    first_seen_at TEXT, last_seen_at TEXT,
    PRIMARY KEY (site, product_id)
);
CREATE TABLE IF NOT EXISTS observations (
    site TEXT NOT NULL,
    product_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    context TEXT NOT NULL,    -- 예: "ranking:바지" · "brand:인사일런스" · "market:데님팬츠(남성)"
    price_original INTEGER, price_sale INTEGER, discount_rate INTEGER,
    review_count INTEGER, rating REAL,
    view_count INTEGER, view_count_display TEXT,
    purchase_count INTEGER, purchase_count_display TEXT,
    like_count INTEGER, like_count_display TEXT,
    viewers_now INTEGER, buyers_now INTEGER,
    sold_out INTEGER,         -- 0/1/NULL(미노출)
    rank INTEGER,
    run_id TEXT,
    PRIMARY KEY (site, product_id, observed_at, context)
);
CREATE INDEX IF NOT EXISTS idx_obs_context ON observations (site, context, observed_at);
CREATE TABLE IF NOT EXISTS variants (          -- 옵션 구성 (정적)
    site TEXT NOT NULL,
    product_id TEXT NOT NULL,
    option_id TEXT NOT NULL,
    option_name TEXT, color TEXT, size TEXT,
    first_seen_at TEXT, last_seen_at TEXT,
    PRIMARY KEY (site, product_id, option_id)
);
CREATE TABLE IF NOT EXISTS variant_observations (  -- 옵션별 재고 관측 (append only)
    site TEXT NOT NULL,
    product_id TEXT NOT NULL,
    option_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    sold_out INTEGER,
    stock_qty INTEGER, stock_display TEXT,
    stock_basis TEXT,          -- option_api/probe_read/probe_cart/unknown
    run_id TEXT,
    PRIMARY KEY (site, product_id, option_id, observed_at)
);
CREATE TABLE IF NOT EXISTS product_attributes (  -- 동적 속성 (D35 — 2026-08-03)
    -- 스키마 변경 없이 분석 축을 늘린다. 핏 하나만 담던 products.attributes(JSON)로는
    -- 컬러·소재·넥라인·시즌을 더할 때마다 컬럼을 추가해야 했다. 축을 행으로 뺀다.
    -- proxy_cache와 같은 모양이라, AI 프록시 판정(D19) 결과도 그대로 이 표로 들어온다.
    site TEXT NOT NULL,
    product_id TEXT NOT NULL,
    attr_name TEXT NOT NULL,      -- "핏" · "컬러" · "소재" · "넥라인" …
    value TEXT,                   -- 판정 값 (null = 판정 실패는 저장하지 않는다)
    basis TEXT,                   -- name/detail/image/group/proxy/unknown
    decided_at TEXT,              -- TTL 기준 시각
    ttl_days INTEGER,             -- 속성별 만료(컬러는 안 변함=크게, 시즌은 짧게). null=기본 90
    PRIMARY KEY (site, product_id, attr_name)
);
CREATE INDEX IF NOT EXISTS idx_attr_name ON product_attributes (attr_name, value);
CREATE TABLE IF NOT EXISTS platforms (
    platform_key TEXT PRIMARY KEY,   -- 예: "musinsa" · "zigzag"
    name TEXT, url TEXT, engine TEXT,
    discovered_for_brand TEXT,
    recon TEXT,               -- 정찰 결과 JSON (channel-scout 산출)
    skill_status TEXT,        -- none/candidate/recon_done/draft/ready
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    site TEXT, story TEXT, target TEXT,
    collected_at TEXT, item_count INTEGER, source_total INTEGER,
    incomplete INTEGER, notes TEXT, raw_file TEXT, loaded_at TEXT
);
CREATE TABLE IF NOT EXISTS proxy_defs (       -- 파생 프록시 정의 카드 (D19)
    proxy_name TEXT PRIMARY KEY,
    question TEXT, material TEXT,  -- name/image/badge/detail
    value_space TEXT,              -- JSON 배열 또는 "numeric"
    method TEXT,                   -- rule/vision/llm
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS proxy_cache (       -- 판정 캐시 — 재료 지문이 같으면 재사용
    proxy_name TEXT NOT NULL,
    site TEXT NOT NULL, product_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,     -- 판정 재료 식별자(image_url·name 등)
    value TEXT, basis TEXT, judged_at TEXT,
    PRIMARY KEY (proxy_name, site, product_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS insights (       -- 인사이트 엔진 산출 (D28)
    -- 팀원이 읽는 창구는 시트다(D31 개정 2026-08-03). PDF는 네 손에서만 나오므로
    -- 결과가 DB를 거쳐야 미러가 실어 나른다. 파이프라인 원칙과 같다 — 무엇도 DB를
    -- 건너뛰지 않는다.
    run_stamp TEXT NOT NULL,       -- 같은 실행의 결과를 묶는 키 (YYYYMMDD-HHmm)
    target TEXT NOT NULL,          -- 리포트 대상 이름
    context TEXT,                  -- 관측 문맥 (쉼표 구분)
    verdict TEXT NOT NULL,         -- strong / weak / rejected
    idx INTEGER NOT NULL,          -- 그 갈래 안의 순번 (1부터)
    claim TEXT, audience TEXT,
    effect REAL, effect_kind TEXT, n INTEGER, p REAL,
    holdout TEXT, fails TEXT, recheck TEXT,
    detail_pdf TEXT, detail_page INTEGER,
    created_at TEXT,
    PRIMARY KEY (run_stamp, target, verdict, idx)
);
CREATE TABLE IF NOT EXISTS sync_state (
    table_name TEXT PRIMARY KEY,
    last_synced_key TEXT,     -- observations는 마지막 rowid, 나머지는 마지막 전체 미러 시각
    updated_at TEXT
);
"""

# v2에서도 그대로 쓰는 공용 표들 — 사전으로 접을 반복 텍스트가 없다.
# 새 DB를 v2로 만들 때 SCHEMA_V2와 함께 실행한다.
SCHEMA_TAIL = """
CREATE TABLE IF NOT EXISTS platforms (
    platform_key TEXT PRIMARY KEY,   -- 예: "musinsa" · "zigzag"
    name TEXT, url TEXT, engine TEXT,
    discovered_for_brand TEXT,
    recon TEXT,               -- 정찰 결과 JSON (channel-scout 산출)
    skill_status TEXT,        -- none/candidate/recon_done/draft/ready
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    site TEXT, story TEXT, target TEXT,
    collected_at TEXT, item_count INTEGER, source_total INTEGER,
    incomplete INTEGER, notes TEXT, raw_file TEXT, loaded_at TEXT
);
CREATE TABLE IF NOT EXISTS proxy_defs (       -- 파생 프록시 정의 카드 (D19)
    proxy_name TEXT PRIMARY KEY,
    question TEXT, material TEXT,  -- name/image/badge/detail
    value_space TEXT,              -- JSON 배열 또는 "numeric"
    method TEXT,                   -- rule/vision/llm
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS proxy_cache (       -- 판정 캐시 — 재료 지문이 같으면 재사용
    proxy_name TEXT NOT NULL,
    site TEXT NOT NULL, product_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,     -- 판정 재료 식별자(image_url·name 등)
    value TEXT, basis TEXT, judged_at TEXT,
    PRIMARY KEY (proxy_name, site, product_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS insights (       -- 인사이트 엔진 산출 (D28)
    -- 팀원이 읽는 창구는 시트다(D31 개정 2026-08-03). PDF는 네 손에서만 나오므로
    -- 결과가 DB를 거쳐야 미러가 실어 나른다. 파이프라인 원칙과 같다 — 무엇도 DB를
    -- 건너뛰지 않는다.
    run_stamp TEXT NOT NULL,       -- 같은 실행의 결과를 묶는 키 (YYYYMMDD-HHmm)
    target TEXT NOT NULL,          -- 리포트 대상 이름
    context TEXT,                  -- 관측 문맥 (쉼표 구분)
    verdict TEXT NOT NULL,         -- strong / weak / rejected
    idx INTEGER NOT NULL,          -- 그 갈래 안의 순번 (1부터)
    claim TEXT, audience TEXT,
    effect REAL, effect_kind TEXT, n INTEGER, p REAL,
    holdout TEXT, fails TEXT, recheck TEXT,
    detail_pdf TEXT, detail_page INTEGER,
    created_at TEXT,
    PRIMARY KEY (run_stamp, target, verdict, idx)
);
CREATE TABLE IF NOT EXISTS sync_state (
    table_name TEXT PRIMARY KEY,
    last_synced_key TEXT,     -- observations는 마지막 rowid, 나머지는 마지막 전체 미러 시각
    updated_at TEXT
);
"""

STATIC_FIELDS = ("name", "url", "image_url", "brand", "category")
# 속성별 ttl_days는 **명시된 것만 저장한다(미지정=NULL)**. NULL이면 reuse-attrs의
# 전역 --ttl-days(기본 90=STATIC_TTL_DAYS)를 따른다 — 그래야 --ttl-days 0 같은 명시
# 전역값이 무시되지 않는다(2026-08-03 버그 수정). 긴 TTL이 필요한 속성(컬러·lifecycle)은
# set-attrs/tag-lifecycle이 ttl_days를 명시해 넣는다.
OBS_FIELDS = (
    "price_original", "price_sale", "discount_rate", "review_count", "rating",
    "view_count", "view_count_display", "purchase_count", "purchase_count_display",
    "like_count", "like_count_display", "viewers_now", "buyers_now", "sold_out", "rank",
)


def _is_v2(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name='obs_base'").fetchone())


def _is_view(conn, name):
    """그 이름이 뷰인가. **뷰에는 업서트를 못 쓴다**(v2에서 옛 이름이 뷰가 됐다 — D45)."""
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return bool(row) and row[0] == "view"


def _migrate_proxy_label(conn):
    """프록시의 **사람이 읽는 축 이름** 컬럼 (D56).

    리포트에 `denim_rise(AI)`가 그대로 찍혀 나왔다 — 2026-08-04 사용자 지적:
    "읽는 사람 입장에서 의미 없는 단어들은 빼". 카드가 `label`을 주면 그걸 쓰고,
    없으면 `proxy_name`으로 되돌아간다.

    **v1·v2 양쪽에서 돈다.** 처음엔 구 스키마 분기에만 넣었다가 v2 DB에서
    `no column named label`로 죽었다(회귀 4건). 스키마 계열과 무관한
    부가 컬럼이니 분기 앞에서 한 번만 손본다. 멱등이다.
    """
    try:
        conn.execute("ALTER TABLE proxy_defs ADD COLUMN label TEXT")
    except sqlite3.OperationalError:
        pass          # 이미 있거나 표가 아직 없다 — 둘 다 정상


def connect(db_path):
    """DB를 연다. **v2 스키마면 뷰·트리거를 얹어 옛 이름 그대로 읽고 쓰게 한다**(D45).

    구 스키마 DB는 그대로 둔다 — 자동으로 갈아엎지 않는다. 이관은 `migrate_v2.py`가
    새 파일에 짓고 검산이 통과한 뒤 사람이 바꿔 끼운다. 정본을 조용히 바꾸지 않는다.
    """
    parent = Path(db_path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    empty = not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
    if empty:
        # **새 DB는 v2로 만든다.** 구 스키마로 시작하면 나중에 또 옮겨야 한다.
        conn.executescript(SCHEMA_V2)
        conn.executescript(SCHEMA_TAIL)   # runs·platforms·proxy_*·insights·sync_state
    _migrate_proxy_label(conn)   # v1·v2 공통 (D56)
    if _is_v2(conn):
        # 뷰·트리거는 매번 다시 만든다(멱등) — 스크립트가 갱신되면 곧바로 반영된다
        conn.executescript(VIEWS_V2)
        conn.executescript(TRIGGERS_V2)
        conn.commit()
        return conn
    conn.executescript(SCHEMA)
    try:  # 구버전 DB 마이그레이션 — raw_extras(재료 보존, D19)
        conn.execute("ALTER TABLE products ADD COLUMN raw_extras TEXT")
    except sqlite3.OperationalError:
        pass
    _migrate_attrs(conn)
    # 구버전 마이그레이션·set-attrs가 속성에 ttl_days=90을 강제로 넣었다 — 그러면
    # reuse-attrs의 전역 --ttl-days(예: 0)가 무시된다. 90을 NULL로 정리해 전역 TTL을
    # 따르게 한다. 멱등(이미 NULL이면 no-op). 명시 TTL(lifecycle 3650·컬러 등)은 90이
    # 아니라 건드리지 않는다. 현행 코드는 90을 강제하지 않으므로(미지정=NULL) 앞으로
    # 90은 생기지 않는다 — 이 정리는 구 데이터용 일회성이다.
    try:
        conn.execute("UPDATE product_attributes SET ttl_days=NULL WHERE ttl_days=90")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return conn


def _migrate_attrs(conn):
    """products.attributes(JSON) → product_attributes(행) 소급 이관 (D35).

    한 번만 옮기면 된다 — 이미 옮긴 (site, product_id, attr_name)은 건너뛴다(멱등).
    **판정 실패(`null`)는 옮기지 않는다.** `{"핏": null}`은 "판정 못 함"이라 재판정
    대상이지 저장 대상이 아니다 — 저장하면 TTL 안에 재시도가 막힌다.
    기존 JSON은 지우지 않는다(폐기 예정 리포트가 아직 읽는다 — 하위호환).
    """
    done = conn.execute(
        "SELECT COUNT(*) FROM product_attributes WHERE basis='migrated'").fetchone()[0]
    if done:
        return
    rows = conn.execute(
        "SELECT site, product_id, attributes, attributes_basis, static_verified_at "
        "FROM products WHERE attributes IS NOT NULL AND attributes != '{}'").fetchall()
    moved = 0
    for r in rows:
        try:
            attrs = json.loads(r["attributes"] or "{}")
        except (TypeError, ValueError):
            continue
        for name, value in attrs.items():
            if value is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO product_attributes "
                "(site, product_id, attr_name, value, basis, decided_at, ttl_days) "
                "VALUES (?,?,?,?,?,?,?)",
                (r["site"], r["product_id"], name, value,
                 r["attributes_basis"] or "migrated",
                 r["static_verified_at"], None))   # ttl_days NULL — 전역 reuse TTL을 따른다
            moved += 1
    if moved:
        # 이관 표식 — 다음 connect에서 다시 돌지 않게 (기존 basis는 보존)
        conn.execute("UPDATE product_attributes SET basis='migrated' "
                     "WHERE basis IS NULL")
        conn.commit()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip()[:19], fmt)
        except (ValueError, AttributeError):
            continue
    return None


STORY_PREFIX = {"brand-linesheet": "brand", "market-scan": "market",
                "ranking-snapshot": "ranking"}


def context_of(meta):
    """`story`로 접두사를 붙이고 `target`을 잇는다 — `brand:로우클래식`.

    **`target`에는 이름만 들어간다.** 접두사까지 넣으면 `brand:brand:로우클래식`이
    되어 같은 대상이 두 문맥으로 갈린다(2026-08-04에 실제로 3개 문맥이 그렇게
    들어갔다). 수집기가 실수로 붙여 와도 여기서 걷어낸다 — 조용히 갈리는 것보다
    낫다. 다른 접두사가 붙어 오면 그건 의도일 수 있으니 건드리지 않는다.
    """
    story = meta.get("story", "")
    target = str(meta.get("target", "") or "")
    prefix = STORY_PREFIX.get(story, story or "adhoc")
    if target.startswith(prefix + ":"):
        target = target[len(prefix) + 1:]
    return f"{prefix}:{target}"


def run_context(row):
    """runs 행(story·target) → observations의 context 문자열. context_of와 같은 규칙이다."""
    story = (row.get("story") or "").strip()
    target = (row.get("target") or "").strip()
    return f"{STORY_PREFIX.get(story, story or 'adhoc')}:{target}"


def load_file(conn, path, quiet=False):
    """데이터 계약 JSON 1개를 적재한다. 반환: (신규 관측 수, 스킵된 중복 관측 수)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    meta, items = data.get("meta", {}), data.get("items", [])
    site = meta.get("site")
    collected_at = meta.get("collected_at") or now_str()
    ctx = context_of(meta)
    if not site:
        raise SystemExit(f"{path}: meta.site가 없다 — 데이터 계약 위반")

    run_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, site, meta.get("story"), meta.get("target"), collected_at,
         meta.get("item_count"), meta.get("source_total"),
         1 if meta.get("incomplete") else 0,
         json.dumps(meta.get("notes", []), ensure_ascii=False), str(path), now_str()),
    )

    new_obs = dup_obs = n_var = 0
    for it in items:
        pid = str(it.get("product_id", "")).strip()
        if not pid:
            continue
        _upsert_product(conn, site, pid, it, collected_at)
        if isinstance(it.get("variants"), list):
            n_var += _load_variants(conn, site, pid, it["variants"], collected_at, run_id)
        cols = [it.get(f) for f in OBS_FIELDS]
        so = it.get("sold_out")
        cols[OBS_FIELDS.index("sold_out")] = None if so is None else (1 if so else 0)
        try:
            conn.execute(
                f"INSERT INTO observations (site, product_id, observed_at, context, "
                f"{', '.join(OBS_FIELDS)}, run_id) VALUES ({', '.join('?' * (len(OBS_FIELDS) + 5))})",
                (site, pid, collected_at, ctx, *cols, run_id),
            )
            new_obs += 1
        except sqlite3.IntegrityError:
            dup_obs += 1
    conn.commit()
    if not quiet:
        var_msg = f", 옵션 관측 {n_var}건" if n_var else ""
        print(f"{Path(path).name}: 관측 {new_obs}건 적재, 중복 {dup_obs}건 스킵{var_msg} (context={ctx})")
    return new_obs, dup_obs


def _load_variants(conn, site, pid, variants, collected_at, run_id):
    """variants[]를 정적(variants)·시변(variant_observations)으로 나눠 적재한다."""
    n = 0
    for v in variants:
        oid = str(v.get("option_id") or v.get("option_name") or "").strip()
        if not oid:
            continue
        # **알던 값을 None으로 덮지 않는다.** `OR REPLACE`는 행을 지우고 새로 넣어서
        # 이번에 안 온 옵션명·색상·사이즈를 NULL로 만든다(v2 트리거는 COALESCE를
        # 유지하므로 두 스키마의 동작이 갈렸다 — PR #9 리뷰). 병합을 여기서 한다.
        prev = conn.execute(
            "SELECT option_name, color, size, first_seen_at FROM variants "
            "WHERE site=? AND product_id=? AND option_id=?", (site, pid, oid)).fetchone()
        keep = lambda new, old: new if new not in (None, "") else old
        conn.execute(
            "INSERT OR REPLACE INTO variants (site, product_id, option_id, option_name,"
            " color, size, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
            (site, pid, oid,
             keep(v.get("option_name"), prev["option_name"] if prev else None),
             keep(v.get("color"), prev["color"] if prev else None),
             keep(v.get("size"), prev["size"] if prev else None),
             (prev["first_seen_at"] if prev else None) or collected_at, collected_at),
        )
        so = v.get("sold_out")
        try:
            conn.execute(
                "INSERT INTO variant_observations (site, product_id, option_id, observed_at,"
                " sold_out, stock_qty, stock_display, stock_basis, run_id)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (site, pid, oid, v.get("observed_at") or collected_at,
                 None if so is None else (1 if so else 0),
                 v.get("stock_qty"), v.get("stock_display"),
                 v.get("stock_basis"), run_id),
            )
            n += 1
        except sqlite3.IntegrityError:
            pass
    return n


def _upsert_product(conn, site, pid, it, seen_at):
    row = conn.execute(
        "SELECT * FROM products WHERE site=? AND product_id=?", (site, pid)
    ).fetchone()
    attrs = it.get("attributes")
    basis = it.get("attributes_basis")
    incoming_has_attrs = bool(attrs) and any(
        v not in (None, "", "unknown") for v in attrs.values()
    )
    extras = it.get("raw_extras")
    if row is None:
        conn.execute(
            "INSERT INTO products (site, product_id, name, url, image_url, brand, category, "
            "attributes, attributes_basis, static_verified_at, first_seen_at, last_seen_at, "
            "raw_extras) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (site, pid, it.get("name"), it.get("url"), it.get("image_url"),
             it.get("brand"), it.get("category"),
             json.dumps(attrs, ensure_ascii=False) if attrs else None, basis,
             seen_at, seen_at, seen_at,
             json.dumps(extras, ensure_ascii=False) if extras else None),
        )
        return
    # 정적 필드는 새 값이 비어 있지 않을 때만 덮는다.
    updates, params = [], []
    for f in STATIC_FIELDS:
        v = it.get(f)
        if v not in (None, ""):
            updates.append(f"{f}=?")
            params.append(v)
    # attributes: 들어온 값이 실질값이면 덮고, 아니면 기존(비싼 판단)을 지킨다.
    if incoming_has_attrs:
        updates += ["attributes=?", "attributes_basis=?"]
        params += [json.dumps(attrs, ensure_ascii=False), basis]
    if extras:
        updates.append("raw_extras=?")
        params.append(json.dumps(extras, ensure_ascii=False))
    updates += ["static_verified_at=?", "last_seen_at=?"]
    params += [seen_at, seen_at, site, pid]
    conn.execute(
        f"UPDATE products SET {', '.join(updates)} WHERE site=? AND product_id=?", params
    )


def team_coverage(conn, site, context, cycle, config, creds):
    """시트의 runs 탭에서 '남이 이미 수집한 것'을 찾는다(D32).

    시트는 완료된 수집만 안다 — runs 행은 적재 후에 생기고 그 다음 미러 때 올라간다.
    따라서 "지금 남이 수집 중"은 여기서 안 잡힌다. 조회에 실패하면 예외 대신
    consulted=False를 돌려준다 — 팀 조회 실패가 수집을 막아서는 안 된다.
    """
    out = {"consulted": False, "fresh": False, "last_collected_at": None, "run_count": 0}
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from sync_sheets import fetch_tab, open_spreadsheet
    except ImportError as e:
        out["error"] = f"sync_sheets를 불러올 수 없다: {e}"
        return out
    sh, err = open_spreadsheet(config, creds)
    if err:
        out["error"] = err[0]
        return out
    try:
        rows = fetch_tab(sh, "runs")
    except Exception as e:
        out["error"] = f"runs 탭 조회 실패: {e}"
        return out
    if rows is None:
        out["error"] = "시트에 runs 탭이 없다 — 아직 미러가 돌지 않았다(sync_sheets.py)"
        return out

    out["consulted"] = True
    mine = {r[0] for r in conn.execute("SELECT run_id FROM runs")}
    cutoff = datetime.now() - timedelta(minutes=cycle)
    latest, count = None, 0
    for r in rows:
        if (r.get("site") or "").strip() != site or run_context(r) != context:
            continue
        if (r.get("run_id") or "").strip() in mine:
            continue        # 내가 올린 것 — 이미 로컬 판정이 봤다
        ts = parse_ts(r.get("collected_at") or "")
        if not ts:
            continue
        count += 1
        if latest is None or ts > latest:
            latest = ts
    out["run_count"] = count
    if latest:
        out["last_collected_at"] = latest.strftime("%Y-%m-%d %H:%M:%S")
        out["fresh"] = latest >= cutoff
    return out


def cmd_check(conn, args):
    """시변 값 스킵 판정. exit 0 = 스킵 가능(신선한 관측 있음), 1 = 수집 필요."""
    cycle = args.cycle_minutes or DEFAULT_CYCLE_MINUTES
    row = conn.execute(
        "SELECT MAX(observed_at) AS last FROM observations WHERE site=? AND context=?",
        (args.site, args.context),
    ).fetchone()
    last = parse_ts(row["last"]) if row and row["last"] else None
    fresh = bool(last and datetime.now() - last <= timedelta(minutes=cycle))
    result = {
        "skip": fresh,
        "source": "local" if fresh else None,
        "last_observed_at": row["last"] if row else None,
        "cycle_minutes": cycle,
        "reason": (
            f"최신 관측이 갱신 주기({cycle}분) 이내 — 재수집 생략 가능" if fresh
            else "신선한 관측 없음 — 수집 필요"
        ),
    }
    if args.team and not fresh:   # 로컬이 이미 신선하면 시트를 볼 이유가 없다
        team = team_coverage(conn, args.site, args.context, cycle, args.config, args.creds)
        result["team"] = team
        if not team["consulted"]:
            print(f"팀 커버리지 조회 실패 — 로컬 DB만으로 판정한다: {team.get('error')}",
                  file=sys.stderr)
            result["reason"] += " (팀 커버리지 미확인)"
        elif team["fresh"]:
            fresh = True
            result.update(skip=True, source="team", reason=(
                f"팀원이 {team['last_collected_at']}에 이미 수집했다(갱신 주기 {cycle}분 이내) "
                f"— 중복 수집이다. 시트 미러를 당겨 쓰거나 팀원 DB를 merge한다"))
        elif team["run_count"]:
            result["reason"] += (
                f" (팀 수집 이력 {team['run_count']}건 있으나 최신이 {team['last_collected_at']} "
                f"— 갱신 주기 밖이다)")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if fresh else 1


def cmd_reuse_attrs(conn, args):
    """raw JSON의 미분류 상품에 DB의 TTL 유효 정적 속성을 채워 넣는다(핏 재분류 절감)."""
    data = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    site = data.get("meta", {}).get("site")
    cutoff = (datetime.now() - timedelta(days=args.ttl_days)).strftime("%Y-%m-%d %H:%M:%S")
    filled = expired = 0
    for it in data.get("items", []):
        attrs = it.get("attributes") or {}
        if any(v not in (None, "", "unknown") for v in attrs.values()):
            continue  # 이미 분류돼 있음
        pid = str(it.get("product_id"))
        # D35: 동적 속성 표를 먼저 본다 — 속성마다 TTL이 다르다(컬러는 안 변함, 시즌은 짧음).
        arows = conn.execute(
            "SELECT attr_name, value, basis, decided_at, ttl_days FROM product_attributes "
            "WHERE site=? AND product_id=? AND value IS NOT NULL",
            (site, pid)).fetchall()
        picked, any_expired = {}, False
        for a in arows:
            # 속성별 TTL이 명시돼 있으면(컬러 3650·lifecycle 3650 등) 그것, 없으면(NULL)
            # reuse의 전역 --ttl-days. `or`는 0을 falsy로 흘려 0일 TTL을 무시했다.
            ttl = a["ttl_days"] if a["ttl_days"] is not None else args.ttl_days
            a_cut = (datetime.now() - timedelta(days=ttl)).strftime("%Y-%m-%d %H:%M:%S")
            if a["decided_at"] and a["decided_at"] < a_cut:
                any_expired = True
                continue
            picked[a["attr_name"]] = (a["value"], a["basis"])
        if picked:
            it["attributes"] = {k: v[0] for k, v in picked.items()}
            it["attributes_basis"] = next(iter(picked.values()))[1]
            filled += 1
            continue
        if any_expired:
            expired += 1
            continue
        # 폴백: 아직 표로 안 옮겨진 구버전 JSON (하위호환)
        row = conn.execute(
            "SELECT attributes, attributes_basis, static_verified_at FROM products "
            "WHERE site=? AND product_id=? AND attributes IS NOT NULL",
            (site, pid),
        ).fetchone()
        if not row:
            continue
        if row["static_verified_at"] and row["static_verified_at"] < cutoff:
            expired += 1
            continue
        db_attrs = json.loads(row["attributes"])
        if any(v not in (None, "", "unknown") for v in db_attrs.values()):
            it["attributes"] = db_attrs
            it["attributes_basis"] = row["attributes_basis"]
            filled += 1
    data.setdefault("meta", {}).setdefault("notes", []).append(
        f"DB 재사용: 속성 {filled}건 채움 (TTL {args.ttl_days}일, 만료로 제외 {expired}건)"
    )
    out = args.out or args.raw
    Path(out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"속성 재사용 {filled}건, TTL 만료 {expired}건 → {out}")


def cmd_set_attrs(conn, args):
    """속성 판정 결과를 product_attributes에 적재한다 (D35).

    입력 JSON: [{"site","product_id","attr_name","value","basis","ttl_days"?}, ...]
    프록시 판정(D19)·핏 재분류·컬러 추출 등 **모든 속성이 이 한 통로로** 들어온다.
    value가 null/빈값이면 건너뛴다 — 판정 실패는 저장하지 않는다(재판정을 막지 않으려고).
    """
    items = json.loads(Path(args.file).read_text(encoding="utf-8"))
    now = now_str()
    added = skipped = 0
    for it in items:
        val = it.get("value")
        if val in (None, "", "unknown"):
            skipped += 1
            continue
        conn.execute(
            # ON CONFLICT 없음 — v2는 뷰 트리거가, 구 스키마는 OR REPLACE가 진다
            "INSERT OR REPLACE INTO product_attributes "
            "(site, product_id, attr_name, value, basis, decided_at, ttl_days) "
            "VALUES (?,?,?,?,?,?,?)",
            (it["site"], str(it["product_id"]), it["attr_name"], val,
             it.get("basis") or "unknown", now, it.get("ttl_days")))   # 미지정이면 NULL
        added += 1
    conn.commit()
    print(f"속성 적재 {added}건, 미판정 건너뜀 {skipped}건")


def cmd_tag_lifecycle(conn, args):
    """자사 상품에 lifecycle(온고잉/시즌)을 태깅한다 (D35 재활용).

    lifecycle은 별도 컬럼이 아니라 product_attributes의 한 속성이다 — 방금 만든 동적
    속성 표(D35)가 정확히 이런 용도다. 스키마를 또 늘리지 않는다.

    자사 목록(assets/own-brand.json)의 스타일명을 match_key로 정규화해 products의
    상품명과 대조한다. 색상 변형은 스타일로 접히므로 스타일명만 맞으면 된다.
    같은 자사 상품이 여러 사이트에 있으면 각 사이트의 product_id에 각각 붙는다.

    **DB에 없는 상품은 지금 태깅되지 않는다 — 그건 오류가 아니다.** 자사몰을 아직 안
    모았으면 대부분이 DB에 없고, 수집 후 이 명령을 다시 돌리면 채워진다. lifecycle에
    TTL을 길게 두는 이유도 이것이다(상품 성격은 잘 안 바뀐다).
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from intel_data import match_key

    own_path = args.file or str(
        Path(__file__).resolve().parents[1] / "assets" / "own-brand.json")
    own = json.loads(Path(own_path).read_text(encoding="utf-8"))
    want = {}
    for lc in ("ongoing", "seasonal"):
        for style in own.get(lc, []):
            want[match_key(style)] = lc

    # 자사 브랜드로 좁힌다 — 다른 브랜드에 같은 스타일명이 있으면 오태깅을 막는다.
    aliases = {a.lower() for a in own.get("brand_aliases", [])}
    rows = conn.execute("SELECT site, product_id, name, brand FROM products "
                        "WHERE name IS NOT NULL").fetchall()
    now = now_str()
    tagged = defaultdict(int)
    unmatched_styles = set(want)
    for r in rows:
        key = match_key(r["name"])
        lc = want.get(key)
        if not lc:
            continue
        # 브랜드가 있으면 자사 브랜드일 때만(오태깅 방지). 브랜드 미상이면 이름 일치만으로
        if r["brand"] and aliases and r["brand"].lower() not in aliases:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO product_attributes "
            "(site, product_id, attr_name, value, basis, decided_at, ttl_days) "
            "VALUES (?,?,?,?,?,?,?)",
            (r["site"], r["product_id"], "lifecycle", lc, "own-brand-list", now, 3650))
        tagged[lc] += 1
        unmatched_styles.discard(key)
    conn.commit()
    print("lifecycle 태깅: 온고잉 %d · 시즌 %d (자사 스타일 %d종 중 DB 매칭)"
          % (tagged["ongoing"], tagged["seasonal"], len(want)))
    if unmatched_styles:
        print("  DB에 아직 없는 스타일 %d종 — 자사몰 수집 후 재실행하면 채워진다"
              % len(unmatched_styles))


def cmd_attr_stats(conn, _args):
    """속성별 판정 현황 — 어느 축이 얼마나 채워졌나."""
    rows = conn.execute(
        "SELECT attr_name, COUNT(*) n, COUNT(DISTINCT value) vals "
        "FROM product_attributes GROUP BY attr_name ORDER BY n DESC").fetchall()
    if not rows:
        print("product_attributes 비어 있음")
        return
    for r in rows:
        top = conn.execute(
            "SELECT value, COUNT(*) c FROM product_attributes WHERE attr_name=? "
            "GROUP BY value ORDER BY c DESC LIMIT 5", (r["attr_name"],)).fetchall()
        dist = ", ".join(f"{t['value']}({t['c']})" for t in top)
        print(f"{r['attr_name']:8} {r['n']:>7}건 · 값 {r['vals']}종 · {dist}")


def cmd_stale_static(conn, args):
    cutoff = (datetime.now() - timedelta(days=args.ttl_days)).strftime("%Y-%m-%d %H:%M:%S")
    q = ("SELECT site, product_id, name, static_verified_at FROM products "
         "WHERE (static_verified_at IS NULL OR static_verified_at < ?)")
    params = [cutoff]
    if args.site:
        q += " AND site=?"
        params.append(args.site)
    rows = conn.execute(q + " ORDER BY static_verified_at", params).fetchall()
    print(json.dumps(
        [dict(r) for r in rows], ensure_ascii=False, indent=2))
    print(f"# TTL {args.ttl_days}일 만료 {len(rows)}건 — 다음 수집에서 재확인 대상", file=sys.stderr)


def cmd_import_snapshots(conn, args):
    files = sorted(Path(args.dir).glob("*.json"))
    total_new = total_dup = 0
    for f in files:
        try:
            n, d = load_file(conn, f, quiet=True)
            total_new += n
            total_dup += d
        except (json.JSONDecodeError, SystemExit) as e:
            print(f"건너뜀 {f.name}: {e}", file=sys.stderr)
    print(f"{len(files)}개 파일 → 관측 {total_new}건 적재, 중복 {total_dup}건 스킵")


def cmd_export(conn, args):
    # v2에서 옛 이름은 뷰라 rowid가 없다 — 뷰는 물리 키를 `_rowid`로 내준다.
    # WHERE·ORDER는 별칭이 아니라 진짜 참조 가능한 표현식으로 짠다 (PR #9 리뷰)
    sel, key = rowid_parts(conn, args.table)
    q = f"{sel} FROM {args.table}"
    if args.since_rowid is not None:
        q += f" WHERE {key} > {int(args.since_rowid)}"
    q += f" ORDER BY {key}"
    rows = conn.execute(q).fetchall()
    if args.format == "json":
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False))
    else:
        w = csv.writer(io.StringIO()) if False else csv.writer(sys.stdout)
        if rows:
            w.writerow(rows[0].keys())
            for r in rows:
                w.writerow(list(r))


def cmd_proxy_load(conn, args):
    """프록시 정의 + 판정 묶음(JSON)을 등록한다. proxy-extractor 반환 형식과 같다."""
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    # proxy-extractor가 다중 카드를 배열로 반환한다 — 배열이면 각 원소를 순차 적재
    if isinstance(data, list):
        for one in data:
            _proxy_load_one(conn, one)
        return
    _proxy_load_one(conn, data)


def _proxy_load_one(conn, data):
    """프록시 정의 + 판정을 적재한다.

    **값 공간이 바뀌면 옛 캐시를 버린다** (D53). 전에는 정의만 덮어쓰고 캐시를
    그대로 뒀는데, 그러면 **같은 이름 아래 두 값 공간이 섞인다.** 2026-08-04 실측:

        name_lang  정의 [혼합·한국어·영문]  캐시에 `영문만` 945 · `한글만` 673
        thumb_cut  정의 [제품 단독컷·착용컷] 캐시에 `스튜디오 모델컷` 65 …

    값 공간 검사는 **적재 시점의 새 정의**로만 도니 옛 값은 검사를 안 거친다.
    그 결과 같은 개념이 두 그룹으로 갈려 그룹 비교의 n이 쪼개지고, 분석은
    "영문과 영문만은 다르다"를 검정하게 된다.
    """
    d = data.get("proxy") or {}
    if not d.get("proxy_name"):
        raise SystemExit("proxy.proxy_name이 없다")
    name = d["proxy_name"]
    space = d.get("value_space")
    new_space = json.dumps(space, ensure_ascii=False)
    prev = conn.execute(
        "SELECT value_space FROM proxy_defs WHERE proxy_name=?", (name,)).fetchone()

    def _space_changed(prev_json, cur):
        """실질 변경만 본다 (PR #12 리뷰 7). JSON 문자열 비교는 리스트 원소
        **순서**만 바뀌어도 변경으로 오판해 캐시를 지운다 — 과잉 드롭 방향이라
        틀리진 않지만, 값 공간을 다듬을 때마다 판정이 통째로 날아간다.
        리스트끼리는 집합으로 비교한다(값 공간에서 순서는 계약이 아니다 —
        판정 검사도 `val not in space` 소속만 본다)."""
        try:
            old = json.loads(prev_json or "null")
        except ValueError:
            return True          # 못 읽는 옛 정의 — 바뀐 것으로 취급(안전한 방향)
        if isinstance(old, list) and isinstance(cur, list):
            return set(old) != set(cur)
        return old != cur

    dropped = 0
    if prev and _space_changed(prev[0], space):
        # 정의가 바뀌었다 — 옛 판정은 다른 계약으로 만들어진 값이라 못 쓴다.
        # **조용히 섞느니 지운다.** 지웠다는 사실과 건수를 찍는다.
        dropped = conn.execute(
            "DELETE FROM proxy_cache WHERE proxy_name=?", (name,)).rowcount
        print("%s: 값 공간이 바뀌어 옛 판정 %d건을 버린다\n  이전 %s\n  이후 %s"
              % (name, dropped, prev[0][:70], new_space[:70]))
    conn.execute(
        "INSERT INTO proxy_defs (proxy_name, question, material, value_space, "
        "method, created_at, label) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(proxy_name) DO UPDATE SET question=excluded.question, "
        "material=excluded.material, value_space=excluded.value_space, "
        "method=excluded.method, label=excluded.label",
        (name, d.get("question"), d.get("material"),
         new_space, d.get("method"), now_str(), d.get("label")))
    is_numeric = space == "numeric"
    new = dup = bad = 0
    for j in data.get("judgments", []):
        val = j.get("value")
        if isinstance(space, list) and val not in space:
            bad += 1  # 값 공간 밖 판정은 버린다 — 정의가 계약이다
            continue
        if is_numeric and val is not None:
            # 수치 프록시는 float로 강제 캐스팅한다. 판정기가 "4.5" 같은 문자열을 주면
            # proxy_cache(TEXT)에 문자열로 들어가고, 리포트 산술(pstdev·Cliff δ)이
            # TypeError로 죽는다. 캐스팅 실패는 거부한다 — 정의가 numeric이면 값도 numeric이다.
            try:
                val = float(val)
            except (TypeError, ValueError):
                bad += 1
                continue
        try:
            conn.execute(
                "INSERT INTO proxy_cache VALUES (?,?,?,?,?,?,?)",
                (d["proxy_name"], j.get("site"), str(j.get("product_id")),
                 j.get("fingerprint"), val, j.get("basis"), now_str()))
            new += 1
        except sqlite3.IntegrityError:
            dup += 1
    conn.commit()
    msg = f"{d['proxy_name']}: 판정 {new}건 적재, 중복 {dup}건 스킵"
    if bad:
        msg += f", 값 공간 밖 {bad}건 거부"
    print(msg)


def cmd_proxy_audit(conn, args):
    """값 공간 밖 판정이 남아 있나 본다 (D53). `--fix`면 지운다.

    적재 시점 가드(위)가 앞으로를 막고, 이건 **이미 들어간 것**을 찾는다.
    조용히 남아 있으면 축이 갈린 채로 분석이 돈다.
    """
    bad_total = 0
    for name, space_json in conn.execute(
            "SELECT proxy_name, value_space FROM proxy_defs ORDER BY 1"):
        try:
            space = json.loads(space_json or "null")
        except ValueError:
            space = None
        rows = conn.execute(
            "SELECT value, COUNT(*) FROM proxy_cache WHERE proxy_name=? GROUP BY 1",
            (name,)).fetchall()
        if space == "numeric":
            bad = [(v, n) for v, n in rows
                   if v is not None and not _is_num(v)]
        elif isinstance(space, list):
            bad = [(v, n) for v, n in rows if v not in space]
        else:
            continue
        if not bad:
            continue
        cnt = sum(n for _, n in bad)
        bad_total += cnt
        print("%s: 값 공간 밖 %d건 — %s"
              % (name, cnt, ", ".join("%s(%d)" % b for b in bad[:5])))
        if getattr(args, "fix", False):
            for v, _ in bad:
                conn.execute("DELETE FROM proxy_cache WHERE proxy_name=? AND value IS ?",
                             (name, v))
            conn.commit()
            print("  → 지웠다")
    if not bad_total:
        print("값 공간 밖 판정 없음 — 전 프록시 정상")
    return 0 if (bad_total == 0 or getattr(args, "fix", False)) else 2


def _is_num(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def cmd_stats(conn, _args):
    for t in ("products", "observations", "variants", "variant_observations", "platforms", "runs", "proxy_defs", "proxy_cache"):
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t:14} {n:>8}")
    ctxs = conn.execute(
        "SELECT context, COUNT(*) n, MIN(observed_at) a, MAX(observed_at) b "
        "FROM observations GROUP BY context ORDER BY n DESC LIMIT 20").fetchall()
    for c in ctxs:
        print(f"  {c['context']}: {c['n']}건 ({c['a']} ~ {c['b']})")


def cmd_merge(conn, args):
    """다른 사람의 DB를 이 DB에 합친다 (D31 완화책).

    전원 배포에 공유 DB가 없어서 각자 자기 DB를 갖는다. 나중에 합칠 길이 없으면
    축적이 사람 수만큼 갈라진 채 영영 못 만난다 — 이 명령이 그 길이다.
    공유 DB로 옮길 때의 마이그레이션 경로이기도 하다.

    충돌 규칙은 `load`와 같다:
    - `observations`·`variant_observations`는 append only + 같은 키는 스킵.
      **관측은 사실이라 덮어쓸 것이 없다** — 같은 시각·같은 문맥이면 같은 관측이다
    - `products`·`variants`·`platforms`·`proxy_defs`는 **비어 있지 않은 값만** 덮는다.
      상대가 모르는 필드를 null로 밀어 내 값을 지우면 안 된다
    - `runs`는 그대로 가져온다(어느 수집에서 왔는지가 이력이다)
    """
    src = sqlite3.connect(args.source)
    src.row_factory = sqlite3.Row
    stats = {}

    # ── 순서가 규칙의 일부다 (D59) ────────────────────────────────────────
    # v2는 관측을 `pk`(상품)와 `run_ref`(수집)로 잇는다. 상품·런이 먼저 없으면
    # 트리거가 그 참조를 NULL로 풀고, 바깥의 `INSERT OR IGNORE`가 그 실패를 조용히
    # 삼킨다 — **예외도 없이 관측 0건**이 된다. 실측(2026-08-04): 빈 DB에 seed를
    # 합쳤더니 상품 2,722은 들어오고 관측 4,670은 전부 사라졌다.
    # 그래서 런 → 정적(상품·옵션) → 관측 → 판정 순으로 간다.
    # 정적 계열 — 빈 값으로 덮지 않는다. `runs`는 이력이라 그대로 가져온다
    for table, keys in (("runs", ("run_id",)),
                        ("products", ("site", "product_id")),
                        ("variants", ("site", "product_id", "option_id")),
                        ("platforms", ("platform_key",)),
                        ("proxy_defs", ("proxy_name",)),
                        ("proxy_cache", ("proxy_name", "site", "product_id", "fingerprint")),
                        # 판정은 **들어온 값이 이긴다** — v2 트리거가 그렇게 하고
                        # (ttl_days=NULL을 의도적으로 넣는다), v1도 같아야 한다
                        ("product_attributes", ("site", "product_id", "attr_name"))):
        try:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue
        if not rows:
            continue
        cols = [c for c in rows[0].keys()]
        upd = [c for c in cols if c not in keys]
        before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        # **v2에서는 옛 이름이 뷰다 — 뷰에는 업서트를 못 쓴다**("cannot UPSERT a view").
        # v2가 업서트 의미를 `INSTEAD OF INSERT` 트리거 안으로 옮겨 뒀으니(D45),
        # 뷰에는 평범한 INSERT를 던지고 덮어쓸지 지킬지는 트리거의 COALESCE가 정한다.
        # D45 때 이 함수가 같이 안 고쳐져서 **새로 만든 DB에서 merge가 전부 죽었다** —
        # 새 DB는 항상 v2로 지어지므로, 팀원이 처음 합치려는 순간 예외가 났다(D59).
        if _is_view(conn, table):
            stmt = ("INSERT INTO %s (%s) VALUES (%s)"
                    % (table, ",".join(cols), ",".join("?" * len(cols))))
        elif table == "product_attributes":
            # 판정은 덮는다(v2 트리거와 같은 규칙) — COALESCE로 지키면 `ttl_days=NULL`의
            # 의도가 무시되고, v1과 v2가 같은 명령에 다르게 반응한다
            sets = ", ".join("%s = excluded.%s" % (c, c) for c in upd)
            stmt = ("INSERT INTO %s (%s) VALUES (%s) ON CONFLICT(%s) DO UPDATE SET %s"
                    % (table, ",".join(cols), ",".join("?" * len(cols)), ",".join(keys), sets))
        else:
            # COALESCE(excluded.x, x) — 들어온 값이 null이면 기존 값을 지킨다
            sets = ", ".join("%s = COALESCE(excluded.%s, %s.%s)" % (c, c, table, c) for c in upd)
            stmt = ("INSERT INTO %s (%s) VALUES (%s) ON CONFLICT(%s) DO UPDATE SET %s"
                    % (table, ",".join(cols), ",".join("?" * len(cols)), ",".join(keys), sets))
        conn.executemany(
            stmt,
            [tuple(r[c] for c in cols) for r in rows])
        after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        stats[table] = (after - before, len(rows))

    # 관측 계열 — 키가 겹치면 스킵. INSERT OR IGNORE가 곧 그 규칙이다.
    # **상품·런 뒤에 온다**(위 주석 D59) — 앞서면 참조가 안 풀려 조용히 0건이 된다
    for table in ("observations", "variant_observations"):
        try:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue                       # 상대 DB가 더 옛 스키마일 수 있다
        if not rows:
            continue
        cols = [c for c in rows[0].keys() if c != "rowid"]
        before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO %s (%s) VALUES (%s)"
            % (table, ",".join(cols), ",".join("?" * len(cols))),
            [tuple(r[c] for c in cols) for r in rows])
        after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        stats[table] = (after - before, len(rows))

    conn.commit()
    src.close()
    print("합침: %s → %s" % (args.source, args.db))
    for t, (added, seen) in stats.items():
        print("  %-22s 신규 %6d / 상대 %6d" % (t, added, seen))
    if not stats:
        print("  가져올 것이 없었다 (빈 DB이거나 스키마가 다르다)")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(DEFAULT_DB))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sp = sub.add_parser("load"); sp.add_argument("raw", nargs="+")
    sp = sub.add_parser("check")
    sp.add_argument("--site", required=True)
    sp.add_argument("--context", required=True)
    sp.add_argument("--cycle-minutes", type=int, default=None)
    sp.add_argument("--team", action="store_true",
                    help="시트 runs 탭도 조회해 팀원이 이미 수집했는지 본다 (D32)")
    sp.add_argument("--config", default="data/sheets_config.json")
    sp.add_argument("--creds", default=os.environ.get(
        "INTEL_SHEETS_CREDENTIALS", str(Path.home() / ".config/intel/service-account.json")))
    sp = sub.add_parser("reuse-attrs")
    sp.add_argument("raw")
    sp.add_argument("--out")
    sp.add_argument("--ttl-days", type=int, default=STATIC_TTL_DAYS)
    sp = sub.add_parser("stale-static")
    sp.add_argument("--site")
    sp.add_argument("--ttl-days", type=int, default=STATIC_TTL_DAYS)
    sp = sub.add_parser("import-snapshots"); sp.add_argument("dir")
    sp = sub.add_parser("proxy-load"); sp.add_argument("file")
    sp = sub.add_parser("proxy-audit", help="값 공간 밖 판정 점검 (D53)")
    sp.add_argument("--fix", action="store_true", help="찾은 것을 지운다")
    sp = sub.add_parser("export")
    sp.add_argument("--table", required=True,
                    choices=["products", "observations", "variants", "variant_observations", "platforms", "runs", "proxy_defs", "proxy_cache"])
    sp.add_argument("--format", choices=["csv", "json"], default="csv")
    sp.add_argument("--since-rowid", type=int, default=None)
    sub.add_parser("stats")
    sp = sub.add_parser("merge")
    sp.add_argument("source", help="합칠 상대 DB 경로 (팀원이 보내준 intel.db)")
    sp = sub.add_parser("set-attrs")   # D35 — 속성 판정 결과 적재
    sp.add_argument("file", help="[{site,product_id,attr_name,value,basis,ttl_days?}] JSON")
    sub.add_parser("attr-stats")       # D35 — 속성별 판정 현황
    sp = sub.add_parser("tag-lifecycle")   # D35 — 자사 상품 온고잉/시즌 태깅
    sp.add_argument("--file", help="자사 목록 JSON (기본 assets/own-brand.json)")
    args = p.parse_args()

    conn = connect(args.db)
    if args.cmd == "init":
        print(f"초기화 완료: {args.db}")
    elif args.cmd == "load":
        for f in args.raw:
            load_file(conn, f)
    elif args.cmd == "check":
        sys.exit(cmd_check(conn, args))
    elif args.cmd == "reuse-attrs":
        cmd_reuse_attrs(conn, args)
    elif args.cmd == "merge":
        cmd_merge(conn, args)
    elif args.cmd == "stale-static":
        cmd_stale_static(conn, args)
    elif args.cmd == "import-snapshots":
        cmd_import_snapshots(conn, args)
    elif args.cmd == "proxy-load":
        cmd_proxy_load(conn, args)
    elif args.cmd == "proxy-audit":
        # `return`이 아니라 `sys.exit` — 진입점이 `main()` 반환값을 버려서
        # 오염을 찾아도 프로세스는 0으로 끝났다(PR #12 리뷰 Blocker).
        # E-PXS-2가 명시한 exit 2 계약은 check 커맨드와 같은 패턴으로만 지켜진다.
        sys.exit(cmd_proxy_audit(conn, args))
    elif args.cmd == "export":
        cmd_export(conn, args)
    elif args.cmd == "stats":
        cmd_stats(conn, args)
    elif args.cmd == "set-attrs":
        cmd_set_attrs(conn, args)
    elif args.cmd == "attr-stats":
        cmd_attr_stats(conn, args)
    elif args.cmd == "tag-lifecycle":
        cmd_tag_lifecycle(conn, args)


if __name__ == "__main__":
    main()
