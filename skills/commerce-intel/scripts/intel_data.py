#!/usr/bin/env python3
"""데이터 층 — 정본 DB에서 분석용 데이터를 뽑는다 (D29 파이프라인 5단 분리).

`build_analysis_report.py`(HTML 대시보드)에서 **계산 로직만** 떼어 왔다. 렌더 층은
D27로 폐기됐지만 이 부분은 산출 형식과 무관하다 — 상품별 최신 관측, 가격 변경 사건,
시계열, 옵션 재고, 플랫폼 합집합을 만드는 일은 HTML이든 PDF든 똑같다.

이 모듈은 **DB만 안다.** EDA·분석·인사이트가 이걸 공유해서 먹는다. 리포트를 모르고,
어떤 통계를 돌릴지도 모른다.

    from intel_data import collect
    data = collect("data/intel.db", ["brand:로우클래식"])
    data["items"]         # 상품별 최신 관측 + 정적 속성
    data["price_events"]  # 가격 변경 사건 (관측 창 증분 포함)
    data["time_series"]   # 시점 2개 이상 관측된 상품
    data["union"]         # 플랫폼 합집합 (2개 이상일 때만)
    data["meta"]["stock"] # 옵션(사이즈) 재고 요약
"""
import json
import re
import sqlite3
import statistics as st
from datetime import datetime

# ── 동일 상품 매칭 ──────────────────────────────────────────────────────────
# 규칙은 하나뿐이다: **정규화 상품명 완전일치.** 유사도·가격 보조 매칭은 금지다 —
# 인사일런스 실측에서 유사도 0.82↑ 후보 11건 중 대부분이 다른 상품이었고,
# `레터링 그래픽 티셔츠`와 `스탠실 그래픽 티셔츠`는 정가까지 66,000원으로 같아
# **가격이 오탐을 확증**했다(EVIDENCE §5). 그래서 가격도 신호로 못 쓴다.
#
# 원래 build_report.py가 갖고 있던 것을 여기로 옮겼다 — 배포 패키지에서 HTML
# 생성기를 빼면서 데이터 층이 렌더 층에 의존할 수 없게 됐기 때문이다(D27).

# 괄호/대괄호 안은 매칭에서 뺀다 — "[2 PACK]", "(단독)", "(5 COLORS)" 같은 유통·옵션
# 표기가 사이트마다 다르게 붙는다. 무신사 `REYA LACE TOP (5 COLORS)`와 자사몰
# `REYA LACE TOP (PINK)`가 같은 상품으로 묶이는 것이 이 규칙 덕분이다(MD 인터뷰 확인).
BRACKETS_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
# 한글·영숫자만 남긴다. 공백·`_`·`/`·하이픈은 사이트마다 다르게 넣는다.
NON_WORD_RE = re.compile(r"[^0-9a-z가-힣]")

# ── 괄호 안이 시즌이면 지우지 않는다 (2026-08-03 실측) ─────────────────────
# 괄호 제거는 색상·유통 표기를 걷어내려는 규칙인데, 시즌 표기까지 지워서
# `(FW23) T-Logo LT Hoodie`와 `(FW24) T-Logo LT Hoodie`가 **같은 상품이 됐다.**
# 서로 다른 시즌 상품은 다른 상품이다.
#
# FW/SS/SP/AW 문자를 **필수**로 요구한다 — 숫자만 보면 상품코드 `[0157]`을 시즌으로
# 오인한다(실측에서 실제로 걸렸다).
#
# 실측 효과: 플랫폼 간 매칭 688건 → 688건(손실 0) · 사이트 내 잘못된 병합 40건 감소.
# 공짜다.
SEASON_RE = re.compile(r"^(?:(?:FW|SS|SP|AW)\s?\d{2,4}|\d{2,4}\s?(?:FW|SS|SP|AW))$", re.I)


def match_key(name):
    """동일 상품 판정에 쓰는 정규화 상품명. 이 키가 같을 때만 같은 상품이다.

    괄호 안은 지우되 **시즌 표기는 키에 남긴다.** 색상 변형은 묶이고(무신사
    `REYA LACE TOP (5 COLORS)` = 자사몰 `REYA LACE TOP (PINK)`), 시즌 변형은
    갈라진다(`(FW23) X` ≠ `(FW24) X`).
    """
    if not name:
        return ""
    kept = []

    def _repl(m):
        inner = m.group(0).strip("[]()").strip()
        if SEASON_RE.match(inner):
            kept.append(inner.upper().replace(" ", ""))
        return " "

    base = NON_WORD_RE.sub("", BRACKETS_RE.sub(_repl, str(name).lower()))
    return base + ("|" + "|".join(sorted(kept)) if kept else "")


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
        key = match_key(it.get("name")) or "\x00%s\x00%s" % (it["site"], it["product_id"])
        row = rows.get(key)
        if row is None:
            row = {"n": it.get("name") or it["product_id"], "c": it.get("category"),
                   "b": it.get("brand"), "i": []}
            rows[key] = row
            order.append(key)
        row["i"].append(idx)
    return [rows[k] for k in order]


def product_series(db_path, contexts):
    """상품별 **전체** 관측 시퀀스. collect()의 time_series와 다르다.

    collect()는 화면에 그릴 목적이라 48시점으로 다운샘플하고 120계열로 자른다.
    이 함수는 **검정용**이라 자르지 않는다 — 사건 전후 구간을 세려면 실제 관측이
    빠짐없이 있어야 하고, 다운샘플된 계열로 이중차분을 하면 없는 구간을 만들어낸다.

    반환: {(site, product_id): [{observed_at, price_sale, like_count, review_count,
                                 sold_out, rank}, ...]}  — 시각 오름차순
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    where, params = "", []
    if contexts:
        where = " WHERE context IN (%s)" % ",".join("?" * len(contexts))
        params = list(contexts)
    rows = conn.execute(
        "SELECT site, product_id, observed_at, price_sale, like_count, review_count, "
        "sold_out, rank FROM observations%s ORDER BY site, product_id, observed_at"
        % where, params).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out.setdefault((r["site"], r["product_id"]), []).append(dict(r))
    return out


# ══════════════════════════════════════════════════════════════════════════
# 스타일 : 변형 2계층 (2026-08-03 — 사용자 지적 #7)
# ══════════════════════════════════════════════════════════════════════════
# 플랫폼마다 **한 상품을 세는 단위가 다르다.** 무신사는 `REYA LACE TOP (5 COLORS)`
# 한 줄로 올리고 자사몰은 색상마다 따로 올린다. 그대로 세면 자사몰 카탈로그가 몇 배로
# 부풀고, 플랫폼 비교의 n이 서로 다른 기준으로 세어진다.
#
# 실측(2026-08-03): 사이트 안에서 같은 스타일로 묶이는 상품이 4,350건.
# 그중 **843개 그룹은 변형끼리 가격이 다르다** — 대표 하나만 쓰면 그 차이가 사라진다.
#
# 그래서 단위를 **명시적으로 고른다**:
#   variant(상품) — 재고·품절·개별 가격. 기본값
#   style(스타일)  — 플랫폼 비교·카탈로그 폭. 대표값은 **중앙값**(첫 건이 아니다)


def style_groups(items):
    """(site, style_key) → 그 스타일의 변형 목록. 같은 사이트 안에서만 묶는다."""
    groups = {}
    for it in items:
        key = match_key(it.get("name"))
        if not key:
            key = "\x00%s\x00%s" % (it["site"], it["product_id"])
        groups.setdefault((it["site"], key), []).append(it)
    return groups


def style_rows(items, numeric_fields=None):
    """상품 목록을 **스타일 단위**로 접는다.

    수치는 변형들의 **중앙값**을 쓴다. 첫 건을 쓰면 어느 색이 먼저 수집됐느냐에 따라
    값이 달라져 재현되지 않고, 평균을 쓰면 한 색만 세일 중일 때 끌려간다.

    `variant_n`(변형 수)과 `variant_price_spread`(변형 간 가격 폭)를 함께 남긴다 —
    접었다는 사실과 접으면서 버린 정보량을 리포트가 밝힐 수 있어야 한다.
    """
    numeric_fields = numeric_fields or [
        "price_original", "price_sale", "discount_rate", "review_count", "rating",
        "purchase_count", "like_count", "viewers_now", "opt_out_rate", "opt_total"]
    out = []
    for (site, key), members in style_groups(items).items():
        rep = dict(members[0])
        rep["style_key"] = key
        rep["variant_n"] = len(members)
        for f in numeric_fields:
            vals = [m[f] for m in members if m.get(f) is not None]
            rep[f] = st.median(vals) if vals else None
        prices = [m["price_sale"] for m in members if m.get("price_sale") is not None]
        rep["variant_price_spread"] = (max(prices) - min(prices)) if len(prices) > 1 else 0
        # 하나라도 재고가 있으면 그 스타일은 살아 있다 — 전부 품절일 때만 품절이다
        so = [m["sold_out"] for m in members if m.get("sold_out") is not None]
        rep["sold_out"] = (1 if all(so) else 0) if so else None
        out.append(rep)
    return out


def matched_pairs(items, metric, unit="style"):
    """플랫폼 간 **같은 상품** 쌍. 쌍체 비교용.

    독립 두 집단으로 플랫폼을 비교하면 상품 구성 차이가 섞인다 — 한쪽에 비싼 아우터가
    많으면 "그 플랫폼이 비싸다"가 나온다. 같은 상품끼리 짝지으면 그 교란이 사라진다.

    매칭은 정규화 상품명 완전일치(match_key)뿐이다 — 리포트 전체가 쓰는 규칙 하나.

    **기본 단위는 스타일이다.** 상품 단위로 짝지으면 색상 변형이 많은 쪽이 같은 쌍을
    여러 번 밀어 넣어 쌍이 부풀고, 순열검정의 n이 실제 독립 관측 수보다 커진다
    (유사 반복 — p값이 실제보다 작게 나온다).

    반환: {(siteA, siteB): [(valueA, valueB, name), ...]}
    """
    rows = style_rows(items) if unit == "style" else items
    by_key = {}
    for it in rows:
        if it.get(metric) is None:
            continue
        key = it.get("style_key") or match_key(it.get("name"))
        if not key:
            continue
        by_key.setdefault(key, {}).setdefault(it["site"], it)
    pairs = {}
    for key, per_site in by_key.items():
        sites = sorted(per_site)
        for i in range(len(sites)):
            for j in range(i + 1, len(sites)):
                a, b = sites[i], sites[j]
                pairs.setdefault((a, b), []).append(
                    (per_site[a][metric], per_site[b][metric],
                     per_site[a].get("name") or key))
    return pairs
