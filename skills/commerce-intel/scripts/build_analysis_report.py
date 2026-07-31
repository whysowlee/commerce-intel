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

# 축 후보 — 구간 표기(view_count)는 제외한다 (오차 무한)
AXES = [
    ("price_sale", "판매가"), ("price_original", "정가"), ("discount_rate", "할인율(%)"),
    ("like_count", "하트"), ("review_count", "후기 수"), ("rating", "평점"),
    ("purchase_count", "누적판매*"), ("viewers_now", "보는 중(랭킹)"),
]


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
    # 페이로드 상한: 관측 시점 많은 순 120계열 (초과분은 리포트에 건수 명시)
    series.sort(key=lambda s: -sum(1 for v in s["rank"] if v is not None))
    ts_capped = len(series)
    series = series[:120]
    time_series = {"stamps": stamps, "series": series, "total_series": ts_capped,
                   "shown": len(series)} if series else None

    times = [i["observed_at"] for i in items if i["observed_at"]]
    meta = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "obs_min": min(times) if times else None, "obs_max": max(times) if times else None,
        "contexts": sorted({i["context"] for i in items}),
        "sites": sorted({i["site"] for i in items}),
        "proxies": proxies,
    }
    return {"meta": meta, "items": items, "price_events": events,
            "time_series": time_series}


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
}


def render(data):
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return HTML.replace("__PAYLOAD__", payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.environ.get("INTEL_DB", "data/intel.db"))
    ap.add_argument("--context", action="append", help="관측 문맥 필터 (반복 지정 가능)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--emit-template", action="store_true")
    ap.add_argument("--emit-json", action="store_true",
                    help="HTML 대신 데이터 JSON을 낸다 — AI 자유 생성 리포트의 결정적 데이터 소스")
    args = ap.parse_args()

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
    out.write_text(render(data), encoding="utf-8")
    n = len(data["items"])
    print(f"완료: {out} (상품 {n}개, 가격 변경 사건 {len(data['price_events'])}건)")


HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>가격-반응 탐색 대시보드</title>
<style>
.viz-root{color-scheme:light;
  --surface-1:#fcfcfb;--surface-2:#f1f0ee;--border:#dedcd6;
  --text-primary:#0b0b0b;--text-secondary:#52514e;--text-muted:#8a8880;
  --series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--warn-bg:#fdf3e0;}
@media (prefers-color-scheme: dark){:root:where(:not([data-theme="light"])) .viz-root{
  color-scheme:dark;--surface-1:#1a1a19;--surface-2:#242422;--border:#3a3936;
  --text-primary:#ffffff;--text-secondary:#c3c2b7;--text-muted:#8a8880;
  --series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--warn-bg:#3a2f1a;}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-1:#1a1a19;--surface-2:#242422;
  --border:#3a3936;--text-primary:#ffffff;--text-secondary:#c3c2b7;--text-muted:#8a8880;
  --series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--warn-bg:#3a2f1a;}
*{box-sizing:border-box}body{margin:0}
.viz-root{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif;
  background:var(--surface-1);color:var(--text-primary);padding:20px;min-height:100vh}
h1{font-size:19px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 8px}
.sub{color:var(--text-secondary);font-size:12.5px}
.notice{background:var(--surface-2);border:1px solid var(--border);border-radius:8px;
  padding:10px 14px;margin:12px 0;font-size:12.5px;color:var(--text-secondary)}
.notice b{color:var(--text-primary)}
details.notice>summary{cursor:pointer;list-style:none;color:var(--text-secondary)}
details.notice>summary::-webkit-details-marker{display:none}
details.notice .more{font-size:11px;color:var(--text-muted);font-weight:400}
details.notice[open] .more{display:none}
.filters{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start;margin:14px 0;
  padding:12px;background:var(--surface-2);border-radius:8px}
.filters label{display:block;font-size:11.5px;color:var(--text-secondary);margin-bottom:4px}
.filters select,.filters input{font:inherit;font-size:13px;background:var(--surface-1);
  color:var(--text-primary);border:1px solid var(--border);border-radius:6px;padding:4px 6px}
.faxis{max-width:340px}
.chips{display:flex;flex-wrap:wrap;gap:4px;max-height:96px;overflow:auto}
.chip{font:inherit;font-size:11.5px;cursor:pointer;background:var(--surface-1);
  color:var(--text-secondary);border:1px solid var(--border);border-radius:12px;padding:2px 9px}
.chip.on{background:var(--series-1);color:#fff;border-color:var(--series-1)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
.kpi{background:var(--surface-2);border-radius:8px;padding:10px 14px}
.kpi .v{font-size:21px;font-weight:700}.kpi .l{font-size:11.5px;color:var(--text-secondary)}
.panel{border:1px solid var(--border);border-radius:8px;padding:14px;margin:12px 0}
.axes{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin-bottom:8px;font-size:12.5px}
.excl{color:var(--text-muted);font-size:12px}
svg text{fill:var(--text-secondary);font-size:11px}
svg .grid{stroke:var(--border);stroke-width:1}
svg .axisline{stroke:var(--text-muted)}
.legend{display:flex;gap:14px;font-size:12px;color:var(--text-secondary);margin-top:4px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px}
#tip{position:fixed;pointer-events:none;background:var(--surface-2);border:1px solid var(--border);
  border-radius:6px;padding:6px 9px;font-size:12px;display:none;max-width:280px;z-index:9}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th{cursor:pointer;text-align:left;border-bottom:2px solid var(--border);padding:5px 8px;
  white-space:nowrap;color:var(--text-secondary)}
td{border-bottom:1px solid var(--border);padding:4px 8px}
td.num,th.num{text-align:right}tr:hover td{background:var(--surface-2)}
a{color:var(--series-1)}
.smalln{background:var(--warn-bg);border-radius:6px;padding:6px 10px;font-size:12px;
  display:none;margin:6px 0}
.tablewrap{max-height:480px;overflow:auto}
</style></head><body><div class="viz-root">
<h1>가격-반응 탐색 대시보드</h1>
<div class="sub" id="range"></div>
<details class="notice"><summary><b>이 도구는 가격-반응의 상관 탐색이지 엄밀한 가격 탄력성 추정이 아니다. 상관은 인과가 아니다.</b> <span class="more">주의사항 펼치기 ▾</span></summary>
· 여기 없는 것: 판매 종료된 상품, 순위권 밖 미관측 구간, 필터에 따라 품절 상품 (생존편향)<br>
· 축 조합을 훑다 발견한 패턴은 가설이지 결론이 아니다 — 다음 관측으로 재확인하라 (다중비교)<br>
· 전체 집합의 상관은 세그먼트(카테고리·핏·브랜드)로 나누면 사라지거나 뒤집힐 수 있다 — 아래 필터가 그 확인 장치다 (심슨의 역설)<br>
· *누적판매·하트는 누적값이라 출시 오래된 상품이 유리하다 · 축약 표기 파싱값은 ±4% 오차</details>
<div class="filters" id="filters"></div>
<div class="smalln" id="smalln">⚠ 표본이 작다 (n &lt; 30) — 이 조건의 패턴은 우연일 가능성이 크다</div>
<div class="kpis" id="kpis"></div>
<div class="panel"><h2 style="margin-top:0">산점도</h2>
<div class="axes">X <select id="ax"></select> <label style="display:inline"><input type="checkbox" id="axlog"> log</label>
 · Y <select id="ay"></select> <label style="display:inline"><input type="checkbox" id="aylog"> log</label>
 <span id="corr" class="sub"></span> <span class="excl" id="excl"></span></div>
<svg id="scatter" width="100%" height="430" viewBox="0 0 920 430"></svg>
<div class="legend" id="legend"></div>
<div class="sub">점을 클릭하면 상품 페이지가 열린다</div></div>
<div class="panel"><h2 style="margin-top:0">분포</h2>
<div class="axes"><select id="hx"></select> <span class="excl" id="hexcl"></span></div>
<svg id="hist" width="100%" height="240" viewBox="0 0 920 240"></svg></div>
<div class="panel"><h2 style="margin-top:0">상품 표</h2><div class="tablewrap" id="table"></div></div>
<div class="panel" id="tspanel" style="display:none"><h2 style="margin-top:0">시계열 추이 (축적 관측)</h2>
<div class="sub">같은 상품의 시점별 지표. <b>순위권 밖 구간은 선을 끊는다</b>(관측 없음 — 이어 그으면 없는 순위를 주장하는 것이다). 이동평균은 노이즈를 줄인 것이지 새 데이터가 아니다. 현재 필터가 적용된 상품 중 상위만 그린다.</div>
<div class="axes">지표 <select id="ts_metric"><option value="rank">순위</option><option value="price">판매가</option><option value="like">하트</option><option value="review">후기 수</option></select>
 · 계열 <input type="number" id="ts_n" value="8" min="1" max="40" style="width:56px">개
 <label style="display:inline"><input type="checkbox" id="ts_ma"> 이동평균</label>
 <span class="sub" id="ts_info"></span></div>
<svg id="ts" width="100%" height="360" viewBox="0 0 1040 360"></svg>
<div class="leg" id="ts_leg"></div></div>
<div class="panel" id="eventspanel"><h2 style="margin-top:0">가격 변경 사건 (관측 간 증분)</h2>
<div class="sub">같은 상품의 연속 관측에서 판매가가 바뀐 사건. 증분은 그 관측 창의 순변화다 — 창 안의 왕복은 잡히지 않는다.</div>
<div class="axes"><span>방향</span>
 <button type="button" class="chip on" id="ev_dir_down">인하</button>
 <button type="button" class="chip on" id="ev_dir_up">인상</button>
 <span style="margin-left:8px">하트 증분</span>
 <button type="button" class="chip" id="ev_like_pos">증가만</button>
 <span class="sub" id="ev_n"></span></div>
<div class="tablewrap" id="events"></div></div>
<div id="tip"></div>
<script type="application/json" id="data">__PAYLOAD__</script>
<script>
const DATA=JSON.parse(document.getElementById('data').textContent);
const AXES=[["price_sale","판매가"],["price_original","정가"],["discount_rate","할인율(%)"],
 ["like_count","하트"],["review_count","후기 수"],["rating","평점"],
 ["purchase_count","누적판매*"],["viewers_now","보는 중(랭킹)"]];
const SITECOLOR={}; const SLOTS=["--series-1","--series-2","--series-3"];
DATA.meta.sites.forEach((s,i)=>SITECOLOR[s]=SLOTS[Math.min(i,2)]); // 엔티티 고정 배색(3개 초과는 3번에 접힘)
const fmt=v=>v==null?"—":(typeof v==="number"?v.toLocaleString("ko-KR"):v);
const css=n=>getComputedStyle(document.querySelector('.viz-root')).getPropertyValue(n).trim();
const S={f:{}};
function uniq(field){return [...new Set(DATA.items.map(d=>d[field]).filter(v=>v!=null&&v!==""))].sort();}
const SEL={};   // 축id -> 켜진 값 Set (비어 있으면 전체 통과)
function countBy(field){const m={};DATA.items.forEach(d=>{const v=d[field];if(v!=null&&v!=="")m[v]=(m[v]||0)+1;});return m;}
// 다중 선택 칩 필터 — 한 축 안에서 여러 값을 켜면 OR, 축끼리는 AND (§4 리포트 공통)
function mkFilter(id,label,vals,counts){
  SEL[id]=new Set();
  const wrap=document.createElement('div');wrap.className='faxis';
  wrap.innerHTML=`<label>${label}</label>`;
  const box=document.createElement('div');box.className='chips';
  vals.forEach(v=>{
    const c=counts?(counts[v]||0):null;
    const chip=document.createElement('button');chip.type='button';chip.className='chip';
    chip.textContent=c!=null?`${v} ${c}`:v;
    chip.onclick=()=>{chip.classList.toggle('on');
      if(chip.classList.contains('on'))SEL[id].add(v);else SEL[id].delete(v);apply();};
    box.appendChild(chip);
  });
  wrap.appendChild(box);document.getElementById('filters').appendChild(wrap);
}
function initFilters(){
  const sc=countBy('site'),cc=countBy('category'),bc=countBy('brand'),fc=countBy('fit');
  mkFilter('site','플랫폼',DATA.meta.sites,sc);
  mkFilter('category','카테고리',uniq('category'),cc);
  if(uniq('fit').length)mkFilter('fit','핏',uniq('fit'),fc);
  mkFilter('brand','브랜드',uniq('brand'),bc);
  (DATA.meta.proxies||[]).filter(p=>!p.numeric).forEach(p=>{
    const key='px_'+p.name, vals=uniq(key), pc=countBy(key);
    if(vals.length)mkFilter(key,p.name+' (AI 판정)',vals.concat(['(미판정)']),
      {...pc,'(미판정)':DATA.items.filter(d=>d[key]==null).length});
  });
  const prices=DATA.items.map(d=>d.price_sale).filter(v=>v!=null);
  const w=document.createElement('div');
  w.innerHTML=`<label>판매가 범위</label><input id="f_pmin" type="number" placeholder="${Math.min(...prices)}" style="width:90px">
   ~ <input id="f_pmax" type="number" placeholder="${Math.max(...prices)}" style="width:90px">`;
  document.getElementById('filters').appendChild(w);
  w.querySelectorAll('input').forEach(i=>i.onchange=apply);
  const so=document.createElement('div');
  so.innerHTML=`<label>품절</label><select id="f_so"><option value="">포함</option><option value="0">판매 중만</option><option value="1">품절만</option></select>`;
  so.querySelector('select').onchange=apply;document.getElementById('filters').appendChild(so);
}
function filtered(){
  const g=id=>{const e=document.getElementById(id);return e?e.value:"";};
  const pmin=+g('f_pmin')||null,pmax=+g('f_pmax')||null,so=g('f_so');
  const axisOK=(id,val)=>{const s=SEL[id];return !s||s.size===0||s.has(val==null?'(미판정)':val);};
  const pxOK=d=>(DATA.meta.proxies||[]).every(p=>axisOK('px_'+p.name,d['px_'+p.name]));
  return DATA.items.filter(d=>
    axisOK('site',d.site)&&axisOK('category',d.category)&&
    axisOK('fit',d.fit)&&axisOK('brand',d.brand)&&
    (pmin==null||d.price_sale>=pmin)&&(pmax==null||d.price_sale<=pmax)&&
    (so===""||(so==="1")===!!d.sold_out)&&pxOK(d));
}
function median(a){if(!a.length)return null;const s=[...a].sort((x,y)=>x-y);return s[Math.floor(s.length/2)];}
function apply(){
  S.data=filtered();
  document.getElementById('smalln').style.display=S.data.length<30?'block':'none';
  renderKPIs();renderScatter();renderHist();renderTable();renderTimeSeries();
}
const TS=DATA.time_series;
const TSMETA={rank:{label:"순위",invert:true},price:{label:"판매가",invert:false},
  like:{label:"하트",invert:false},review:{label:"후기 수",invert:false}};
function movavg(arr,w){ // null 유지하며 관측된 값만 창 평균
  return arr.map((v,i)=>{if(v==null)return null;let s=0,c=0;
    for(let j=Math.max(0,i-w+1);j<=i;j++){if(arr[j]!=null){s+=arr[j];c++;}}
    return c?s/c:null;});
}
function renderTimeSeries(){
  const panel=document.getElementById('tspanel');
  if(!TS||!TS.series||!TS.series.length){panel.style.display='none';return;}
  panel.style.display='';
  const metric=document.getElementById('ts_metric').value;
  const N=Math.max(1,Math.min(40,+document.getElementById('ts_n').value||8));
  const ma=document.getElementById('ts_ma').checked;
  const meta=TSMETA[metric], stamps=TS.stamps;
  // 현재 필터에 든 상품만 (site+product_id 매칭)
  const keep=new Set(S.data.map(d=>d.site+'/'+d.product_id));
  let ser=TS.series.filter(s=>keep.has(s.site+'/'+s.product_id))
    .filter(s=>s[metric].some(v=>v!=null));
  // 관측 시점 많은 순 → 상위 N
  ser=ser.slice().sort((a,b)=>b[metric].filter(v=>v!=null).length-a[metric].filter(v=>v!=null).length).slice(0,N);
  document.getElementById('ts_info').textContent=
    `· 표시 ${ser.length}계열 (필터 내 축적 상품 중 상위, 전체 축적 ${TS.total_series}) · 시점 ${stamps.length}`;
  const svg=document.getElementById('ts');svg.innerHTML="";
  if(!ser.length){document.getElementById('ts_leg').innerHTML="";return;}
  const P={l:56,r:16,t:14,b:40},W=1040,H=360;
  const vals=[];ser.forEach(s=>{(ma?movavg(s[metric],3):s[metric]).forEach(v=>{if(v!=null)vals.push(v);});});
  let mn=Math.min(...vals),mx=Math.max(...vals);if(mn===mx){mn-=1;mx+=1;}
  const sx=i=>P.l+(stamps.length<=1?0.5:i/(stamps.length-1))*(W-P.l-P.r);
  const sy=v=>{const t=(v-mn)/(mx-mn);return meta.invert? P.t+t*(H-P.t-P.b) : H-P.b-t*(H-P.t-P.b);};
  let g="";
  for(let k=0;k<=4;k++){const vv=mn+(mx-mn)*k/4,y=sy(vv);
    g+=`<line class="grid" x1="${P.l}" x2="${W-P.r}" y1="${y}" y2="${y}"/><text x="${P.l-8}" y="${y+4}" text-anchor="end">${fmt(Math.round(vv))}</text>`;}
  // 시간축 라벨 (5개)
  [0,Math.floor(stamps.length/4),Math.floor(stamps.length/2),Math.floor(stamps.length*3/4),stamps.length-1]
    .filter((v,i,a)=>a.indexOf(v)===i).forEach(i=>{
      g+=`<text x="${sx(i)}" y="${H-P.b+16}" text-anchor="middle">${(stamps[i]||'').slice(5,16)}</text>`;});
  g+=`<text x="${P.l}" y="${P.t-2}" font-size="10">${meta.label}${meta.invert?' (위=1위)':''}${ma?' · 이동평균':''}</text>`;
  const COL=["--series-1","--series-2","--series-3"];
  ser.forEach((s,si)=>{
    const arr=ma?movavg(s[metric],3):s[metric];
    const col=si<5?css(COL[si%3]):css('--text-muted');
    // 연속 관측 구간만 선으로 잇는다(순위권 밖 = null은 끊는다)
    let d="",pen=false;
    arr.forEach((v,i)=>{if(v==null){pen=false;return;}
      d+=(pen?"L":"M")+sx(i)+" "+sy(v)+" ";pen=true;});
    g+=`<path d="${d}" fill="none" stroke="${col}" stroke-width="${si<5?2:1}" stroke-opacity="${si<5?0.9:0.4}"/>`;
  });
  svg.innerHTML=g;
  document.getElementById('ts_leg').innerHTML=ser.slice(0,5).map((s,i)=>
    `<span><i style="background:${css(COL[i%3])}"></i>${(s.name||s.product_id).slice(0,20)}</span>`).join("")
    +(ser.length>5?`<span class="sub">외 ${ser.length-5}계열(회색)</span>`:"");
}
['ts_metric','ts_n','ts_ma'].forEach(id=>{const e=document.getElementById(id);
  if(e)e.addEventListener('input',renderTimeSeries);});
function renderKPIs(){
  const d=S.data,ps=d.map(x=>x.price_sale).filter(v=>v!=null);
  const dr=d.map(x=>x.discount_rate).filter(v=>v!=null);
  document.getElementById('kpis').innerHTML=[
    ["n (현재 조건)",d.length.toLocaleString()],["중위 판매가",fmt(median(ps))],
    ["중위 할인율",dr.length?median(dr)+"%":"—"],
    ["품절",d.filter(x=>x.sold_out).length.toLocaleString()+"건"]]
    .map(([l,v])=>`<div class="kpi"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");
}
function scale(vals,lo,hi,log){
  // 로그 스케일은 0·음수를 1로 클램프한다 — 0이 축 밖으로 떨어지는 것을 막는다
  const cl=v=>log?Math.max(v,1):v;
  const mn=cl(Math.min(...vals)),mx=cl(Math.max(...vals));
  if(log){const l=Math.log10;const a=l(mn),b=l(mx);
    return v=>b===a?(lo+hi)/2:lo+(l(cl(v))-a)/(b-a)*(hi-lo);}
  return v=>mx===mn?(lo+hi)/2:lo+(v-mn)/(mx-mn)*(hi-lo);
}
function tickfmt(v){return v>=100?Math.round(v).toLocaleString("ko-KR"):(Math.round(v*100)/100).toLocaleString("ko-KR");}
function ticks(vals,n,log){const mn=log?Math.max(Math.min(...vals),1):Math.min(...vals),mx=Math.max(...vals);
  if(log){const out=[];let p=Math.pow(10,Math.floor(Math.log10(mn)));
    while(p<=mx){if(p>=mn)out.push(p);p*=10;}return out.length?out:[mn,mx];}
  const out=[];for(let i=0;i<=n;i++)out.push(mn+(mx-mn)*i/n);return out;}
function pearson(xs,ys){const n=xs.length;if(n<3)return null;
  const mx=xs.reduce((a,b)=>a+b)/n,my=ys.reduce((a,b)=>a+b)/n;
  let sxy=0,sx=0,sy=0;for(let i=0;i<n;i++){sxy+=(xs[i]-mx)*(ys[i]-my);sx+=(xs[i]-mx)**2;sy+=(ys[i]-my)**2;}
  return sx&&sy?sxy/Math.sqrt(sx*sy):null;}
const tip=document.getElementById('tip');
function showTip(ev,html){tip.innerHTML=html;tip.style.display='block';
  tip.style.left=Math.min(ev.clientX+12,innerWidth-300)+'px';tip.style.top=(ev.clientY+12)+'px';}
function renderScatter(){
  const xf=document.getElementById('ax').value,yf=document.getElementById('ay').value;
  const xlog=document.getElementById('axlog').checked,ylog=document.getElementById('aylog').checked;
  const pts=S.data.filter(d=>d[xf]!=null&&d[yf]!=null);
  const excl=S.data.length-pts.length;
  document.getElementById('excl').textContent=excl?`· 제외 ${excl}건(미노출)`:"";
  const svg=document.getElementById('scatter');svg.innerHTML="";
  if(!pts.length){document.getElementById('corr').textContent="";return;}
  const P={l:70,r:20,t:14,b:34},W=920,H=430;
  const sx=scale(pts.map(d=>d[xf]),P.l,W-P.r,xlog),sy=scale(pts.map(d=>d[yf]),H-P.b,P.t,ylog);
  let g="";
  ticks(pts.map(d=>d[yf]),5,ylog).forEach(t=>{const y=sy(t);
    g+=`<line class="grid" x1="${P.l}" x2="${W-P.r}" y1="${y}" y2="${y}"/><text x="${P.l-8}" y="${y+4}" text-anchor="end">${tickfmt(t)}</text>`;});
  ticks(pts.map(d=>d[xf]),6,xlog).forEach(t=>{const x=sx(t);
    g+=`<text x="${Math.min(x,W-P.r-30)}" y="${H-P.b+18}" text-anchor="middle">${tickfmt(t)}</text>`;});
  g+=`<line class="axisline" x1="${P.l}" x2="${W-P.r}" y1="${H-P.b}" y2="${H-P.b}"/>`;
  svg.innerHTML=g;
  pts.forEach((d,i)=>{
    const c=document.createElementNS('http://www.w3.org/2000/svg','circle');
    c.setAttribute('cx',sx(d[xf]));c.setAttribute('cy',sy(d[yf]));c.setAttribute('r',4.5);
    c.setAttribute('fill',css(SITECOLOR[d.site]||'--series-3'));c.setAttribute('fill-opacity',.75);
    c.setAttribute('stroke',css('--surface-1'));c.setAttribute('stroke-width',1);
    c.style.cursor='pointer';
    const lbl=f=>{const a=AXES.find(a=>a[0]===f);return a?a[1]:f.replace('px_','')+' (AI)';};
    c.onmousemove=ev=>showTip(ev,`<b>${d.name||d.product_id}</b><br>${d.brand||""} · ${d.site}<br>`+
      `${lbl(xf)}: ${fmt(d[xf])} · ${lbl(yf)}: ${fmt(d[yf])}`);
    c.onmouseleave=()=>tip.style.display='none';
    c.onclick=()=>{if(d.url&&d.url!=="#")window.open(d.url);};
    svg.appendChild(c);});
  const r=pearson(pts.map(d=>d[xf]),pts.map(d=>d[yf]));
  document.getElementById('corr').textContent=r==null?"":`r = ${r.toFixed(2)} (n=${pts.length}, 탐색용 — 인과 아님)`;
  document.getElementById('legend').innerHTML=DATA.meta.sites.length>1?
    DATA.meta.sites.map(s=>`<span><i style="background:${css(SITECOLOR[s])}"></i>${s}</span>`).join(""):"";
}
function renderHist(){
  const f=document.getElementById('hx').value;
  const vals=S.data.map(d=>d[f]).filter(v=>v!=null);
  const excl=S.data.length-vals.length;
  document.getElementById('hexcl').textContent=excl?`제외 ${excl}건(미노출)`:"";
  const svg=document.getElementById('hist');svg.innerHTML="";if(!vals.length)return;
  const P={l:70,r:20,t:10,b:30},W=920,H=240,NB=24;
  const mn=Math.min(...vals),mx=Math.max(...vals),bw=(mx-mn)/NB||1;
  const bins=Array(NB).fill(0);
  vals.forEach(v=>bins[Math.min(NB-1,Math.floor((v-mn)/bw))]++);
  const top=Math.max(...bins),bwpx=(W-P.l-P.r)/NB;
  let g=`<line class="axisline" x1="${P.l}" x2="${W-P.r}" y1="${H-P.b}" y2="${H-P.b}"/>`;
  bins.forEach((b,i)=>{const h=b/top*(H-P.t-P.b);
    g+=`<rect x="${P.l+i*bwpx+1}" y="${H-P.b-h}" width="${bwpx-2}" height="${h}" rx="3"
      fill="${css('--series-1')}" fill-opacity=".8"><title>${fmt(Math.round(mn+i*bw))}~${fmt(Math.round(mn+(i+1)*bw))}: ${b}건</title></rect>`;});
  [0,NB/2,NB].forEach(i=>{g+=`<text x="${P.l+i*bwpx}" y="${H-P.b+16}" text-anchor="middle">${fmt(Math.round(mn+i*bw))}</text>`;});
  const md=median(vals),mdx=P.l+((md-mn)/(mx-mn||1))*(W-P.l-P.r);
  g+=`<line x1="${mdx}" x2="${mdx}" y1="${P.t}" y2="${H-P.b}" stroke="${css('--text-muted')}" stroke-dasharray="4 3"/>
      <text x="${mdx+4}" y="${P.t+10}">중위 ${fmt(md)}</text>`;
  svg.innerHTML=g;
}
const COLS=[["name","상품"],["brand","브랜드"],["category","카테고리"],["fit","핏"],
 ["site","플랫폼"],["price_sale","판매가"],["discount_rate","할인율"],["like_count","하트"],
 ["review_count","후기"],["rating","평점"],["sold_out","품절"]];
let sortCol="like_count",sortDir=-1;
function renderTable(){
  const d=[...S.data].sort((a,b)=>{
    const av=a[sortCol],bv=b[sortCol];
    if(av==null)return 1;if(bv==null)return -1;   // 값 없는 행은 방향 무관 뒤로
    return (av<bv?-1:av>bv?1:0)*sortDir;});
  let h="<table><thead><tr>"+COLS.map(([f,l])=>
    `<th class="num" onclick="sortBy('${f}')">${l} ${sortCol===f?(sortDir>0?"▲":"▼"):"↕"}</th>`).join("")+"</tr></thead><tbody>";
  d.forEach(r=>{h+="<tr>"+COLS.map(([f])=>{
    let v=r[f];
    if(f==="name")v=r.url&&r.url!=="#"?`<a href="${r.url}" target="_blank" rel="noopener">${r.name||r.product_id}</a>`:(r.name||r.product_id);
    else if(f==="sold_out")v=r.sold_out?"품절":"";
    else v=fmt(v);
    return `<td class="${f==='name'?'':'num'}">${v}</td>`;}).join("")+"</tr>";});
  document.getElementById('table').innerHTML=h+"</tbody></table>";
}
function sortBy(f){if(sortCol===f)sortDir*=-1;else{sortCol=f;sortDir=-1;}renderTable();}
let evSortCol="like_delta",evSortDir=-1;
const EV_COLS=[["name","상품"],["site","사이트"],["pct","변동"],["price_to","판매가"],
  ["window","관측 창"],["like_delta","하트 증분"],["review_delta","후기 증분"]];
function evFilters(){
  const down=document.getElementById('ev_dir_down').classList.contains('on');
  const up=document.getElementById('ev_dir_up').classList.contains('on');
  const posOnly=document.getElementById('ev_like_pos').classList.contains('on');
  return DATA.price_events.map(e=>({...e,
      pct:e.price_from?(e.price_to-e.price_from)/e.price_from*100:0,
      window:`${e.from_at} ~ ${e.to_at}`})).filter(e=>{
    const dir=e.pct<0?down:e.pct>0?up:(down||up);
    return dir && (!posOnly || (e.like_delta!=null&&e.like_delta>0));});
}
function renderEvents(){
  if(!DATA.price_events.length){document.getElementById('eventspanel').style.display='none';return;}
  const rows=evFilters().sort((a,b)=>{
    let av=a[evSortCol],bv=b[evSortCol];
    if(av==null)return 1;if(bv==null)return -1;
    return (av<bv?-1:av>bv?1:0)*evSortDir;});
  document.getElementById('ev_n').textContent=`· ${rows.length}건`;
  let h="<table><thead><tr>"+EV_COLS.map(([f,l])=>{
    const num=["pct","price_to","like_delta","review_delta"].includes(f);
    return `<th class="${num?'num':''}" onclick="evSort('${f}')" style="cursor:pointer">${l} ${evSortCol===f?(evSortDir>0?"▲":"▼"):"↕"}</th>`;}).join("")+"</tr></thead><tbody>";
  rows.forEach(e=>{h+=`<tr><td>${(e.name||e.product_id).slice(0,36)}</td><td>${e.site}</td>
    <td class="num" style="color:${e.pct<0?'var(--series-2)':'var(--series-1)'}">${e.pct.toFixed(1)}%</td>
    <td class="num">${fmt(e.price_from)}→${fmt(e.price_to)}</td>
    <td style="font-size:11px">${e.window}</td>
    <td class="num">${e.like_delta>0?'+':''}${fmt(e.like_delta)}</td>
    <td class="num">${e.review_delta==null?'—':(e.review_delta>0?'+':'')+e.review_delta}</td></tr>`;});
  document.getElementById('events').innerHTML=h+"</tbody></table>";
}
function evSort(f){if(evSortCol===f)evSortDir*=-1;else{evSortCol=f;evSortDir=-1;}renderEvents();}
['ev_dir_down','ev_dir_up','ev_like_pos'].forEach(id=>{
  const el=document.getElementById(id);
  if(el)el.onclick=()=>{el.classList.toggle('on');renderEvents();};});
function init(){
  const m=DATA.meta;
  const pxsum=(m.proxies||[]).map(p=>`${p.name}: 판정 ${p.judged}·미판정 ${p.unjudged}`).join(" / ");
  if(pxsum){const el=document.createElement('div');el.className='sub';
    el.textContent='AI 판정 프록시 — '+pxsum+' (판정이지 사실이 아니다)';
    document.getElementById('range').after(el);}
  document.getElementById('range').textContent=
    `관측 범위 ${m.obs_min||"—"} ~ ${m.obs_max||"—"} · 문맥: ${m.contexts.join(", ")} · 생성 ${m.generated_at}`+
    (m.generated_at==="TEMPLATE"?" · ⚠ 샘플 데이터(템플릿)":"");
  const avail=AXES.filter(([f])=>DATA.items.some(d=>d[f]!=null));
  (DATA.meta.proxies||[]).filter(p=>p.numeric).forEach(p=>{
    const key='px_'+p.name;
    if(DATA.items.some(d=>d[key]!=null))avail.push([key,p.name+' (AI 판정)']);});
  for(const id of['ax','ay','hx']){
    document.getElementById(id).innerHTML=avail.map(([f,l])=>`<option value="${f}">${l}</option>`).join("");
    document.getElementById(id).onchange=apply;}
  document.getElementById('ax').value='price_sale';
  document.getElementById('ay').value=avail.some(a=>a[0]==='like_count')?'like_count':avail[0][0];
  document.getElementById('hx').value='price_sale';
  const lk=document.getElementById('aylog');
  if(document.getElementById('ay').value==='like_count')lk.checked=true;  // 하트는 자릿수를 넘나든다
  document.getElementById('axlog').onchange=apply;lk.onchange=apply;
  initFilters();renderEvents();apply();
}
init();
</script></div></body></html>
"""

if __name__ == "__main__":
    main()
