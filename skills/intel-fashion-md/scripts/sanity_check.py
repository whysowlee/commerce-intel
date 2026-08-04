#!/usr/bin/env python3
"""리포트 수치 확정 검증 (sanity check). 2026-08-04 피드백 §6 반영.

결정적으로 동작한다: AI·난수·네트워크·시각 의존 없음.
같은 DB + 같은 claims 파일 → 언제, 어느 세션에서 실행해도 같은 결과.
clean context 세션에서의 재현 검증도 이 스크립트 재실행으로 한다.

사용:
  python3 sanity_check.py --db data/intel.db --claims output/claims-xxx.json
  python3 sanity_check.py --db data/intel.db --profile   # claim 없이 DB 개요만

exit code (하우스 규약):
  0 PASS — 전 claim 일치
  1 WARN — 일치하지만 경고 있음 (n < min_n 등)
  2 FAIL — 불일치 또는 claim 해석 불가 (리포트 생성 금지)

규칙:
- null ≠ 0. 집계는 non-null만 쓰고 n(사용 행)과 n_null(제외 행)을 항상 병기.
- 구간표기(view_count_display 등)는 검증 대상 아님 — 정수 칸만 다룬다.
- 허용 오차 기본 0 (반올림 명시 claim만 tolerance 사용).
"""

import argparse
import json
import sqlite3
import statistics as st
import sys

MIN_N_DEFAULT = 30  # 이보다 작으면 WARN (통과는 시킴)
ALLOWED_TABLES = ("observations", "products", "variant_observations", "variants")
METRICS = ("count_rows", "n", "n_null", "median", "mean", "sum", "min", "max",
           "distinct_count", "share", "ratio", "group_median")


def fail(msg):
    print("[FAIL] " + msg)


def open_ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def table_columns(con, table):
    return [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]


def build_rows(con, claim):
    """claim의 필터를 적용한 행(dict 리스트)을 결정적 순서로 반환."""
    table = claim.get("table", "observations")
    if table not in ALLOWED_TABLES:
        raise ValueError("허용되지 않은 table: %s" % table)
    cols = table_columns(con, table)
    if not cols:
        raise ValueError("테이블 없음: %s" % table)

    where, params = [], []
    filters = claim.get("filters", {}) or {}
    attr_filters = {}
    for key, cond in sorted(filters.items()):
        if key.startswith("attr:"):
            attr_filters[key[5:]] = cond
            continue
        if key == "context_prefix":
            where.append("context LIKE ? ESCAPE '\\'")
            params.append(str(cond).replace("%", r"\%").replace("_", r"\_") + "%")
            continue
        if key not in cols:
            raise ValueError("컬럼 없음: %s.%s" % (table, key))
        if isinstance(cond, dict):
            op = cond.get("op")
            if op not in (">", ">=", "<", "<=", "=", "!="):
                raise ValueError("허용되지 않은 op: %r" % op)
            where.append("%s %s ?" % (key, op))
            params.append(cond.get("value"))
        else:
            where.append("%s = ?" % key)
            params.append(cond)
    if claim.get("observed_from"):
        if "observed_at" not in cols:
            raise ValueError("observed_at 없음: %s" % table)
        where.append("observed_at >= ?")
        params.append(claim["observed_from"])
    if claim.get("observed_to"):
        where.append("observed_at <= ?")
        params.append(claim["observed_to"])

    sql = "SELECT * FROM %s" % table
    if where:
        sql += " WHERE " + " AND ".join(where)
    order_cols = [c for c in ("site", "product_id", "option_id", "context",
                              "observed_at", "attr_name") if c in cols]
    if order_cols:
        sql += " ORDER BY " + ", ".join(order_cols)
    cur = con.execute(sql, params)
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchall()]

    # attr:<속성명> 필터 — product_attributes 조인 (등호만)
    for attr_name, want in sorted(attr_filters.items()):
        amap = {}
        for site, pid, val in con.execute(
                "SELECT site, product_id, value FROM product_attributes "
                "WHERE attr_name = ?", (attr_name,)):
            amap[(site, pid)] = val
        rows = [r for r in rows
                if amap.get((r.get("site"), r.get("product_id"))) == want]

    # latest_only — (site, product_id[, context])별 최신 관측 1건 (시점 혼합 방지)
    if claim.get("latest_only") and "observed_at" in cols:
        keycols = [c for c in ("site", "product_id", "option_id", "context")
                   if c in cols]
        best = {}
        for r in rows:
            k = tuple(r.get(c) for c in keycols)
            if k not in best or (r["observed_at"] or "") > (best[k]["observed_at"] or ""):
                best[k] = r
        rows = [best[k] for k in sorted(best.keys(), key=lambda t: tuple(str(x) for x in t))]
    return rows


def compute(rows, claim):
    """지표 재계산. (값, n_사용행, n_null, 경고리스트) 반환. null은 항상 제외."""
    metric = claim["metric"]
    field = claim.get("field")
    warns = []

    if metric == "count_rows":
        return len(rows), len(rows), 0, warns

    if metric in ("n", "n_null", "median", "mean", "sum", "min", "max",
                  "distinct_count", "share"):
        if not field:
            raise ValueError("metric %s 에는 field 필요" % metric)
        if rows and field not in rows[0]:
            raise ValueError("field 없음: %s" % field)
        vals = [r[field] for r in rows if r.get(field) is not None]
        n_null = len(rows) - len(vals)
        if metric == "n":
            return len(vals), len(vals), n_null, warns
        if metric == "n_null":
            return n_null, len(vals), n_null, warns
        if metric == "distinct_count":
            return len(set(vals)), len(vals), n_null, warns
        if metric == "share":
            target = claim.get("value")
            if not vals:
                raise ValueError("share: non-null 값 0건")
            return round(sum(1 for v in vals if v == target) / len(vals), 6), \
                len(vals), n_null, warns
        if not vals:
            raise ValueError("%s: non-null 값 0건" % metric)
        nums = [float(v) for v in vals]
        out = {"median": st.median, "mean": st.mean, "sum": sum,
               "min": min, "max": max}[metric](nums)
        return round(out, 6), len(vals), n_null, warns

    if metric == "ratio":
        num_f, den_f = claim.get("numerator"), claim.get("denominator")
        if not num_f or not den_f:
            raise ValueError("ratio 에는 numerator/denominator 필요")
        pairs = [(float(r[num_f]), float(r[den_f])) for r in rows
                 if r.get(num_f) is not None and r.get(den_f) is not None]
        n_null = len(rows) - len(pairs)
        den_sum = sum(p[1] for p in pairs)
        if not pairs or den_sum == 0:
            raise ValueError("ratio: 유효 쌍 0건 또는 분모 합 0")
        return round(sum(p[0] for p in pairs) / den_sum, 6), len(pairs), n_null, warns

    if metric == "group_median":
        field = claim.get("field")
        gb = claim.get("group_by")
        if not field or not gb:
            raise ValueError("group_median 에는 field/group_by 필요")
        groups = {}
        n_null = 0
        for r in rows:
            g, v = r.get(gb), r.get(field)
            if g is None or v is None:
                n_null += 1
                continue
            groups.setdefault(str(g), []).append(float(v))
        out = {g: round(st.median(vs), 6) for g, vs in sorted(groups.items())}
        n_used = sum(len(vs) for vs in groups.values())
        for g in sorted(groups):
            if len(groups[g]) < claim.get("min_n", MIN_N_DEFAULT):
                warns.append("그룹 '%s' n=%d < min_n" % (g, len(groups[g])))
        return out, n_used, n_null, warns

    raise ValueError("허용되지 않은 metric: %s (허용: %s)" % (metric, ", ".join(METRICS)))


def check_claim(con, claim):
    """claim 1건 검증. dict 결과 반환 (status: PASS/WARN/FAIL)."""
    cid = claim.get("id", "?")
    res = {"id": cid, "desc": claim.get("desc", "")}
    try:
        rows = build_rows(con, claim)
        got, n_used, n_null, warns = compute(rows, claim)
    except Exception as e:  # claim 해석 불가 = FAIL (조용히 스킵 금지)
        res.update(status="FAIL", reason="해석 불가: %s" % e)
        return res

    expected = claim.get("expected")
    tol = float(claim.get("tolerance", 0))
    if isinstance(expected, dict) and isinstance(got, dict):
        bad = {}
        for k in sorted(set(expected) | set(got)):
            e_v, g_v = expected.get(k), got.get(k)
            if e_v is None or g_v is None or abs(float(e_v) - float(g_v)) > tol:
                bad[k] = {"expected": e_v, "got": g_v}
        ok = not bad
        res["mismatch"] = bad
    elif isinstance(expected, (int, float)) and isinstance(got, (int, float)):
        ok = abs(float(expected) - float(got)) <= tol
    else:
        ok = expected == got

    if n_used < claim.get("min_n", MIN_N_DEFAULT):
        warns = warns + ["n=%d < min_n=%d" % (n_used, claim.get("min_n", MIN_N_DEFAULT))]
    res.update(expected=expected, got=got, n=n_used, n_null=n_null,
               status=("PASS" if ok and not warns else "WARN" if ok else "FAIL"),
               warnings=warns)
    return res


def profile(con):
    """claim 없이 DB 개요를 결정적으로 출력 (눈대중 sanity용)."""
    print("== DB 프로파일 (결정적) ==")
    tabs = sorted(r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"))
    for t in tabs:
        cnt = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        print("%-22s rows=%d" % (t, cnt))
    if "observations" in tabs:
        print("-- observations: site별 상품 수 / price_sale 중앙값 (null 제외)")
        for site, n_prod in con.execute(
                "SELECT site, COUNT(DISTINCT product_id) FROM observations "
                "GROUP BY site ORDER BY site"):
            vals = [r[0] for r in con.execute(
                "SELECT price_sale FROM observations WHERE site=? "
                "AND price_sale IS NOT NULL ORDER BY rowid", (site,))]
            med_s = ("%.1f" % st.median([float(v) for v in vals])) if vals else "값 없음"
            print("  %-20s products=%d price_sale_median=%s (n=%d)"
                  % (site, n_prod, med_s, len(vals)))


def main():
    ap = argparse.ArgumentParser(description="리포트 수치 확정 검증 (sanity check)")
    ap.add_argument("--db", required=True, help="data/intel.db 경로")
    ap.add_argument("--claims", help="claims JSON 경로")
    ap.add_argument("--profile", action="store_true", help="DB 개요만 출력")
    ap.add_argument("--json", action="store_true", help="결과를 JSON으로도 출력")
    args = ap.parse_args()

    con = open_ro(args.db)
    if args.profile and not args.claims:
        profile(con)
        return 0
    if not args.claims:
        ap.error("--claims 또는 --profile 필요")

    with open(args.claims, encoding="utf-8") as f:
        doc = json.load(f)
    claims = doc.get("claims", doc if isinstance(doc, list) else [])
    if not claims:
        fail("claims 0건 — 빈 검증으로 PASS 처리하지 않음")
        return 2

    results = [check_claim(con, c) for c in claims]
    n_pass = sum(1 for r in results if r["status"] == "PASS")
    n_warn = sum(1 for r in results if r["status"] == "WARN")
    n_fail = sum(1 for r in results if r["status"] == "FAIL")

    print("== sanity check: %d claims → PASS %d / WARN %d / FAIL %d =="
          % (len(results), n_pass, n_warn, n_fail))
    for r in results:
        line = "[%s] %s %s | expected=%r got=%r n=%s n_null=%s" % (
            r["status"], r["id"], r.get("desc", ""), r.get("expected"),
            r.get("got"), r.get("n", "-"), r.get("n_null", "-"))
        if r.get("warnings"):
            line += " | warn: " + "; ".join(r["warnings"])
        if r.get("reason"):
            line += " | " + r["reason"]
        if r.get("mismatch"):
            line += " | mismatch=%s" % json.dumps(r["mismatch"], ensure_ascii=False,
                                                  sort_keys=True)
        print(line)

    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False, sort_keys=True))
    return 2 if n_fail else 1 if n_warn else 0


if __name__ == "__main__":
    sys.exit(main())
