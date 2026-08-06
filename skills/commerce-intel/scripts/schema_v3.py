#!/usr/bin/env python3
"""정본 DB 스키마 v3 — 관계를 정식화한다 (D65, 2026-08-05 사용자 확정 설계).

v2(D45)가 반복 텍스트를 정수로 접었다면, v3는 **묵시적이던 관계를 테이블·제약으로
못 박는다.** 확정된 결정 10항:

1. `contexts.name`에 CHECK — 접두사 4종(brand/market/ranking/adhoc) 고정.
   별도 테이블 없이 제약만 건다
2. 상품 경량화 — `attributes`(JSON)·`attributes_basis`는 attr_base로 완전 이관됐고,
   `first_seen_at`·`last_seen_at`·`raw_extras`는 읽는 곳이 없다(쓰기만 했다). 제거
3. 카테고리 계층화 — self-FK(`parent_category_id`)·`depth`. 상품과는
   `product_categories`(N:M, source='platform')로 잇고 상품의 `category_id`는 제거.
   AI 분류 카테고리는 attr_base에 `ai_카테고리_*` 행으로 간다
4. `brand_aliases` — 플랫폼별 표기를 brands에 매단다. `brands.name`은
   `representative_name`으로 리네임
5. `brand_platforms` — 브랜드-입점처 N:M. `platforms.discovered_for_brand`
   (쉼표 텍스트) 제거
6. `obs_attr` — 시점별 비정형 지표(SNS 언급수 등)를 key-value로. 고정 컬럼은 유지
7. `runs`에 정수 PK(`id`) — v2의 `run_ref`(rowid 참조, FK 불가)가 정식 FK
   `run_id INTEGER REFERENCES runs(id)`가 된다
8. 프록시는 **별도 DB**(`data/proxy.db`) — defs↔cache FK(ON DELETE CASCADE) +
   rule 카드 본문(`rules`)을 defs에 저장해 lazy 판정이 가능해진다
9. 옵션 경량화 — `first_seen_at`·`last_seen_at` 제거(미사용)
10. 이관은 `migrate_v3.py` — 백업·검산·PRAGMA integrity_check까지

## 뷰 계약은 v2에서 이어받는다

옛 이름 뷰(products·observations·variants·variant_observations·product_attributes)는
유지하되 **제거된 컬럼은 뷰에서도 사라진다.** 시각은 v2처럼 TEXT로 왕복한다.

- `products.category`는 이제 **파생 컬럼**이다 — product_categories의 'platform'
  매핑 하나(category_id 최소값, 결정적)를 골라 계층을 `상위 > … > 리프`로 편다.
  깊이는 5단까지 편다(실측 최대 3단 — ranking_targets 카탈로그·라이브 DB).
- **트리거는 NEW.category를 쓰지 않는다.** 경로 분해(`'A > B > C'` → 행 3개)는
  SQL 트리거로 못 하므로 카테고리 쓰기는 파이썬 `assign_category()`가 유일한
  통로다. 뷰에 INSERT하는 코드(적재·merge·seed)는 반드시 그걸 함께 불러야 한다 —
  안 부르면 카테고리만 조용히 빠진다.
- 트리거의 브랜드는 **대표명 완전일치**만 처리한다. 표기 변형 흡수(brand_key
  매칭 → 별명 등록)는 파이썬 `resolve_brand()`(intel_db)가 적재 경로에서 한다.
"""
import os
import sys
from pathlib import Path

# 사전 + 본체. 뷰가 구 이름을 그대로 쓰므로 물리 테이블은 `_base`를 유지한다.
SCHEMA_V3 = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sites (
    site_id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);

-- 접두사 4종 고정 (D65-1). 위반은 트리거의 INSERT OR IGNORE에서 조용히 무시돼
-- context_id가 NULL이 되고, obs_base의 NOT NULL이 그 자리에서 실패를 낸다 —
-- 새 접두사가 필요하면 스키마 개정이 먼저다.
CREATE TABLE IF NOT EXISTS contexts (
    context_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
        CHECK (name LIKE 'brand:%' OR name LIKE 'market:%'
               OR name LIKE 'ranking:%' OR name LIKE 'adhoc:%'));

CREATE TABLE IF NOT EXISTS brands (
    brand_id INTEGER PRIMARY KEY,
    representative_name TEXT NOT NULL UNIQUE);   -- v2의 name (D65-4)

-- 플랫폼별 표기 변형 (D65-4). 수집이 모르는 표기를 만나면 brand_key 매칭으로
-- 기존 브랜드에 candidate 별명을 단다 — 확정/기각은 사람이 verify_status로.
CREATE TABLE IF NOT EXISTS brand_aliases (
    alias_id INTEGER PRIMARY KEY,
    brand_id INTEGER NOT NULL REFERENCES brands(brand_id),
    notation TEXT NOT NULL,
    site_id INTEGER REFERENCES sites(site_id),
    source TEXT,           -- platform/transliteration/manual
    verify_status TEXT,    -- confirmed/candidate/rejected
    verified_at TEXT,
    UNIQUE (brand_id, notation));
CREATE INDEX IF NOT EXISTS idx_alias_notation ON brand_aliases (notation);

-- 계층 카테고리 (D65-3). depth는 플랫폼이 정해준 깊이 그대로(1=최상위).
-- UNIQUE(name, parent)의 NULL은 SQLite에서 서로 다른 값이라 최상위 중복을 못
-- 막는다 — 부분 인덱스가 그 구멍을 막는다.
CREATE TABLE IF NOT EXISTS categories (
    category_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    parent_category_id INTEGER REFERENCES categories(category_id),
    depth INTEGER,
    UNIQUE (name, parent_category_id));
CREATE UNIQUE INDEX IF NOT EXISTS idx_cat_root ON categories (name)
    WHERE parent_category_id IS NULL;

-- 수집 이력 (D65-7). v2는 run_id TEXT가 PK였고 관측이 rowid를 비공식 참조했다
-- (선언된 PK/UNIQUE만 FK 대상이라 제약을 못 걸었다). 정수 id를 선언해 정식 FK로.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    run_id TEXT UNIQUE,
    site TEXT, story TEXT, target TEXT,
    collected_at TEXT, item_count INTEGER, source_total INTEGER,
    incomplete INTEGER, notes TEXT, raw_file TEXT, loaded_at TEXT);

-- 입점처 카탈로그. discovered_for_brand(쉼표 텍스트)는 brand_platforms로 정규화 (D65-5)
CREATE TABLE IF NOT EXISTS platforms (
    platform_key TEXT PRIMARY KEY,   -- 예: "musinsa" · "zigzag"
    name TEXT, url TEXT, engine TEXT,
    recon TEXT,               -- 정찰 결과 JSON (channel-scout 산출)
    skill_status TEXT,        -- none/candidate/recon_done/draft/ready
    updated_at TEXT);

CREATE TABLE IF NOT EXISTS brand_platforms (
    brand_id INTEGER NOT NULL REFERENCES brands(brand_id),
    platform_key TEXT NOT NULL REFERENCES platforms(platform_key),
    brand_page_url TEXT,
    discovered_at TEXT,
    product_count INTEGER,
    PRIMARY KEY (brand_id, platform_key));

CREATE TABLE IF NOT EXISTS product_base (
    pk INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(site_id),
    product_id TEXT NOT NULL,
    name TEXT,
    url TEXT,
    image_url TEXT,
    brand_id INTEGER REFERENCES brands(brand_id),
    static_verified_at INTEGER,
    UNIQUE (site_id, product_id));

-- 상품-카테고리 N:M (D65-3). 같은 상품이 플랫폼별로 다른 카테고리를 가질 수 있다.
-- source='platform'만 여기 온다 — AI 분류는 attr_base의 ai_카테고리_* 행이다.
CREATE TABLE IF NOT EXISTS product_categories (
    pk INTEGER NOT NULL REFERENCES product_base(pk),
    category_id INTEGER NOT NULL REFERENCES categories(category_id),
    source TEXT NOT NULL,
    site_id INTEGER REFERENCES sites(site_id),
    PRIMARY KEY (pk, category_id, source));

CREATE TABLE IF NOT EXISTS obs_base (
    id INTEGER PRIMARY KEY,
    pk INTEGER NOT NULL REFERENCES product_base(pk),
    observed_at INTEGER NOT NULL,           -- unix epoch (초)
    context_id INTEGER NOT NULL REFERENCES contexts(context_id),
    run_id INTEGER REFERENCES runs(id),     -- v3: 정식 FK (구 run_ref)
    price_original INTEGER, price_sale INTEGER, discount_rate INTEGER,
    review_count INTEGER, rating REAL,
    view_count INTEGER, view_count_display TEXT,
    purchase_count INTEGER, purchase_count_display TEXT,
    like_count INTEGER, like_count_display TEXT,
    viewers_now INTEGER, buyers_now INTEGER,
    sold_out INTEGER, rank INTEGER,
    UNIQUE (pk, observed_at, context_id)
);
CREATE INDEX IF NOT EXISTS idx_obs_ctx ON obs_base (context_id, observed_at);

-- 시점별 비정형 지표 (D65-6) — SNS 언급수·트렌드 점수 등 간헐 지표를 행으로.
-- 고정 컬럼(가격·순위 등)은 obs_base에 그대로 있다.
CREATE TABLE IF NOT EXISTS obs_attr (
    id INTEGER PRIMARY KEY,
    -- CASCADE: 솎기(prune)가 관측을 지울 때 매달린 속성이 함께 간다 — 없으면
    -- FK RESTRICT가 솎기를 막는다
    obs_id INTEGER NOT NULL REFERENCES obs_base(id) ON DELETE CASCADE,
    attr_name TEXT NOT NULL,
    value TEXT,
    basis TEXT,            -- llm/vision/rule/api/manual
    UNIQUE (obs_id, attr_name));

CREATE TABLE IF NOT EXISTS variant_base (
    vk INTEGER PRIMARY KEY,
    pk INTEGER NOT NULL REFERENCES product_base(pk),
    option_id TEXT NOT NULL,
    option_name TEXT, color TEXT, size TEXT,
    UNIQUE (pk, option_id));
CREATE TABLE IF NOT EXISTS variant_obs_base (
    id INTEGER PRIMARY KEY,
    vk INTEGER NOT NULL REFERENCES variant_base(vk),
    observed_at INTEGER NOT NULL,
    sold_out INTEGER, stock_qty INTEGER, stock_display TEXT, stock_basis TEXT,
    run_id INTEGER REFERENCES runs(id),     -- v3: 정식 FK (구 run_ref)
    UNIQUE (vk, observed_at));
CREATE TABLE IF NOT EXISTS attr_base (
    pk INTEGER NOT NULL REFERENCES product_base(pk),
    attr_name TEXT NOT NULL,
    value TEXT, basis TEXT, decided_at INTEGER, ttl_days INTEGER,
    PRIMARY KEY (pk, attr_name));
CREATE INDEX IF NOT EXISTS idx_attr_name2 ON attr_base (attr_name, value);

-- 상품 정적 속성 변경 이력 (D68) — 상품명·브랜드·카테고리·URL·이미지가 수집마다
-- 덮이면서 "바뀌었다"는 사실 자체가 소실됐다. append only — 지우지도 고치지도 않는다.
CREATE TABLE IF NOT EXISTS product_history (
    id INTEGER PRIMARY KEY,
    pk INTEGER NOT NULL REFERENCES product_base(pk),
    changed_at TEXT NOT NULL,        -- 변경 감지 시각 (그 수집의 collected_at)
    field TEXT NOT NULL,             -- name / brand / category / url / image_url
    old_value TEXT,
    new_value TEXT,
    run_id INTEGER REFERENCES runs(id)   -- 어떤 수집에서 감지됐는지
);
CREATE INDEX IF NOT EXISTS idx_product_history_pk ON product_history (pk, changed_at);

-- 동적 속성 변경 이력 (D68) — 핏·AI 카테고리 재판정의 이전 값 보존.
-- 쓰기는 product_attributes 뷰 트리거가 잡는다(모든 쓰기 경로가 뷰를 지난다).
CREATE TABLE IF NOT EXISTS attr_history (
    id INTEGER PRIMARY KEY,
    pk INTEGER NOT NULL REFERENCES product_base(pk),
    attr_name TEXT NOT NULL,
    old_value TEXT, new_value TEXT,
    old_basis TEXT, new_basis TEXT,
    changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attr_history_pk ON attr_history (pk, changed_at);

CREATE TABLE IF NOT EXISTS insights (       -- 인사이트 엔진 산출 (D28)
    run_stamp TEXT NOT NULL,
    target TEXT NOT NULL,
    context TEXT,
    verdict TEXT NOT NULL,
    idx INTEGER NOT NULL,
    claim TEXT, audience TEXT,
    effect REAL, effect_kind TEXT, n INTEGER, p REAL,
    holdout TEXT, fails TEXT, recheck TEXT,
    detail_pdf TEXT, detail_page INTEGER,
    created_at TEXT,
    PRIMARY KEY (run_stamp, target, verdict, idx));
CREATE TABLE IF NOT EXISTS sync_state (
    table_name TEXT PRIMARY KEY,
    last_synced_key TEXT,
    updated_at TEXT);

-- ── 프록시 (본 DB 통합 — D69, 구 D65-8 별도 파일 폐기) ────────────────────
-- lazy 판정으로 전환한 뒤 캐시가 천천히 쌓이므로 분리의 이점이 약해졌고,
-- FK/트리거/자동동기화를 못 하는 대가가 더 커졌다 (D69).
CREATE TABLE IF NOT EXISTS proxy_defs (
    proxy_name TEXT PRIMARY KEY,
    question TEXT, material TEXT,  -- name/image/badge/detail
    value_space TEXT,              -- JSON 배열 또는 "numeric"
    method TEXT,                   -- rule/vision/llm
    created_at TEXT,
    label TEXT,                    -- 사람이 읽는 축 이름 (D56)
    rules TEXT);                   -- rule/numeric 카드 본문(JSON) — lazy 판정 재료 (D65-8)
CREATE TABLE IF NOT EXISTS proxy_cache (
    proxy_name TEXT NOT NULL REFERENCES proxy_defs(proxy_name) ON DELETE CASCADE,
    site TEXT NOT NULL, product_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    value TEXT, basis TEXT, judged_at TEXT,
    PRIMARY KEY (proxy_name, site, product_id, fingerprint));
CREATE TABLE IF NOT EXISTS proxy_history (
    id INTEGER PRIMARY KEY,
    proxy_name TEXT NOT NULL,
    site TEXT NOT NULL, product_id TEXT NOT NULL,
    old_value TEXT, new_value TEXT,
    old_fingerprint TEXT, new_fingerprint TEXT,
    changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proxy_history ON proxy_history (proxy_name, site, product_id);
"""


# ── Turso(libSQL) 연결 추상화 (D67) ────────────────────────────────────────
# 목표: 팀원 여러 명이 하나의 클라우드 정본을 쓰되, 환경변수가 없으면 지금처럼
# 로컬 SQLite 파일로 돈다(테스트·CI·오프라인 개발 하위호환).
#
#     INTEL_DB_URL=libsql://...  INTEL_DB_TOKEN=...   → Turso
#     (미설정)                                        → 로컬 파일 (기존 그대로)
#
# **드라이버 호환 래퍼가 이 층의 본체다.** libsql_experimental은 sqlite3과
# 비슷해 보이지만 실측(2026-08-05)으로 갈린 게 넷이다:
#   ① row_factory 없음 — 행이 튜플로만 온다. 이 코드베이스는 전부 r["컬럼"]
#      이름 접근이라 그대로 꽂으면 전면 파손된다 → description 기반 Row 셔ム
#   ② 제약 위반이 sqlite3.IntegrityError가 아니라 **ValueError**로 온다 —
#      적재의 "중복 N건 스킵" 카운팅이 첫 중복에서 죽는다 → 예외 번역
#   ③ total_changes 없음 → SELECT total_changes()로 대행
#   ④ lastrowid가 불안정 → last_insert_rowid() 조회로 대행 (_dim이 이 값으로
#      사전 id를 받는다 — 틀리면 조용히 엉뚱한 브랜드에 매달린다)


def is_libsql_url(target):
    return isinstance(target, str) and target.startswith("libsql://")


class LibsqlRow(tuple):
    """sqlite3.Row 흉내 — 인덱스·이름 접근, keys(), dict() 캐스팅이 다 된다."""
    __slots__ = ()
    _names = ()

    def __new__(cls, names, values):
        row = super().__new__(cls, values)
        # tuple 서브클래스라 인스턴스 속성을 못 둔다(__slots__) — 동적 서브클래스에
        # 이름을 싣는 대신, 호출부(_wrap_rows)가 이름별 클래스를 만들어 재사용한다
        return row

    def keys(self):
        return list(self._names)

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return super().__getitem__(self._names.index(key))
            except ValueError:
                raise IndexError(f"no such column: {key}")
        return super().__getitem__(key)


def _row_class(names):
    cls = type("LibsqlRowN", (LibsqlRow,), {"_names": tuple(names), "__slots__": ()})
    return cls


def _translate_libsql_error(e):
    """libsql의 ValueError를 sqlite3 예외 체계로 번역한다 — 호출부의
    `except sqlite3.IntegrityError`(중복 카운팅)가 그대로 살아야 한다."""
    import sqlite3
    msg = str(e)
    if any(k in msg for k in ("UNIQUE constraint", "NOT NULL constraint",
                              "CHECK constraint", "FOREIGN KEY constraint",
                              "미등록 run_id")):   # 트리거 RAISE(ABORT) — sqlite3와 같은 분류로
        return sqlite3.IntegrityError(msg)
    return sqlite3.OperationalError(msg)


class LibsqlCursor:
    """커서 래퍼 — description 기반으로 행을 LibsqlRow로 감싼다."""

    def __init__(self, conn, cur):
        self._conn = conn
        self._cur = cur
        desc = cur.description
        self._cls = _row_class([d[0] for d in desc]) if desc else None

    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self):
        return getattr(self._cur, "rowcount", -1)

    @property
    def lastrowid(self):
        # 드라이버의 lastrowid가 실측에서 어긋났다 — SQLite 함수로 직접 묻는다
        return self._conn._raw.execute("SELECT last_insert_rowid()").fetchone()[0]

    def _wrap(self, row):
        return self._cls(None, row) if (row is not None and self._cls) else row

    def fetchone(self):
        return self._wrap(self._cur.fetchone())

    def fetchall(self):
        return [self._wrap(r) for r in self._cur.fetchall()]

    def fetchmany(self, size):
        return [self._wrap(r) for r in self._cur.fetchmany(size)]

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row


class LibsqlConnection:
    """libsql_experimental 커넥션의 sqlite3 호환 래퍼.

    row_factory 대입은 받아만 둔다(항상 Row처럼 동작하므로 무해) — 기존 코드의
    `conn.row_factory = sqlite3.Row` 줄이 죽지 않게.
    """

    def __init__(self, raw):
        self._raw = raw
        self.row_factory = None

    def execute(self, sql, params=()):
        # 드라이버는 list 파라미터를 안 받는다('list' object cannot be converted
        # to 'PyTuple') — sqlite3처럼 list도 받게 여기서 변환한다 (호출부 전수보다
        # 래퍼 한 곳이 계약이다)
        if isinstance(params, list):
            params = tuple(params)
        try:
            return LibsqlCursor(self, self._raw.execute(sql, params))
        except ValueError as e:
            raise _translate_libsql_error(e) from e

    def executemany(self, sql, seq):
        try:
            return LibsqlCursor(self, self._raw.executemany(
                sql, [tuple(p) if isinstance(p, list) else p for p in seq]))
        except ValueError as e:
            raise _translate_libsql_error(e) from e

    def executescript(self, script):
        try:
            return self._raw.executescript(script)
        except ValueError as e:
            raise _translate_libsql_error(e) from e

    def commit(self):
        self._raw.commit()

    def close(self):
        # 드라이버 버전에 따라 close가 없을 수 있다 — 없으면 GC에 맡긴다
        if hasattr(self._raw, "close"):
            self._raw.close()

    @property
    def total_changes(self):
        return self._raw.execute("SELECT total_changes()").fetchone()[0]


def open_db(target, token=None):
    """경로 또는 libsql:// URL → 커넥션. 이 저장소의 **모든 DB 열기 통로**다 (D67).

    - libsql:// → Turso. 토큰은 인자 또는 INTEL_DB_TOKEN
    - 그 외     → 로컬 SQLite (file: 접두사 허용). row_factory=Row까지 맞춰 준다
    """
    import sqlite3
    if is_libsql_url(target):
        import libsql_experimental as libsql   # Turso 모드에서만 필요 (조건부 의존)
        raw = libsql.connect(
            target, auth_token=token or os.environ.get("INTEL_DB_TOKEN", ""))
        conn = LibsqlConnection(raw)
    else:
        path = str(target)
        if path.startswith("file:") and "?" not in path:
            path = path[len("file:"):]
        conn = sqlite3.connect(path, uri=path.startswith("file:"))
        conn.row_factory = sqlite3.Row
    # FK는 커넥션 설정이라 파일이 아니라 **여기서** 켜야 한다 — 안 켠 커넥션에서는
    # ON DELETE CASCADE가 조용히 안 돈다. 예전엔 proxy_connect()가 강제로 켰는데
    # D69로 그 통로가 사라지면서 open_db()만 거치는 호출부(proxy_auto·intel_data)가
    # FK 없이 열렸다 (PR #16 리뷰 Blocker 2·7). 모든 열기 통로인 여기서 켠다.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def default_db_target():
    """정본 DB의 기본 대상 — INTEL_DB_URL > INTEL_DB > data/intel.db.

    명시적 --db 인자가 있으면 그게 이긴다(호출부 규칙) — 테스트가 임시 파일을
    넘기는데 환경변수가 그걸 Turso로 납치하면 안 된다.
    """
    return (os.environ.get("INTEL_DB_URL")
            or os.environ.get("INTEL_DB")
            or "data/intel.db")


# ── 뷰 — 구 이름·구 컬럼 그대로 (제거된 컬럼만 빠졌다) ─────────────────────
# 읽는 코드가 안 바뀌게 하는 층이다. **컬럼 이름과 순서를 v2 뷰와 맞추되**
# attributes·attributes_basis·first_seen_at·last_seen_at·raw_extras는 v3에서
# 저장 자체가 사라졌으므로 뷰에도 없다 (D65-2·9).
#
# category는 파생이다: product_categories의 'platform' 매핑 하나(category_id
# 최소값 — 결정적)를 골라 부모 사슬을 `상위 > … > 리프`로 편다. 5단까지 —
# 실측 최대 3단(라이브 DB·카탈로그)이라 여유가 2단 있다. 6단이 필요해지는 날
# 이 뷰가 리프 쪽 5단만 보여주므로, 그런 카탈로그가 나타나면 뷰를 늘려라.
VIEWS_V3 = """
DROP VIEW IF EXISTS products;
CREATE VIEW products AS
SELECT s.name AS site, p.product_id, p.name AS name,
       p.url,
       p.image_url,
       b.representative_name AS brand,
       CASE WHEN c0.category_id IS NULL THEN NULL ELSE
            COALESCE(c4.name || ' > ','') || COALESCE(c3.name || ' > ','') ||
            COALESCE(c2.name || ' > ','') || COALESCE(c1.name || ' > ','') ||
            c0.name END AS category,
       datetime(p.static_verified_at,'unixepoch') AS static_verified_at,
       p.pk AS _rowid                       -- 증분 미러·export용 물리 키 (맨 뒤)
FROM product_base p
JOIN sites s ON s.site_id = p.site_id
LEFT JOIN brands b ON b.brand_id = p.brand_id
LEFT JOIN product_categories pc
       ON pc.pk = p.pk AND pc.source = 'platform'
      AND pc.category_id = (SELECT MIN(x.category_id) FROM product_categories x
                            WHERE x.pk = p.pk AND x.source = 'platform')
LEFT JOIN categories c0 ON c0.category_id = pc.category_id
LEFT JOIN categories c1 ON c1.category_id = c0.parent_category_id
LEFT JOIN categories c2 ON c2.category_id = c1.parent_category_id
LEFT JOIN categories c3 ON c3.category_id = c2.parent_category_id
LEFT JOIN categories c4 ON c4.category_id = c3.parent_category_id
;

DROP VIEW IF EXISTS observations;
CREATE VIEW observations AS
SELECT s.name AS site, p.product_id,
       datetime(o.observed_at,'unixepoch') AS observed_at,
       cx.name AS context,
       o.price_original, o.price_sale, o.discount_rate,
       o.review_count, o.rating,
       o.view_count, o.view_count_display,
       o.purchase_count, o.purchase_count_display,
       o.like_count, o.like_count_display,
       o.viewers_now, o.buyers_now, o.sold_out, o.rank,
       r.run_id AS run_id,
       o.id AS _rowid
FROM obs_base o
JOIN product_base p ON p.pk = o.pk
JOIN sites s ON s.site_id = p.site_id
JOIN contexts cx ON cx.context_id = o.context_id
LEFT JOIN runs r ON r.id = o.run_id;

DROP VIEW IF EXISTS variants;
CREATE VIEW variants AS
SELECT s.name AS site, p.product_id, v.option_id,
       v.option_name, v.color, v.size,
       v.vk AS _rowid
FROM variant_base v
JOIN product_base p ON p.pk = v.pk
JOIN sites s ON s.site_id = p.site_id;

DROP VIEW IF EXISTS variant_observations;
CREATE VIEW variant_observations AS
SELECT s.name AS site, p.product_id, v.option_id,
       datetime(vo.observed_at,'unixepoch') AS observed_at,
       vo.sold_out, vo.stock_qty, vo.stock_display, vo.stock_basis,
       r.run_id AS run_id,
       vo.id AS _rowid
FROM variant_obs_base vo
JOIN variant_base v ON v.vk = vo.vk
JOIN product_base p ON p.pk = v.pk
JOIN sites s ON s.site_id = p.site_id
LEFT JOIN runs r ON r.id = vo.run_id;

DROP VIEW IF EXISTS product_attributes;
CREATE VIEW product_attributes AS
SELECT s.name AS site, p.product_id, a.attr_name, a.value, a.basis,
       datetime(a.decided_at,'unixepoch') AS decided_at, a.ttl_days,
       a.rowid AS _rowid
FROM attr_base a
JOIN product_base p ON p.pk = a.pk
JOIN sites s ON s.site_id = p.site_id;

-- 변경 이력 조회 뷰 (D68) — 물리 pk 대신 (site, product_id)로 묻게 한다
DROP VIEW IF EXISTS product_changes;
CREATE VIEW product_changes AS
SELECT s.name AS site, p.product_id, p.name AS 현재명,
       h.field, h.old_value, h.new_value, h.changed_at,
       r.run_id AS run_id,
       h.id AS _rowid
FROM product_history h
JOIN product_base p ON p.pk = h.pk
JOIN sites s ON s.site_id = p.site_id
LEFT JOIN runs r ON r.id = h.run_id;

DROP VIEW IF EXISTS attr_changes;
CREATE VIEW attr_changes AS
SELECT s.name AS site, p.product_id, h.attr_name,
       h.old_value, h.new_value, h.old_basis, h.new_basis, h.changed_at,
       h.id AS _rowid
FROM attr_history h
JOIN product_base p ON p.pk = h.pk
JOIN sites s ON s.site_id = p.site_id;
"""


# ── 쓰기 트리거 — 뷰를 쓸 수 있게 만든다 (규약은 v2 D45 그대로) ────────────
# 업서트 의미는 트리거 안에 있고(뷰에 업서트 불가), 중복 관측은 여전히
# IntegrityError를 낸다(적재 코드가 "중복 N건"을 그걸로 센다).
#
# v3에서 달라진 것:
#   - NEW.category는 **버려진다** — 경로 분해가 SQL로 안 되므로 파이썬
#     assign_category()가 유일한 카테고리 쓰기 경로다 (모듈 docstring 참조)
#   - 브랜드는 대표명 완전일치. 표기 변형은 resolve_brand()(intel_db)가 적재
#     경로에서 대표명으로 바꿔 온다
#   - first_seen_at·last_seen_at·raw_extras·attributes는 컬럼 자체가 없다

TRIGGERS_V3 = """
DROP TRIGGER IF EXISTS trg_products_ins;
CREATE TRIGGER trg_products_ins INSTEAD OF INSERT ON products BEGIN
  INSERT OR IGNORE INTO sites(name) VALUES (NEW.site);
  INSERT OR IGNORE INTO brands(representative_name)
    SELECT NEW.brand WHERE NEW.brand IS NOT NULL;
  INSERT INTO product_base (site_id, product_id, name, url, image_url,
      brand_id, static_verified_at)
  VALUES (
    (SELECT site_id FROM sites WHERE name=NEW.site), NEW.product_id, NEW.name,
    NEW.url, NEW.image_url,
    (SELECT brand_id FROM brands WHERE representative_name=NEW.brand),
    CAST(strftime('%s',NEW.static_verified_at) AS INTEGER))
  ON CONFLICT(site_id, product_id) DO UPDATE SET
    name=COALESCE(excluded.name, name),
    url=COALESCE(excluded.url, url),
    image_url=COALESCE(excluded.image_url, image_url),
    brand_id=COALESCE(excluded.brand_id, brand_id),
    static_verified_at=COALESCE(excluded.static_verified_at, static_verified_at);
END;

DROP TRIGGER IF EXISTS trg_products_upd;
CREATE TRIGGER trg_products_upd INSTEAD OF UPDATE ON products BEGIN
  INSERT OR IGNORE INTO brands(representative_name)
    SELECT NEW.brand WHERE NEW.brand IS NOT NULL;
  UPDATE product_base SET
    name=NEW.name,
    url=NEW.url, image_url=NEW.image_url,
    brand_id=(SELECT brand_id FROM brands WHERE representative_name=NEW.brand),
    static_verified_at=CAST(strftime('%s',NEW.static_verified_at) AS INTEGER)
  WHERE pk = (SELECT p.pk FROM product_base p JOIN sites s ON s.site_id=p.site_id
              WHERE s.name=OLD.site AND p.product_id=OLD.product_id);
END;

DROP TRIGGER IF EXISTS trg_obs_ins;
CREATE TRIGGER trg_obs_ins INSTEAD OF INSERT ON observations BEGIN
  -- 미등록 run_id는 **그 자리에서 죽는다** (PR #13 리뷰). NULL로 흘리면 관측이
  -- 수집 이력과 조용히 끊긴다 — D59가 두 번 재발한 바로 그 함정이라, 호출 순서
  -- 규율(런 먼저)만으로는 부족하고 트리거가 막는다. run_id 없는 관측은 허용.
  SELECT RAISE(ABORT, '미등록 run_id — 런(runs)을 먼저 넣어라 (D59)')
   WHERE NEW.run_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM runs WHERE run_id = NEW.run_id);
  INSERT OR IGNORE INTO contexts(name) VALUES (NEW.context);
  INSERT INTO obs_base (pk, observed_at, context_id, run_id,
      price_original, price_sale, discount_rate, review_count, rating,
      view_count, view_count_display, purchase_count, purchase_count_display,
      like_count, like_count_display, viewers_now, buyers_now, sold_out, rank)
  VALUES (
    (SELECT p.pk FROM product_base p JOIN sites s ON s.site_id=p.site_id
     WHERE s.name=NEW.site AND p.product_id=NEW.product_id),
    CAST(strftime('%s',NEW.observed_at) AS INTEGER),
    (SELECT context_id FROM contexts WHERE name=NEW.context),
    (SELECT id FROM runs WHERE run_id=NEW.run_id),
    NEW.price_original, NEW.price_sale, NEW.discount_rate, NEW.review_count, NEW.rating,
    NEW.view_count, NEW.view_count_display, NEW.purchase_count, NEW.purchase_count_display,
    NEW.like_count, NEW.like_count_display, NEW.viewers_now, NEW.buyers_now,
    NEW.sold_out, NEW.rank);
END;

DROP TRIGGER IF EXISTS trg_variants_ins;
CREATE TRIGGER trg_variants_ins INSTEAD OF INSERT ON variants BEGIN
  INSERT INTO variant_base (pk, option_id, option_name, color, size)
  VALUES (
    (SELECT p.pk FROM product_base p JOIN sites s ON s.site_id=p.site_id
     WHERE s.name=NEW.site AND p.product_id=NEW.product_id),
    NEW.option_id, NEW.option_name, NEW.color, NEW.size)
  ON CONFLICT(pk, option_id) DO UPDATE SET
    option_name=COALESCE(excluded.option_name, option_name),
    color=COALESCE(excluded.color, color),
    size=COALESCE(excluded.size, size);
END;

DROP TRIGGER IF EXISTS trg_vobs_ins;
CREATE TRIGGER trg_vobs_ins INSTEAD OF INSERT ON variant_observations BEGIN
  SELECT RAISE(ABORT, '미등록 run_id — 런(runs)을 먼저 넣어라 (D59)')
   WHERE NEW.run_id IS NOT NULL
     AND NOT EXISTS (SELECT 1 FROM runs WHERE run_id = NEW.run_id);
  INSERT INTO variant_obs_base (vk, observed_at, sold_out, stock_qty,
                                stock_display, stock_basis, run_id)
  VALUES (
    (SELECT v.vk FROM variant_base v JOIN product_base p ON p.pk=v.pk
     JOIN sites s ON s.site_id=p.site_id
     WHERE s.name=NEW.site AND p.product_id=NEW.product_id AND v.option_id=NEW.option_id),
    CAST(strftime('%s',NEW.observed_at) AS INTEGER),
    NEW.sold_out, NEW.stock_qty, NEW.stock_display, NEW.stock_basis,
    (SELECT id FROM runs WHERE run_id=NEW.run_id));
END;

DROP TRIGGER IF EXISTS trg_attr_ins;
CREATE TRIGGER trg_attr_ins INSTEAD OF INSERT ON product_attributes BEGIN
  -- 값이 실제로 바뀌는 업서트면 이전 값을 이력에 남긴다 (D68). 트리거에서
  -- 하는 이유: 속성 쓰기(set-attrs·tag-lifecycle·적재·merge)가 전부 이 뷰를
  -- 지나므로 어느 경로도 이력을 빠뜨리지 못한다.
  INSERT INTO attr_history (pk, attr_name, old_value, new_value,
                            old_basis, new_basis, changed_at)
  SELECT a.pk, a.attr_name, a.value, NEW.value, a.basis, NEW.basis,
         COALESCE(NEW.decided_at, datetime('now'))
  FROM attr_base a
  WHERE a.attr_name = NEW.attr_name
    AND a.pk = (SELECT p.pk FROM product_base p JOIN sites s ON s.site_id=p.site_id
                WHERE s.name=NEW.site AND p.product_id=NEW.product_id)
    AND a.value IS NOT NEW.value;
  INSERT INTO attr_base (pk, attr_name, value, basis, decided_at, ttl_days)
  VALUES (
    (SELECT p.pk FROM product_base p JOIN sites s ON s.site_id=p.site_id
     WHERE s.name=NEW.site AND p.product_id=NEW.product_id),
    NEW.attr_name, NEW.value, NEW.basis,
    CAST(strftime('%s',NEW.decided_at) AS INTEGER), NEW.ttl_days)
  -- COALESCE를 쓰지 않는다: set-attrs는 `ttl_days=NULL`을 **의도적으로** 넣어
  -- 전역 --ttl-days가 먹게 한다(E-DB-8).
  ON CONFLICT(pk, attr_name) DO UPDATE SET
    value=excluded.value, basis=excluded.basis,
    decided_at=excluded.decided_at, ttl_days=excluded.ttl_days;
END;

DROP TRIGGER IF EXISTS trg_attr_upd;
CREATE TRIGGER trg_attr_upd INSTEAD OF UPDATE ON product_attributes BEGIN
  INSERT INTO attr_history (pk, attr_name, old_value, new_value,
                            old_basis, new_basis, changed_at)
  SELECT a.pk, a.attr_name, a.value, NEW.value, a.basis, NEW.basis,
         COALESCE(NEW.decided_at, datetime('now'))
  FROM attr_base a
  WHERE a.attr_name = OLD.attr_name
    AND a.pk = (SELECT p.pk FROM product_base p JOIN sites s ON s.site_id=p.site_id
                WHERE s.name=OLD.site AND p.product_id=OLD.product_id)
    AND a.value IS NOT NEW.value;
  UPDATE attr_base SET value=NEW.value, basis=NEW.basis,
    decided_at=CAST(strftime('%s',NEW.decided_at) AS INTEGER), ttl_days=NEW.ttl_days
  WHERE attr_name=OLD.attr_name AND pk=(
    SELECT p.pk FROM product_base p JOIN sites s ON s.site_id=p.site_id
    WHERE s.name=OLD.site AND p.product_id=OLD.product_id);
END;
"""


# ── 사전 조회 (적재 경로가 쓴다) ────────────────────────────────────────────
def _dim(conn, table, id_col, name_col, value, cache):
    """사전에서 id를 찾고 없으면 만든다. 캐시는 호출자가 들고 있는 dict."""
    if value is None:
        return None
    key = (table, value)
    if key in cache:
        return cache[key]
    row = conn.execute("SELECT %s FROM %s WHERE %s=?" % (id_col, table, name_col),
                       (value,)).fetchone()
    if row is None:
        cur = conn.execute("INSERT INTO %s (%s) VALUES (?)" % (table, name_col), (value,))
        rid = cur.lastrowid
    else:
        rid = row[0]
    cache[key] = rid
    return rid


def site_id(conn, name, cache):
    return _dim(conn, "sites", "site_id", "name", name, cache)


def context_id(conn, name, cache):
    return _dim(conn, "contexts", "context_id", "name", name, cache)


def brand_id(conn, name, cache):
    """대표명 완전일치로 브랜드 id. 표기 변형 흡수는 intel_db.resolve_brand()가
    이 함수 **앞에서** 한다 — 여기는 사전 조회일 뿐 별명을 모른다."""
    return _dim(conn, "brands", "brand_id", "representative_name", name, cache)


# ── 카테고리 경로 (D65-3) ──────────────────────────────────────────────────
def split_category(path):
    """`'여성 > 스커트 > 미니'` → ['여성', '스커트', '미니']. 빈 조각은 버린다.

    **알려진 한계**: 카테고리 이름 자체에 `>`가 들어 있으면 그 지점에서 갈린다 —
    평문 경로 문자열에는 구분자와 리터럴을 가를 방법이 없다(이스케이프 규약이
    없다). 실측 카탈로그 1,546개·라이브 442행에는 그런 이름이 없고, 나타나면
    수집 어댑터가 이름을 치환하는 것이 맞다(저장 형식이 아니라 입력의 문제다).
    """
    if not path:
        return []
    return [p.strip() for p in str(path).split(">") if p.strip()]


def ensure_category_path(conn, path, cache=None):
    """카테고리 경로 문자열 → 계층 행을 보장하고 **리프의 category_id**를 돌려준다.

    depth는 경로 안 위치(1부터)다 — 플랫폼이 정해준 깊이 그대로(D65-3).
    같은 이름이라도 부모가 다르면 다른 행이다(UNIQUE(name, parent)).
    """
    parts = split_category(path)
    if not parts:
        return None
    if len(parts) > 5:
        # products 뷰는 5단까지만 도로 편다(리프 쪽 우선) — 조용히 잘리면 아무도
        # 모른다(PR #13 리뷰). 저장은 전 단계가 되므로 뷰(VIEWS_V3)만 늘리면 된다.
        print("경고: 카테고리 깊이 %d단 — products 뷰는 5단까지만 편다. "
              "VIEWS_V3의 카테고리 체인을 늘려라: %s" % (len(parts), path),
              file=sys.stderr)
    cache = cache if cache is not None else {}
    parent = None
    for depth, name in enumerate(parts, start=1):
        key = ("cat", name, parent)
        if key in cache:
            parent = cache[key]
            continue
        row = conn.execute(
            "SELECT category_id FROM categories WHERE name=? AND parent_category_id IS ?",
            (name, parent)).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO categories (name, parent_category_id, depth) VALUES (?,?,?)",
                (name, parent, depth))
            cid = cur.lastrowid
        else:
            cid = row[0]
        cache[key] = cid
        parent = cid
    return parent


def assign_category(conn, site, product_id, path, cache=None, source="platform"):
    """상품에 플랫폼 카테고리를 단다 — **카테고리 쓰기의 유일한 통로.**

    뷰 트리거는 NEW.category를 버리므로(모듈 docstring), 뷰에 상품을 넣는 코드는
    이걸 함께 불러야 한다. 상품이 아직 없으면 조용히 넘어가지 않고 False를
    돌려준다 — 호출 순서가 틀렸다는 신호다.
    """
    leaf = ensure_category_path(conn, path, cache)
    if leaf is None:
        return True                    # 카테고리가 없는 상품 — 정상
    row = conn.execute(
        "SELECT p.pk, p.site_id FROM product_base p JOIN sites s ON s.site_id=p.site_id "
        "WHERE s.name=? AND p.product_id=?", (site, str(product_id))).fetchone()
    if row is None:
        return False
    conn.execute(
        "INSERT OR IGNORE INTO product_categories (pk, category_id, source, site_id) "
        "VALUES (?,?,?,?)", (row[0], leaf, source, row[1]))
    return True


def category_path_map(conn):
    """{category_id: '상위 > … > 리프'} — 계층을 파이썬에서 편다.

    뷰는 5단까지만 펴지만 이 맵은 깊이 제한이 없다. 분석(category_hierarchy)이
    계층을 배울 때 이걸 쓴다.
    """
    rows = conn.execute(
        "SELECT category_id, name, parent_category_id FROM categories").fetchall()
    by_id = {r[0]: (r[1], r[2]) for r in rows}
    out = {}

    def path_of(cid, guard=0):
        if cid in out:
            return out[cid]
        if cid not in by_id or guard > 32:      # 고리 방어 — 자기 참조 데이터가 와도 안 돈다
            return None
        name, parent = by_id[cid]
        if parent is None:
            out[cid] = name
        else:
            pp = path_of(parent, guard + 1)
            out[cid] = (pp + " > " + name) if pp else name
        return out[cid]

    for cid in by_id:
        path_of(cid)
    return out


# ── 프록시 전이 이력·무효화 (D68) ──────────────────────────────────────────
def invalidate_material_proxies(pconn, site, product_id, material, new_fp, now):
    """재료(상품명·이미지)가 바뀐 상품의 옛 판정을 무효화한다.

    지문 불일치만으로도 재판정은 일어나지만(lazy가 miss로 본다), 옛 행이 캐시에
    남으면 "언제 어떤 값에서 바뀌었나"를 물을 수 없다. 옛 판정을 **재판정 대기
    이력**(new_value NULL)으로 옮기고 캐시에서 지운다 — 다음 판정이
    record_proxy_transition()으로 그 행을 완성한다.

    같은 재료에서 나온 **AI 속성 판정(attr_base)** 도 함께 무효화한다 (D71) —
    이름에서 뽑은 핏(basis='name')은 이름이 바뀌면 근거가 사라진 판정이다.
    attr_history에 무효화 행(new_value NULL)을 남기고 지운다. 뷰에는 DELETE
    트리거가 없으므로 attr_base를 직접 지운다. D69로 프록시가 본 DB에 있어
    전부 **같은 커넥션·같은 트랜잭션**이다.
    """
    rows = pconn.execute(
        "SELECT c.proxy_name, c.fingerprint, c.value FROM proxy_cache c "
        "JOIN proxy_defs d ON d.proxy_name = c.proxy_name "
        "WHERE d.material=? AND c.site=? AND c.product_id=? AND c.fingerprint != ?",
        (material, site, str(product_id), str(new_fp))).fetchall()
    for r in rows:
        pconn.execute(
            "INSERT INTO proxy_history (proxy_name, site, product_id, old_value, "
            "new_value, old_fingerprint, new_fingerprint, changed_at) "
            "VALUES (?,?,?,?,NULL,?,?,?)",
            (r["proxy_name"], site, str(product_id), r["value"],
             r["fingerprint"], str(new_fp), now))
        pconn.execute(
            "DELETE FROM proxy_cache WHERE proxy_name=? AND site=? AND product_id=? "
            "AND fingerprint=?",
            (r["proxy_name"], site, str(product_id), r["fingerprint"]))
    arows = pconn.execute(
        "SELECT a.pk, a.attr_name, a.value, a.basis FROM attr_base a "
        "JOIN product_base p ON p.pk = a.pk JOIN sites s ON s.site_id = p.site_id "
        "WHERE s.name=? AND p.product_id=? AND a.basis=?",
        (site, str(product_id), material)).fetchall()
    for r in arows:
        pconn.execute(
            "INSERT INTO attr_history (pk, attr_name, old_value, new_value, "
            "old_basis, new_basis, changed_at) VALUES (?,?,?,NULL,?,NULL,?)",
            (r["pk"], r["attr_name"], r["value"], r["basis"], now))
        pconn.execute("DELETE FROM attr_base WHERE pk=? AND attr_name=?",
                      (r["pk"], r["attr_name"]))
    return len(rows) + len(arows)


def record_proxy_transition(pconn, proxy_name, site, product_id, new_fp, new_val, now):
    """새 판정을 캐시에 쓰기 **직전에** 이전 판정과의 전이를 이력으로 남긴다.

    ① 무효화가 남긴 재판정 대기 행(new_value NULL, 같은 새 지문)이 있으면 완성한다
    ② 아니면 캐시의 최신 판정과 비교해 값·지문이 달라졌으면 이력을 쓰고,
       옛 지문 행을 정리한다(다지문 누적 방지 — 최신 판정만 캐시에 남는다)
    같은 값·같은 지문이면 아무것도 안 한다 — 이력은 전이만 담는다.
    """
    pending = pconn.execute(
        "SELECT id FROM proxy_history WHERE proxy_name=? AND site=? AND product_id=? "
        "AND new_value IS NULL AND new_fingerprint=? ORDER BY id DESC LIMIT 1",
        (proxy_name, site, str(product_id), str(new_fp))).fetchone()
    if pending:
        pconn.execute("UPDATE proxy_history SET new_value=?, changed_at=? WHERE id=?",
                      (new_val, now, pending[0]))
        return
    old = pconn.execute(
        "SELECT fingerprint, value FROM proxy_cache WHERE proxy_name=? AND site=? "
        "AND product_id=? ORDER BY judged_at DESC, rowid DESC LIMIT 1",
        (proxy_name, site, str(product_id))).fetchone()
    if old is None:
        return                       # 첫 판정 — 전이가 아니다
    if old["fingerprint"] == str(new_fp) and old["value"] == new_val:
        return
    pconn.execute(
        "INSERT INTO proxy_history (proxy_name, site, product_id, old_value, "
        "new_value, old_fingerprint, new_fingerprint, changed_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (proxy_name, site, str(product_id), old["value"], new_val,
         old["fingerprint"], str(new_fp), now))
    # 이 키의 캐시를 **전부** 비운다 — 호출부가 바로 새 판정을 넣는다. 지문이
    # 같고 값만 정정된 경우(rule 수정 후 재판정)에 옛 지문만 지우면 옛 값 행이
    # PK 충돌로 살아남아, 이력은 "바뀌었다"는데 캐시는 옛 값을 서빙한다
    # (PR #14 리뷰 Blocker).
    pconn.execute(
        "DELETE FROM proxy_cache WHERE proxy_name=? AND site=? AND product_id=?",
        (proxy_name, site, str(product_id)))


# ── 증분 키 (`_rowid`) — v2와 같은 규약 ────────────────────────────────────
def rowid_parts(conn, table):
    """(SELECT 접두사, WHERE·ORDER에 쓸 표현식). 물리 테이블은 진짜 rowid,
    뷰는 실제 컬럼 `_rowid` — WHERE에 별칭을 쓰지 않는다(PR #9 리뷰)."""
    is_view = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?", (table,)).fetchone())
    if is_view:
        return "SELECT *", "_rowid"
    return "SELECT rowid AS _rowid, *", "rowid"
