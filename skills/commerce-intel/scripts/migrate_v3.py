#!/usr/bin/env python3
"""v2 스키마 DB → v3 (D65). 프록시는 별도 `proxy.db`로 갈라져 나간다.

    python3 migrate_v3.py --src data/intel.db                # 이관→검산→백업→교체
    python3 migrate_v3.py --src data/intel.db --no-swap      # 새 파일만 만들고 교체 안 함
    python3 migrate_v3.py --src data/intel.db --verify-only  # 검산만

## 절차 (사용자 확정 설계 — D65-10)

새 파일에 짓고 검산이 통과했을 때만 교체한다. 교체 직전 원본을
`<src>.v2-backup`으로 남긴다 — 제자리 변환은 중간에 죽으면 정본이 반쯤
망가진 채 남는다(D45와 같은 원칙, 백업·교체 자동화만 D65-10로 더해졌다).

## 무엇이 바뀌나

- **정수 키 보존**: pk·vk·obs id는 그대로 복사한다 — attr_base·관측의 참조와
  sync_state 진행점(observations·variant_observations)이 번역 없이 성립한다.
- **runs.id = 구 rowid**: v2의 run_ref는 runs.rowid를 비공식 참조했다. 새 id를
  구 rowid 값 그대로 매기면 관측의 run_ref 숫자가 곧 정식 FK 값이 된다.
- **카테고리 계층화**: v2 categories의 문자열(경로 포함)을 분해해 계층 행으로
  짓고, product_base.category_id를 product_categories 매핑으로 옮긴다.
- **attributes JSON → attr_base**: 아직 표에 없는 (pk, attr_name)만 옮긴다.
  판정 실패(null 값)는 옮기지 않는다(D35 규칙 — 재판정을 막지 않는다).
- **discovered_for_brand → brand_platforms**: 쉼표 텍스트를 행으로 편다.
- **proxy_defs·proxy_cache → proxy.db**: rowid 순서를 보존해 옮기고,
  sync_state의 proxy_cache 진행점을 새 번호로 번역한다(D64와 같은 규칙 —
  새 키 = 옛 키 이하의 행 수).
- 이관 후 PRAGMA integrity_check + foreign_key_check + ANALYZE.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from schema_v3 import (SCHEMA_V3, VIEWS_V3,  # noqa: E402
                       ensure_category_path, split_category)

OBS_METRICS = ("price_original", "price_sale", "discount_rate", "review_count",
               "rating", "view_count", "view_count_display", "purchase_count",
               "purchase_count_display", "like_count", "like_count_display",
               "viewers_now", "buyers_now", "sold_out", "rank")


def _table_exists(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name=?", (name,)).fetchone())


def _guard_sync_progress(dst, table, src_n, new_n):
    """이관 중 행이 떨어졌으면 그 표의 미러 진행점을 리셋한다 (PR #13 3R Blocker 3).

    관측 계열은 id를 보존해 복사하므로 정상 경로에서는 src_n == new_n이고
    진행점이 그대로 유효하다. 그러나 어떤 이유로든 행이 떨어졌다면(미래의 필터
    추가·소스 손상) 보존된 진행점은 **떨어진 행 수만큼 앞서 나간 거짓말**이 된다
    — D64가 잡은 무소음 미러 누락과 같은 종류다. 그때는 last_synced_key를 NULL로
    리셋해 다음 미러가 전량 재동기화하게 한다(느리지만 정직하다).
    """
    if src_n == new_n:
        return
    if not _table_exists(dst, "sync_state"):
        return
    dst.execute("UPDATE sync_state SET last_synced_key=NULL WHERE table_name=?",
                (table,))
    print("  경고: %s %d행이 이관에서 떨어졌다(%d→%d) — sync_state를 리셋했다. "
          "다음 시트 미러가 전량 재동기화한다" % (table, src_n - new_n, src_n, new_n))


def migrate(src_path, dst_path, proxy_path):
    """v2 → v3 이관. **dst_path·proxy_path는 둘 다 스테이징 경로다** — 라이브
    파일에 직접 짓지 않는다(PR #13 3R Blocker 1). 교체는 main()이 검산 통과
    후에만 한다.
    """
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row
    if not _table_exists(src, "obs_base"):
        raise SystemExit(f"{src_path}: v2 스키마가 아니다 — v1이면 migrate_v2.py를 먼저 돌려라")
    # obs_base는 v2·v3 양쪽에 있다 — v3 표식은 product_categories다(PR #13 3R
    # Blocker 2). 이 가드가 없으면 이미 v3인 정본에 재실행해도 통과해서
    # 스테이징을 다시 짓고, 교체 단계가 멀쩡한 파일들을 갈아엎는다.
    if _table_exists(src, "product_categories"):
        raise SystemExit(f"{src_path}: 이미 v3다 — 이관할 것이 없다")
    for p in (dst_path, proxy_path):
        if os.path.exists(p):
            os.remove(p)          # 스테이징 잔여물만 지운다 — 라이브 경로가 아니다
    dst = sqlite3.connect(dst_path)
    dst.executescript(SCHEMA_V3)
    dst.execute("PRAGMA foreign_keys = ON")
    report = {}

    # ① 사전 — id를 그대로 보존한다 (참조가 번역 없이 성립하게)
    dst.executemany("INSERT INTO sites (site_id, name) VALUES (?,?)",
                    src.execute("SELECT site_id, name FROM sites").fetchall())
    dst.executemany("INSERT INTO hosts (host_id, prefix) VALUES (?,?)",
                    src.execute("SELECT host_id, prefix FROM hosts").fetchall())
    dst.executemany("INSERT INTO brands (brand_id, representative_name) VALUES (?,?)",
                    src.execute("SELECT brand_id, name FROM brands").fetchall())
    # contexts는 CHECK(접두사 4종)를 새로 통과해야 한다 — 위반은 **조용히 못 넘어간다**
    bad_ctx = [r["name"] for r in src.execute("SELECT name FROM contexts")
               if not any(r["name"].startswith(p + ":")
                          for p in ("brand", "market", "ranking", "adhoc"))]
    if bad_ctx:
        raise SystemExit(
            "contexts에 접두사 4종(brand/market/ranking/adhoc) 밖의 값이 있다 — "
            "이관 전에 정리해야 한다: %s" % ", ".join(bad_ctx[:10]))
    dst.executemany("INSERT INTO contexts (context_id, name) VALUES (?,?)",
                    src.execute("SELECT context_id, name FROM contexts").fetchall())

    # ② runs — id = 구 rowid. run_ref 숫자가 그대로 정식 FK 값이 된다
    rcols = [d[1] for d in src.execute("PRAGMA table_info(runs)")]
    rows = src.execute(
        "SELECT rowid AS _rid, %s FROM runs ORDER BY rowid" % ",".join(rcols)).fetchall()
    dst.executemany(
        "INSERT INTO runs (id, %s) VALUES (%s)" % (",".join(rcols),
                                                   ",".join("?" * (len(rcols) + 1))),
        [tuple([r["_rid"]] + [r[c] for c in rcols]) for r in rows])
    report["runs"] = len(rows)
    run_ids = {r["_rid"] for r in rows}

    # ③ 카테고리 — 문자열(경로 포함)을 계층으로. 구 id → 새 리프 id 맵을 만든다
    cat_cache, leaf_of = {}, {}
    for r in src.execute("SELECT category_id, name FROM categories"):
        leaf_of[r["category_id"]] = ensure_category_path(dst, r["name"], cat_cache)
    report["categories"] = dst.execute("SELECT COUNT(*) FROM categories").fetchone()[0]

    # ④ 상품 — pk 보존, 제거 컬럼(attributes·seen_at·raw_extras)은 버리고
    #    category_id는 product_categories로. attributes JSON은 모아 뒀다가
    #    attr_base 본체를 옮긴 **뒤에** OR IGNORE로 넣는다 — 표의 판정이 정본이고,
    #    그래야 "JSON에서 새로 온 것" 건수가 부풀지 않는다
    n_prod = n_map = 0
    json_attrs = []
    for r in src.execute("SELECT * FROM product_base ORDER BY pk"):
        dst.execute(
            "INSERT INTO product_base (pk, site_id, product_id, name, url_host,"
            " url_path, img_host, img_path, brand_id, static_verified_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["pk"], r["site_id"], r["product_id"], r["name"], r["url_host"],
             r["url_path"], r["img_host"], r["img_path"], r["brand_id"],
             r["static_verified_at"]))
        n_prod += 1
        leaf = leaf_of.get(r["category_id"])
        if leaf is not None:
            dst.execute(
                "INSERT OR IGNORE INTO product_categories (pk, category_id, source,"
                " site_id) VALUES (?,?,'platform',?)", (r["pk"], leaf, r["site_id"]))
            n_map += 1
        if r["attributes"]:
            try:
                attrs = json.loads(r["attributes"])
            except (TypeError, ValueError):
                attrs = {}
            for name, value in (attrs or {}).items():
                if value is None:
                    continue          # 판정 실패는 저장하지 않는다 (D35)
                json_attrs.append((r["pk"], name, value,
                                   r["attributes_basis"] or "migrated",
                                   r["static_verified_at"]))
    report["products"] = n_prod
    report["product_categories"] = n_map

    # attr_base 본체 먼저 — 표의 판정이 정본이다. JSON은 빠진 것만 채운다
    acols = [d[1] for d in src.execute("PRAGMA table_info(attr_base)")]
    rows = src.execute("SELECT %s FROM attr_base" % ",".join(acols)).fetchall()
    dst.executemany(
        "INSERT INTO attr_base (%s) VALUES (%s)"
        % (",".join(acols), ",".join("?" * len(acols))),
        [tuple(r[c] for c in acols) for r in rows])
    before = dst.total_changes
    dst.executemany(
        "INSERT OR IGNORE INTO attr_base (pk, attr_name, value, basis, decided_at,"
        " ttl_days) VALUES (?,?,?,?,?,NULL)", json_attrs)
    if dst.total_changes - before:
        report["attrs_from_json"] = dst.total_changes - before
    report["product_attributes"] = dst.execute(
        "SELECT COUNT(*) FROM attr_base").fetchone()[0]

    # ⑤ 관측 — id·pk 보존, run_ref → run_id(끊어진 참조는 NULL — FK가 살아 있다)
    n = 0
    batch = []
    ins = ("INSERT INTO obs_base (id, pk, observed_at, context_id, run_id, %s)"
           " VALUES (%s)" % (",".join(OBS_METRICS), ",".join("?" * (len(OBS_METRICS) + 5))))
    for r in src.execute("SELECT * FROM obs_base ORDER BY id"):
        rr = r["run_ref"] if r["run_ref"] in run_ids else None
        batch.append([r["id"], r["pk"], r["observed_at"], r["context_id"], rr]
                     + [r[c] for c in OBS_METRICS])
        if len(batch) >= 5000:
            dst.executemany(ins, batch)
            n += len(batch)
            batch = []
    if batch:
        dst.executemany(ins, batch)
        n += len(batch)
    report["observations"] = n

    # ⑥ 옵션 — vk·id 보존, seen_at 제거, run_ref → run_id
    n = 0
    for r in src.execute("SELECT * FROM variant_base ORDER BY vk"):
        dst.execute(
            "INSERT INTO variant_base (vk, pk, option_id, option_name, color, size)"
            " VALUES (?,?,?,?,?,?)",
            (r["vk"], r["pk"], r["option_id"], r["option_name"], r["color"], r["size"]))
        n += 1
    report["variants"] = n
    n = 0
    for r in src.execute("SELECT * FROM variant_obs_base ORDER BY id"):
        rr = r["run_ref"] if r["run_ref"] in run_ids else None
        dst.execute(
            "INSERT INTO variant_obs_base (id, vk, observed_at, sold_out, stock_qty,"
            " stock_display, stock_basis, run_id) VALUES (?,?,?,?,?,?,?,?)",
            (r["id"], r["vk"], r["observed_at"], r["sold_out"], r["stock_qty"],
             r["stock_display"], r["stock_basis"], rr))
        n += 1
    report["variant_observations"] = n

    # ⑦ platforms — discovered_for_brand를 brand_platforms로 편다 (D65-5)
    n_bp = 0
    if _table_exists(src, "platforms"):
        for r in src.execute("SELECT * FROM platforms"):
            dst.execute(
                "INSERT INTO platforms (platform_key, name, url, engine, recon,"
                " skill_status, updated_at) VALUES (?,?,?,?,?,?,?)",
                (r["platform_key"], r["name"], r["url"], r["engine"], r["recon"],
                 r["skill_status"], r["updated_at"]))
            dfb = r["discovered_for_brand"] if "discovered_for_brand" in r.keys() else None
            for bname in [b.strip() for b in (dfb or "").split(",") if b.strip()]:
                dst.execute("INSERT OR IGNORE INTO brands (representative_name)"
                            " VALUES (?)", (bname,))
                dst.execute(
                    "INSERT OR IGNORE INTO brand_platforms (brand_id, platform_key,"
                    " discovered_at) SELECT brand_id, ?, ? FROM brands"
                    " WHERE representative_name=?",
                    (r["platform_key"], r["updated_at"], bname))
                n_bp += 1
        report["platforms"] = dst.execute("SELECT COUNT(*) FROM platforms").fetchone()[0]
    if n_bp:
        report["brand_platforms"] = n_bp

    # ⑧ insights·sync_state — 그대로
    for t in ("insights", "sync_state"):
        if not _table_exists(src, t):
            continue
        cols = [d[1] for d in src.execute("PRAGMA table_info(%s)" % t)]
        rows = src.execute("SELECT %s FROM %s" % (",".join(cols), t)).fetchall()
        dst.executemany("INSERT INTO %s (%s) VALUES (%s)"
                        % (t, ",".join(cols), ",".join("?" * len(cols))),
                        [tuple(r[c] for c in cols) for r in rows])
        report[t] = len(rows)

    # 관측 계열 진행점 가드 (3R Blocker 3) — sync_state 복사 **뒤**에 돌아야
    # 리셋이 남는다. 정상 경로(행 보존)에서는 아무것도 안 한다.
    _guard_sync_progress(dst, "observations",
                         src.execute("SELECT COUNT(*) FROM obs_base").fetchone()[0],
                         report.get("observations", 0))
    _guard_sync_progress(dst, "variant_observations",
                         src.execute("SELECT COUNT(*) FROM variant_obs_base").fetchone()[0],
                         report.get("variant_observations", 0))

    # ⑨ 프록시 → proxy.db (D65-8). rowid 순서 보존 — 증분 미러 진행점 번역의 전제
    px = sqlite3.connect(proxy_path)
    # D69: proxy 테이블은 SCHEMA_V3에 포함 — 별도 스키마 불필요
    pass  # proxy.db 생성 로직은 D69에서 폐기
    px.execute("PRAGMA foreign_keys = ON")
    if _table_exists(src, "proxy_defs"):
        dcols = [d[1] for d in src.execute("PRAGMA table_info(proxy_defs)")]
        rows = src.execute("SELECT %s FROM proxy_defs" % ",".join(dcols)).fetchall()
        px.executemany("INSERT INTO proxy_defs (%s) VALUES (%s)"
                       % (",".join(dcols), ",".join("?" * len(dcols))),
                       [tuple(r[c] for c in dcols) for r in rows])
        report["proxy_defs"] = len(rows)
        known = {r["proxy_name"] for r in rows}
        n = orphan = 0
        if _table_exists(src, "proxy_cache"):
            batch = []
            for r in src.execute("SELECT * FROM proxy_cache ORDER BY rowid"):
                if r["proxy_name"] not in known:
                    orphan += 1        # 정의 없는 판정 — FK가 못 받는다. 세어서 보고
                    continue
                batch.append((r["proxy_name"], r["site"], r["product_id"],
                              r["fingerprint"], r["value"], r["basis"], r["judged_at"]))
                if len(batch) >= 5000:
                    px.executemany("INSERT INTO proxy_cache VALUES (?,?,?,?,?,?,?)", batch)
                    n += len(batch)
                    batch = []
            if batch:
                px.executemany("INSERT INTO proxy_cache VALUES (?,?,?,?,?,?,?)", batch)
                n += len(batch)
        report["proxy_cache"] = n
        if orphan:
            report["proxy_cache_orphan"] = orphan
        # sync_state의 proxy_cache 진행점 번역 (D64 규칙 — 새 키 = 옛 키 이하의 행 수)
        if _table_exists(src, "sync_state"):
            row = dst.execute("SELECT last_synced_key FROM sync_state "
                              "WHERE table_name='proxy_cache'").fetchone()
            if row and row[0] and str(row[0]).isdigit():
                new_key = src.execute(
                    "SELECT COUNT(*) FROM proxy_cache WHERE rowid <= ? AND proxy_name IN "
                    "(SELECT proxy_name FROM proxy_defs)", (int(row[0]),)).fetchone()[0]
                if new_key != int(row[0]):
                    dst.execute("UPDATE sync_state SET last_synced_key=? "
                                "WHERE table_name='proxy_cache'", (str(new_key),))
                    print("  sync_state.proxy_cache: 진행점 %s → %s (rowid 재번호 번역)"
                          % (row[0], new_key))
    px.commit()
    px.execute("ANALYZE")
    px.close()

    dst.executescript(VIEWS_V3)
    dst.commit()
    # 무결성 — 깨진 채 교체되면 안 된다 (D65-10)
    ic = dst.execute("PRAGMA integrity_check").fetchone()[0]
    fk = dst.execute("PRAGMA foreign_key_check").fetchall()
    if ic != "ok" or fk:
        raise SystemExit("무결성 검사 실패: integrity=%s, fk 위반 %d건 — 교체하지 마라"
                         % (ic, len(fk)))
    dst.execute("ANALYZE")
    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    src.close()
    return report


# ── 검산 ────────────────────────────────────────────────────────────────────
CHECK_TABLES = ["products", "observations", "variants", "variant_observations",
                "product_attributes", "runs"]


def _canon_cat(v):
    """카테고리 비교 정규화 — v2는 원문 문자열, v3는 계층을 도로 편 것이라
    구분자 공백만 다를 수 있다(`a>b` 대 `a > b`). 조각으로 비교한다."""
    return tuple(split_category(v)) if v else v


def verify(src_path, dst_path, proxy_path, sample=400):
    """행 수 + 값 대조. 계층으로 접은 카테고리가 그대로 도로 펴지는지가 핵심이다."""
    src = sqlite3.connect(src_path); src.row_factory = sqlite3.Row
    dst = sqlite3.connect(dst_path); dst.row_factory = sqlite3.Row
    ok = True
    for t in CHECK_TABLES:
        a = src.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        b = dst.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        # attr_base는 JSON 소급 이관으로 **늘 수만 있다** — 그건 불일치가 아니다
        mark = "OK" if (a == b or (t == "product_attributes" and b >= a)) else "!! 불일치"
        if mark != "OK":
            ok = False
        print("  %-22s v2 %8s → v3 %8s  %s"
              % (t, "{:,}".format(a), "{:,}".format(b), mark))

    shared = ["site", "product_id", "name", "url", "image_url", "brand",
              "category", "static_verified_at"]
    rows_a = src.execute("SELECT %s FROM products ORDER BY site, product_id LIMIT %d"
                         % (",".join(shared), sample)).fetchall()
    rows_b = dst.execute("SELECT %s FROM products ORDER BY site, product_id LIMIT %d"
                         % (",".join(shared), sample)).fetchall()
    diffs = []
    for ra, rb in zip(rows_a, rows_b):
        for c in shared:
            va, vb = ra[c], rb[c]
            if c == "category":
                va, vb = _canon_cat(va), _canon_cat(vb)
            if (va or "") != (vb or ""):
                diffs.append((c, ra[c], rb[c]))
    if diffs:
        ok = False
        print("  %-22s 값 불일치 %d건 — 예: %s" % ("products", len(diffs), diffs[:3]))
    else:
        print("  %-22s 표본 %d행 전 컬럼 값 일치" % ("products", len(rows_a)))

    ocols = ["site", "product_id", "observed_at", "context", "price_original",
             "price_sale", "discount_rate", "like_count", "rank", "run_id"]
    q = ("SELECT %s FROM observations ORDER BY site, product_id, observed_at, context"
         " LIMIT %d" % (",".join(ocols), sample))
    diffs = [(c, ra[c], rb[c]) for ra, rb in zip(src.execute(q), dst.execute(q))
             for c in ocols if (ra[c] or "") != (rb[c] or "")]
    if diffs:
        ok = False
        print("  %-22s 값 불일치 %d건 — 예: %s" % ("observations", len(diffs), diffs[:3]))
    else:
        print("  %-22s 표본 전 컬럼 값 일치 (run_id 연결 포함)" % "observations")

    if _table_exists(src, "proxy_cache") and os.path.exists(proxy_path):
        px = sqlite3.connect(proxy_path)
        a = src.execute("SELECT COUNT(*) FROM proxy_cache WHERE proxy_name IN "
                        "(SELECT proxy_name FROM proxy_defs)").fetchone()[0]
        b = px.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0]
        print("  %-22s v2 %8s → proxy.db %8s  %s"
              % ("proxy_cache", "{:,}".format(a), "{:,}".format(b),
                 "OK" if a == b else "!! 불일치"))
        if a != b:
            ok = False
        px.close()
    src.close(); dst.close()
    return ok


def main():
    ap = argparse.ArgumentParser(description="v2 → v3 이관 (D65)")
    ap.add_argument("--src", default="data/intel.db")
    ap.add_argument("--dst", default=None, help="기본 <src 폴더>/intel-v3.db")
    ap.add_argument("--proxy", default=None, help="기본 <src 폴더>/proxy.db")
    ap.add_argument("--no-swap", action="store_true",
                    help="검산이 통과해도 교체하지 않고 새 파일만 남긴다")
    ap.add_argument("--verify-only", action="store_true")
    a = ap.parse_args()
    dst = a.dst or os.path.join(os.path.dirname(a.src) or ".", "intel-v3.db")
    # 프록시도 메인과 같은 패턴이다 (3R Blocker 1): **스테이징에 짓고** 검산
    # 통과 시에만 교체한다. 라이브 proxy.db에 직접 쓰면 중간 실패가 원본을 지운다.
    # D69: proxy.db 분리 폐기 — 기존 proxy.db가 있으면 migrate_proxy_merge.py로 이관
    proxy_final = a.proxy or str(Path(a.src).parent / "proxy.db")
    proxy_staging = proxy_final + ".staging"

    if not a.verify_only:
        rep = migrate(a.src, dst, proxy_staging)
        print("── 이관 ──")
        for k, v in rep.items():
            print("  %-22s %s" % (k, "{:,}".format(v)))
        if rep.get("proxy_cache_orphan"):
            print("  ※ 정의 없는 판정 %d건은 옮기지 않았다 (proxy_defs에 카드가 없다)"
                  % rep["proxy_cache_orphan"])
    print("── 검산 ──")
    # --verify-only는 교체 후 재검산 용도다 — 스테이징이 남아 있으면 그걸,
    # 없으면(이미 교체됨) 최종 경로를 본다
    proxy_check = proxy_staging if os.path.exists(proxy_staging) else proxy_final
    ok = verify(a.src, dst, proxy_check)
    for p, label in ((a.src, "v2"), (dst, "v3"), (proxy_check, "px")):
        if os.path.exists(p):
            print("  %s %8.2f MB" % (label, os.path.getsize(p) / 1048576))
    if not ok:
        print("검산 실패 — 교체하지 않는다 (스테이징: %s, %s)" % (dst, proxy_staging))
        return 1
    if a.verify_only or a.no_swap:
        print("검산 통과 (교체 안 함 — 스테이징: %s, %s)" % (dst, proxy_staging))
        return 0
    backup = a.src + ".v2-backup"
    shutil.copy2(a.src, backup)                     # 자동 백업 (D65-10)
    os.replace(dst, a.src)
    if os.path.exists(proxy_final):                 # 재실행 등으로 이미 있으면 보존
        shutil.copy2(proxy_final, proxy_final + ".pre-v3-backup")
    os.replace(proxy_staging, proxy_final)
    print("검산 통과 — 백업 %s, 교체 완료: %s (+ %s)" % (backup, a.src, proxy_final))
    return 0


if __name__ == "__main__":
    sys.exit(main())
