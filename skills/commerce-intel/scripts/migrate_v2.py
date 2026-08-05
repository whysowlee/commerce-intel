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


def _flush(dst, sql, batch):
    """배치를 넣고 **실제로 들어간 수**를 돌려준다.

    `executemany` + `OR IGNORE`는 몇 건이 무시됐는지 알려주지 않는다.
    `total_changes` 차이로 센다 — 이게 없으면 "51,034건 이관"이 거짓일 수 있다.
    """
    before = dst.total_changes
    dst.executemany(sql, batch)
    return dst.total_changes - before


def _copy_plain(src, dst, table):
    """사전으로 접을 게 없는 표는 그대로 옮긴다 (runs·platforms·proxy_*·insights)."""
    cols = [d[1] for d in src.execute("PRAGMA table_info(%s)" % table)]
    if not cols:
        return 0
    ddl = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    dst.execute(ddl[0])
    # rowid 순서를 보존해야 한다 — 새 파일은 1..N으로 다시 매겨지는데, 순서가
    # 다르면 아래 sync_state 진행점 번역(행 수 = 새 번호)이 성립하지 않는다.
    rows = src.execute(
        "SELECT %s FROM %s ORDER BY rowid" % (",".join(cols), table)).fetchall()
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
    #
    # **"없는 표"와 "깨진 표"를 가른다** (PR #9 리뷰). 전부 삼키고 0으로 적으면
    # 스키마 오류가 "0행이었나 보다"로 읽힌다. 없는 표는 조용히 넘기고, 그 밖의
    # 오류는 사유를 남긴다.
    for t in ("runs", "platforms", "proxy_defs", "proxy_cache", "insights", "sync_state"):
        exists = src.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
        if not exists:
            report[t] = 0
            report.setdefault("_missing", []).append(t)
            continue
        try:
            report[t] = _copy_plain(src, dst, t)
        except sqlite3.Error as e:
            report[t] = 0
            report.setdefault("_errors", []).append("%s: %s" % (t, e))

    # sync_state 진행점을 새 번호 체계로 번역한다 (D64). 증분 미러 키는 rowid인데
    # 삭제로 구멍 난 표(proxy_cache)는 이관에서 1..N으로 다시 매겨진다. 옛 키를
    # 그대로 들고 가면 구멍 크기만큼의 새 행이 "이미 미러됨"으로 읽혀 조용히
    # 건너뛰어진다 — 2026-08-05 실측: 500,835를 들고 가면 새 판정 14,818행이 빠진다.
    # 순서 보존 재번호이므로 새 키 = 옛 키 이하의 행 수다.
    if "sync_state" not in report.get("_missing", []):
        for tname, key in dst.execute(
                "SELECT table_name, last_synced_key FROM sync_state").fetchall():
            if not key or not str(key).isdigit():
                continue
            if not src.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                               "AND name=?", (tname,)).fetchone():
                continue
            new_key = src.execute("SELECT COUNT(*) FROM %s WHERE rowid <= ?" % tname,
                                  (int(key),)).fetchone()[0]
            if new_key != int(key):
                dst.execute("UPDATE sync_state SET last_synced_key=? "
                            "WHERE table_name=?", (str(new_key), tname))
                print("  sync_state.%s: 진행점 %s → %s (rowid 재번호 번역)"
                      % (tname, key, new_key))
    # runs가 없으면 아래 조회가 `no such table`로 이관 전체를 죽인다. 빈 매핑으로
    # 진행하고 그 사실을 남긴다 — 관측의 run_ref만 NULL이 될 뿐 나머지는 온전하다.
    run_ref = ({r[0]: r[1] for r in dst.execute("SELECT run_id, rowid FROM runs")}
               if "runs" not in report.get("_missing", []) else {})

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
        report["_obs_tried"] = report.get("_obs_tried", 0) + 1
        if len(batch) >= 5000:
            n += _flush(dst, ins, batch); batch = []
    if batch:
        n += _flush(dst, ins, batch)
    report["observations"] = n
    # `INSERT OR IGNORE`는 정상 중복과 **NOT NULL 위반(시각이 안 읽히는 행)을
    # 함께 삼킨다.** 시도 수를 그대로 보고하면 이관이 조용히 행을 잃는다
    # (PR #9 리뷰). 실제 삽입 수와 시도 수의 차이를 남긴다.
    tried = report.pop("_obs_tried", 0)
    if tried != n:
        report["obs_skipped"] = tried - n

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
        # 시도가 아니라 **실제 삽입 수**를 센다 — `OR IGNORE`는 중복과 제약 위반을
        # 함께 삼켜서, 시도 수를 보고하면 이관 리포트가 부풀려진다 (PR #9 리뷰)
        before = dst.total_changes
        dst.execute(
            "INSERT OR IGNORE INTO variant_obs_base (vk, observed_at, sold_out,"
            " stock_qty, stock_display, stock_basis, run_ref) VALUES (?,"
            + TS_SQL.format(col="?") + ",?,?,?,?,?)",
            (vk, r["observed_at"], r["sold_out"], r["stock_qty"], r["stock_display"],
             r["stock_basis"], run_ref.get(r["run_id"])))
        n += dst.total_changes - before
    report["variant_observations"] = n

    n = 0
    for r in src.execute("SELECT * FROM product_attributes"):
        pk = pk_of.get((r["site"], r["product_id"]))
        if pk is None:
            continue
        before = dst.total_changes
        dst.execute(
            "INSERT OR IGNORE INTO attr_base (pk, attr_name, value, basis, decided_at,"
            " ttl_days) VALUES (?,?,?,?," + TS_SQL.format(col="?") + ",?)",
            (pk, r["attr_name"], r["value"], r["basis"], r["decided_at"], r["ttl_days"]))
        n += dst.total_changes - before
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
        for t in rep.pop("_missing", []) or []:
            print("  ※ 원본에 `%s` 표가 없어 건너뛰었다" % t)
        for msg in rep.pop("_errors", []) or []:
            print("  !! 이관 실패: %s" % msg)
        if rep.get("obs_skipped"):
            print("  ※ 관측 %d건이 삽입되지 않았다 — 중복이거나 시각을 읽지 못한 행이다. "
                  "**행 수 검산이 이걸 잡는다**" % rep["obs_skipped"])
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
