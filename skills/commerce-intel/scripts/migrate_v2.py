#!/usr/bin/env python3
"""구 스키마 DB → v2 스키마로 옮긴다 (D45).

**원본을 고치지 않는다.** 새 파일에 짓고, 검산이 통과하면 그때 사람이 바꿔 끼운다.
제자리 변환은 중간에 죽으면 정본이 반쯤 망가진 채 남는다.

    python3 migrate_v2.py --src data/intel.db --dst data/intel-v2.db
    python3 migrate_v2.py --src data/intel.db --dst data/intel-v2.db --verify-only

## 검산이 통과 조건이다

행 수만 세지 않는다. **뷰로 되읽어 원본과 값까지 대조한다** — 사전으로 접은 값이
제대로 펴지는지, 시각이 왕복해도 같은지가 이 마이그레이션의 전부다.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from schema_v2 import (SCHEMA_V2, VIEWS_V2, brand_id, category_id,  # noqa: E402
                       context_id, site_id, split_url)

# 옛 스키마의 시각 컬럼은 "YYYY-MM-DD HH:MM:SS" 문자열이다. 정수로 접었다가 뷰에서
# 다시 같은 문자열로 펴야 하므로 **UTC 해석으로 왕복을 고정한다** — 로컬 시간대로
# 읽으면 기계마다 다른 값이 나오고, 그건 조용히 어긋나는 종류의 버그다.
TS_SQL = "CAST(strftime('%s', {col}) AS INTEGER)"


def _copy_plain(src, dst, table):
    """사전으로 접을 게 없는 표는 그대로 옮긴다 (runs·platforms·proxy_*·insights)."""
    cols = [d[1] for d in src.execute("PRAGMA table_info(%s)" % table)]
    if not cols:
        return 0
    ddl = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    dst.execute(ddl[0])
    rows = src.execute("SELECT %s FROM %s" % (",".join(cols), table)).fetchall()
    dst.executemany("INSERT INTO %s (%s) VALUES (%s)"
                    % (table, ",".join(cols), ",".join("?" * len(cols))), rows)
    return len(rows)


def migrate(src_path, dst_path):
    if os.path.exists(dst_path):
        os.remove(dst_path)
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(dst_path)
    dst.executescript(SCHEMA_V2)
    cache = {}
    report = {}

    # ① 사전으로 안 접는 표 먼저 — obs_base.run_ref가 runs.rowid를 가리킨다
    for t in ("runs", "platforms", "proxy_defs", "proxy_cache", "insights", "sync_state"):
        try:
            report[t] = _copy_plain(src, dst, t)
        except sqlite3.Error:
            report[t] = 0
    run_ref = {r[0]: r[1] for r in dst.execute("SELECT run_id, rowid FROM runs")}

    # ② 상품 — 여기서 사전이 채워진다
    pk_of = {}
    for r in src.execute("SELECT * FROM products"):
        sid = site_id(dst, r["site"], cache)
        uh, up = split_url(dst, r["url"], cache)
        ih, ip = split_url(dst, r["image_url"], cache)
        cur = dst.execute(
            "INSERT INTO product_base (site_id, product_id, name, url_host, url_path,"
            " img_host, img_path, brand_id, category_id, attributes, attributes_basis,"
            " static_verified_at, first_seen_at, last_seen_at, raw_extras)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,"
            + TS_SQL.format(col="?") + "," + TS_SQL.format(col="?") + ","
            + TS_SQL.format(col="?") + ", ?)",
            (sid, r["product_id"], r["name"], uh, up, ih, ip,
             brand_id(dst, r["brand"], cache), category_id(dst, r["category"], cache),
             r["attributes"], r["attributes_basis"],
             r["static_verified_at"], r["first_seen_at"], r["last_seen_at"],
             r["raw_extras"] if "raw_extras" in r.keys() else None))
        pk_of[(r["site"], r["product_id"])] = cur.lastrowid
    report["products"] = len(pk_of)

    # ③ 관측 — 행이 제일 많다. 원본 rowid 순서를 지켜야 sync_sheets 증분이 안 어긋난다
    ocols = [d[1] for d in src.execute("PRAGMA table_info(observations)")]
    metric = [c for c in ocols if c not in
              ("site", "product_id", "observed_at", "context", "run_id")]
    n = 0
    batch = []
    ins = ("INSERT OR IGNORE INTO obs_base (pk, observed_at, context_id, run_ref, %s)"
           " VALUES (?,%s,?,?,%s)" % (",".join(metric), TS_SQL.format(col="?"),
                                      ",".join("?" * len(metric))))
    for r in src.execute("SELECT rowid AS _r, * FROM observations ORDER BY rowid"):
        pk = pk_of.get((r["site"], r["product_id"]))
        if pk is None:      # 상품 표에 없는 관측 — 옛 DB의 고아 행. 세어서 보고한다
            report["orphan_obs"] = report.get("orphan_obs", 0) + 1
            continue
        batch.append([pk, r["observed_at"], context_id(dst, r["context"], cache),
                      run_ref.get(r["run_id"])] + [r[c] for c in metric])
        if len(batch) >= 5000:
            dst.executemany(ins, batch); n += len(batch); batch = []
    if batch:
        dst.executemany(ins, batch); n += len(batch)
    report["observations"] = n

    # ④ 옵션·옵션 관측·속성
    vk_of = {}
    for r in src.execute("SELECT * FROM variants"):
        pk = pk_of.get((r["site"], r["product_id"]))
        if pk is None:
            continue
        cur = dst.execute(
            "INSERT INTO variant_base (pk, option_id, option_name, color, size,"
            " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,"
            + TS_SQL.format(col="?") + "," + TS_SQL.format(col="?") + ")",
            (pk, r["option_id"], r["option_name"], r["color"], r["size"],
             r["first_seen_at"], r["last_seen_at"]))
        vk_of[(r["site"], r["product_id"], r["option_id"])] = cur.lastrowid
    report["variants"] = len(vk_of)

    n = 0
    for r in src.execute("SELECT * FROM variant_observations ORDER BY rowid"):
        vk = vk_of.get((r["site"], r["product_id"], r["option_id"]))
        if vk is None:
            continue
        dst.execute(
            "INSERT OR IGNORE INTO variant_obs_base (vk, observed_at, sold_out,"
            " stock_qty, stock_display, stock_basis, run_ref) VALUES (?,"
            + TS_SQL.format(col="?") + ",?,?,?,?,?)",
            (vk, r["observed_at"], r["sold_out"], r["stock_qty"], r["stock_display"],
             r["stock_basis"], run_ref.get(r["run_id"])))
        n += 1
    report["variant_observations"] = n

    n = 0
    for r in src.execute("SELECT * FROM product_attributes"):
        pk = pk_of.get((r["site"], r["product_id"]))
        if pk is None:
            continue
        dst.execute(
            "INSERT OR IGNORE INTO attr_base (pk, attr_name, value, basis, decided_at,"
            " ttl_days) VALUES (?,?,?,?," + TS_SQL.format(col="?") + ",?)",
            (pk, r["attr_name"], r["value"], r["basis"], r["decided_at"], r["ttl_days"]))
        n += 1
    report["product_attributes"] = n

    dst.executescript(VIEWS_V2)
    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    src.close()
    return report


# ── 검산 ────────────────────────────────────────────────────────────────────
CHECK_TABLES = ["products", "observations", "variants", "variant_observations",
                "product_attributes", "runs"]


def verify(src_path, dst_path, sample=400):
    """행 수 + **값 대조**. 사전으로 접은 것이 그대로 펴지는지가 핵심이다."""
    src = sqlite3.connect(src_path); src.row_factory = sqlite3.Row
    dst = sqlite3.connect(dst_path); dst.row_factory = sqlite3.Row
    ok = True
    for t in CHECK_TABLES:
        a = src.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        b = dst.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        mark = "OK" if a == b else "!! 불일치"
        if a != b:
            ok = False
        print("  %-22s 원본 %8s → v2 %8s  %s"
              % (t, "{:,}".format(a), "{:,}".format(b), mark))

    # 값 대조 — 뷰로 되읽어 원본 행과 컬럼별로 맞춰 본다
    for t, key in (("products", "site, product_id"),
                   ("observations", "site, product_id, observed_at, context")):
        cols = [d[1] for d in src.execute("PRAGMA table_info(%s)" % t)]
        order = "ORDER BY " + key
        rows_a = src.execute("SELECT %s FROM %s %s LIMIT %d"
                             % (",".join(cols), t, order, sample)).fetchall()
        rows_b = dst.execute("SELECT %s FROM %s %s LIMIT %d"
                             % (",".join(cols), t, order, sample)).fetchall()
        diffs = []
        for ra, rb in zip(rows_a, rows_b):
            for c in cols:
                if (ra[c] or "") != (rb[c] or ""):
                    diffs.append((c, ra[c], rb[c]))
        if diffs:
            ok = False
            print("  %-22s 값 불일치 %d건 — 예: %s" % (t, len(diffs), diffs[:3]))
        else:
            print("  %-22s 표본 %d행 전 컬럼 값 일치" % (t, len(rows_a)))
    src.close(); dst.close()
    return ok


def main():
    ap = argparse.ArgumentParser(description="구 스키마 → v2 (D45)")
    ap.add_argument("--src", default="data/intel.db")
    ap.add_argument("--dst", default="data/intel-v2.db")
    ap.add_argument("--verify-only", action="store_true")
    a = ap.parse_args()

    if not a.verify_only:
        rep = migrate(a.src, a.dst)
        print("── 이관 ──")
        for k, v in rep.items():
            print("  %-22s %s" % (k, "{:,}".format(v)))
        if rep.get("orphan_obs"):
            print("  ※ 상품 표에 없는 관측 %d건은 옮기지 않았다 (옛 DB의 고아 행)"
                  % rep["orphan_obs"])
    print("── 검산 ──")
    ok = verify(a.src, a.dst)
    for p, label in ((a.src, "원본"), (a.dst, "v2  ")):
        print("  %s %6.2f MB" % (label, os.path.getsize(p) / 1048576))
    print("검산 %s" % ("통과" if ok else "실패 — 바꿔 끼우지 마라"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
