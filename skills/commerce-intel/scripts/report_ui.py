#!/usr/bin/env python3
"""리포트 공통 UI — 스토리 리포트와 분석 대시보드가 **같은 조작 규약**을 쓰게 하는 곳.

두 생성기는 데이터 출처가 다르다(원본 JSON vs 정본 DB). 그래서 그리는 방식까지 같을 수는
없지만, **읽는 사람이 배우는 규약은 하나여야 한다** — 리포트를 옮겨 다닐 때마다 필터가
칩이었다 셀렉트였다 하면 그건 도구가 두 개인 것이다.

여기 있는 것 (양쪽이 그대로 공유):
  · 팔레트와 색 토큰 — 라이트/다크, 계열 5색 (dataviz 검증값)
  · 컴포넌트 CSS — 칩·패싯·표·툴팁·KPI·배너·섹션·범례
  · 툴팁 JS — `data-tip` 위임 방식(즉시 표시. `title` 속성은 1초 넘게 걸린다)
  · 불리언 수식 컴파일러 — 플랫폼/값을 AND·OR·NOT·괄호로 조합
  · 표 정렬·칩 필터·계층 캐스케이드 JS (DOM 기반)

여기 없는 것 (구조가 달라 공유가 불가능한 것):
  · 대시보드의 `filteredData` 재계산 파이프라인 — DOM을 숨기는 게 아니라 데이터를 다시
    거른 뒤 전 차트를 다시 그린다. **술어(predicate)는 공유하되 적용 방식은 각자다.**
    수식 컴파일러를 함수로 뽑아 둔 이유가 이것이다 — 양쪽이 같은 문법을 같은 의미로 읽는다.
"""

# ── dataviz 기본 팔레트 (검증 통과값) ────────────────────────────────────────
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]
SEQ_LIGHT = "#2a78d6"
SEQ_DARK = "#3987e5"

# 계열 색은 5개다. 그 이상은 색을 늘리지 않고 **형태를 바꾼다**(작은 배수·회색 처리) —
# 6번째 색부터는 사람이 범례와 선을 짝짓지 못한다.
SERIES_MAX = 5

# 마커 모양 — 색만으로 계열을 구분하지 않기 위한 두 번째 신호(적록색약 8%).
MARK_SHAPES = ["circle", "tri", "sq", "dia", "cross"]

_LIGHT = {"seq": SEQ_LIGHT, "s": SERIES_LIGHT}
_DARK = {"seq": SEQ_DARK, "s": SERIES_DARK}


def _block(lines, pad):
    return "".join("%s%s\n" % (pad, line) for line in lines)


def _light(pad):
    return _block([
        "color-scheme: light;",
        "--page: #f9f9f7; --surface: #fcfcfb;",
        "--text: #0b0b0b; --text-2: #52514e; --muted: #898781;",
        "--grid: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);",
        "--seq: %s;" % SEQ_LIGHT,
        "--series-1: %s; --series-2: %s; --series-3: %s;" % tuple(SERIES_LIGHT[:3]),
        "--series-4: %s; --series-5: %s;" % tuple(SERIES_LIGHT[3:]),
        "--good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;",
        "--up: #006300; --down: #d03b3b;",
    ], pad)


def _dark(pad):
    # 다크에서는 상승색만 바꾼다 — 어두운 배경에서 #006300은 거의 검정으로 읽힌다.
    return _block([
        "color-scheme: dark;",
        "--page: #0d0d0d; --surface: #1a1a19;",
        "--text: #ffffff; --text-2: #c3c2b7; --muted: #898781;",
        "--grid: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);",
        "--seq: %s;" % SEQ_DARK,
        "--series-1: %s; --series-2: %s; --series-3: %s;" % tuple(SERIES_DARK[:3]),
        "--series-4: %s; --series-5: %s;" % tuple(SERIES_DARK[3:]),
        "--up: #0ca30c;",
    ], pad)


def theme_tokens():
    """색 토큰. 라이트가 기본이고 다크는 **시스템 설정과 `data-theme` 둘 다** 받는다.

    두 리포트가 이 함수 하나를 쓴다 — 팔레트를 각자 적어 두면 한쪽만 고쳐진다.
    """
    return (
        "\n:root {\n" + _light("  ") + "}\n"
        "@media (prefers-color-scheme: dark) {\n"
        "  :root:not([data-theme=\"light\"]) {\n" + _dark("    ") + "  }\n}\n"
        ":root[data-theme=\"dark\"] {\n" + _dark("  ") + "}\n"
    )


# ── 컴포넌트 CSS ────────────────────────────────────────────────────────────
# 두 리포트가 같은 클래스 이름과 같은 생김새를 쓴다. 여기 없는 것(스토리3 스파크라인 등)만
# 각 생성기가 자기 CSS에 덧붙인다.
COMPONENT_CSS = r"""
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 20px 96px;
  background: var(--page); color: var(--text);
  font: 15px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 26px; margin: 0 0 14px; letter-spacing: -0.01em; }
h2 { font-size: 18px; margin: 0 0 12px; }
h3 { font-size: 14px; margin: 0 0 8px; color: var(--text-2); font-weight: 600; }
h4 { font-size: 14px; margin: 0 0 6px; }
a { color: inherit; text-decoration: none; }
a:hover { text-decoration: underline; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.9em;
  background: var(--grid); padding: 1px 5px; border-radius: 4px; }

header { background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 20px 22px; margin-bottom: 20px; }
.meta { display: flex; flex-wrap: wrap; gap: 8px 32px; margin: 0; }
.meta-item dt { font-size: 12px; color: var(--muted); margin: 0; }
.meta-item dd { margin: 2px 0 0; font-size: 14px; font-variant-numeric: tabular-nums; }

.banner { margin-top: 14px; padding: 10px 14px; border-radius: 8px; font-size: 14px;
  border-left: 3px solid var(--muted); background: var(--page); }
.banner-critical { border-left-color: var(--critical); }
.banner-warning { border-left-color: var(--warning); }
.banner-info { border-left-color: var(--series-1); }

.kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px; margin-bottom: 24px; }
.kpi { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px; }
.kpi-label { font-size: 12px; color: var(--muted); }
.kpi-value { font-size: 26px; font-weight: 600; letter-spacing: -0.02em; margin-top: 2px; }
.kpi-note { font-size: 12px; color: var(--text-2); margin-top: 2px; }

section { background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 20px 22px; margin-bottom: 20px; }
.section-note { font-size: 13px; color: var(--muted); margin: -6px 0 14px; }
details.section-note > summary { cursor: pointer; list-style: none; font-size: 12px;
  color: var(--muted); width: fit-content; }
details.section-note > summary::-webkit-details-marker { display: none; }
details.section-note[open] > summary { margin-bottom: 6px; }
.empty { color: var(--muted); font-size: 14px; margin: 4px 0; }

/* auto-fill이어야 블록이 1개일 때 트랙이 합쳐지지 않는다.
   auto-fit은 빈 트랙을 접어서 520px 차트를 컨테이너 폭까지 늘려 버린다 — 옆 섹션과
   크기가 안 맞는 원인이었다(2026-07-30). */
.chart-grid { display: grid; gap: 26px;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 440px), 1fr)); }
.chart-block { min-width: 0; }
/* 그리드 밖에 홀로 놓이는 차트(추이 등)도 본문 폭까지 늘어나지 않게 묶는다. */
.chart-solo { max-width: 880px; }
.mini-list { list-style: none; margin: 4px 0 0; padding: 0; }
.mini-list li { padding: 5px 0; border-bottom: 1px solid var(--grid); font-size: 14px; }
.mini-list li:last-child { border-bottom: 0; }
.mini-list strong { font-variant-numeric: tabular-nums; margin-right: 8px; }
.chart { width: 100%; height: auto; overflow: visible; }
.bar { fill: var(--seq); }
.bar:hover { fill-opacity: 0.82; }
/* 0 기준 좌우 막대의 음수 쪽. 색은 보조 신호다 — 방향과 부호 표기가 본 신호다. */
.bar-neg { fill: var(--series-2); }
.bar-label { font-size: 11px; fill: var(--text-2); }
.bar-value { font-size: 11px; fill: var(--text-2); font-variant-numeric: tabular-nums; }
/* 막대 안에 들어가는 값 라벨 — 채운 막대 위라 흰색이어야 읽힌다 */
.bar-value-in { fill: #fff; font-weight: 600; }
.axis { font-size: 10px; fill: var(--muted); font-variant-numeric: tabular-nums; }
/* 축이 무엇을 재는지·단위가 무엇인지는 생략하지 않는다 (report-spec 차트 규칙) */
.chart-axis-note { font-size: 11px; color: var(--muted); margin: 4px 0 0; }
.grid { stroke: var(--grid); stroke-width: 1; }
.baseline { stroke: var(--baseline); stroke-width: 1; }
.line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.dot { stroke: var(--surface); stroke-width: 2; }
.series-label { font-size: 11px; fill: var(--text-2); }

.table-tools { display: flex; align-items: center; gap: 12px; margin-bottom: 10px;
  flex-wrap: wrap; }
/* 계층 묶어 보기 — 표가 길면 집계 행만 보고 필요한 품목만 편다 */
.group-tool { display: inline-flex; align-items: center; gap: 6px; font-size: 12px;
  color: var(--muted); }
.group-level { font: inherit; font-size: 13px; padding: 5px 8px; border-radius: 7px;
  border: 1px solid var(--border); background: var(--page); color: var(--text); }
tr.group-row > td.group-head { background: var(--page); cursor: pointer;
  border-bottom: 1px solid var(--baseline); padding: 9px 10px; user-select: none; }
tr.group-row:hover > td.group-head { background: var(--grid); }
.group-caret { display: inline-block; width: 14px; color: var(--muted); }
.group-count { margin-left: 8px; font-size: 12px; color: var(--muted);
  font-variant-numeric: tabular-nums; }
.group-agg { margin-left: 14px; font-size: 12px; color: var(--text-2);
  font-variant-numeric: tabular-nums; }
.group-agg b { font-weight: 600; }
.group-miss { margin-left: 4px; font-style: normal; font-size: 11px; color: var(--muted); }
tr.in-group > td:first-child { border-left: 2px solid var(--grid); }
.filter { flex: 0 1 300px; padding: 7px 10px; font: inherit; font-size: 13px;
  border: 1px solid var(--border); border-radius: 7px;
  background: var(--page); color: var(--text); }
.table-count { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }

/* 다중 선택 필터 — 여러 개를 켜면 OR, 서로 다른 그룹끼리는 AND로 걸린다. */
.facets { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
.facet-group { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px; }
.facet-expr { align-items: center; }
.facet-expr .expr-chips { display: inline-flex; flex-wrap: wrap; gap: 4px; }
.chip.expr-op { font-weight: 600; color: var(--seq); }
.chip.expr-ins { background: color-mix(in srgb, var(--seq) 10%, transparent); }
.expr-input { flex: 1; min-width: 240px; font: inherit; font-size: 13px; padding: 4px 8px;
  border: 1px solid var(--grid); border-radius: 6px; background: var(--surface); color: var(--text); }
.expr-status { font-size: 12px; color: var(--muted); }
.expr-status.err { color: var(--down); }
.expr-hint { display: inline-flex; width: 15px; height: 15px; align-items: center;
  justify-content: center; border-radius: 50%; background: var(--grid); color: var(--muted);
  font-size: 10px; cursor: help; }
.facet-group.cascade-collapsed .chip:not(.chip-all) { display: none; }
.facet-group.cascade-collapsed::after {
  content: "상위를 고르면 나옵니다"; font-size: 11px; color: var(--muted); }
.facet-label { font-size: 12px; color: var(--muted); margin-right: 4px;
  flex: 0 0 auto; min-width: 52px; }
.chip { font: inherit; font-size: 12px; padding: 3px 9px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--page); color: var(--text-2);
  cursor: pointer; white-space: nowrap; }
.chip:hover { color: var(--text); border-color: var(--baseline); }
.chip.is-on { background: var(--seq); border-color: var(--seq); color: #fff; }
.facet-mode { margin-left: 6px; padding: 1px 6px; border-radius: 8px;
  background: var(--grid); color: var(--muted); font-size: 11px; font-weight: 600; }
.chip em { font-style: normal; opacity: 0.65; margin-left: 4px;
  font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }
table.grid { border-collapse: collapse; width: 100%; font-size: 13px; }
table.grid th, table.grid td { padding: 8px 10px; text-align: left;
  border-bottom: 1px solid var(--grid); vertical-align: middle; }
table.grid thead th { position: sticky; top: 0; background: var(--surface);
  font-size: 12px; color: var(--text-2); font-weight: 600; white-space: nowrap; z-index: 1; }
/* 정렬 가능한 열은 눌러 보기 전에도 눌리는 줄 알아야 한다 — 중립 화살표를 늘 띄운다. */
table.grid th[data-sort]:not([data-sort="none"]) { cursor: pointer; user-select: none; }
table.grid th[data-sort]:not([data-sort="none"])::after {
  content: " \2195"; font-size: 10px; opacity: 0.3; }
table.grid th[data-sort]:not([data-sort="none"]):hover { color: var(--text); }
table.grid th[data-sort]:not([data-sort="none"]):hover::after { opacity: 0.7; }
table.grid th.sorted-asc::after { content: " \25B2"; font-size: 9px; opacity: 1; }
table.grid th.sorted-desc::after { content: " \25BC"; font-size: 9px; opacity: 1; }
/* 이름만으로 뜻이 안 통하는 열은 점선 밑줄로 표시하고 hover에 풀이를 붙인다 */
.th-tip { text-decoration: underline dotted; text-decoration-color: var(--muted);
  text-underline-offset: 3px; cursor: help; }

/* 스토리3 — 표 안 스파크라인. 그리기는 인라인 JS가 하고 여기선 자리와 크기만 잡는다. */
.col-spark { width: 124px; }
.col-spark .vs { white-space: nowrap; }
.col-pick { width: 30px; text-align: center; }
.spark { display: block; width: 104px; height: 24px; }
.spark svg { display: block; width: 100%; height: 100%; overflow: visible; }
.spark-line { fill: none; stroke: var(--seq); stroke-width: 1.6;
  stroke-linejoin: round; stroke-linecap: round; }
.spark-dot { fill: var(--seq); }
.spark-band { fill: color-mix(in srgb, var(--critical) 9%, transparent); }
.spark-empty { fill: var(--muted); }
.chart-strip { height: 54px; }
.dot-warn { fill: var(--warning) !important; }

/* 추이 차트 조작부 */
.trend-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 18px;
  margin-bottom: 14px; }
.trend-field { display: inline-flex; align-items: center; gap: 6px; font-size: 13px;
  color: var(--text-2); }
.trend-n { width: 72px; padding: 5px 8px; font: inherit; font-size: 13px;
  border: 1px solid var(--border); border-radius: 7px;
  background: var(--page); color: var(--text); }
.trend-mode { padding: 5px 8px; font: inherit; font-size: 13px;
  border: 1px solid var(--border); border-radius: 7px;
  background: var(--page); color: var(--text); }
.trend-check { gap: 5px; }
.trend-status { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.trend-host { min-height: 40px; }
.panel-grid { display: grid; gap: 18px;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr)); }
.panel { min-width: 0; }
.panel h4 { font-size: 12px; margin: 0 0 2px; font-weight: 600; }
.panel h4 a { color: inherit; }
.panel .panel-note { font-size: 11px; color: var(--muted); margin: 0 0 4px;
  font-variant-numeric: tabular-nums; }
.axis-right { fill: var(--series-2); }
.price-line { fill: none; stroke: var(--series-2); stroke-width: 1.6;
  stroke-dasharray: 4 2; stroke-linejoin: round; }
.detail-row > td { background: var(--page); padding: 14px 16px; }
tr.is-open { background: var(--page); }
table.grid tbody tr:hover { background: var(--page); }
.col-num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.col-name { min-width: 240px; max-width: 380px; }
.col-img { width: 64px; }
.thumb { width: 52px; height: 66px; object-fit: cover; border-radius: 5px;
  background: var(--grid); display: block; }
.thumb-missing { background: repeating-linear-gradient(45deg, var(--grid),
  var(--grid) 4px, var(--surface) 4px, var(--surface) 8px); }
.na { color: var(--muted); font-size: 12px; }
.col-variant { text-align: left; white-space: normal; min-width: 150px; }
.vopts { display: flex; flex-wrap: wrap; gap: 3px; }
.vopt { font-size: 11px; padding: 1px 6px; border-radius: 5px; background: var(--grid);
  color: var(--text); white-space: nowrap; }
.vopt sub { color: var(--muted); font-size: 9px; margin-left: 2px; vertical-align: baseline; }
.vopt-out { text-decoration: line-through; color: var(--muted); opacity: 0.7; }
.approx { color: var(--text-2); font-size: 12px; }
.price-sale { font-weight: 600; }
.price-was { color: var(--muted); text-decoration: line-through; font-size: 12px; }
.tag { display: inline-block; font-size: 11px; padding: 1px 6px; border-radius: 4px;
  background: var(--grid); color: var(--text-2); margin-left: 4px; white-space: nowrap; }
.tag-out { background: var(--grid); color: var(--text-2); }
.tag-off { background: color-mix(in srgb, var(--critical) 16%, transparent); color: var(--critical); }
.tag-stay { background: color-mix(in srgb, var(--seq) 14%, transparent); color: var(--seq); }
.tag-in { background: color-mix(in srgb, var(--good) 16%, transparent); color: var(--up); }
.tag-out2 { background: color-mix(in srgb, var(--critical) 14%, transparent); color: var(--down); }
.tag-mid { background: var(--grid); color: var(--muted); }
.delta-up { color: var(--up); } .delta-down { color: var(--down); } .delta-flat { color: var(--muted); }
.arrow { color: var(--muted); }
/* 규모 대비 반응 지수 — 1.0이 '규모만큼'이다. 과대/과소만 표시하고 색으로 단정하지 않는다. */
.idx { font-variant-numeric: tabular-nums; }
.idx-over { color: var(--up); font-weight: 600; }
.idx-under { color: var(--down); }
.vs { display: block; font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }

/* 입점 칼럼 — 어느 플랫폼에 있는 상품인지. 색으로 우열을 매기지 않는다(사실 표기다). */
.col-presence { white-space: nowrap; }
.tag-presence { display: inline-block; font-size: 11px; padding: 2px 7px; border-radius: 10px;
  background: var(--grid); color: var(--text-2); white-space: nowrap; margin: 0; }
/* 전 플랫폼 입점만 채워서 구분한다. 단독 입점은 결함이 아니라 유통 사실이라 중립색이다. */
.tag-presence.is-all { background: color-mix(in srgb, var(--seq) 18%, transparent);
  color: var(--seq); font-weight: 600; }

/* 품목 대응 근거 — 통합축을 독자가 검증할 수 있게 접어서 싣는다. */
details.axis-map { margin-top: 10px; }
details.axis-map > summary { cursor: pointer; font-size: 13px; color: var(--text-2);
  user-select: none; }
details.axis-map ul { margin: 8px 0 0; padding-left: 18px; columns: 2; column-gap: 28px;
  font-size: 12px; color: var(--text-2); }
details.axis-map li { margin: 2px 0; break-inside: avoid; }
details.axis-map code { font-size: 11px; background: var(--grid); padding: 1px 4px;
  border-radius: 3px; }
@media (max-width: 720px) { details.axis-map ul { columns: 1; } }

/* 시점별 원자료 — 통합 표 아래에 접어 둔다. */
details.raw { margin-top: 14px; border-top: 1px solid var(--grid); padding-top: 10px; }
details.raw > summary { cursor: pointer; font-size: 13px; color: var(--text-2);
  user-select: none; }
details.raw > summary:hover { color: var(--text); }
details.raw > summary::marker { color: var(--muted); }
details.raw .table-wrap { margin-top: 10px; }

/* 순위 추이 위에 겹치는 할인 시점 마커.
   점선 = 시점 확정, 띠 = 순위권 밖이라 구간으로만 아는 사건. */
.event-line { stroke-width: 1.5; stroke-dasharray: 3 3; opacity: 0.75; }
.event-band { opacity: 0.10; }
.event-tick { font-size: 9px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; margin: 8px 0 0;
  font-size: 12px; color: var(--text-2); }
.legend-item { display: inline-flex; align-items: center; gap: 6px; }
.legend-swatch { width: 22px; height: 0; border-top: 2px solid currentColor; flex: 0 0 auto; }
.legend-swatch.dashed { border-top-style: dashed; }
.legend-swatch.band { height: 11px; border-top: 0;
  background: color-mix(in srgb, currentColor 20%, transparent); }
/* 계열 범례 — 이름은 본문 색으로 읽고 색은 스와치만 쓴다 */
.legend-series { gap: 4px 18px; }
.legend-series .legend-name { color: var(--text-2); }

#tip { position: fixed; z-index: 20; pointer-events: none; opacity: 0;
  transition: opacity .1s; background: var(--text); color: var(--surface);
  font-size: 12px; padding: 5px 9px; border-radius: 6px; max-width: 260px; }
footer { max-width: 1180px; margin: 0 auto; color: var(--muted); font-size: 12px; }
"""


# ── 공통 JS ─────────────────────────────────────────────────────────────────
# `data-tip` 툴팁 · 표 정렬 · 칩 다중선택 · 불리언 수식 · 계층 캐스케이드.
# 원시 문자열이다 — 안의 정규식 이스케이프가 JS로 그대로 나가야 한다.
COMMON_JS = r"""
(function () {
  var tip = document.getElementById('tip');
  document.addEventListener('mouseover', function (e) {
    var t = e.target.closest('[data-tip]');
    if (!t) { tip.style.opacity = 0; return; }
    tip.textContent = t.getAttribute('data-tip');
    tip.style.opacity = 1;
  });
  document.addEventListener('mousemove', function (e) {
    if (tip.style.opacity === '0' || tip.style.opacity === '') return;
    var x = e.clientX + 14, y = e.clientY + 14;
    if (x + tip.offsetWidth > window.innerWidth - 8) x = e.clientX - tip.offsetWidth - 14;
    if (y + tip.offsetHeight > window.innerHeight - 8) y = e.clientY - tip.offsetHeight - 14;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  });

  document.querySelectorAll('table.grid thead th[data-sort]').forEach(function (th) {
    if (th.dataset.sort === 'none') return;
    th.addEventListener('click', function () {
      var table = th.closest('table');
      var idx = Array.prototype.indexOf.call(th.parentNode.children, th);
      var asc = !th.classList.contains('sorted-asc');
      table.querySelectorAll('th').forEach(function (o) {
        o.classList.remove('sorted-asc', 'sorted-desc');
      });
      th.classList.add(asc ? 'sorted-asc' : 'sorted-desc');
      var body = table.tBodies[0];
      var rows = Array.prototype.slice.call(body.rows).filter(function (r) {
        return !r.classList.contains('detail-row');   // 펼친 상세는 정렬 대상이 아니다
      });
      body.querySelectorAll('tr.detail-row').forEach(function (r) { r.remove(); });
      body.querySelectorAll('tr.is-open').forEach(function (r) { r.classList.remove('is-open'); });
      var numeric = th.dataset.sort === 'num';
      rows.sort(function (a, b) {
        var x = a.cells[idx].dataset.k, y = b.cells[idx].dataset.k;
        if (x === '' && y === '') return 0;
        if (x === '') return 1;           // 값 없는 행은 방향과 무관하게 뒤로
        if (y === '') return -1;
        if (numeric) return asc ? x - y : y - x;
        return asc ? x.localeCompare(y, 'ko') : y.localeCompare(x, 'ko');
      });
      rows.forEach(function (r) { body.appendChild(r); });
      // 정렬이 바뀌면 '표 순서대로 상위 N'을 따르는 차트도 다시 그려야 한다
      document.dispatchEvent(new CustomEvent('tableview'));
    });
  });

  // 텍스트 검색과 칩 필터를 한 곳에서 합쳐 적용한다.
  // 같은 그룹의 칩 여러 개 = OR, 서로 다른 그룹 = AND.
  var state = {};   // tableId -> {q: '', picked: {facetIdx: Set}}

  function stateOf(id) {
    if (!state[id]) state[id] = { q: '', picked: {}, mode: {}, expr: {} };
    return state[id];
  }

  // 입점 수식 — 플랫폼 이름을 AND·OR·NOT·괄호로 조합. 행의 입점 집합에 대해 평가한다.
  // shunting-yard로 RPN을 만들고 행마다 평가한다(NOT>AND>OR, 괄호). 오류면 null(필터 미적용).
  function compileExpr(src, plats) {
    if (!src.trim()) return { rpn: null };
    var names = plats.slice().sort(function (a, b) { return b.length - a.length; });
    var s = src, i = 0, toks = [];
    while (i < s.length) {
      var c = s[i];
      if (/\s/.test(c)) { i++; continue; }
      if (c === '(' || c === ')') { toks.push(c); i++; continue; }
      var rest = s.slice(i);
      var op = /^(AND|OR|NOT)\b/i.exec(rest) || /^(&&|\|\||&|\||!)/.exec(rest);
      if (op) {
        var o = op[0].toUpperCase();
        o = (o === '&&' || o === '&') ? 'AND' : (o === '||' || o === '|') ? 'OR' : (o === '!') ? 'NOT' : o;
        toks.push(o); i += op[0].length; continue;
      }
      var found = null;
      for (var k = 0; k < names.length; k++) {
        if (rest.slice(0, names[k].length).toLowerCase() === names[k].toLowerCase()) { found = names[k]; break; }
      }
      if (found) { toks.push({ p: found }); i += found.length; continue; }
      return { error: '알 수 없는 항목: "' + rest.slice(0, 14) + '"' };
    }
    var prec = { NOT: 3, AND: 2, OR: 1 }, out = [], ops = [];
    for (var t = 0; t < toks.length; t++) {
      var tk = toks[t];
      if (typeof tk === 'object') out.push(tk);
      else if (tk === '(') ops.push(tk);
      else if (tk === ')') {
        while (ops.length && ops[ops.length - 1] !== '(') out.push(ops.pop());
        if (!ops.length) return { error: '괄호가 안 맞습니다' };
        ops.pop();
      } else {
        while (ops.length && ops[ops.length - 1] !== '(' &&
               prec[ops[ops.length - 1]] >= prec[tk] && tk !== 'NOT') out.push(ops.pop());
        ops.push(tk);
      }
    }
    while (ops.length) { var op2 = ops.pop(); if (op2 === '(') return { error: '괄호가 안 맞습니다' }; out.push(op2); }
    return { rpn: out };
  }
  // 수식 컴파일러는 분석 대시보드도 쓴다 — 같은 문법을 같은 뜻으로 읽어야 한다.
  // 스토리 리포트는 행의 **입점 플랫폼 집합**에, 대시보드는 상품의 **값 집합**(플랫폼·
  // 카테고리·핏·브랜드·프록시·품절)에 평가한다. 평가 대상만 다르고 규칙은 하나다.
  window.reportExpr = { compile: compileExpr, eval: evalRpn };

  function evalRpn(rpn, present) {
    var st = [];
    for (var i = 0; i < rpn.length; i++) {
      var t = rpn[i];
      if (typeof t === 'object') st.push(present.has(t.p));
      else if (t === 'NOT') st.push(!st.pop());
      else if (t === 'AND') { var b = st.pop(), a = st.pop(); st.push(a && b); }
      else if (t === 'OR') { var b2 = st.pop(), a2 = st.pop(); st.push(a2 || b2); }
    }
    return st.length === 1 ? !!st[0] : true;
  }

  function apply(id) {
    var table = document.getElementById(id);
    if (!table) return;
    var st = stateOf(id);
    var counter = document.getElementById(id + '-count');
    var shown = 0;
    var lastHidden = false;
    Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
      // 펼친 상세는 바로 위 행에 딸린 것이다 — 부모가 걸러지면 같이 숨는다.
      // (안 그러면 필터에서 빠진 상품의 차트만 표에 남는다)
      if (row.classList.contains('detail-row')) { row.hidden = lastHidden; return; }
      var hit = !st.q || row.textContent.toLowerCase().indexOf(st.q) !== -1;
      if (hit) {
        for (var idx in st.picked) {
          var picked = st.picked[idx];
          if (!picked || !picked.length) continue;
          var have = row.getAttribute('data-f' + idx) || '';
          // 축 기본은 OR. 입점 축처럼 data-match="all"인 축만 AND다.
          var test = function (v) { return have.indexOf('|' + v + '|') !== -1; };
          var ok = st.mode[idx] === 'all' ? picked.every(test) : picked.some(test);
          if (!ok) { hit = false; break; }
        }
      }
      if (hit) {   // 입점 수식 축
        for (var eidx in st.expr) {
          var rpn = st.expr[eidx];
          if (!rpn) continue;
          var raw = row.getAttribute('data-f' + eidx) || '';
          var present = new Set(raw.split('|').filter(Boolean));
          if (!evalRpn(rpn, present)) { hit = false; break; }
        }
      }
      row.hidden = !hit;
      lastHidden = !hit;
      if (hit) shown++;
    });
    if (counter) counter.textContent = shown + '행';
    document.dispatchEvent(new CustomEvent('tableview'));
  }

  document.querySelectorAll('.filter').forEach(function (input) {
    var id = input.dataset.for;
    if (!document.getElementById(id)) return;
    input.addEventListener('input', function () {
      stateOf(id).q = input.value.trim().toLowerCase();
      apply(id);
    });
  });

  // 입점 수식 축 — 삽입 칩·연산자 버튼·텍스트 입력을 잇는다.
  document.querySelectorAll('.facet-group.facet-expr').forEach(function (group) {
    var id = group.dataset.for, idx = group.dataset.facet;
    var input = group.querySelector('.expr-input');
    var status = group.querySelector('.expr-status');
    var plats = Array.prototype.map.call(group.querySelectorAll('.expr-ins'),
      function (b) { return b.dataset.ins; });
    function recompile() {
      var res = compileExpr(input.value, plats);
      if (res.error) { status.textContent = '⚠ ' + res.error; status.className = 'expr-status err';
        stateOf(id).expr[idx] = null; }
      else { stateOf(id).expr[idx] = res.rpn;
        status.textContent = res.rpn ? '유효' : ''; status.className = 'expr-status'; }
      apply(id);
    }
    group.addEventListener('click', function (e) {
      var b = e.target.closest('[data-ins]');
      if (!b) return;
      if (b.classList.contains('expr-clear')) { input.value = ''; }
      else {
        var v = b.dataset.ins, pos = input.selectionStart || input.value.length;
        input.value = input.value.slice(0, pos) + v + input.value.slice(input.selectionEnd || pos);
        input.focus();
      }
      recompile();
    });
    input.addEventListener('input', recompile);
  });

  var pending = {};
  document.querySelectorAll('.facet-group:not(.facet-expr)').forEach(function (group) {
    var id = group.dataset.for;
    var idx = group.dataset.facet;
    var all = group.querySelector('.chip-all');
    if (group.dataset.match === 'all') stateOf(id).mode[idx] = 'all';
    group.addEventListener('click', function (e) {
      var chip = e.target.closest('.chip');
      if (!chip) return;
      if (chip === all) {
        group.querySelectorAll('.chip').forEach(function (c) { c.classList.remove('is-on'); });
        all.classList.add('is-on');
      } else {
        chip.classList.toggle('is-on');
        var on = group.querySelectorAll('.chip:not(.chip-all).is-on');
        all.classList.toggle('is-on', on.length === 0);
      }
      var picked = [];
      group.querySelectorAll('.chip:not(.chip-all).is-on').forEach(function (c) {
        picked.push(c.dataset.v);
      });
      stateOf(id).picked[idx] = picked;
      apply(id);
    });
    // 서버가 미리 켜 둔 칩(기본 필터)을 로드 시점에 실제로 적용한다.
    var preset = [];
    group.querySelectorAll('.chip:not(.chip-all).is-on').forEach(function (c) {
      preset.push(c.dataset.v);
    });
    if (preset.length) {
      stateOf(id).picked[idx] = preset;
      pending[id] = true;
    }
  });
  Object.keys(pending).forEach(apply);

  // 계층 카테고리 캐스케이드: 대분류 → 중분류 → 소분류. 상위를 골라야 하위 칩이 나온다.
  // 하위 칩은 data-parent가 바로 위 단계의 선택값과 맞을 때만 보인다. 체인으로 전파한다.
  (function () {
    var chains = {};   // (for|ckey) → { level: groupEl }
    document.querySelectorAll('.facet-group[data-ckey]').forEach(function (g) {
      var k = g.dataset.for + '|' + g.dataset.ckey;
      (chains[k] = chains[k] || {})[+g.dataset.clevel] = g;
    });
    Object.keys(chains).forEach(function (k) {
      var byLevel = chains[k];
      var levels = Object.keys(byLevel).map(Number).sort(function (a, b) { return a - b; });
      function selVals(g) {
        var out = [];
        g.querySelectorAll('.chip:not(.chip-all).is-on').forEach(function (c) { out.push(c.dataset.v); });
        return out;
      }
      function sync() {
        for (var i = 1; i < levels.length; i++) {
          var parent = byLevel[levels[i - 1]], child = byLevel[levels[i]];
          var sel = selVals(parent);
          var anyParent = sel.length > 0;
          child.classList.toggle('cascade-collapsed', !anyParent);
          child.querySelectorAll('.chip:not(.chip-all)').forEach(function (c) {
            var show = anyParent && sel.indexOf(c.dataset.parent) >= 0;
            c.style.display = show ? '' : 'none';
            if (!show && c.classList.contains('is-on')) c.click();  // 끄며 상태·필터 갱신
          });
        }
      }
      levels.forEach(function (L) {
        byLevel[L].addEventListener('click', function () { setTimeout(sync, 0); });
      });
      sync();
    });
  })();
})();

// ── 계층 묶어 보기 ─────────────────────────────────────────────────────────
// 수천 행짜리 표는 훑을 수가 없다. 대/중/소 분류로 접어 **집계 행만** 먼저 보고 필요한
// 품목만 펼친다. 집계는 **지금 필터를 통과한 행만** 센다 — 화면의 수와 표의 수가 다르면
// 그건 거짓말이다. 정렬·필터가 바뀌면 다시 묶는다(tableview 이벤트).
// 대시보드는 표를 다시 그리므로(`tbody.innerHTML` 교체) 여기서 붙인 핸들러가 살아 있어야
// 한다 — **tbody 엘리먼트 자체는 두고 안만 바꾸는 것**이 두 리포트의 공통 규약이다.
window.initGroupViews = function () {
  document.querySelectorAll('table.grid[data-groupby]').forEach(function (table) {
    if (table.__grouped) return;
    table.__grouped = true;
    var cfg;
    try { cfg = JSON.parse(table.dataset.groupby); } catch (e) { return; }
    var sel = document.querySelector('.group-level[data-for="' + table.id + '"]');
    if (!sel) return;
    var body = table.tBodies[0];
    var open = {};        // 펼쳐 둔 그룹 (다시 그려도 유지된다)
    var busy = false;

    function prefix(v, depth) {
      var parts = String(v == null || v === '' ? '(없음)' : v).split(' > ');
      return parts.slice(0, depth).join(' > ');
    }
    function numOf(row, col) {
      var c = row.cells[col];
      if (!c) return null;
      var k = c.dataset.k;
      return k === '' || k == null || isNaN(k) ? null : +k;
    }
    function median(a) {
      if (!a.length) return null;
      var s = a.slice().sort(function (x, y) { return x - y; });
      var m = s.length >> 1;
      return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
    }
    function fmtNum(v, kind) {
      if (v == null) return '—';
      var t = Math.round(v).toLocaleString('ko-KR');
      return kind.indexOf('won') >= 0 ? t + '원' : t;
    }
    // 집계는 **값이 있는 행만** 쓴다 — 미노출을 0으로 세면 평균이 조용히 낮아진다.
    function aggText(rows) {
      return (cfg.aggs || []).map(function (a) {
        var vals = [];
        rows.forEach(function (r) { var v = numOf(r, a[1]); if (v != null) vals.push(v); });
        if (!vals.length) return '';
        var v = a[2].indexOf('median') === 0 ? median(vals)
              : vals.reduce(function (x, y) { return x + y; }, 0);
        var miss = rows.length - vals.length;
        return '<span class="group-agg">' + a[0] + ' <b>' + fmtNum(v, a[2]) + '</b>' +
               (miss ? '<i class="group-miss">미노출 ' + miss + '</i>' : '') + '</span>';
      }).join('');
    }
    function clear() {
      var gs = body.querySelectorAll('tr.group-row');
      for (var i = 0; i < gs.length; i++) gs[i].remove();
    }
    function render() {
      if (busy) return;
      busy = true;
      clear();
      var depth = +sel.value;
      var rows = Array.prototype.slice.call(body.rows).filter(function (r) {
        return !r.classList.contains('detail-row');
      });
      // **접어서 숨긴 것**과 **필터에서 걸러진 것**을 구분한다(data-gh).
      // 구분하지 않으면 접힌 행이 다음 렌더에서 '필터 탈락'으로 보여 그룹에서 사라진다.
      rows.forEach(function (r) {
        if (r.dataset.gh === '1') { r.hidden = false; delete r.dataset.gh; }
      });
      if (!depth) {   // 안 묶음 — 필터 상태는 apply가 정한 그대로 둔다
        rows.forEach(function (r) { r.classList.remove('in-group'); });
        busy = false;
        return;
      }
      var visible = rows.filter(function (r) { return !r.hidden; });
      var groups = [], byKey = {};
      visible.forEach(function (r) {
        var key = prefix(r.cells[cfg.cat] ? r.cells[cfg.cat].dataset.k : '', depth);
        if (!byKey[key]) { byKey[key] = { key: key, rows: [] }; groups.push(byKey[key]); }
        byKey[key].rows.push(r);
      });
      groups.forEach(function (g) {
        var tr = document.createElement('tr');
        var isOpen = !!open[g.key];
        tr.className = 'group-row' + (isOpen ? ' is-open' : '');
        tr.innerHTML = '<td class="group-head" colspan="' + cfg.span + '">' +
          '<span class="group-caret">' + (isOpen ? '▾' : '▸') + '</span>' +
          '<strong>' + g.key + '</strong>' +
          '<span class="group-count">' + g.rows.length.toLocaleString('ko-KR') + '개</span>' +
          aggText(g.rows) + '</td>';
        body.insertBefore(tr, g.rows[0]);
        g.rows.forEach(function (r) {
          r.classList.add('in-group');
          if (!isOpen) { r.hidden = true; r.dataset.gh = '1'; }
        });
        tr.addEventListener('click', function () {
          open[g.key] = !open[g.key];
          render();
        });
      });
      busy = false;
    }
    sel.addEventListener('change', function () { open = {}; render(); });
    document.addEventListener('tableview', render);
    render();
  });
};
window.initGroupViews();
"""
