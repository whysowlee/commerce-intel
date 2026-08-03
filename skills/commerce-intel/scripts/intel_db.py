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
from datetime import datetime, timedelta
from pathlib import Path

# 작업 폴더 기준이다(스킬 §파일 규약). 스크립트 위치 기준이 아니므로
# 스킬을 어디에 설치하든 DB는 그 작업의 data/ 아래에 생긴다.
DEFAULT_DB = os.environ.get("INTEL_DB", "data/intel.db")
STATIC_TTL_DAYS = 90          # D7
DEFAULT_CYCLE_MINUTES = 1440  # D8 — 갱신 주기 미상일 때 24시간

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
OBS_FIELDS = (
    "price_original", "price_sale", "discount_rate", "review_count", "rating",
    "view_count", "view_count_display", "purchase_count", "purchase_count_display",
    "like_count", "like_count_display", "viewers_now", "buyers_now", "sold_out", "rank",
)


def connect(db_path):
    parent = Path(db_path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:  # 구버전 DB 마이그레이션 — raw_extras(재료 보존, D19)
        conn.execute("ALTER TABLE products ADD COLUMN raw_extras TEXT")
    except sqlite3.OperationalError:
        pass
    return conn


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip()[:19], fmt)
        except (ValueError, AttributeError):
            continue
    return None


def context_of(meta):
    story = meta.get("story", "")
    target = meta.get("target", "")
    prefix = {"brand-linesheet": "brand", "market-scan": "market", "ranking-snapshot": "ranking"}
    return f"{prefix.get(story, story or 'adhoc')}:{target}"


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
        conn.execute(
            "INSERT INTO variants VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(site, product_id, option_id) DO UPDATE SET "
            "option_name=COALESCE(excluded.option_name, option_name), "
            "color=COALESCE(excluded.color, color), size=COALESCE(excluded.size, size), "
            "last_seen_at=excluded.last_seen_at",
            (site, pid, oid, v.get("option_name"), v.get("color"), v.get("size"),
             collected_at, collected_at),
        )
        so = v.get("sold_out")
        try:
            conn.execute(
                "INSERT INTO variant_observations VALUES (?,?,?,?,?,?,?,?,?)",
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
        "last_observed_at": row["last"] if row else None,
        "cycle_minutes": cycle,
        "reason": (
            f"최신 관측이 갱신 주기({cycle}분) 이내 — 재수집 생략 가능" if fresh
            else "신선한 관측 없음 — 수집 필요"
        ),
    }
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
        row = conn.execute(
            "SELECT attributes, attributes_basis, static_verified_at FROM products "
            "WHERE site=? AND product_id=? AND attributes IS NOT NULL",
            (site, str(it.get("product_id"))),
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
    q = f"SELECT * FROM {args.table}"  # 테이블명은 choices로 제한됨
    if args.since_rowid is not None:
        q += f" WHERE rowid > {int(args.since_rowid)}"
    q += " ORDER BY rowid"
    rows = conn.execute(
        q.replace("SELECT *", "SELECT rowid AS _rowid, *", 1)).fetchall()
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
    d = data.get("proxy") or {}
    if not d.get("proxy_name"):
        raise SystemExit("proxy.proxy_name이 없다")
    conn.execute(
        "INSERT INTO proxy_defs VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(proxy_name) DO UPDATE SET question=excluded.question, "
        "material=excluded.material, value_space=excluded.value_space, method=excluded.method",
        (d["proxy_name"], d.get("question"), d.get("material"),
         json.dumps(d.get("value_space"), ensure_ascii=False), d.get("method"), now_str()))
    space = d.get("value_space")
    new = dup = bad = 0
    for j in data.get("judgments", []):
        if isinstance(space, list) and j.get("value") not in space:
            bad += 1  # 값 공간 밖 판정은 버린다 — 정의가 계약이다
            continue
        try:
            conn.execute(
                "INSERT INTO proxy_cache VALUES (?,?,?,?,?,?,?)",
                (d["proxy_name"], j.get("site"), str(j.get("product_id")),
                 j.get("fingerprint"), j.get("value"), j.get("basis"), now_str()))
            new += 1
        except sqlite3.IntegrityError:
            dup += 1
    conn.commit()
    msg = f"{d['proxy_name']}: 판정 {new}건 적재, 중복 {dup}건 스킵"
    if bad:
        msg += f", 값 공간 밖 {bad}건 거부"
    print(msg)


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

    # 관측 계열 — 키가 겹치면 스킵. INSERT OR IGNORE가 곧 그 규칙이다
    for table in ("observations", "variant_observations", "runs"):
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

    # 정적 계열 — 빈 값으로 덮지 않는다
    for table, keys in (("products", ("site", "product_id")),
                        ("variants", ("site", "product_id", "option_id")),
                        ("platforms", ("platform_key",)),
                        ("proxy_defs", ("proxy_name",)),
                        ("proxy_cache", ("proxy_name", "site", "product_id", "fingerprint"))):
        try:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue
        if not rows:
            continue
        cols = [c for c in rows[0].keys()]
        upd = [c for c in cols if c not in keys]
        before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        # COALESCE(excluded.x, x) — 들어온 값이 null이면 기존 값을 지킨다
        sets = ", ".join("%s = COALESCE(excluded.%s, %s.%s)" % (c, c, table, c) for c in upd)
        conn.executemany(
            "INSERT INTO %s (%s) VALUES (%s) ON CONFLICT(%s) DO UPDATE SET %s"
            % (table, ",".join(cols), ",".join("?" * len(cols)), ",".join(keys), sets),
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
    sp = sub.add_parser("reuse-attrs")
    sp.add_argument("raw")
    sp.add_argument("--out")
    sp.add_argument("--ttl-days", type=int, default=STATIC_TTL_DAYS)
    sp = sub.add_parser("stale-static")
    sp.add_argument("--site")
    sp.add_argument("--ttl-days", type=int, default=STATIC_TTL_DAYS)
    sp = sub.add_parser("import-snapshots"); sp.add_argument("dir")
    sp = sub.add_parser("proxy-load"); sp.add_argument("file")
    sp = sub.add_parser("export")
    sp.add_argument("--table", required=True,
                    choices=["products", "observations", "variants", "variant_observations", "platforms", "runs", "proxy_defs", "proxy_cache"])
    sp.add_argument("--format", choices=["csv", "json"], default="csv")
    sp.add_argument("--since-rowid", type=int, default=None)
    sub.add_parser("stats")
    sp = sub.add_parser("merge")
    sp.add_argument("source", help="합칠 상대 DB 경로 (팀원이 보내준 intel.db)")
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
    elif args.cmd == "export":
        cmd_export(conn, args)
    elif args.cmd == "stats":
        cmd_stats(conn, args)


if __name__ == "__main__":
    main()
