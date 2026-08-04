#!/usr/bin/env python3
"""정본 DB 스키마 v2 — 반복 텍스트를 정수로 접는다 (D45).

## 왜 바꾸나 (2026-08-04 실측)

지금 31MB는 문제가 아니다. **문제는 행당 비용과 증가 속도다.**

    observations 1행 = 266바이트 (테이블 5.36MB + 자동인덱스 4.36MB + idx 3.27MB)
    하루 2,400~3,400행 · D41로 크론이 4사이트 늘었다 → 시간당 6사이트면 연 500만 행
    266바이트 × 500만 = 연 1.3GB

그 266바이트의 대부분이 **같은 문자열의 반복**이다:

    context      고유 16개    를 51,034행에 = 1.11MB
    site         고유 12개    를 51,034행에 = 0.34MB
    run_id       고유 221개   를 51,034행에 = 0.58MB
    observed_at  TEXT 19바이트 × 51,034행  = 0.92MB
    PK가 텍스트 4개(site·product_id·observed_at·context) → 자동인덱스만 4.36MB

products도 같다 — `image_url` 2.90MB 중 91%가 CDN 호스트 2개의 반복이다.

## 무엇을 바꾸나

1. **사전 테이블** — site·context·brand·category·URL 호스트를 정수 id로
2. **정수 대리키** — 상품을 `(site TEXT, product_id TEXT)` 대신 `pk INTEGER`로 가리킨다.
   관측이 상품을 참조하는 비용이 20바이트에서 2~3바이트가 된다
3. **시각은 INTEGER**(unix epoch) — TEXT 19바이트 → 4~5바이트
4. **인덱스도 정수 위에** — 텍스트 4개 복합키가 사라진다

## 읽는 코드는 하나도 안 고친다 (사용자 결정 2026-08-04)

구 이름 그대로의 **뷰**를 얹는다. `SELECT site, product_id, observed_at, context …
FROM observations`가 한 글자도 안 바뀌고 돈다. 회귀 129건이 그대로 검증해 주므로
마이그레이션이 맞게 됐는지 바로 안다.

**대신 쓰기는 뷰로 못 한다** — SQLite 뷰는 읽기 전용이다. 적재 경로(`intel_db.py`)는
새 구조를 알아야 하고, 그건 이 파일의 `resolve_*` 헬퍼를 쓴다.

## 보존 정책 (사용자 결정 2026-08-04)

오래된 관측은 간격을 드물게 하되 **가격·순위가 바뀐 관측은 무조건 남긴다.**
값이 안 변한 구간만 솎으므로 이중차분·용량반응 분석이 보는 "변화 순간"은 사라지지 않는다.
`prune.py` 참조.
"""

# 사전 5종 + 본체 5종. 뷰가 구 이름을 그대로 쓰므로 물리 테이블은 `_base`를 붙인다.
SCHEMA_V2 = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sites (
    site_id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS contexts (
    context_id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS brands (
    brand_id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS categories (
    category_id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
-- URL 앞부분(스킴+호스트)만 접는다. 뒤 경로는 상품마다 달라 접을 게 없다.
CREATE TABLE IF NOT EXISTS hosts (
    host_id INTEGER PRIMARY KEY, prefix TEXT NOT NULL UNIQUE);

CREATE TABLE IF NOT EXISTS product_base (
    pk INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(site_id),
    product_id TEXT NOT NULL,
    name TEXT,
    url_host INTEGER REFERENCES hosts(host_id), url_path TEXT,
    img_host INTEGER REFERENCES hosts(host_id), img_path TEXT,
    brand_id INTEGER REFERENCES brands(brand_id),
    category_id INTEGER REFERENCES categories(category_id),
    attributes TEXT, attributes_basis TEXT,
    static_verified_at INTEGER, first_seen_at INTEGER, last_seen_at INTEGER,
    raw_extras TEXT,                        -- D19 원문 보존. 접을 게 없어 그대로 둔다
    UNIQUE (site_id, product_id)
);

-- `id`를 명시한다: sync_sheets가 rowid 증분으로 시트를 미러하므로 WITHOUT ROWID를
-- 쓸 수 없다. 자동인덱스는 사라지지 않지만 **정수 3개짜리**라 텍스트 4개보다 훨씬 작다.
CREATE TABLE IF NOT EXISTS obs_base (
    id INTEGER PRIMARY KEY,
    pk INTEGER NOT NULL REFERENCES product_base(pk),
    observed_at INTEGER NOT NULL,           -- unix epoch (초)
    context_id INTEGER NOT NULL REFERENCES contexts(context_id),
    -- runs.rowid를 가리키지만 **FK 제약은 못 건다** — SQLite의 외래키는 선언된
    -- PK나 UNIQUE 컬럼만 가리킬 수 있고 rowid는 그 대상이 아니다. 뷰의 LEFT JOIN이
    -- 끊어진 참조를 NULL로 흘려보내므로 조회는 안전하다.
    run_ref INTEGER,
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

CREATE TABLE IF NOT EXISTS variant_base (
    vk INTEGER PRIMARY KEY,
    pk INTEGER NOT NULL REFERENCES product_base(pk),
    option_id TEXT NOT NULL,
    option_name TEXT, color TEXT, size TEXT,
    first_seen_at INTEGER, last_seen_at INTEGER,
    UNIQUE (pk, option_id)
);
CREATE TABLE IF NOT EXISTS variant_obs_base (
    id INTEGER PRIMARY KEY,
    vk INTEGER NOT NULL REFERENCES variant_base(vk),
    observed_at INTEGER NOT NULL,
    sold_out INTEGER, stock_qty INTEGER, stock_display TEXT, stock_basis TEXT,
    run_ref INTEGER,                        -- runs.rowid (FK 불가 — obs_base 주석 참조)
    UNIQUE (vk, observed_at)
);
CREATE TABLE IF NOT EXISTS attr_base (
    pk INTEGER NOT NULL REFERENCES product_base(pk),
    attr_name TEXT NOT NULL,
    value TEXT, basis TEXT, decided_at INTEGER, ttl_days INTEGER,
    PRIMARY KEY (pk, attr_name)
);
CREATE INDEX IF NOT EXISTS idx_attr_name2 ON attr_base (attr_name, value);
"""

# ── 뷰 — 구 이름·구 컬럼 그대로 ────────────────────────────────────────────
# 읽는 코드가 한 글자도 안 바뀌게 하는 층이다. **컬럼 이름과 순서를 옛 스키마와
# 정확히 맞춘다** — `SELECT *`로 읽는 곳이 있어서 순서가 어긋나면 조용히 틀린다.
#
# 시각은 다시 TEXT로 돌려준다(`datetime(...,'unixepoch')`). 저장만 정수로 바꾼 것이지
# 바깥 계약은 그대로다 — 비교·정렬이 문자열 사전순으로 돌아가던 코드가 많다.
VIEWS_V2 = """
DROP VIEW IF EXISTS products;
CREATE VIEW products AS
SELECT s.name AS site, p.product_id, p.name AS name,
       CASE WHEN p.url_path IS NULL THEN NULL
            ELSE COALESCE(hu.prefix,'') || p.url_path END AS url,
       CASE WHEN p.img_path IS NULL THEN NULL
            ELSE COALESCE(hi.prefix,'') || p.img_path END AS image_url,
       b.name AS brand, c.name AS category,
       p.attributes, p.attributes_basis,
       datetime(p.static_verified_at,'unixepoch') AS static_verified_at,
       datetime(p.first_seen_at,'unixepoch') AS first_seen_at,
       datetime(p.last_seen_at,'unixepoch') AS last_seen_at,
       p.raw_extras,
       p.pk AS _rowid                       -- 증분 미러·export용 물리 키 (맨 뒤)
FROM product_base p
JOIN sites s ON s.site_id = p.site_id
LEFT JOIN brands b ON b.brand_id = p.brand_id
LEFT JOIN categories c ON c.category_id = p.category_id
LEFT JOIN hosts hu ON hu.host_id = p.url_host
LEFT JOIN hosts hi ON hi.host_id = p.img_host;

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
LEFT JOIN runs r ON r.rowid = o.run_ref;

DROP VIEW IF EXISTS variants;
CREATE VIEW variants AS
SELECT s.name AS site, p.product_id, v.option_id,
       v.option_name, v.color, v.size,
       datetime(v.first_seen_at,'unixepoch') AS first_seen_at,
       datetime(v.last_seen_at,'unixepoch') AS last_seen_at,
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
LEFT JOIN runs r ON r.rowid = vo.run_ref;

DROP VIEW IF EXISTS product_attributes;
CREATE VIEW product_attributes AS
SELECT s.name AS site, p.product_id, a.attr_name, a.value, a.basis,
       datetime(a.decided_at,'unixepoch') AS decided_at, a.ttl_days,
       a.rowid AS _rowid
FROM attr_base a
JOIN product_base p ON p.pk = a.pk
JOIN sites s ON s.site_id = p.site_id;
"""


# ── 쓰기 트리거 — 뷰를 쓸 수 있게 만든다 ──────────────────────────────────
# SQLite 뷰는 기본적으로 읽기 전용이지만 `INSTEAD OF` 트리거를 걸면 쓸 수 있다.
# 그러면 적재 코드가 `INSERT INTO observations (...) VALUES (...)`를 그대로 쓴다.
#
# **한 가지는 못 넘긴다** — `INSERT ... ON CONFLICT ... DO UPDATE`(업서트)는 뷰에
# 못 쓴다("cannot UPSERT a view"). 그래서 업서트 의미를 **트리거 안으로 옮겼다**:
# 호출부는 `ON CONFLICT` 절을 빼고 평범한 INSERT를 하면 되고, 덮어쓸지 지킬지는
# 여기 적힌 COALESCE 규칙이 정한다. 규칙이 한곳에 모이는 편이 낫다.
#
# 중복 관측은 **여전히 IntegrityError를 낸다** — 적재 코드가 그걸 잡아 "중복 N건
# 스킵"을 세고 있어서, 조용히 삼키면 그 숫자가 거짓이 된다.

# URL을 스킴+호스트 / 경로로 자르는 식. 파이썬 split_url()과 같은 규칙이어야 한다.
_HOST = ("CASE WHEN instr({u},'//')=0 THEN '' ELSE substr({u},1,"
         "instr({u},'//')+instr(substr({u},instr({u},'//')+2),'/')) END")
_PATH = ("CASE WHEN instr({u},'//')=0 THEN {u} ELSE substr({u},"
         "instr({u},'//')+instr(substr({u},instr({u},'//')+2),'/')+1) END")

TRIGGERS_V2 = """
DROP TRIGGER IF EXISTS trg_products_ins;
CREATE TRIGGER trg_products_ins INSTEAD OF INSERT ON products BEGIN
  INSERT OR IGNORE INTO sites(name) VALUES (NEW.site);
  INSERT OR IGNORE INTO brands(name) SELECT NEW.brand WHERE NEW.brand IS NOT NULL;
  INSERT OR IGNORE INTO categories(name) SELECT NEW.category WHERE NEW.category IS NOT NULL;
  INSERT OR IGNORE INTO hosts(prefix) SELECT %(uh)s WHERE NEW.url IS NOT NULL;
  INSERT OR IGNORE INTO hosts(prefix) SELECT %(ih)s WHERE NEW.image_url IS NOT NULL;
  INSERT INTO product_base (site_id, product_id, name, url_host, url_path,
      img_host, img_path, brand_id, category_id, attributes, attributes_basis,
      static_verified_at, first_seen_at, last_seen_at, raw_extras)
  VALUES (
    (SELECT site_id FROM sites WHERE name=NEW.site), NEW.product_id, NEW.name,
    (SELECT host_id FROM hosts WHERE prefix=%(uh)s), %(up)s,
    (SELECT host_id FROM hosts WHERE prefix=%(ih)s), %(ip)s,
    (SELECT brand_id FROM brands WHERE name=NEW.brand),
    (SELECT category_id FROM categories WHERE name=NEW.category),
    NEW.attributes, NEW.attributes_basis,
    CAST(strftime('%%s',NEW.static_verified_at) AS INTEGER),
    CAST(strftime('%%s',NEW.first_seen_at) AS INTEGER),
    CAST(strftime('%%s',NEW.last_seen_at) AS INTEGER), NEW.raw_extras)
  ON CONFLICT(site_id, product_id) DO UPDATE SET
    name=COALESCE(excluded.name, name),
    url_host=COALESCE(excluded.url_host, url_host),
    url_path=COALESCE(excluded.url_path, url_path),
    img_host=COALESCE(excluded.img_host, img_host),
    img_path=COALESCE(excluded.img_path, img_path),
    brand_id=COALESCE(excluded.brand_id, brand_id),
    category_id=COALESCE(excluded.category_id, category_id),
    attributes=COALESCE(excluded.attributes, attributes),
    attributes_basis=COALESCE(excluded.attributes_basis, attributes_basis),
    static_verified_at=COALESCE(excluded.static_verified_at, static_verified_at),
    last_seen_at=COALESCE(excluded.last_seen_at, last_seen_at),
    raw_extras=COALESCE(excluded.raw_extras, raw_extras);
END;

-- UPDATE는 SET 목록이 호출부마다 다르다. 트리거는 NEW.*로 전 컬럼을 받는데,
-- SET에 없는 컬럼은 NEW=OLD라 전부 다시 써도 결과가 같다.
DROP TRIGGER IF EXISTS trg_products_upd;
CREATE TRIGGER trg_products_upd INSTEAD OF UPDATE ON products BEGIN
  INSERT OR IGNORE INTO brands(name) SELECT NEW.brand WHERE NEW.brand IS NOT NULL;
  INSERT OR IGNORE INTO categories(name) SELECT NEW.category WHERE NEW.category IS NOT NULL;
  INSERT OR IGNORE INTO hosts(prefix) SELECT %(uh)s WHERE NEW.url IS NOT NULL;
  INSERT OR IGNORE INTO hosts(prefix) SELECT %(ih)s WHERE NEW.image_url IS NOT NULL;
  UPDATE product_base SET
    name=NEW.name,
    url_host=(SELECT host_id FROM hosts WHERE prefix=%(uh)s), url_path=%(up)s,
    img_host=(SELECT host_id FROM hosts WHERE prefix=%(ih)s), img_path=%(ip)s,
    brand_id=(SELECT brand_id FROM brands WHERE name=NEW.brand),
    category_id=(SELECT category_id FROM categories WHERE name=NEW.category),
    attributes=NEW.attributes, attributes_basis=NEW.attributes_basis,
    static_verified_at=CAST(strftime('%%s',NEW.static_verified_at) AS INTEGER),
    last_seen_at=CAST(strftime('%%s',NEW.last_seen_at) AS INTEGER),
    raw_extras=NEW.raw_extras
  WHERE pk = (SELECT p.pk FROM product_base p JOIN sites s ON s.site_id=p.site_id
              WHERE s.name=OLD.site AND p.product_id=OLD.product_id);
END;

DROP TRIGGER IF EXISTS trg_obs_ins;
CREATE TRIGGER trg_obs_ins INSTEAD OF INSERT ON observations BEGIN
  INSERT OR IGNORE INTO contexts(name) VALUES (NEW.context);
  INSERT INTO obs_base (pk, observed_at, context_id, run_ref,
      price_original, price_sale, discount_rate, review_count, rating,
      view_count, view_count_display, purchase_count, purchase_count_display,
      like_count, like_count_display, viewers_now, buyers_now, sold_out, rank)
  VALUES (
    (SELECT p.pk FROM product_base p JOIN sites s ON s.site_id=p.site_id
     WHERE s.name=NEW.site AND p.product_id=NEW.product_id),
    CAST(strftime('%%s',NEW.observed_at) AS INTEGER),
    (SELECT context_id FROM contexts WHERE name=NEW.context),
    (SELECT rowid FROM runs WHERE run_id=NEW.run_id),
    NEW.price_original, NEW.price_sale, NEW.discount_rate, NEW.review_count, NEW.rating,
    NEW.view_count, NEW.view_count_display, NEW.purchase_count, NEW.purchase_count_display,
    NEW.like_count, NEW.like_count_display, NEW.viewers_now, NEW.buyers_now,
    NEW.sold_out, NEW.rank);
END;

DROP TRIGGER IF EXISTS trg_variants_ins;
CREATE TRIGGER trg_variants_ins INSTEAD OF INSERT ON variants BEGIN
  INSERT INTO variant_base (pk, option_id, option_name, color, size,
                            first_seen_at, last_seen_at)
  VALUES (
    (SELECT p.pk FROM product_base p JOIN sites s ON s.site_id=p.site_id
     WHERE s.name=NEW.site AND p.product_id=NEW.product_id),
    NEW.option_id, NEW.option_name, NEW.color, NEW.size,
    CAST(strftime('%%s',NEW.first_seen_at) AS INTEGER),
    CAST(strftime('%%s',NEW.last_seen_at) AS INTEGER))
  ON CONFLICT(pk, option_id) DO UPDATE SET
    option_name=COALESCE(excluded.option_name, option_name),
    color=COALESCE(excluded.color, color),
    size=COALESCE(excluded.size, size),
    last_seen_at=excluded.last_seen_at;
END;

DROP TRIGGER IF EXISTS trg_vobs_ins;
CREATE TRIGGER trg_vobs_ins INSTEAD OF INSERT ON variant_observations BEGIN
  INSERT INTO variant_obs_base (vk, observed_at, sold_out, stock_qty,
                                stock_display, stock_basis, run_ref)
  VALUES (
    (SELECT v.vk FROM variant_base v JOIN product_base p ON p.pk=v.pk
     JOIN sites s ON s.site_id=p.site_id
     WHERE s.name=NEW.site AND p.product_id=NEW.product_id AND v.option_id=NEW.option_id),
    CAST(strftime('%%s',NEW.observed_at) AS INTEGER),
    NEW.sold_out, NEW.stock_qty, NEW.stock_display, NEW.stock_basis,
    (SELECT rowid FROM runs WHERE run_id=NEW.run_id));
END;

DROP TRIGGER IF EXISTS trg_attr_ins;
CREATE TRIGGER trg_attr_ins INSTEAD OF INSERT ON product_attributes BEGIN
  INSERT INTO attr_base (pk, attr_name, value, basis, decided_at, ttl_days)
  VALUES (
    (SELECT p.pk FROM product_base p JOIN sites s ON s.site_id=p.site_id
     WHERE s.name=NEW.site AND p.product_id=NEW.product_id),
    NEW.attr_name, NEW.value, NEW.basis,
    CAST(strftime('%%s',NEW.decided_at) AS INTEGER), NEW.ttl_days)
  -- COALESCE를 쓰지 않는다: set-attrs는 `ttl_days=NULL`을 **의도적으로** 넣어
  -- 전역 --ttl-days가 먹게 한다(intel_db.py 주석 "미지정이면 NULL"). COALESCE로
  -- 옛 값을 지키면 그 의도가 조용히 무시된다.
  ON CONFLICT(pk, attr_name) DO UPDATE SET
    value=excluded.value, basis=excluded.basis,
    decided_at=excluded.decided_at, ttl_days=excluded.ttl_days;
END;

DROP TRIGGER IF EXISTS trg_attr_upd;
CREATE TRIGGER trg_attr_upd INSTEAD OF UPDATE ON product_attributes BEGIN
  UPDATE attr_base SET value=NEW.value, basis=NEW.basis,
    decided_at=CAST(strftime('%%s',NEW.decided_at) AS INTEGER), ttl_days=NEW.ttl_days
  WHERE attr_name=OLD.attr_name AND pk=(
    SELECT p.pk FROM product_base p JOIN sites s ON s.site_id=p.site_id
    WHERE s.name=OLD.site AND p.product_id=OLD.product_id);
END;
""" % {"uh": _HOST.format(u="NEW.url"), "up": _PATH.format(u="NEW.url"),
       "ih": _HOST.format(u="NEW.image_url"), "ip": _PATH.format(u="NEW.image_url")}


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
    return _dim(conn, "brands", "brand_id", "name", name, cache)


def category_id(conn, name, cache):
    return _dim(conn, "categories", "category_id", "name", name, cache)


def split_url(conn, url, cache):
    """URL을 (호스트 id, 나머지 경로)로 나눈다.

    호스트는 상품 수만큼 반복되는 문자열이라 접으면 크게 준다(실측: `image_url`
    2.90MB 중 91%가 CDN 2곳). 경로는 상품마다 달라 접을 게 없다.
    """
    if not url:
        return None, None
    s = str(url)
    i = s.find("//")
    j = s.find("/", i + 2) if i >= 0 else -1
    if i < 0 or j < 0:
        return _dim(conn, "hosts", "host_id", "prefix", "", cache), s
    return _dim(conn, "hosts", "host_id", "prefix", s[:j], cache), s[j:]


# ── 증분 키 (`_rowid`) ─────────────────────────────────────────────────────
# 구 스키마에서는 `rowid`가 곧 증분 키였다. v2에서 옛 이름은 **뷰**라 rowid가 없어서
# 뷰가 물리 키를 `_rowid`로 노출한다. 증분 미러(sync_sheets)와 export가 이 헬퍼를 쓴다.

def select_rowid(conn, table):
    """`SELECT ...` 접두사. 뷰면 `_rowid`가 이미 있고, 표면 `rowid`를 붙여 준다."""
    is_view = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?", (table,)).fetchone())
    return "SELECT *" if is_view else "SELECT rowid AS _rowid, *"
