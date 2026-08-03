#!/usr/bin/env python3
"""분석 대시보드 생성기 — 가설 도출 도구. references/analysis-report.md 스펙 구현.

    python3 build_analysis_report.py --db data/intel.db --out output/analysis-<대상>-<ts>.html
    python3 build_analysis_report.py --db data/intel.db --context "market:데님팬츠(남성)" --out ...
    python3 build_analysis_report.py --emit-template --out assets/analysis-template.html

정본 DB에서 상품별 최신 관측을 뽑아 단일 HTML 대시보드를 만든다.
- 변인통제 패널(플랫폼·카테고리·속성·브랜드·가격대·품절) → filteredData 하나 →
  KPI·산점도·분포·표가 전부 거기서 다시 그려진다 (단일 파이프라인)
- 축 선택형 산점도(로그 토글·호버·클릭), 분포 히스토그램, 가격 변경 사건 표(관측 증분)
- 정직성 규칙: 상관≠인과 문구, null 제외 건수, n 표시, 심슨·생존편향·다중비교 경고
- 외부 리소스 0. 구간 표기 값은 축으로 쓰지 않는다(view_count는 아예 싣지 않는다).
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import build_report as br     # 상품 매칭 규칙(match_key)을 한 곳에서 쓴다
import report_ui as ui        # 색 토큰·컴포넌트 CSS·공통 조작 JS — 스토리 리포트와 같은 것

# 축 후보 — 구간 표기(view_count)는 제외한다 (오차 무한)
AXES = [
    ("price_sale", "판매가"), ("price_original", "정가"), ("discount_rate", "할인율(%)"),
    ("like_count", "하트"), ("review_count", "후기 수"), ("rating", "평점"),
    ("purchase_count", "누적판매*"), ("viewers_now", "보는 중(랭킹)"),
    # 옵션(사이즈) — 값이 있는 문맥에서만 축 목록에 뜬다
    ("opt_total", "옵션 수"), ("opt_out_rate", "옵션 품절률(%)"), ("stock_sum", "재고 수량 합"),
    ("sold_min", "최소 판매량*"),
]

# ── 모듈 ────────────────────────────────────────────────────────────────────
# 리포트는 종류가 아니라 **모듈의 조합**이다. 필요한 것만 골라 한 리포트로 만든다.
# `need`는 그 모듈이 성립할 데이터가 있는지 보는 함수다 — 없으면 넣지 않고, 뺐다는 사실을
# 리포트에 적는다(조용히 빠지면 독자는 그 분석을 안 한 게 아니라 못 한 걸 모른다).
MODULES = [
    ("kpi", "요약 KPI", lambda d: True),
    ("scatter", "산점도 (축 선택)", lambda d: bool(numeric_axes(d))),
    ("dist", "분포 (히스토그램)", lambda d: bool(numeric_axes(d))),
    ("group", "그룹 비교 (박스 플롯)", lambda d: bool(numeric_axes(d))),
    ("variants", "사이즈별 재고", lambda d: bool(d["meta"].get("stock"))),
    ("timeseries", "시계열 추이", lambda d: bool(d.get("time_series"))),
    ("events", "가격 변경 사건", lambda d: bool(d.get("price_events"))),
    ("linesheet", "라인시트 (플랫폼 합집합)", lambda d: bool(d.get("union"))),
    ("table", "상품 표", lambda d: True),
]
MODULE_IDS = [m[0] for m in MODULES]


def numeric_axes(data):
    """값이 하나라도 있는 수치 축. 전부 null인 축으로는 차트를 만들지 않는다."""
    fields = [f for f, _ in AXES] + [
        "px_" + p["name"] for p in data["meta"].get("proxies", []) if p.get("numeric")]
    return [f for f in fields if any(i.get(f) is not None for i in data["items"])]


def collect(db_path, contexts):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ctx_where, params = "", []
    if contexts:
        ctx_where = " AND o.context IN (%s)" % ",".join("?" * len(contexts))
        params = list(contexts)
    # 상품별 최신 관측 + 정적 속성
    rows = conn.execute(f"""
        SELECT p.site, p.product_id, p.name, p.url, p.image_url, p.brand, p.category,
               p.attributes, o.observed_at, o.context,
               o.price_original, o.price_sale, o.discount_rate, o.review_count, o.rating,
               o.purchase_count, o.like_count, o.viewers_now, o.sold_out, o.rank
        FROM products p
        JOIN observations o ON o.site = p.site AND o.product_id = p.product_id
        WHERE o.observed_at = (
            SELECT MAX(o2.observed_at) FROM observations o2
            WHERE o2.site = o.site AND o2.product_id = o.product_id{ctx_where.replace('o.context','o2.context')}
        ){ctx_where}
    """, params + params).fetchall()
    items, seen = [], set()
    for r in rows:
        key = (r["site"], r["product_id"])
        if key in seen:
            continue
        seen.add(key)
        d = dict(r)
        attrs = json.loads(d.pop("attributes") or "{}")
        d["fit"] = attrs.get("핏")
        items.append(d)

    # 가격 변경 사건 — 상품별 마지막 관측 상태 대비(기존 diff 규칙과 동일 철학)
    events = []
    obs = conn.execute(f"""
        SELECT o.site, o.product_id, o.observed_at, o.price_sale, o.like_count, o.review_count
        FROM observations o WHERE o.price_sale IS NOT NULL{ctx_where}
        ORDER BY o.site, o.product_id, o.observed_at
    """, params).fetchall()
    prev = {}
    names = {(i["site"], i["product_id"]): i["name"] for i in items}
    for r in obs:
        key = (r["site"], r["product_id"])
        p = prev.get(key)
        if p and p["price_sale"] != r["price_sale"]:
            events.append({
                "site": r["site"], "product_id": r["product_id"],
                "name": names.get(key), "from_at": p["observed_at"], "to_at": r["observed_at"],
                "price_from": p["price_sale"], "price_to": r["price_sale"],
                "like_delta": (r["like_count"] - p["like_count"])
                              if r["like_count"] is not None and p["like_count"] is not None else None,
                "review_delta": (r["review_count"] - p["review_count"])
                                if r["review_count"] is not None and p["review_count"] is not None else None,
            })
        prev[key] = dict(r)

    # 파생 프록시 주입 (D19) — 재료 지문이 현재 값과 맞는 캐시만 유효하다
    proxies = []
    try:
        defs = conn.execute("SELECT * FROM proxy_defs").fetchall()
    except sqlite3.OperationalError:
        defs = []
    for d in defs:
        pn, mat = d["proxy_name"], d["material"]
        cache = {(r["site"], r["product_id"]): (r["fingerprint"], r["value"])
                 for r in conn.execute(
                     "SELECT site, product_id, fingerprint, value FROM proxy_cache "
                     "WHERE proxy_name=? ORDER BY judged_at", (pn,))}
        fp_field = {"image": "image_url", "name": "name"}.get(mat)
        judged = 0
        for it in items:
            hit = cache.get((it["site"], it["product_id"]))
            fp_now = it.get(fp_field) if fp_field else None
            valid = hit and (fp_field is None or hit[0] == fp_now)
            it["px_" + pn] = hit[1] if valid else None
            judged += 1 if valid else 0
        try:
            space = json.loads(d["value_space"]) if d["value_space"] else None
        except (TypeError, ValueError):
            space = d["value_space"]
        proxies.append({"name": pn, "question": d["question"], "method": d["method"],
                        "numeric": space == "numeric",
                        "judged": judged, "unjudged": len(items) - judged})

    # 시계열 — 축적 관측이 있는 상품의 시점별 지표. 시점이 2개 이상인 상품만.
    ts_rows = conn.execute(f"""
        SELECT site, product_id, observed_at, rank, price_sale, like_count, review_count
        FROM observations o WHERE 1=1{ctx_where} ORDER BY observed_at
    """, params).fetchall()
    stamps_set, per = set(), {}
    for r in ts_rows:
        key = (r["site"], r["product_id"])
        stamps_set.add(r["observed_at"])
        per.setdefault(key, {})[r["observed_at"]] = r
    stamps = sorted(stamps_set)
    # 시점 48개 초과면 균등 구간 대표 시점만 그린다(평균 금지 — 기존 규칙, 첫·끝 포함)
    MAXP = 48
    if len(stamps) > MAXP:
        idx = [round(i * (len(stamps) - 1) / (MAXP - 1)) for i in range(MAXP)]
        stamps = [stamps[i] for i in sorted(set(idx))]
    series = []
    for key, byt in per.items():
        pts = [byt.get(s) for s in stamps]
        if sum(1 for p in pts if p is not None) < 2:
            continue   # 시계열이 성립하려면 2시점 이상
        it0 = next((i for i in items if (i["site"], i["product_id"]) == key), None)
        series.append({
            "site": key[0], "product_id": key[1],
            "name": (it0 or {}).get("name") or key[1],
            "rank": [p["rank"] if p else None for p in pts],
            "price": [p["price_sale"] if p else None for p in pts],
            "like": [p["like_count"] if p else None for p in pts],
            "review": [p["review_count"] if p else None for p in pts],
        })
    # 페이로드 상한: 관측 시점 많은 순 120계열 (초과분은 리포트에 건수 명시).
    # 순위만 세면 랭킹이 아닌 문맥(전수조사 축적 등)에서는 전부 0이라 아무 순서로나 잘린다 —
    # 네 지표의 관측 수를 합쳐 센다.
    series.sort(key=lambda s: -sum(
        1 for k in ("rank", "price", "like", "review") for v in s[k] if v is not None))
    ts_capped = len(series)
    series = series[:120]
    time_series = {"stamps": stamps, "series": series, "total_series": ts_capped,
                   "shown": len(series)} if series else None

    stock = attach_variants(conn, items, ctx_where, params)
    union = build_union_rows(items)

    times = [i["observed_at"] for i in items if i["observed_at"]]
    meta = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "obs_min": min(times) if times else None, "obs_max": max(times) if times else None,
        "contexts": sorted({i["context"] for i in items}),
        "sites": sorted({i["site"] for i in items}),
        "proxies": proxies,
        "stock": stock,
    }
    return {"meta": meta, "items": items, "price_events": events,
            "time_series": time_series, "union": union}


def attach_variants(conn, items, ctx_where, params):
    """옵션(사이즈)별 재고를 상품에 붙인다.

    `variants`(정적 구성) + `variant_observations`(시변 재고)에서 **옵션마다 마지막 관측**을
    가져온다. 수집이 L1(사이트가 이미 준 품절 상태)까지만 돌면 `stock_qty`는 전부 null이고
    **품절 여부만** 있는 것이다 — 그때 재고 '수량'을 만들어내지 않는다. SPEC-INTEL §6-4의
    옵션별 판매수량(재고 감소분)도 수량 축적이 있어야 성립하므로 여기서 계산하지 않는다.
    """
    try:
        rows = conn.execute(f"""
            SELECT v.site, v.product_id, v.option_id, v.size, v.option_name, v.color,
                   o.sold_out, o.stock_qty, o.stock_display, o.stock_basis, o.observed_at
            FROM variants v
            JOIN variant_observations o
              ON o.site = v.site AND o.product_id = v.product_id AND o.option_id = v.option_id
            WHERE o.observed_at = (
                SELECT MAX(o2.observed_at) FROM variant_observations o2
                WHERE o2.site = v.site AND o2.product_id = v.product_id
                  AND o2.option_id = v.option_id)
        """).fetchall()
    except sqlite3.OperationalError:
        return None
    by_product = {}
    for r in rows:
        by_product.setdefault((r["site"], r["product_id"]), []).append(r)

    has_any, qty_seen, bases = False, False, set()
    for it in items:
        vs = by_product.get((it["site"], it["product_id"]))
        if not vs:
            continue
        has_any = True
        out = sum(1 for v in vs if v["sold_out"])
        qty = [v["stock_qty"] for v in vs if v["stock_qty"] is not None]
        qty_seen = qty_seen or bool(qty)
        bases.update(v["stock_basis"] for v in vs if v["stock_basis"])
        it["opt_total"] = len(vs)
        it["opt_out"] = out
        it["opt_live"] = len(vs) - out
        # 품절률은 옵션 **개수** 비율이다 — 수량 비율이 아니다(수량은 대개 없다)
        it["opt_out_rate"] = round(out / len(vs) * 100, 1)
        if qty:
            it["stock_sum"] = sum(qty)
        # 사이즈별 집계용 — 이름은 size가 없으면 option_name을 그대로 쓴다(축을 지어내지 않는다)
        it["sizes"] = [[v["size"] or v["option_name"] or "?", 1 if v["sold_out"] else 0,
                        v["stock_qty"]] for v in vs]
    if not has_any:
        return None
    sold = min_sold(conn, items)
    return {"products": sum(1 for i in items if i.get("opt_total")),
            "options": sum(i.get("opt_total") or 0 for i in items),
            "has_qty": qty_seen, "basis": sorted(bases), "sold": sold}


def min_sold(conn, items):
    """옵션별 **최소 판매량** — 관측된 재고의 감소분이다 (SPEC-INTEL §6-4).

    관측 두 개의 차이이므로 추정이 아니다. 다만 한계가 둘 있고 리포트가 그대로 밝힌다:
    ① 재입고가 섞인 구간은 **과소 집계**된다(재입고 전에 팔린 수량이 가려진다)
    ② 같은 옵션을 두 번 이상 수량까지 관측해야 성립한다 — 축적 전에는 값이 없다.
    "판매량"이라고만 쓰면 실제보다 작은 수를 진짜 판매량으로 읽는다.
    """
    rows = conn.execute("""
        SELECT site, product_id, option_id, observed_at, stock_qty
        FROM variant_observations WHERE stock_qty IS NOT NULL
        ORDER BY site, product_id, option_id, observed_at
    """).fetchall()
    per, prev = {}, {}
    restock, windows = 0, []
    for r in rows:
        key = (r["site"], r["product_id"], r["option_id"])
        p = prev.get(key)
        if p is not None:
            diff = p[1] - r["stock_qty"]
            if diff > 0:
                per[key] = per.get(key, 0) + diff
            elif diff < 0:
                restock += 1
            windows.append((p[0], r["observed_at"]))
        prev[key] = (r["observed_at"], r["stock_qty"])
    if not per:
        return None
    by_product = {}
    for (site, pid, _opt), qty in per.items():
        by_product[(site, pid)] = by_product.get((site, pid), 0) + qty
    for it in items:
        v = by_product.get((it["site"], it["product_id"]))
        if v:
            it["sold_min"] = v
    return {"options": len(per), "total": sum(per.values()), "restock": restock,
            "from_at": min(w[0] for w in windows), "to_at": max(w[1] for w in windows)}


def build_union_rows(items):
    """플랫폼 합집합 — 같은 상품을 한 행으로 묶는다.

    매칭 규칙은 스토리 리포트와 **같은 것 하나**다(정규화 상품명 완전일치, build_report.match_key).
    두 곳에 규칙을 각각 적으면 같은 데이터에서 다른 합집합이 나온다.
    행에는 상품 인덱스만 담는다 — 상품 값을 복사하면 페이로드가 두 배가 된다.
    """
    sites = {i["site"] for i in items}
    if len(sites) < 2:
        return None
    rows, order = {}, []
    for idx, it in enumerate(items):
        key = br.match_key(it.get("name")) or "\x00%s\x00%s" % (it["site"], it["product_id"])
        row = rows.get(key)
        if row is None:
            row = {"n": it.get("name") or it["product_id"], "c": it.get("category"),
                   "b": it.get("brand"), "i": []}
            rows[key] = row
            order.append(key)
        row["i"].append(idx)
    return [rows[k] for k in order]


SAMPLE = {  # --emit-template 용 — 실데이터가 아님을 화면에 명시한다
    "meta": {"generated_at": "TEMPLATE", "obs_min": None, "obs_max": None,
             "contexts": ["template:샘플"], "sites": ["musinsa", "29cm"], "proxies": []},
    "items": [
        {"site": "musinsa", "product_id": str(i), "name": f"샘플 상품 {i}", "url": "#",
         "image_url": None, "brand": ["브랜드A", "브랜드B"][i % 2], "category": "데님팬츠",
         "fit": ["와이드", "슬림", None][i % 3], "observed_at": "2026-07-31 12:00:00",
         "context": "template:샘플", "price_original": 100000 + i * 7000,
         "price_sale": 89000 + i * 6100, "discount_rate": 10, "review_count": i * 13,
         "rating": 4.0 + (i % 10) / 10, "purchase_count": None, "like_count": i * i * 3 + 5,
         "viewers_now": None, "sold_out": i % 7 == 0, "rank": None} for i in range(1, 25)
    ],
    "price_events": [],
    "time_series": None,
    "union": None,
}


def pick_modules(data, wanted):
    """넣을 모듈과 뺀 모듈을 가른다. 뺀 이유는 리포트에 적는다."""
    if not wanted or wanted == ["all"]:
        wanted = list(MODULE_IDS)
    unknown = [m for m in wanted if m not in MODULE_IDS]
    if unknown:
        sys.exit("알 수 없는 모듈: %s — 쓸 수 있는 것: %s"
                 % (", ".join(unknown), ", ".join(MODULE_IDS)))
    on, skipped = [], []
    for mid, title, need in MODULES:
        if mid not in wanted:
            continue
        if need(data):
            on.append(mid)
        else:
            skipped.append((mid, title))
    return on, skipped


def render(data, modules, skipped):
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    body = "".join(section_html(mid, data) for mid in modules)
    note = ""
    if skipped:
        note = ('<p class="section-note">데이터가 없어 뺀 모듈: <strong>%s</strong> — '
                "요청했지만 만들지 못한 것이다(안 만든 것과 다르다)。</p>"
                % " · ".join("%s(%s)" % (t, m) for m, t in skipped))
    bar = "".join(
        '<label class="modtoggle"><input type="checkbox" data-mod="%s" checked>%s</label>'
        % (mid, title)
        for mid, title, _ in MODULES if mid in modules)
    return (HTML
            .replace("__CSS__", ui.theme_tokens() + ui.COMPONENT_CSS + OWN_CSS)
            .replace("__COMMON_JS__", ui.COMMON_JS)
            .replace("__MODBAR__", bar)
            .replace("__SKIPPED__", note)
            .replace("__BODY__", body)
            .replace("__MODULES__", json.dumps(modules))
            .replace("__AXES__", json.dumps(AXES, ensure_ascii=False))
            .replace("__SITELABEL__", json.dumps(
                {s: br.site_name(s) for s in data["meta"]["sites"]}, ensure_ascii=False))
            .replace("__TABLE_COLS__", json.dumps(_cols(data), ensure_ascii=False))
            .replace("__PAYLOAD__", payload))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get("INTEL_DB", "data/intel.db"))
    ap.add_argument("--context", action="append", help="관측 문맥 필터 (반복 지정 가능)")
    ap.add_argument("--out")
    ap.add_argument("--modules", default="all",
                    help="넣을 모듈을 쉼표로 (기본 all). 쓸 수 있는 것: " + ", ".join(MODULE_IDS))
    ap.add_argument("--list-modules", action="store_true", help="모듈 목록만 출력하고 끝낸다")
    ap.add_argument("--emit-template", action="store_true")
    ap.add_argument("--emit-json", action="store_true",
                    help="HTML 대신 데이터 JSON을 낸다 — AI 자유 생성 리포트의 결정적 데이터 소스")
    args = ap.parse_args()

    if args.list_modules:
        for mid, title, _ in MODULES:
            print("%-11s %s" % (mid, title))
        return
    if not args.out:
        sys.exit("--out이 필요하다")

    if args.emit_template:
        data = SAMPLE
    else:
        if not Path(args.db).exists():
            sys.exit(f"DB가 없다: {args.db} — intel_db.py load로 먼저 적재할 것")
        data = collect(args.db, args.context)
        if not data["items"]:
            sys.exit("조건에 맞는 관측이 없다 — context 철자와 적재 여부를 확인할 것")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.emit_json:
        out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(f"완료(JSON): {out} (상품 {len(data['items'])}개 — "
              f"AI 생성 리포트는 이 파일을 그대로 임베드한다, 손으로 옮겨 적지 않는다)")
        return
    modules, skipped = pick_modules(data, [m.strip() for m in args.modules.split(",") if m.strip()])
    out.write_text(render(data, modules, skipped), encoding="utf-8")
    n = len(data["items"])
    stock = data["meta"].get("stock") or {}
    print(f"완료: {out} (상품 {n}개, 가격 변경 사건 {len(data['price_events'])}건"
          + (f", 옵션 {stock['options']}개/{stock['products']}상품" if stock else "")
          + f")\n  모듈: {', '.join(modules)}"
          + (f"\n  뺀 모듈(데이터 없음): {', '.join(m for m, _ in skipped)}" if skipped else ""))


# ── 대시보드 전용 CSS ───────────────────────────────────────────────────────
# 공통 컴포넌트(칩·표·툴팁·KPI·섹션)는 report_ui가 갖는다. 여기 있는 것은 대시보드에만
# 있는 것뿐이다 — 같은 것을 두 번 정의하면 두 리포트의 생김새가 서서히 갈라진다.
OWN_CSS = """
.modbar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 10px 14px; margin-bottom: 18px; font-size: 13px; }
.modbar .modlabel { color: var(--muted); font-size: 12px; margin-right: 4px; }
.modtoggle { display: inline-flex; align-items: center; gap: 5px; color: var(--text-2);
  cursor: pointer; }
.axes { display: flex; gap: 10px 16px; flex-wrap: wrap; align-items: center;
  margin-bottom: 10px; font-size: 13px; color: var(--text-2); }
.axes select, .axes input[type="number"], .axes input[type="search"] {
  font: inherit; font-size: 13px; padding: 4px 7px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--page); color: var(--text); }
.axes label { display: inline-flex; align-items: center; gap: 4px; }
.excl { color: var(--muted); font-size: 12px; }
.sub { color: var(--text-2); font-size: 12.5px; }
.smalln { background: color-mix(in srgb, var(--warning) 18%, transparent);
  border-radius: 8px; padding: 8px 12px; font-size: 13px; display: none; margin: 0 0 14px; }
.tablewrap { max-height: 520px; overflow: auto; }
.axistitle { font-size: 12px; fill: var(--text-2); }
svg text { fill: var(--text-2); font-size: 11px; }
svg .grid { stroke: var(--grid); stroke-width: 1; }
svg .axisline { stroke: var(--muted); }
.legend i { display: inline-block; width: 9px; height: 9px; border-radius: 50%;
  margin-right: 4px; }
.legend svg { vertical-align: -3px; margin-right: 3px; }
.expr-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
"""


# ── 모듈 HTML ───────────────────────────────────────────────────────────────

def _cols(data):
    """상품 표의 열. 옵션이 수집된 문맥에서만 옵션 열이 붙는다."""
    cols = [("name", "상품", "text"), ("brand", "브랜드", "text"),
            ("category", "카테고리", "text"), ("fit", "핏", "text"),
            ("site", "플랫폼", "text"), ("price_sale", "판매가", "num"),
            ("discount_rate", "할인율", "num"), ("like_count", "하트", "num"),
            ("review_count", "후기", "num"), ("rating", "평점", "num"),
            ("sold_out", "품절", "text")]
    if data["meta"].get("stock"):
        cols[10:10] = [("opt_total", "옵션 수", "num"), ("opt_out_rate", "옵션 품절률", "num")]
    return cols


def _thead(cols):
    return "".join('<th class="%s" data-sort="%s">%s</th>'
                   % ("col-num" if t == "num" else "", t, label) for _, label, t in cols)


def _group_cfg(cols, cat, aggs, offset=0):
    idx = {f: i + offset for i, (f, _l, _t) in enumerate(cols)}
    if cat not in idx:
        return ""
    got = [[label, idx[f], kind] for label, f, kind in aggs if f in idx]
    return " data-groupby='%s'" % json.dumps(
        {"cat": idx[cat], "aggs": got, "span": len(cols) + offset}, ensure_ascii=False)


def _table_tools(table_id, placeholder):
    """검색·건수·묶어 보기 — 스토리 리포트와 **같은 조작부**다."""
    options = "".join('<option value="%d">%s</option>' % (d, n) for d, n in br.GROUP_LEVELS)
    return (
        '<div class="table-tools">'
        '<input class="filter" type="search" placeholder="%s" data-for="%s" aria-label="표 거르기">'
        '<span class="table-count" id="%s-count">0행</span>'
        '<span class="group-tool"><label for="%s-group">묶어 보기</label>'
        '<select id="%s-group" class="group-level" data-for="%s">%s</select>'
        '<span class="th-tip" data-tip="카테고리를 대/중/소 단계로 접는다. 집계 행에는 '
        '지금 조건을 통과한 행만 들어간다">?</span></span></div>'
        % (placeholder, table_id, table_id, table_id, table_id, table_id, options))


def section_html(mid, data):
    """모듈 하나의 HTML. 열 구성이 데이터에 따라 달라지므로 함수로 만든다."""
    if mid == "kpi":
        return ('<section data-mod="kpi"><h2>요약</h2>'
                '<div class="kpi-row" id="kpis"></div>'
                '<div class="smalln" id="smalln">⚠ 표본이 작다 (n &lt; 30) — '
                '이 조건의 패턴은 우연일 가능성이 크다</div></section>')

    if mid == "scatter":
        return (
            '<section data-mod="scatter"><h2>산점도</h2>'
            '<details class="section-note"><summary>설명·주의 ▾</summary>'
            '두 수치의 관계를 본다. <strong>미노출(null)은 그리지 않는다</strong> — 0으로 찍으면 '
            "'반응 없음'으로 읽힌다. 제외 건수는 축 옆에 적는다. 하트·누적판매는 자릿수를 "
            '넘나들어 로그 축이 기본이다. 점을 클릭하면 상품 페이지가 열린다.</details>'
            '<div class="axes">X <select id="ax"></select>'
            '<label><input type="checkbox" id="axlog"> 로그</label>'
            ' · Y <select id="ay"></select>'
            '<label><input type="checkbox" id="aylog"> 로그</label>'
            ' · 색 기준 <select id="acolor"></select>'
            '<span class="th-tip" data-tip="점을 무엇으로 갈라 볼지 고른다. 색과 함께 마커 모양도 '
            '달라진다 — 색만으로 구분하면 색약에서 읽히지 않는다">?</span>'
            '<span id="corr" class="sub"></span> <span class="excl" id="excl"></span></div>'
            '<svg id="scatter" width="100%" height="430" viewBox="0 0 920 430"></svg>'
            '<div class="legend" id="legend"></div></section>')

    if mid == "dist":
        return ('<section data-mod="dist"><h2>분포</h2>'
                '<div class="axes"><select id="hx"></select> '
                '<span class="excl" id="hexcl"></span></div>'
                '<svg id="hist" width="100%" height="240" viewBox="0 0 920 240"></svg></section>')

    if mid == "group":
        return (
            '<section data-mod="group"><h2>그룹 비교 (분포)</h2>'
            '<details class="section-note"><summary>설명·주의 ▾</summary>'
            '한 지표를 <strong>그룹으로 나눠</strong> 분포를 나란히 본다. 전체에서 보이던 차이가 '
            '그룹 안에서 사라지거나 뒤집히면 그것이 <strong>심슨의 역설</strong>이다 — 결론 전에 '
            '여기서 확인한다. 상자는 25~75%(IQR), 가운데 굵은 선이 중위값, 수염은 1.5×IQR 안의 '
            '최소·최대이고 그 밖의 값은 점으로 찍는다(버리지 않는다).</details>'
            '<div class="axes">그룹 <select id="gb"></select> · 지표 <select id="gm"></select>'
            ' <span class="excl" id="gexcl"></span></div>'
            '<svg id="groups" width="100%" height="320" viewBox="0 0 920 320"></svg></section>')

    if mid == "variants":
        st = data["meta"]["stock"]
        sold = st.get("sold")
        if not st.get("has_qty"):
            qty = ("이 문맥에는 <strong>재고 수량이 없다</strong> — 사이트가 이미 준 품절 여부(L1)까지만 "
                   "수집됐다. 그래서 여기 나오는 것은 <strong>옵션 개수 기준 품절률</strong>이지 "
                   "수량 비율이 아니다.")
        elif sold:
            qty = ("옵션별 <strong>재고 수량</strong>이 관측됐고, 감소분으로 "
                   "<strong>최소 판매량 %s개</strong>를 얻었다(옵션 %s개 · 관측 창 %s ~ %s · "
                   "재입고 %s회 감지). <em>최소</em>인 이유는 재입고가 섞인 구간에서 재입고 전에 "
                   "팔린 수량이 가려지기 때문이다 — 실제 판매량은 이보다 크다."
                   % (format(sold["total"], ","), format(sold["options"], ","),
                      sold["from_at"], sold["to_at"], sold["restock"]))
        else:
            qty = ("옵션별 <strong>재고 수량</strong>은 관측됐지만 <strong>옵션마다 시점이 하나뿐</strong>이라 "
                   "판매수량(재고 감소분)은 아직 산출할 수 없다 — 같은 옵션을 한 번 더 관측해야 "
                   "차이가 생긴다. 없는 값을 만들지 않는다.")
        return (
            '<section data-mod="variants"><h2>사이즈별 재고</h2>'
            '<details class="section-note"><summary>설명·주의 ▾</summary>'
            '상품 %s개의 옵션 %s개를 옵션 단위로 센다(관측 근거: <code>%s</code>). %s '
            '사이즈는 <strong>사이트가 준 옵션 문자열 그대로</strong>다 — 표기가 다른 사이트를 '
            '한 이름으로 합치지 않는다.</details>'
            '<div class="axes">기준 <select id="vmode">'
            '<option value="size">사이즈</option><option value="site">플랫폼</option>'
            '<option value="category">카테고리</option></select>'
            ' <span class="excl" id="vexcl"></span></div>'
            '<svg id="variants" width="100%%" height="320" viewBox="0 0 920 320"></svg>'
            '<p class="chart-axis-note">가로: 옵션 수(개) · 채운 부분이 품절 · 오른쪽에 품절률</p>'
            "</section>"
            % (format(st["products"], ","), format(st["options"], ","),
               " · ".join(st["basis"]) or "unknown", qty))

    if mid == "timeseries":
        return (
            '<section data-mod="timeseries"><h2>시계열 추이 (축적 관측)</h2>'
            '<details class="section-note"><summary>설명·주의 ▾</summary>'
            '같은 상품의 시점별 지표. <strong>순위권 밖 구간은 선을 끊는다</strong>(관측 없음 — '
            '이어 그으면 없는 순위를 주장하는 것이다). 이동평균은 노이즈를 줄인 것이지 새 '
            '데이터가 아니다. 예측·외삽은 하지 않는다. 현재 조건에 든 상품 중 관측이 많은 '
            '순으로 그린다.</details>'
            '<div class="axes">지표 <select id="ts_metric">'
            '<option value="rank">순위</option><option value="price">판매가</option>'
            '<option value="like">하트</option><option value="review">후기 수</option></select>'
            ' · 계열 <input type="number" id="ts_n" value="8" min="1" max="40" style="width:56px">개'
            ' <label><input type="checkbox" id="ts_ma"> 이동평균</label>'
            ' <span class="sub" id="ts_info"></span></div>'
            '<svg id="ts" width="100%" height="360" viewBox="0 0 1040 360"></svg>'
            '<div class="legend" id="ts_leg"></div></section>')

    if mid == "events":
        return (
            '<section data-mod="events"><h2>가격 변경 사건 (관측 간 증분)</h2>'
            '<details class="section-note"><summary>설명·주의 ▾</summary>'
            '같은 상품의 연속 관측에서 판매가가 바뀐 사건. 증분은 그 관측 창의 <strong>순변화</strong>다 '
            '— 창 안에서 왕복한 것은 잡히지 않는다. 창이 다른 상품끼리 증분을 나란히 놓고 크기를 '
            '비교하지 말 것.</details>'
            '<div class="axes"><span>방향</span>'
            '<button type="button" class="chip is-on" id="ev_dir_down">인하</button>'
            '<button type="button" class="chip is-on" id="ev_dir_up">인상</button>'
            '<span style="margin-left:8px">하트 증분</span>'
            '<button type="button" class="chip" id="ev_like_pos">증가만</button>'
            '<span class="sub" id="ev_n"></span></div>'
            '<div class="tablewrap" id="events"></div></section>')

    if mid == "linesheet":
        sites = data["meta"]["sites"]
        cols = [("name", "상품", "text"), ("presence", "입점", "text"),
                ("brand", "브랜드", "text"), ("category", "품목", "text")]
        # 사이트 이름 표기는 스토리 리포트와 같은 것을 쓴다(무신사·29CM…) — 같은 플랫폼이
        # 리포트마다 `musinsa`였다 `무신사`였다 하면 같은 것으로 안 읽힌다.
        for s in sites:
            cols.append(("p_" + s, "%s 판매가" % br.site_name(s), "num"))
        if len(sites) == 2:
            cols.append(("gap", "가격 차이", "num"))
        for s in sites:
            cols.append(("l_" + s, "%s 하트" % br.site_name(s), "num"))
        cfg = _group_cfg(cols, "category",
                         [("%s 중위가" % br.site_name(s), "p_" + s, "median-won") for s in sites])
        return (
            '<section data-mod="linesheet"><h2>라인시트 (플랫폼 합집합)</h2>'
            '<details class="section-note"><summary>설명·주의 ▾</summary>'
            '같은 상품 판정은 <strong>정규화 상품명 완전일치</strong>뿐이다 — 스토리 리포트와 '
            '같은 규칙 하나를 쓴다. 유사도·가격으로 추정 매칭하지 않는다(정가까지 같은 다른 '
            '상품이 실측됐다). 매칭되지 않은 상품은 오류가 아니라 <em>단독 입점 사실</em>이고, '
            '한쪽에만 있는 칸의 <span class="na">—</span>는 미노출이 아니라 <em>그 플랫폼에 그 '
            '상품이 없다</em>는 뜻이다. 현재 조건(위 필터)이 그대로 걸린다.</details>'
            "%s"
            '<div class="table-wrap"><table id="t-union" class="grid"%s>'
            "<thead><tr>%s</tr></thead><tbody></tbody></table></div></section>"
            % (_table_tools("t-union", "상품명·브랜드로 거르기"), cfg, _thead(cols)))

    if mid == "table":
        cols = _cols(data)
        cfg = _group_cfg(cols, "category",
                         [("중위가", "price_sale", "median-won"), ("하트 합", "like_count", "sum"),
                          ("후기 합", "review_count", "sum")])
        return (
            '<section data-mod="table"><h2>상품 표</h2>'
            "%s"
            '<div class="table-wrap"><table id="t-items" class="grid"%s>'
            "<thead><tr>%s</tr></thead><tbody></tbody></table></div></section>"
            % (_table_tools("t-items", "상품명·브랜드로 거르기"), cfg, _thead(cols)))

    return ""


HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>분석 리포트 — 가격-반응 탐색</title>
<style>__CSS__</style></head><body>
<main>
<header>
  <h1>분석 리포트 — 가격-반응 탐색</h1>
  <dl class="meta" id="meta"></dl>
  <details class="banner banner-info" open><summary><strong>이 도구는 가격-반응의 상관 탐색이지 엄밀한 가격 탄력성 추정이 아니다. 상관은 인과가 아니다.</strong> <span class="sub">주의사항 펼치기 ▾</span></summary>
  · 여기 없는 것: 판매 종료된 상품, 순위권 밖 미관측 구간, 조건에 따라 빠진 품절 상품 (<strong>생존편향</strong>) — 지금 팔리는 것만 보고 성공 요인을 읽는 오류를 조심하라<br>
  · 축 조합을 훑다 발견한 패턴은 가설이지 결론이 아니다 — 다음 관측으로 재확인하라 (<strong>다중비교</strong>)<br>
  · 전체 집합의 상관은 세그먼트(카테고리·핏·브랜드)로 나누면 사라지거나 뒤집힐 수 있다 — 아래 조건 패널과 '그룹 비교'가 그 확인 장치다 (<strong>심슨의 역설</strong>)<br>
  · 누적판매·하트는 <strong>누적값</strong>이라 출시가 오래된 상품이 유리하다 · 축약 표기에서 파싱한 값은 ±4% 오차 · 구간 표기(<code>300회 이상</code>)는 축으로 쓰지 않는다
  </details>
</header>
<div class="modbar"><span class="modlabel">모듈</span>__MODBAR__</div>
__SKIPPED__
<section id="controls"><h2>조건 (변인통제)</h2>
<details class="section-note"><summary>설명·주의 ▾</summary>
한 축에서 여러 개를 켜면 <strong>OR</strong>, 축끼리는 <strong>AND</strong>다. 더 복잡한 조합은
아래 <strong>수식</strong>에 직접 쓴다 — 스토리 리포트의 입점 수식과 같은 문법이다.
<strong>모든 모듈이 같은 조건을 공유한다</strong>(단일 파이프라인) — 차트마다 따로 거르지 않는다.
</details>
<div class="facets" id="facets"></div>
<div class="facet-group expr-row">
  <span class="facet-label">수식</span>
  <span class="expr-hint" data-tip="값 이름을 AND·OR·NOT·괄호로 조합한다. 예: (와이드 OR 세미와이드) AND NOT 품절 · 무신사 AND NOT 29CM. 위 칩에 있는 값이면 무엇이든 쓸 수 있다">?</span>
  <span class="expr-chips" id="expr_ops"></span>
  <input type="text" class="expr-input" id="expr" placeholder="예: 무신사 AND NOT 품절" spellcheck="false">
  <span class="expr-status" id="expr_status"></span>
</div>
<div class="axes">판매가 <input type="number" id="f_pmin" style="width:100px"> ~
 <input type="number" id="f_pmax" style="width:100px">
 · 품절 <select id="f_so"><option value="">포함</option><option value="0">판매 중만</option>
 <option value="1">품절만</option></select>
 <span class="sub" id="cond"></span></div>
</section>
__BODY__
</main>
<footer>수치는 사이트가 노출한 값만 쓴다. 미노출은 미노출로 두고 추정하지 않는다.
정본 DB(<code>data/intel.db</code>)에서 뽑았고 외부 리소스를 부르지 않는다.</footer>
<div id="tip" role="status"></div>
<script type="application/json" id="data">__PAYLOAD__</script>
<script>__COMMON_JS__</script>
<script>
const DATA=JSON.parse(document.getElementById('data').textContent);
const MODS=__MODULES__;
const has=id=>MODS.indexOf(id)>=0;
const $=id=>document.getElementById(id);

const AXES=__AXES__;
// 사이트 표기는 스토리 리포트와 같다(무신사·29CM…). 거르기 값은 원래 키를 쓰고 **보이는
// 이름만** 바꾼다 — 표기를 데이터로 쓰면 사이트 이름이 바뀔 때 필터가 조용히 깨진다.
const SITELABEL=__SITELABEL__;
const disp=(f,v)=>f==="site"?(SITELABEL[v]||v):v;
const COL=["--series-1","--series-2","--series-3","--series-4","--series-5"];
// 색만으로 계열을 구분하지 않는다(적록색약 8%) — 모양이 두 번째 신호다.
const SHAPES=["circle","tri","sq","dia","cross"];
const NS="http://www.w3.org/2000/svg";
const GROUP_CAP=12;

const fmt=v=>v==null?"—":(typeof v==="number"?(Math.round(v*100)/100).toLocaleString("ko-KR"):v);
// 값은 전부 사이트에서 긁어온 문자열이다 — innerHTML에 그대로 넣지 않는다.
const esc=v=>String(v==null?"":v).replace(/[&<>"']/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const escf=v=>esc(fmt(v));
const css=n=>getComputedStyle(document.body).getPropertyValue(n).trim();
const NA='<span class="na">—</span>';
const S={data:DATA.items};

function median(a){if(!a.length)return null;const s=[...a].sort((x,y)=>x-y);
  const m=s.length>>1;return s.length%2?s[m]:(s[m-1]+s[m])/2;}
function quantile(s,q){if(!s.length)return null;const pos=(s.length-1)*q,lo=Math.floor(pos),hi=Math.ceil(pos);
  return lo===hi?s[lo]:s[lo]+(s[hi]-s[lo])*(pos-lo);}
function labelOf(f){const a=AXES.find(a=>a[0]===f);return a?a[1]:f.replace("px_","")+" (AI 판정)";}
function markPath(shape,x,y,r){
  if(shape==="tri")return `M${x} ${y-r*1.2}L${x+r*1.15} ${y+r*0.85}L${x-r*1.15} ${y+r*0.85}Z`;
  if(shape==="sq")return `M${x-r*0.9} ${y-r*0.9}h${r*1.8}v${r*1.8}h${-r*1.8}Z`;
  if(shape==="dia")return `M${x} ${y-r*1.35}L${x+r*1.15} ${y}L${x} ${y+r*1.35}L${x-r*1.15} ${y}Z`;
  if(shape==="cross")return `M${x-r*1.2} ${y}h${r*2.4}M${x} ${y-r*1.2}v${r*2.4}`;
  return null;
}
function swatch(i){
  const c=css(COL[Math.min(i,COL.length-1)]),sh=SHAPES[Math.min(i,SHAPES.length-1)];
  const d=markPath(sh,7,7,4.5);
  const body=d==null?`<circle cx="7" cy="7" r="4.5" fill="${c}"/>`
    :(sh==="cross"?`<path d="${d}" stroke="${c}" stroke-width="1.8" fill="none"/>`
                  :`<path d="${d}" fill="${c}"/>`);
  return `<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">${body}</svg>`;
}

// ── 조건(변인통제) ─────────────────────────────────────────────────────────
// 칩 = 스토리 리포트와 같은 마크업·같은 규칙(축 안 OR / 축 간 AND, 건수 병기).
// 다만 여기서는 DOM을 숨기는 게 아니라 **데이터를 다시 걸러** 전 모듈을 다시 그린다.
const SEL={}, AXIS=[];
const getf=f=>d=>d[f];
// 계층 카테고리는 **대→중→소 캐스케이드**다 — 스토리 리포트와 같은 규약. 전 경로를 칩으로
// 펼치면 100개가 넘어 화면이 칩으로만 찬다(실측: 로우클래식 카테고리 116종).
function catPrefix(v,depth){
  return String(v==null||v===""?"(없음)":v).split(" > ").slice(0,depth).join(" > ");
}
function valuesOf(get){
  const m={};DATA.items.forEach(d=>{const v=get(d);
    if(v!=null&&v!=="")m[v]=(m[v]||0)+1;});
  return m;
}
function uniq(field){return Object.keys(valuesOf(getf(field))).sort();}
function mkFacet(id,label,get,counts,order,meta){
  SEL[id]=new Set();AXIS.push({id:id,get:get,label:label});
  const chips=(order||Object.keys(counts).sort()).map(v=>{
    const extra=meta&&meta.parent?` data-parent="${esc(meta.parent(v))}"`:"";
    return `<button type="button" class="chip" data-axis="${esc(id)}" data-v="${esc(v)}"${extra}>`
      +`${esc(meta&&meta.label?meta.label(v):disp(id,v))}`
      +`<em>${(counts[v]||0).toLocaleString("ko-KR")}</em></button>`;
  }).join("");
  const casc=meta&&meta.level!=null?` data-clevel="${meta.level}"`:"";
  return `<div class="facet-group"${casc}><span class="facet-label">${esc(label)}</span>`
    +`<button type="button" class="chip chip-all is-on" data-axis="${esc(id)}" data-v="">전체</button>`
    +chips+`</div>`;
}
function initFacets(){
  let h="";
  if(DATA.meta.sites.length>1)
    h+=mkFacet("site","플랫폼",getf("site"),valuesOf(getf("site")),DATA.meta.sites);
  // 카테고리 3단계 — 상위를 골라야 하위 칩이 나온다
  const depths=[["대분류",1],["중분류",2],["소분류",3]];
  const maxDepth=Math.max(...DATA.items.map(d=>String(d.category||"").split(" > ").length));
  depths.forEach(([name,depth],li)=>{
    if(depth>maxDepth)return;
    const get=d=>catPrefix(d.category,depth);
    const counts=valuesOf(get);
    if(Object.keys(counts).length<2&&li===0)return;
    h+=mkFacet("cat"+depth,"카테고리 · "+name,get,counts,Object.keys(counts).sort(),
      li===0?{level:0}:{level:li,parent:v=>catPrefix(v,depth-1),
                        label:v=>v.split(" > ").slice(-1)[0]});
  });
  if(uniq("fit").length)h+=mkFacet("fit","핏",getf("fit"),valuesOf(getf("fit")));
  if(uniq("brand").length>1)h+=mkFacet("brand","브랜드",getf("brand"),valuesOf(getf("brand")));
  (DATA.meta.proxies||[]).filter(p=>!p.numeric).forEach(p=>{
    const key="px_"+p.name,get=getf(key),counts=valuesOf(get);
    if(!Object.keys(counts).length)return;
    counts["(미판정)"]=DATA.items.filter(d=>d[key]==null).length;
    h+=mkFacet(key,p.name+" (AI 판정)",d=>d[key]==null?"(미판정)":d[key],counts);
  });
  $("facets").innerHTML=h;
  $("facets").addEventListener("click",e=>{
    const chip=e.target.closest(".chip");if(!chip)return;
    const axis=chip.dataset.axis,group=chip.parentElement;
    const all=group.querySelector(".chip-all");
    if(chip===all){group.querySelectorAll(".chip").forEach(c=>c.classList.remove("is-on"));
      all.classList.add("is-on");SEL[axis].clear();}
    else{chip.classList.toggle("is-on");
      SEL[axis].has(chip.dataset.v)?SEL[axis].delete(chip.dataset.v):SEL[axis].add(chip.dataset.v);
      all.classList.toggle("is-on",SEL[axis].size===0);}
    syncCascade();apply();
  });
  syncCascade();
  // 수식에 끼워 넣을 연산자 칩 — 스토리 리포트의 입점 수식과 같은 조작이다
  $("expr_ops").innerHTML=[" AND "," OR "," NOT ","(",")"]
    .map(o=>`<button type="button" class="chip expr-op" data-ins="${esc(o)}">${esc(o.trim())}</button>`)
    .join("")+`<button type="button" class="chip expr-clear" data-ins="">지우기</button>`;
  $("expr_ops").addEventListener("click",e=>{
    const b=e.target.closest("[data-ins]");if(!b)return;
    const inp=$("expr");
    if(b.classList.contains("expr-clear"))inp.value="";
    else{const pos=inp.selectionStart||inp.value.length;
      inp.value=inp.value.slice(0,pos)+b.dataset.ins+inp.value.slice(inp.selectionEnd||pos);}
    inp.focus();recompile();
  });
  $("expr").addEventListener("input",recompile);
  ["f_pmin","f_pmax","f_so"].forEach(id=>$(id).addEventListener("change",apply));
  const prices=DATA.items.map(d=>d.price_sale).filter(v=>v!=null);
  if(prices.length){$("f_pmin").placeholder=Math.min(...prices);$("f_pmax").placeholder=Math.max(...prices);}
}
// 하위 단계 칩은 **부모가 켜져 있을 때만** 보인다. 부모가 꺼지면 켜져 있던 하위 선택도
// 함께 지운다 — 안 보이는 조건이 남아서 결과를 바꾸면 그건 숨은 필터다.
function syncCascade(){
  const groups=[...document.querySelectorAll(".facet-group[data-clevel]")]
    .sort((a,b)=>+a.dataset.clevel-+b.dataset.clevel);
  for(let i=1;i<groups.length;i++){
    const parent=groups[i-1],child=groups[i];
    const on=new Set([...parent.querySelectorAll(".chip:not(.chip-all).is-on")]
      .map(c=>c.dataset.v));
    child.classList.toggle("cascade-collapsed",on.size===0);
    child.querySelectorAll(".chip:not(.chip-all)").forEach(c=>{
      const show=on.size>0&&on.has(c.dataset.parent);
      c.style.display=show?"":"none";
      if(!show&&c.classList.contains("is-on")){
        c.classList.remove("is-on");
        SEL[c.dataset.axis].delete(c.dataset.v);
        const all=child.querySelector(".chip-all");
        if(all)all.classList.toggle("is-on",SEL[c.dataset.axis].size===0);
      }
    });
  }
}
// 수식이 읽을 수 있는 값 = 칩에 있는 값 전부 + 품절/판매 중.
// **화면에 보이는 이름(무신사)과 원래 키(musinsa) 둘 다** 받는다 — 칩에는 '무신사'라고
// 써 놓고 수식에서는 'musinsa'만 받으면 쓸 수가 없다.
function exprTokens(){
  const t=[];
  AXIS.forEach(a=>Object.keys(valuesOf(a.get)).forEach(v=>{
    t.push(String(v));
    const d=String(disp(a.id,v));
    if(d!==String(v))t.push(d);
  }));
  return t.concat(["품절","판매 중"]);
}
function valueSet(d){
  const s=new Set();
  AXIS.forEach(a=>{const v=a.get(d);
    if(v!=null&&v!==""){s.add(String(v));s.add(String(disp(a.id,v)));}});
  s.add(d.sold_out?"품절":"판매 중");
  return s;
}
let EXPR=null;
function recompile(){
  const src=$("expr").value;
  if(!src.trim()){EXPR=null;$("expr_status").textContent="";$("expr_status").className="expr-status";
    apply();return;}
  const res=window.reportExpr.compile(src,exprTokens());
  if(res.error){EXPR=null;$("expr_status").textContent="⚠ "+res.error;
    $("expr_status").className="expr-status err";}
  else{EXPR=res.rpn;$("expr_status").textContent="유효";$("expr_status").className="expr-status";}
  apply();
}
const fval=id=>{const e=$(id);return e?e.value.trim():"";};
function anyFilterOn(){
  if(fval("f_pmin")!==""||fval("f_pmax")!==""||fval("f_so")!==""||EXPR)return true;
  return Object.keys(SEL).some(k=>SEL[k]&&SEL[k].size>0);
}
function filtered(){
  const pmin=fval("f_pmin")===""?null:+fval("f_pmin");
  const pmax=fval("f_pmax")===""?null:+fval("f_pmax");
  const so=fval("f_so");
  return DATA.items.filter(d=>{
    // 한 축에서 여러 개를 켜면 OR, 축끼리는 AND — 스토리 리포트와 같은 규칙이다
    for(const a of AXIS){const s=SEL[a.id];
      if(s&&s.size){const v=a.get(d);
        if(!s.has(v==null||v===""?"(미판정)":String(v)))return false;}}
    if(pmin!=null&&!(d.price_sale>=pmin))return false;
    if(pmax!=null&&!(d.price_sale<=pmax))return false;
    if(so!==""&&(so==="1")!==!!d.sold_out)return false;
    if(EXPR&&!window.reportExpr.eval(EXPR,valueSet(d)))return false;
    return true;
  });
}
function apply(){
  S.data=filtered();
  S.keys=new Set(S.data.map(d=>d.site+"/"+d.product_id));
  const el=$("smalln");if(el)el.style.display=S.data.length<30?"block":"none";
  $("cond").textContent=`· 조건 통과 ${S.data.length.toLocaleString("ko-KR")}개 / 전체 ${DATA.items.length.toLocaleString("ko-KR")}개`;
  if(has("kpi"))renderKPIs();
  if(has("scatter"))renderScatter();
  if(has("dist"))renderHist();
  if(has("group"))renderGroups();
  if(has("variants"))renderVariants();
  if(has("table"))renderTable();
  if(has("linesheet"))renderUnion();
  if(has("timeseries"))renderTimeSeries();
}

// ── KPI ────────────────────────────────────────────────────────────────────
function renderKPIs(){
  const d=S.data,ps=d.map(x=>x.price_sale).filter(v=>v!=null);
  const dr=d.map(x=>x.discount_rate).filter(v=>v!=null);
  const mid=median(ps);
  const tiles=[["n (현재 조건)",d.length.toLocaleString("ko-KR"),"전체 "+DATA.items.length.toLocaleString("ko-KR")+"개 중"],
    ["중위 판매가",mid==null?"—":fmt(mid)+"원",ps.length?null:"판매가 미노출"],
    ["중위 할인율",dr.length?median(dr)+"%":"—",null],
    ["품절",d.filter(x=>x.sold_out).length.toLocaleString("ko-KR")+"건",null]];
  const st=DATA.meta.stock;
  if(st){const withOpt=d.filter(x=>x.opt_total);
    const rate=withOpt.length?withOpt.reduce((a,b)=>a+b.opt_out_rate,0)/withOpt.length:null;
    tiles.push(["평균 옵션 품절률",rate==null?"—":Math.round(rate*10)/10+"%",
      withOpt.length+"개 상품에 옵션 정보"]);}
  $("kpis").innerHTML=tiles.map(([l,v,n])=>
    `<div class="kpi"><div class="kpi-label">${esc(l)}</div><div class="kpi-value">${v}</div>`
    +(n?`<div class="kpi-note">${esc(n)}</div>`:"")+`</div>`).join("");
}

// ── 산점도 ─────────────────────────────────────────────────────────────────
function scale(vals,lo,hi,log){
  const cl=v=>log?Math.max(v,1):v;   // 로그 축은 0·음수를 1로 클램프한다
  const mn=cl(Math.min(...vals)),mx=cl(Math.max(...vals));
  if(log){const l=Math.log10,a=l(mn),b=l(mx);
    return v=>b===a?(lo+hi)/2:lo+(l(cl(v))-a)/(b-a)*(hi-lo);}
  return v=>mx===mn?(lo+hi)/2:lo+(v-mn)/(mx-mn)*(hi-lo);
}
function tickfmt(v){return v>=100?Math.round(v).toLocaleString("ko-KR")
  :(Math.round(v*100)/100).toLocaleString("ko-KR");}
function ticks(vals,n,log){
  const mn=log?Math.max(Math.min(...vals),1):Math.min(...vals),mx=Math.max(...vals);
  if(log){const out=[];let p=Math.pow(10,Math.floor(Math.log10(mn)));
    while(p<=mx){if(p>=mn)out.push(p);p*=10;}return out.length?out:[mn,mx];}
  const out=[];for(let i=0;i<=n;i++)out.push(mn+(mx-mn)*i/n);return out;
}
function pearson(xs,ys){const n=xs.length;if(n<3)return null;
  const mx=xs.reduce((a,b)=>a+b)/n,my=ys.reduce((a,b)=>a+b)/n;
  let sxy=0,sx=0,sy=0;
  for(let i=0;i<n;i++){sxy+=(xs[i]-mx)*(ys[i]-my);sx+=(xs[i]-mx)**2;sy+=(ys[i]-my)**2;}
  return sx&&sy?sxy/Math.sqrt(sx*sy):null;}
const tip=$("tip");
function showTip(ev,html){tip.innerHTML=html;tip.style.opacity=1;
  tip.style.left=Math.min(ev.clientX+14,innerWidth-300)+"px";tip.style.top=(ev.clientY+14)+"px";}
function hideTip(){tip.style.opacity=0;}
// 색 기준 — 단일 사이트 문맥에서 전부 같은 색으로 찍히던 것을 고른 축으로 가른다
function colorGroups(){
  const f=$("acolor").value;
  if(!f)return {key:null,values:[]};
  // 색은 5개뿐이다 — **점이 많은 값 5개**에 준다. 가나다순으로 자르면 어느 값이 색을
  // 받을지가 이름 순서로 정해져 그림이 우연히 달라진다. 나머지는 회색 + 건수 명시.
  const n={};S.data.forEach(d=>{const v=d[f];if(v!=null&&v!=="")n[v]=(n[v]||0)+1;});
  const vals=Object.keys(n).sort((a,b)=>n[b]-n[a]);
  return {key:f,values:vals.slice(0,5),rest:Math.max(0,vals.length-5)};
}
function renderScatter(){
  const xf=$("ax").value,yf=$("ay").value;
  const xlog=$("axlog").checked,ylog=$("aylog").checked;
  const pts=S.data.filter(d=>d[xf]!=null&&d[yf]!=null);
  const excl=S.data.length-pts.length;
  $("excl").textContent=excl?`· 제외 ${excl.toLocaleString("ko-KR")}건(미노출)`:"";
  const svg=$("scatter");svg.innerHTML="";
  if(!pts.length){$("corr").textContent="";$("legend").innerHTML="";return;}
  const P={l:74,r:20,t:14,b:44},W=920,H=430;
  const sx=scale(pts.map(d=>d[xf]),P.l,W-P.r,xlog),sy=scale(pts.map(d=>d[yf]),H-P.b,P.t,ylog);
  let g="";
  ticks(pts.map(d=>d[yf]),5,ylog).forEach(t=>{const y=sy(t);
    g+=`<line class="grid" x1="${P.l}" x2="${W-P.r}" y1="${y}" y2="${y}"/>`
      +`<text x="${P.l-8}" y="${y+4}" text-anchor="end">${tickfmt(t)}</text>`;});
  ticks(pts.map(d=>d[xf]),6,xlog).forEach(t=>{const x=sx(t);
    g+=`<text x="${Math.min(x,W-P.r-30)}" y="${H-P.b+18}" text-anchor="middle">${tickfmt(t)}</text>`;});
  g+=`<line class="axisline" x1="${P.l}" x2="${W-P.r}" y1="${H-P.b}" y2="${H-P.b}"/>`;
  g+=`<text class="axistitle" x="${(P.l+W-P.r)/2}" y="${H-6}" text-anchor="middle">`
    +`${esc(labelOf(xf))}${xlog?" (로그 축)":""}</text>`;
  g+=`<text class="axistitle" x="16" y="${(P.t+H-P.b)/2}" text-anchor="middle" `
    +`transform="rotate(-90 16 ${(P.t+H-P.b)/2})">${esc(labelOf(yf))}${ylog?" (로그 축)":""}</text>`;
  svg.innerHTML=g;
  const grp=colorGroups();
  pts.forEach(d=>{
    const gi=grp.key?grp.values.indexOf(String(d[grp.key])):0;
    const idx=gi<0?COL.length-1:gi;
    const shape=SHAPES[Math.min(idx,SHAPES.length-1)];
    const x=sx(d[xf]),y=sy(d[yf]),path=markPath(shape,x,y,4.4);
    const el=document.createElementNS(NS,path==null?"circle":"path");
    if(path==null){el.setAttribute("cx",x);el.setAttribute("cy",y);el.setAttribute("r",4.5);}
    else el.setAttribute("d",path);
    const color=gi<0?css("--muted"):css(COL[Math.min(idx,COL.length-1)]);
    if(shape==="cross"){el.setAttribute("stroke",color);el.setAttribute("stroke-width",1.8);
      el.setAttribute("fill","none");}
    else{el.setAttribute("fill",color);el.setAttribute("fill-opacity",.75);
      el.setAttribute("stroke",css("--surface"));el.setAttribute("stroke-width",1);}
    el.style.cursor="pointer";
    el.addEventListener("mousemove",ev=>showTip(ev,
      `<b>${esc(d.name||d.product_id)}</b><br>${esc(d.brand||"")} · ${esc(d.site)}<br>`
      +`판매가 ${escf(d.price_sale)}<br>${esc(labelOf(xf))}: ${escf(d[xf])} · `
      +`${esc(labelOf(yf))}: ${escf(d[yf])}`));
    el.addEventListener("mouseleave",hideTip);
    el.addEventListener("click",()=>{if(d.url&&d.url!=="#")window.open(d.url);});
    svg.appendChild(el);
  });
  const r=pearson(pts.map(d=>d[xf]),pts.map(d=>d[yf]));
  $("corr").innerHTML=r==null?"":`r = ${r.toFixed(2)} (n=${pts.length.toLocaleString("ko-KR")}, 탐색용 — 인과 아님)`
    +(anyFilterOn()?"":" <b>· 전체 집합이다 — 카테고리·핏·브랜드로 나눠도 같은지 확인하라</b>");
  $("legend").innerHTML=grp.key?
    grp.values.map((v,i)=>`<span>${swatch(i)}${esc(disp(grp.key,v))}</span>`).join("")
    +(grp.rest?`<span class="sub">외 ${grp.rest}종(회색)</span>`:""):"";
}

// ── 분포 ───────────────────────────────────────────────────────────────────
function renderHist(){
  const f=$("hx").value;
  const vals=S.data.map(d=>d[f]).filter(v=>v!=null);
  const excl=S.data.length-vals.length;
  $("hexcl").textContent=excl?`제외 ${excl.toLocaleString("ko-KR")}건(미노출)`:"";
  const svg=$("hist");svg.innerHTML="";if(!vals.length)return;
  const P={l:74,r:20,t:24,b:32},W=920,H=240,NB=24;
  const mn=Math.min(...vals),mx=Math.max(...vals),bw=(mx-mn)/NB||1;
  const bins=Array(NB).fill(0);
  vals.forEach(v=>bins[Math.min(NB-1,Math.floor((v-mn)/bw))]++);
  const top=Math.max(...bins),bwpx=(W-P.l-P.r)/NB;
  let g=`<line class="axisline" x1="${P.l}" x2="${W-P.r}" y1="${H-P.b}" y2="${H-P.b}"/>`;
  for(let k=0;k<=2;k++){const cv=Math.round(top*k/2),y=H-P.b-(top?cv/top:0)*(H-P.t-P.b);
    g+=`<line class="grid" x1="${P.l}" x2="${W-P.r}" y1="${y}" y2="${y}"/>`
      +`<text x="${P.l-8}" y="${y+4}" text-anchor="end">${fmt(cv)}</text>`;}
  g+=`<text class="axistitle" x="${(P.l+W-P.r)/2}" y="${H-2}" text-anchor="middle">`
    +`${esc(labelOf(f))} 구간 (라벨은 구간 하한)</text>`;
  g+=`<text class="axistitle" x="14" y="${(P.t+H-P.b)/2}" text-anchor="middle" `
    +`transform="rotate(-90 14 ${(P.t+H-P.b)/2})">상품 수(개)</text>`;
  bins.forEach((b,i)=>{const h=b/top*(H-P.t-P.b);
    g+=`<rect x="${P.l+i*bwpx+1}" y="${H-P.b-h}" width="${bwpx-2}" height="${h}" rx="3" `
      +`fill="${css("--seq")}" fill-opacity=".85"><title>${fmt(Math.round(mn+i*bw))}~`
      +`${fmt(Math.round(mn+(i+1)*bw))}: ${b}건</title></rect>`;});
  [0,NB/2,NB].forEach(i=>{g+=`<text x="${P.l+i*bwpx}" y="${H-P.b+16}" text-anchor="middle">`
    +`${fmt(Math.round(mn+i*bw))}</text>`;});
  const md=median(vals),mdx=P.l+((md-mn)/(mx-mn||1))*(W-P.l-P.r);
  const right=mdx>(P.l+W-P.r)/2;
  g+=`<line x1="${mdx}" x2="${mdx}" y1="${P.t-8}" y2="${H-P.b}" stroke="${css("--muted")}" stroke-dasharray="4 3"/>`
    +`<text x="${mdx+(right?-5:5)}" y="${P.t-11}" text-anchor="${right?"end":"start"}">중위 ${escf(md)}</text>`;
  svg.innerHTML=g;
}

// ── 그룹 비교 (박스 플롯) ───────────────────────────────────────────────────
function shortLabel(k){
  const s=String(k),p=s.split(" > ");
  let t=p.length>1?p.slice(-2).join(" > "):s;
  if(t.length>15)t=t.slice(-15);
  return (t.length<s.length?"…":"")+t;
}
function boxOf(vals){
  const s=[...vals].sort((a,b)=>a-b);
  const q1=quantile(s,.25),q2=quantile(s,.5),q3=quantile(s,.75),iqr=q3-q1;
  const lof=q1-1.5*iqr,hif=q3+1.5*iqr;
  const inb=s.filter(v=>v>=lof&&v<=hif);
  return {n:s.length,q1,q2,q3,lo:inb.length?inb[0]:s[0],
    hi:inb.length?inb[inb.length-1]:s[s.length-1],out:s.filter(v=>v<lof||v>hif)};
}
function renderGroups(){
  const gf=$("gb").value,mf=$("gm").value,svg=$("groups");
  if(!gf){svg.innerHTML="";return;}
  const buckets={};let excl=0;
  S.data.forEach(d=>{const v=d[mf];if(v==null){excl++;return;}
    const k=d[gf]==null||d[gf]===""?"(미분류)":String(d[gf]);
    (buckets[k]=buckets[k]||[]).push(v);});
  const boxes=Object.keys(buckets).map(k=>Object.assign({key:k},boxOf(buckets[k])));
  // 그릴 그룹은 **표본이 큰 순**으로 고른다 — 중위값 순으로 자르면 n=1짜리가 n=1,000짜리를
  // 화면 밖으로 밀어낸다. 고른 뒤에는 중위값 순으로 세운다.
  const dropped=Math.max(0,boxes.length-GROUP_CAP);
  const shown=boxes.slice().sort((a,b)=>b.n-a.n).slice(0,GROUP_CAP).sort((a,b)=>b.q2-a.q2);
  $("gexcl").textContent=(excl?`제외 ${excl.toLocaleString("ko-KR")}건(미노출) · `:"")
    +`그룹 ${boxes.length}개`+(dropped?` 중 표본 큰 ${GROUP_CAP}개만 표시 (${dropped}개 생략) · 중위값 순`:" · 중위값 순");
  svg.innerHTML="";if(!shown.length)return;
  const P={l:150,r:62,t:26,b:36},W=920,rowH=26,H=P.t+P.b+shown.length*rowH;
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.setAttribute("height",H);
  const all=shown.flatMap(b=>b.out.concat([b.lo,b.hi]));
  let mn=Math.min(...all),mx=Math.max(...all);if(mn===mx){mn-=1;mx+=1;}
  const sx=v=>P.l+(v-mn)/(mx-mn)*(W-P.l-P.r);
  let g="";
  for(let k=0;k<=4;k++){const v=mn+(mx-mn)*k/4,x=sx(v);
    g+=`<line class="grid" x1="${x}" x2="${x}" y1="${P.t}" y2="${H-P.b}"/>`
      +`<text x="${x}" y="${H-P.b+16}" text-anchor="middle">${tickfmt(v)}</text>`;}
  g+=`<text class="axistitle" x="${(P.l+W-P.r)/2}" y="${H-4}" text-anchor="middle">${esc(labelOf(mf))}</text>`;
  const col=css("--seq");
  shown.forEach((b,i)=>{
    const y=P.t+i*rowH+rowH/2,h=13,x1=sx(b.q1),x3=sx(b.q3),xm=sx(b.q2);
    g+=`<text x="${P.l-10}" y="${y+4}" text-anchor="end">${esc(shortLabel(b.key))}`
      +`<title>${esc(b.key)}</title></text>`;
    g+=`<line x1="${sx(b.lo)}" x2="${sx(b.hi)}" y1="${y}" y2="${y}" stroke="${css("--muted")}"/>`
      +`<line x1="${sx(b.lo)}" x2="${sx(b.lo)}" y1="${y-5}" y2="${y+5}" stroke="${css("--muted")}"/>`
      +`<line x1="${sx(b.hi)}" x2="${sx(b.hi)}" y1="${y-5}" y2="${y+5}" stroke="${css("--muted")}"/>`;
    g+=`<rect x="${x1}" y="${y-h/2}" width="${Math.max(1,x3-x1)}" height="${h}" rx="2" `
      +`fill="${col}" fill-opacity=".28" stroke="${col}"><title>${esc(b.key)} · n=${b.n} · `
      +`중위 ${fmt(b.q2)} · IQR ${fmt(b.q1)}~${fmt(b.q3)}</title></rect>`;
    g+=`<line x1="${xm}" x2="${xm}" y1="${y-h/2}" y2="${y+h/2}" stroke="${col}" stroke-width="2.5"/>`;
    b.out.forEach(v=>{g+=`<circle cx="${sx(v)}" cy="${y}" r="2" fill="${css("--muted")}" `
      +`fill-opacity=".7"><title>${fmt(v)} (이상치)</title></circle>`;});
    g+=`<text x="${W-P.r+6}" y="${y+4}" font-size="10" fill="${b.n<30?css("--series-2"):css("--muted")}">`
      +`n=${b.n}${b.n<30?" ⚠":""}</text>`;
  });
  svg.innerHTML=g;
}

// ── 사이즈별 재고 ───────────────────────────────────────────────────────────
// 사이즈는 **자연 순서**로 세운다(가나다순 금지 규칙의 예외 — 자연 순서가 있는 축이다).
const SIZE_ORDER=["XXS","XS","S","M","L","XL","XXL","2XL","3XL","4XL","FREE","F","ONE SIZE","ONESIZE"];
function sizeRank(s){
  const u=String(s).toUpperCase().trim();
  const i=SIZE_ORDER.indexOf(u);if(i>=0)return [0,i];
  const n=parseFloat(u);if(!isNaN(n))return [1,n];
  return [2,0];
}
function renderVariants(){
  const mode=$("vmode").value,svg=$("variants");
  const agg={};let prods=0;
  S.data.forEach(d=>{
    if(!d.sizes||!d.sizes.length)return;
    prods++;
    d.sizes.forEach(v=>{
      const key=mode==="size"?String(v[0]):String(d[mode]==null?"(없음)":d[mode]);
      const a=agg[key]=agg[key]||{total:0,out:0};
      a.total++;a.out+=v[1]?1:0;
    });
  });
  const keys=Object.keys(agg);
  $("vexcl").textContent=`옵션 정보가 있는 상품 ${prods.toLocaleString("ko-KR")}개 · `
    +(mode==="size"?"사이즈":mode==="site"?"플랫폼":"카테고리")+` ${keys.length}종`
    +(prods?"":" — 현재 조건에는 옵션이 없다");
  svg.innerHTML="";if(!keys.length)return;
  // 그릴 것은 **옵션이 많은 순**으로 고르고, 고른 뒤 사이즈는 자연 순서로 세운다.
  // 자연 순서로 자르면 뒤쪽 사이즈(3XL 등)가 통째로 잘려 나간다.
  const shown=keys.slice().sort((a,b)=>agg[b].total-agg[a].total).slice(0,GROUP_CAP);
  if(mode==="size")shown.sort((a,b)=>{const ra=sizeRank(a),rb=sizeRank(b);
    return ra[0]-rb[0]||ra[1]-rb[1]||a.localeCompare(b,"ko");});
  const P={l:120,r:96,t:16,b:28},W=920,rowH=26,H=P.t+P.b+shown.length*rowH;
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.setAttribute("height",H);
  const top=Math.max(...shown.map(k=>agg[k].total))||1;
  const sx=v=>v/top*(W-P.l-P.r);
  let g="";
  const live=css("--seq"),out=css("--series-2");
  shown.forEach((k,i)=>{
    const a=agg[k],y=P.t+i*rowH,h=15;
    const wTotal=Math.max(1,sx(a.total)),wOut=sx(a.out);
    const rate=a.total?a.out/a.total*100:0;
    g+=`<text x="${P.l-10}" y="${y+h}" text-anchor="end">${esc(String(k).slice(0,12))}`
      +`<title>${esc(k)}</title></text>`;
    g+=`<rect x="${P.l}" y="${y+3}" width="${wTotal}" height="${h}" rx="3" fill="${live}" `
      +`fill-opacity=".85"><title>${esc(k)} · 옵션 ${a.total}개 · 판매 중 ${a.total-a.out}개</title></rect>`;
    if(wOut>0)g+=`<rect x="${P.l}" y="${y+3}" width="${Math.max(1,wOut)}" height="${h}" rx="3" `
      +`fill="${out}"><title>${esc(k)} · 품절 ${a.out}개 / ${a.total}개</title></rect>`;
    g+=`<text x="${P.l+wTotal+8}" y="${y+h}">${a.total}개 · 품절 ${a.out} `
      +`(${Math.round(rate)}%)</text>`;
  });
  g+=`<text class="axistitle" x="${P.l}" y="${H-6}">주황 = 품절 옵션 · 파랑 = 판매 중 옵션</text>`;
  svg.innerHTML=g;
  if(keys.length>shown.length)
    $("vexcl").textContent+=` — 옵션 많은 ${GROUP_CAP}개만 표시 (${keys.length-shown.length}종 생략)`;
}

// ── 시계열 ─────────────────────────────────────────────────────────────────
const TS=DATA.time_series;
const TSMETA={rank:{label:"순위",invert:true},price:{label:"판매가",invert:false},
  like:{label:"하트",invert:false},review:{label:"후기 수",invert:false}};
function movavg(arr,w){
  return arr.map((v,i)=>{if(v==null)return null;let s=0,c=0;
    for(let j=Math.max(0,i-w+1);j<=i;j++){if(arr[j]!=null){s+=arr[j];c++;}}
    return c?s/c:null;});
}
function renderTimeSeries(){
  if(!TS||!TS.series||!TS.series.length)return;
  const metric=$("ts_metric").value;
  const N=Math.max(1,Math.min(40,+$("ts_n").value||8));
  const ma=$("ts_ma").checked,meta=TSMETA[metric],stamps=TS.stamps;
  let ser=TS.series.filter(s=>S.keys.has(s.site+"/"+s.product_id))
    .filter(s=>s[metric].some(v=>v!=null));
  ser=ser.slice().sort((a,b)=>b[metric].filter(v=>v!=null).length-a[metric].filter(v=>v!=null).length)
    .slice(0,N);
  $("ts_info").textContent=`· 표시 ${ser.length}계열 (조건 내 축적 상품 중 상위, 전체 축적 ${TS.total_series}) · 시점 ${stamps.length}`;
  const svg=$("ts");svg.innerHTML="";
  if(!ser.length){$("ts_leg").innerHTML="";return;}
  const P={l:60,r:16,t:16,b:40},W=1040,H=360;
  const vals=[];ser.forEach(s=>{(ma?movavg(s[metric],3):s[metric]).forEach(v=>{if(v!=null)vals.push(v);});});
  let mn=Math.min(...vals),mx=Math.max(...vals);if(mn===mx){mn-=1;mx+=1;}
  const sx=i=>P.l+(stamps.length<=1?0.5:i/(stamps.length-1))*(W-P.l-P.r);
  const sy=v=>{const t=(v-mn)/(mx-mn);
    return meta.invert?P.t+t*(H-P.t-P.b):H-P.b-t*(H-P.t-P.b);};
  let g="";
  for(let k=0;k<=4;k++){const vv=mn+(mx-mn)*k/4,y=sy(vv);
    g+=`<line class="grid" x1="${P.l}" x2="${W-P.r}" y1="${y}" y2="${y}"/>`
      +`<text x="${P.l-8}" y="${y+4}" text-anchor="end">${fmt(Math.round(vv))}</text>`;}
  [0,Math.floor(stamps.length/4),Math.floor(stamps.length/2),
   Math.floor(stamps.length*3/4),stamps.length-1].filter((v,i,a)=>a.indexOf(v)===i).forEach(i=>{
    g+=`<text x="${sx(i)}" y="${H-P.b+16}" text-anchor="middle">${esc((stamps[i]||"").slice(5,16))}</text>`;});
  g+=`<text class="axistitle" x="${P.l}" y="${P.t-3}">${meta.label}`
    +`${meta.invert?" (위=1위)":""}${ma?" · 이동평균":""}</text>`;
  ser.forEach((s,si)=>{
    const arr=ma?movavg(s[metric],3):s[metric];
    const col=si<5?css(COL[si]):css("--muted");
    let d="",pen=false;
    arr.forEach((v,i)=>{if(v==null){pen=false;return;}
      d+=(pen?"L":"M")+sx(i)+" "+sy(v)+" ";pen=true;});
    g+=`<path d="${d}" fill="none" stroke="${col}" stroke-width="${si<5?2:1}" `
      +`stroke-opacity="${si<5?0.9:0.4}"/>`;
    if(si<5)arr.forEach((v,i)=>{if(v==null)return;
      g+=`<circle cx="${sx(i)}" cy="${sy(v)}" r="2.6" fill="${col}" data-tip="${
        esc(s.name+" · "+(stamps[i]||"").slice(0,16)+" · "+meta.label+" "+fmt(v)+(ma?" (이동평균)":""))}"/>`;});
  });
  svg.innerHTML=g;
  $("ts_leg").innerHTML=ser.slice(0,5).map((s,i)=>
    `<span><i style="background:${css(COL[i])}"></i>${esc((s.name||s.product_id).slice(0,20))}</span>`)
    .join("")+(ser.length>5?`<span class="sub">외 ${ser.length-5}계열(회색)</span>`:"");
}

// ── 상품 표 · 라인시트 ─────────────────────────────────────────────────────
// 표는 스토리 리포트와 같은 `table.grid`다 — 정렬·검색·묶어 보기가 공통 JS로 붙는다.
// **tbody 엘리먼트는 두고 안만 바꾼다** (핸들러가 살아 있어야 한다).
const COLS=__TABLE_COLS__;
function cellOf(d,f){
  if(f==="name")return d.url&&d.url!=="#"
    ?[`<a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.name||d.product_id)}</a>`,d.name||""]
    :[esc(d.name||d.product_id),d.name||""];
  if(f==="sold_out")return [d.sold_out?'<span class="tag tag-out">품절</span>':"",d.sold_out?1:0];
  if(f==="price_sale")return [d.price_sale==null?NA:escf(d.price_sale)+"원",d.price_sale==null?"":d.price_sale];
  if(f==="discount_rate")return [d.discount_rate==null?NA:d.discount_rate+"%",
    d.discount_rate==null?"":d.discount_rate];
  if(f==="opt_out_rate")return [d.opt_out_rate==null?NA:d.opt_out_rate+"%",
    d.opt_out_rate==null?"":d.opt_out_rate];
  const v=d[f];
  if(f==="site")return [esc(disp("site",v)),v||""];
  return [v==null||v===""?NA:esc(fmt(v)),v==null||v===""?"":v];
}
function fillTable(id,html){
  const t=$(id);if(!t)return;
  t.tBodies[0].innerHTML=html;
  const inp=document.querySelector('.filter[data-for="'+id+'"]');
  // 검색어가 걸려 있으면 새 행에도 다시 적용한다(공통 JS가 처리한다)
  if(inp)inp.dispatchEvent(new Event("input",{bubbles:true}));
  else document.dispatchEvent(new CustomEvent("tableview"));
}
function renderTable(){
  const rows=S.data.map(d=>"<tr>"+COLS.map(c=>{
    const [html,key]=cellOf(d,c[0]);
    return `<td class="${c[2]==="num"?"col-num":""}" data-k="${esc(key)}">${html}</td>`;
  }).join("")+"</tr>").join("");
  fillTable("t-items",rows);
}
const UNION=DATA.union;
function renderUnion(){
  if(!UNION)return;
  const sites=DATA.meta.sites;
  const keep=new Set(S.data.map(d=>DATA.items.indexOf(d)));
  const rows=[];
  UNION.forEach(u=>{
    const idxs=u.i.filter(i=>keep.has(i));
    if(!idxs.length)return;
    const by={};idxs.forEach(i=>{by[DATA.items[i].site]=DATA.items[i];});
    const present=sites.filter(s=>by[s]);
    // `단독`은 정확히 1곳일 때만이다 — 2곳이면 '단독'이 아니라 'N곳 입점'이다
    const label=present.length===1?disp("site",present[0])+" 단독"
      :present.length===sites.length?(sites.length===2?"양쪽 입점":"전 플랫폼 입점")
      :present.length+"곳 입점 ("+present.map(s=>disp("site",s)).join(" · ")+")";
    const rep=by[present[0]];
    let tds=`<td data-k="${esc(u.n)}">`+(rep.url?`<a href="${esc(rep.url)}" target="_blank" rel="noopener">${esc(u.n)}</a>`:esc(u.n))+`</td>`
      +`<td data-k="${esc(label)}"><span class="tag tag-presence${present.length===sites.length?" is-all":""}">${esc(label)}</span></td>`
      +`<td data-k="${esc(u.b||"")}">${esc(u.b||"")}</td>`
      +`<td data-k="${esc(u.c||"")}">${esc(u.c||"")}</td>`;
    sites.forEach(s=>{const it=by[s];
      tds+=`<td class="col-num" data-k="${it&&it.price_sale!=null?it.price_sale:""}">`
        +(it?(it.price_sale==null?NA:escf(it.price_sale)+"원"):NA)+`</td>`;});
    if(sites.length===2){
      const a=by[sites[0]],b=by[sites[1]];
      const gap=(a&&b&&a.price_sale!=null&&b.price_sale!=null)?a.price_sale-b.price_sale:null;
      tds+=`<td class="col-num" data-k="${gap==null?"":gap}">`
        +(gap==null?'<span class="na">비교 불가</span>'
          :gap===0?'<span class="na">동일</span>'
          :`<span class="tag tag-${gap<0?"off":"out"}">${gap>0?"+":"−"}${Math.abs(gap).toLocaleString("ko-KR")}</span>`)
        +`</td>`;}
    sites.forEach(s=>{const it=by[s];
      tds+=`<td class="col-num" data-k="${it&&it.like_count!=null?it.like_count:""}">`
        +(it?(it.like_count==null?NA:escf(it.like_count)):NA)+`</td>`;});
    rows.push("<tr>"+tds+"</tr>");
  });
  fillTable("t-union",rows.join(""));
}

// ── 가격 변경 사건 ──────────────────────────────────────────────────────────
let evSortCol="like_delta",evSortDir=-1;
const EV_COLS=[["name","상품"],["site","사이트"],["pct","변동"],["price_to","판매가"],
  ["window","관측 창"],["like_delta","하트 증분"],["review_delta","후기 증분"]];
function evFilters(){
  const down=$("ev_dir_down").classList.contains("is-on");
  const up=$("ev_dir_up").classList.contains("is-on");
  const posOnly=$("ev_like_pos").classList.contains("is-on");
  return DATA.price_events.map(e=>({...e,
      pct:e.price_from?(e.price_to-e.price_from)/e.price_from*100:0,
      window:`${e.from_at} ~ ${e.to_at}`}))
    .filter(e=>{const dir=e.pct<0?down:e.pct>0?up:(down||up);
      return dir&&(!posOnly||(e.like_delta!=null&&e.like_delta>0));});
}
function renderEvents(){
  if(!has("events")||!DATA.price_events.length)return;
  const rows=evFilters().sort((a,b)=>{
    let av=a[evSortCol],bv=b[evSortCol];
    if(av==null)return 1;if(bv==null)return -1;
    return (av<bv?-1:av>bv?1:0)*evSortDir;});
  $("ev_n").textContent=`· ${rows.length}건`;
  let h='<table class="grid"><thead><tr>'+EV_COLS.map(([f,l])=>{
    const num=["pct","price_to","like_delta","review_delta"].includes(f);
    return `<th class="${num?"col-num":""}" onclick="evSort('${f}')" style="cursor:pointer">${l} `
      +`${evSortCol===f?(evSortDir>0?"▲":"▼"):"↕"}</th>`;}).join("")+"</tr></thead><tbody>";
  rows.forEach(e=>{h+=`<tr><td>${esc((e.name||e.product_id).slice(0,36))}</td><td>${esc(e.site)}</td>`
    +`<td class="col-num" style="color:${e.pct<0?"var(--series-2)":"var(--series-1)"}">${e.pct.toFixed(1)}%</td>`
    +`<td class="col-num">${escf(e.price_from)}→${escf(e.price_to)}</td>`
    +`<td style="font-size:11px">${esc(e.window)}</td>`
    +`<td class="col-num">${e.like_delta>0?"+":""}${escf(e.like_delta)}</td>`
    +`<td class="col-num">${e.review_delta==null?"—":(e.review_delta>0?"+":"")+e.review_delta}</td></tr>`;});
  $("events").innerHTML=h+"</tbody></table>";
}
function evSort(f){if(evSortCol===f)evSortDir*=-1;else{evSortCol=f;evSortDir=-1;}renderEvents();}

// ── 시작 ───────────────────────────────────────────────────────────────────
function init(){
  const m=DATA.meta;
  const rows=[["관측 범위",`${m.obs_min||"—"} ~ ${m.obs_max||"—"}`],
    ["문맥",m.contexts.join(", ")],["플랫폼",m.sites.join(" · ")],
    ["상품",DATA.items.length.toLocaleString("ko-KR")+"개"],
    ["생성",m.generated_at+(m.generated_at==="TEMPLATE"?" · ⚠ 샘플 데이터(템플릿)":"")]];
  if(m.stock)rows.push(["옵션",`${m.stock.products.toLocaleString("ko-KR")}개 상품 · `
    +`${m.stock.options.toLocaleString("ko-KR")}개 옵션 (근거 ${m.stock.basis.join("/")||"unknown"})`]);
  if((m.proxies||[]).length)rows.push(["AI 판정 프록시",
    m.proxies.map(p=>`${p.name}: 판정 ${p.judged}·미판정 ${p.unjudged}`).join(" / ")+" (판정이지 사실이 아니다)"]);
  $("meta").innerHTML=rows.map(([k,v])=>
    `<div class="meta-item"><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("");

  // 모듈 토글 — 껐다 켜도 데이터는 그대로다(다시 만들 필요가 없다)
  document.querySelector(".modbar").addEventListener("change",e=>{
    const box=e.target.closest("[data-mod]");if(!box)return;
    const sec=document.querySelector(`section[data-mod="${box.dataset.mod}"]`);
    if(sec)sec.style.display=box.checked?"":"none";
  });

  const avail=AXES.filter(([f])=>DATA.items.some(d=>d[f]!=null)).map(a=>[a[0],a[1]]);
  (m.proxies||[]).filter(p=>p.numeric).forEach(p=>{
    const key="px_"+p.name;
    if(DATA.items.some(d=>d[key]!=null))avail.push([key,p.name+" (AI 판정)"]);});
  const opts=list=>list.map(([f,l])=>`<option value="${esc(f)}">${esc(l)}</option>`).join("");
  if(avail.length){
    ["ax","ay","hx","gm"].forEach(id=>{const el=$(id);if(!el)return;
      el.innerHTML=opts(avail);el.addEventListener("change",apply);});
    if($("ax"))$("ax").value=avail.some(a=>a[0]==="price_sale")?"price_sale":avail[0][0];
    if($("ay")){$("ay").value=avail.some(a=>a[0]==="like_count")?"like_count":avail[avail.length-1][0];
      if($("ay").value==="like_count")$("aylog").checked=true;}  // 하트는 자릿수를 넘나든다
    if($("hx"))$("hx").value=avail.some(a=>a[0]==="price_sale")?"price_sale":avail[0][0];
    if($("gm"))$("gm").value=avail.some(a=>a[0]==="price_sale")?"price_sale":avail[0][0];
    ["axlog","aylog"].forEach(id=>{const el=$(id);if(el)el.addEventListener("change",apply);});
  }
  // 그룹·색 기준 축 = 값이 2종 이상인 범주형만 (1종이면 가를 게 없다)
  const cats=[["site","플랫폼"],["category","카테고리"],["brand","브랜드"],["fit","핏"]]
    .concat((m.proxies||[]).filter(p=>!p.numeric).map(p=>["px_"+p.name,p.name+" (AI 판정)"]))
    .filter(([f])=>new Set(DATA.items.map(d=>d[f]).filter(v=>v!=null&&v!=="")).size>1);
  if($("gb")){
    if(cats.length){$("gb").innerHTML=opts(cats);$("gb").addEventListener("change",renderGroups);}
    else{const s=document.querySelector('section[data-mod="group"]');if(s)s.style.display="none";}
  }
  if($("acolor")){
    $("acolor").innerHTML='<option value="">없음 (한 가지 색)</option>'+opts(cats);
    if(cats.length)$("acolor").value=cats[0][0];
    $("acolor").addEventListener("change",renderScatter);
  }
  ["ts_metric","ts_n","ts_ma"].forEach(id=>{const e=$(id);
    if(e)e.addEventListener("input",renderTimeSeries);});
  ["ev_dir_down","ev_dir_up","ev_like_pos"].forEach(id=>{const el=$(id);
    if(el)el.addEventListener("click",()=>{el.classList.toggle("is-on");renderEvents();});});
  if($("vmode"))$("vmode").addEventListener("change",renderVariants);

  initFacets();
  renderEvents();
  apply();
}
init();
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
