#!/usr/bin/env python3
"""수집 JSON(또는 diff JSON)을 단일 HTML 리포트로 만든다.

usage:
    # 스토리1 브랜드 라인시트 (여러 사이트를 함께 주면 플랫폼 비교 섹션이 붙는다)
    python3 build_report.py data/raw/musinsa-*.json --out output/linesheet.html

    # 스토리2 시장 전수조사 (리뷰 인사이트는 에이전트가 쓴 JSON을 받는다)
    python3 build_report.py data/raw/scan.json --validation data/validation.json --out output/scan.html

    # 스토리3 스냅샷 1개 또는 diff_snapshots.py가 만든 diff
    python3 build_report.py data/diff.json --out output/ranking.html

옵션:
    --validation <검증JSON>  validate_data.py --json 결과. 리포트 헤더에 결측 요약을 싣는다(여러 개 가능)
    --title "..."            리포트 제목 덮어쓰기

출력은 외부 의존성이 없는 단일 HTML이다. 상품 이미지는 원본 URL을 핫링크한다.
"""

import argparse
import html
import json
import os
import sys

# ── dataviz 기본 팔레트 (검증 통과값) ────────────────────────────────────────
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]
SEQ_LIGHT = "#2a78d6"
SEQ_DARK = "#3987e5"

STORY_LABEL = {
    "brand-linesheet": "브랜드 라인시트",
    "market-scan": "시장 전수조사",
    "ranking-snapshot": "랭킹 스냅샷",
    "ranking-diff": "랭킹 변화",
}

SITE_LABEL = {"musinsa": "무신사", "29cm": "29CM"}

PRICE_CHANGE_LABEL = {
    "discount_started": "할인 시작",
    "discount_deepened": "할인 확대",
    "discount_reduced": "할인 축소",
    "discount_ended": "할인 종료",
    "price_up": "가격 인상",
    "price_down": "가격 인하",
}

MISSING = '<span class="na">미노출</span>'


# ── 값 포맷 ─────────────────────────────────────────────────────────────────

def esc(value):
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def num(value, suffix=""):
    if not is_num(value):
        return MISSING
    if isinstance(value, float) and value != int(value):
        return "%s%s" % (format(round(value, 2), ",g"), suffix)
    return "%s%s" % (format(int(value), ","), suffix)


def won(value):
    if not is_num(value):
        return MISSING
    return "%s원" % format(int(value), ",")


def approx_cell(item, exact_key, display_key):
    """정확한 수치가 있으면 숫자로, 사이트가 구간으로만 보여주면 그 문구 그대로.

    "300회 이상 (최근 1개월)" 같은 표기를 300이라는 정수로 바꾸지 않는다 —
    없는 정밀도를 만들어내는 일이다. 정렬 키도 비워 둬서 순위 비교에 끼지 않게 한다.
    """
    if is_num(item.get(exact_key)):
        return num(item[exact_key]), sort_key(item[exact_key])
    text = item.get(display_key)
    if isinstance(text, str) and text.strip():
        return '<span class="approx">%s</span>' % esc(text), ""
    return MISSING, ""


def rating_fmt(value):
    """평점은 자릿수를 고정해야 열이 흔들리지 않는다."""
    if not is_num(value):
        return MISSING
    return "%.1f" % value


def site_name(site):
    return SITE_LABEL.get(site, site or "-")


def sort_key(value):
    """정렬용 숫자 키. 없으면 표에서 맨 뒤로 간다."""
    return value if is_num(value) else ""


# ── SVG 차트 ────────────────────────────────────────────────────────────────

def rounded_end_bar(x, y, width, height, radius=4):
    """가로 막대. 데이터 끝(오른쪽)만 둥글고 기준선 쪽은 각지게 둔다."""
    r = max(0, min(radius, height / 2, width))
    if width <= 0:
        return ""
    return (
        "M%.1f %.1f H%.1f a%.1f %.1f 0 0 1 %.1f %.1f V%.1f "
        "a%.1f %.1f 0 0 1 %.1f %.1f H%.1f Z"
        % (x, y, x + width - r, r, r, r, r, y + height - r, r, r, -r, r, x)
    )


def hbar_chart(rows, unit="", max_rows=10):
    """가로 막대 — 크기 비교. 한 가지 색(sequential)만 쓴다.

    rows: [(label, value, subtitle)]
    항목이 2개 이하면 막대그래프를 그리지 않는다 — 비교할 게 없는 차트는 형태가 틀렸다.
    """
    rows = [r for r in rows if is_num(r[1])][:max_rows]
    if not rows:
        return '<p class="empty">표시할 값이 없다 (해당 지표가 모두 미노출)</p>'

    if len(rows) <= 2:
        return '<ul class="mini-list">%s</ul>' % "".join(
            "<li><strong>%s%s</strong> %s%s</li>"
            % (
                format(int(value), ","),
                esc(unit),
                esc(label),
                ' <span class="na">%s</span>' % esc(subtitle) if subtitle else "",
            )
            for label, value, subtitle in rows
        )

    top = max(r[1] for r in rows) or 1
    row_h, gap, label_w = 24, 6, 140
    width, pad_r = 520, 76
    height = len(rows) * (row_h + gap) - gap + 8
    plot_w = width - label_w - pad_r

    parts = [
        '<svg class="chart" viewBox="0 0 %d %d" role="img" '
        'preserveAspectRatio="xMinYMin meet" aria-label="상위 %d개 비교">'
        % (width, height, len(rows))
    ]
    for idx, (label, value, subtitle) in enumerate(rows):
        y = idx * (row_h + gap)
        bar_w = max(2.0, value / top * plot_w)
        tip = "%s — %s%s" % (label, format(int(value), ","), unit)
        if subtitle:
            tip += " · %s" % subtitle
        parts.append(
            '<text class="bar-label" x="%d" y="%.1f" text-anchor="end">%s</text>'
            % (label_w - 10, y + row_h * 0.68, esc(clip(label, 13)))
        )
        parts.append(
            '<path class="bar" d="%s" data-tip="%s"><title>%s</title></path>'
            % (rounded_end_bar(label_w, y + 3, bar_w, row_h - 6), esc(tip), esc(tip))
        )
        parts.append(
            '<text class="bar-value" x="%.1f" y="%.1f">%s%s</text>'
            % (label_w + bar_w + 8, y + row_h * 0.68, format(int(value), ","), esc(unit))
        )
    parts.append(
        '<line class="baseline" x1="%d" y1="0" x2="%d" y2="%d"/>' % (label_w, label_w, height - 8)
    )
    parts.append("</svg>")
    return "".join(parts)


def dist_chart(buckets, unit="개"):
    """세로 막대 — 분포. buckets: [(label, count)]"""
    buckets = [b for b in buckets if is_num(b[1])]
    if not buckets:
        return '<p class="empty">분포를 계산할 값이 없다</p>'
    top = max(b[1] for b in buckets) or 1
    width, height = 520, 210
    pad_l, pad_b, pad_t = 40, 44, 16
    plot_w = width - pad_l - 12
    plot_h = height - pad_b - pad_t
    slot = plot_w / len(buckets)
    bar_w = max(6.0, slot - 4)  # 인접 막대 사이 2px 이상 표면 간격

    parts = ['<svg class="chart" viewBox="0 0 %d %d" role="img" '
             'preserveAspectRatio="xMinYMin meet" aria-label="분포">' % (width, height)]
    for line_i in range(4):
        gy = pad_t + plot_h * line_i / 3
        parts.append('<line class="grid" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>'
                     % (pad_l, gy, width - 12, gy))
        parts.append('<text class="axis" x="%d" y="%.1f" text-anchor="end">%s</text>'
                     % (pad_l - 8, gy + 4, format(int(round(top * (3 - line_i) / 3)), ",")))
    for idx, (label, count) in enumerate(buckets):
        x = pad_l + idx * slot + (slot - bar_w) / 2
        h = (count / top) * plot_h
        y = pad_t + plot_h - h
        tip = "%s — %s%s" % (label, format(int(count), ","), unit)
        if h > 0:
            parts.append(
                '<rect class="bar" x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" '
                'data-tip="%s"><title>%s</title></rect>' % (x, y, bar_w, h, esc(tip), esc(tip))
            )
        parts.append(
            '<text class="axis" x="%.1f" y="%d" text-anchor="middle">%s</text>'
            % (x + bar_w / 2, height - pad_b + 18, esc(clip(label, 12)))
        )
        parts.append(
            '<text class="bar-value" x="%.1f" y="%.1f" text-anchor="middle">%s</text>'
            % (x + bar_w / 2, y - 5, format(int(count), ","))
        )
    parts.append('<line class="baseline" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>'
                 % (pad_l, pad_t + plot_h, width - 12, pad_t + plot_h))
    parts.append("</svg>")
    return "".join(parts)


# 추이 요약 규칙 (2026-07-29 확정): 시점이 48개를 넘으면 균등 구간의 마지막 스냅샷만 그린다.
# 평균을 내지 않고 실측 스냅샷을 고른다 — 없는 값을 만들지 않는다. 첫·끝 시점은 항상 포함.
# 48 = 무신사 하루치(30분×48). 가격·할인 변화 감지는 이 규칙과 무관하게 원본 전체로 한다.
MAX_TREND_POINTS = 48


def downsample_indices(count, limit=MAX_TREND_POINTS):
    if count <= limit:
        return list(range(count))
    picked = {0, count - 1}
    for bucket in range(1, limit + 1):
        picked.add((count * bucket + limit - 1) // limit - 1)
    return sorted(picked)


def value_trend_chart(series_list, stamps, unit="명"):
    """실시간 지표 등 일반 수치의 추이 선그래프. 0을 기준선으로 둔다. 최대 5개 계열."""
    series_list = [s for s in series_list if any(is_num(p) for p in s["points"])][:5]
    if not series_list or len(stamps) < 2:
        return None

    top = max(p for s in series_list for p in s["points"] if is_num(p))
    if top <= 0:
        return None

    width, height = 720, 260
    pad_l, pad_r, pad_t, pad_b = 52, 150, 16, 44
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    step = plot_w / max(1, len(stamps) - 1)

    def y_of(value):
        return pad_t + (1 - value / top) * plot_h

    parts = ['<svg class="chart" viewBox="0 0 %d %d" role="img" '
             'preserveAspectRatio="xMinYMin meet" aria-label="실시간 지표 추이">' % (width, height)]
    for i in range(4):
        value = top * (3 - i) / 3
        gy = pad_t + plot_h * i / 3
        parts.append('<line class="grid" x1="%d" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                     % (pad_l, gy, pad_l + plot_w, gy))
        parts.append('<text class="axis" x="%d" y="%.1f" text-anchor="end">%s</text>'
                     % (pad_l - 8, gy + 4, format(int(round(value)), ",")))
    label_every = max(1, len(stamps) // 8)
    for idx, stamp in enumerate(stamps):
        if idx % label_every and idx != len(stamps) - 1:
            continue
        parts.append('<text class="axis" x="%.1f" y="%d" text-anchor="middle">%s</text>'
                     % (pad_l + idx * step, height - pad_b + 18, esc(stamp[5:10])))

    for s_idx, series in enumerate(series_list):
        color_var = "var(--series-%d)" % (s_idx + 1)
        pts = [
            (pad_l + idx * step, y_of(value), idx, value)
            for idx, value in enumerate(series["points"])
            if is_num(value)
        ]
        if not pts:
            continue
        path = " ".join(
            ("M%.1f %.1f" if i == 0 else "L%.1f %.1f") % (p[0], p[1])
            for i, p in enumerate(pts)
        )
        parts.append('<path class="line" d="%s" style="stroke:%s"/>' % (path, color_var))
        for x, y, idx, value in pts:
            tip = "%s · %s · %s%s" % (series["name"], stamps[idx], format(int(value), ","), unit)
            parts.append(
                '<circle class="dot" cx="%.1f" cy="%.1f" r="4" style="fill:%s" '
                'data-tip="%s"><title>%s</title></circle>' % (x, y, color_var, esc(tip), esc(tip))
            )
        parts.append(
            '<text class="series-label" x="%.1f" y="%.1f">%s</text>'
            % (pts[-1][0] + 10, pts[-1][1] + 4, esc(clip(series["name"], 16)))
        )
    parts.append('<line class="baseline" x1="%d" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                 % (pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h))
    parts.append("</svg>")
    return "".join(parts)


def rank_trend_chart(series_list, stamps):
    """순위 추이 선그래프. 순위는 작을수록 위로 간다. 최대 5개 계열."""
    series_list = series_list[:5]
    if not series_list or len(stamps) < 2:
        return '<p class="empty">추이를 그리려면 스냅샷이 2개 이상 필요하다</p>'

    ranks = [p for s in series_list for p in s["points"] if is_num(p)]
    if not ranks:
        return '<p class="empty">순위 값이 없다</p>'
    worst = max(ranks)
    best = min(ranks)
    span = max(1, worst - best)

    width, height = 720, 300
    pad_l, pad_r, pad_t, pad_b = 44, 150, 16, 44
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    step = plot_w / max(1, len(stamps) - 1)

    def y_of(rank):
        return pad_t + (rank - best) / span * plot_h

    parts = ['<svg class="chart" viewBox="0 0 %d %d" role="img" '
             'preserveAspectRatio="xMinYMin meet" aria-label="순위 추이">' % (width, height)]
    for i in range(4):
        rank = best + span * i / 3
        gy = y_of(rank)
        parts.append('<line class="grid" x1="%d" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                     % (pad_l, gy, pad_l + plot_w, gy))
        parts.append('<text class="axis" x="%d" y="%.1f" text-anchor="end">%d위</text>'
                     % (pad_l - 8, gy + 4, round(rank)))
    for idx, stamp in enumerate(stamps):
        x = pad_l + idx * step
        parts.append('<text class="axis" x="%.1f" y="%d" text-anchor="middle">%s</text>'
                     % (x, height - pad_b + 18, esc(stamp[5:10])))

    for s_idx, series in enumerate(series_list):
        color_var = "var(--series-%d)" % (s_idx + 1)
        pts = []
        for idx, rank in enumerate(series["points"]):
            if not is_num(rank):
                continue
            pts.append((pad_l + idx * step, y_of(rank), idx, rank))
        if not pts:
            continue
        path = " ".join(
            ("M%.1f %.1f" if i == 0 else "L%.1f %.1f") % (p[0], p[1])
            for i, p in enumerate(pts)
        )
        parts.append('<path class="line" d="%s" style="stroke:%s"/>' % (path, color_var))
        for x, y, idx, rank in pts:
            tip = "%s · %s · %d위" % (series["name"], stamps[idx], rank)
            parts.append(
                '<circle class="dot" cx="%.1f" cy="%.1f" r="4.5" style="fill:%s" '
                'data-tip="%s"><title>%s</title></circle>' % (x, y, color_var, esc(tip), esc(tip))
            )
        last_x, last_y = pts[-1][0], pts[-1][1]
        parts.append(
            '<text class="series-label" x="%.1f" y="%.1f">%s</text>'
            % (last_x + 10, last_y + 4, esc(clip(series["name"], 16)))
        )
    parts.append("</svg>")
    return "".join(parts)


def clip(text, limit):
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── 집계 ────────────────────────────────────────────────────────────────────

def price_label(value):
    """가격 축 라벨. 만원 단위가 읽기 쉽다."""
    if value >= 10000:
        man = value / 10000.0
        return "%.0f만" % man if abs(man - round(man)) < 0.05 else "%.1f만" % man
    return format(int(value), ",")


def nice_buckets(values, target=8):
    """가격 등 연속값을 읽기 좋은 구간으로 나눈다. 라벨은 구간 하한이다."""
    values = sorted(v for v in values if is_num(v))
    if not values:
        return []
    low, high = values[0], values[-1]
    if high == low:
        return [(price_label(low), len(values))]
    raw = (high - low) / target
    magnitude = 10 ** max(0, len(str(int(raw))) - 1)
    step = magnitude * 10
    for mult in (1, 2, 2.5, 5, 10):
        if (high - low) / (magnitude * mult) <= target:
            step = magnitude * mult
            break
    buckets = []
    edge = int(low // step) * step
    while edge <= high:
        upper = edge + step
        count = len([v for v in values if edge <= v < upper or (upper > high and v == high)])
        buckets.append((price_label(edge), count))
        edge = upper
    return buckets


# 스토리2는 리뷰 본문을 수집하지 않는다(2026-07-29 결정).
# 만족/불만족 판단 재료는 사이트가 노출한 후기 수와 평균 평점뿐이다.
RATING_BUCKETS = (
    ("~2.9", 0.0, 2.95),
    ("3.0~3.4", 2.95, 3.45),
    ("3.5~3.9", 3.45, 3.95),
    ("4.0~4.4", 3.95, 4.45),
    ("4.5~", 4.45, 5.01),
)


def product_rating_summary(items):
    """상품에 노출된 평점·후기 수만으로 집계한다. 리뷰 본문은 쓰지 않는다."""
    dist = {label: 0 for label, _, _ in RATING_BUCKETS}
    hidden = 0
    weighted_sum = 0.0
    weight = 0
    for item in items:
        rating = item.get("rating")
        if not is_num(rating):
            hidden += 1
            continue
        for label, low, high in RATING_BUCKETS:
            if low <= rating < high:
                dist[label] += 1
                break
        count = item.get("review_count")
        if is_num(count) and count > 0:
            weighted_sum += rating * count
            weight += int(count)
    return {
        "dist": [(label, dist[label]) for label, _, _ in RATING_BUCKETS],
        "hidden": hidden,
        "weighted_avg": (weighted_sum / weight) if weight else None,
    }


def count_attributes(items):
    """market-scan 속성 분포. {속성명: [(값, 개수)]}"""
    tally = {}
    for item in items:
        attrs = item.get("attributes")
        if attrs is None:
            attrs = {"분류": "unknown"}
        if not isinstance(attrs, dict):
            attrs = {"분류": attrs}
        for key, value in attrs.items():
            value = value if value not in (None, "") else "unknown"
            tally.setdefault(str(key), {})
            tally[str(key)][str(value)] = tally[str(key)].get(str(value), 0) + 1
    return {
        key: sorted(counts.items(), key=lambda kv: -kv[1])
        for key, counts in tally.items()
    }


def kpi_tiles(tiles):
    cells = []
    for label, value, note in tiles:
        note_html = '<div class="kpi-note">%s</div>' % esc(note) if note else ""
        cells.append(
            '<div class="kpi"><div class="kpi-label">%s</div>'
            '<div class="kpi-value">%s</div>%s</div>' % (esc(label), value, note_html)
        )
    return '<div class="kpi-row">%s</div>' % "".join(cells)


# ── 표 ──────────────────────────────────────────────────────────────────────

def product_table(items, columns, table_id):
    head = "".join(
        '<th class="%s" data-sort="%s">%s</th>' % (col.get("cls", ""), col["type"], esc(col["label"]))
        for col in columns
    )
    rows = []
    for item in items:
        cells = []
        for col in columns:
            value, key = col["render"](item)
            cells.append('<td class="%s" data-k="%s">%s</td>' % (col.get("cls", ""), esc(key), value))
        rows.append("<tr>%s</tr>" % "".join(cells))
    return (
        '<div class="table-tools">'
        '<input class="filter" type="search" placeholder="상품명·브랜드로 거르기" '
        'data-for="%s" aria-label="표 거르기">'
        '<span class="table-count" id="%s-count">%d행</span></div>'
        '<div class="table-wrap"><table id="%s" class="grid"><thead><tr>%s</tr></thead>'
        "<tbody>%s</tbody></table></div>" % (table_id, table_id, len(items), table_id, head, "".join(rows))
    )


def thumb(item):
    url = item.get("image_url")
    name = esc(item.get("name"))
    if not url:
        return '<div class="thumb thumb-missing" title="이미지 미수집"></div>', ""
    return (
        '<a href="%s" target="_blank" rel="noopener"><img class="thumb" src="%s" alt="%s" '
        'loading="lazy" referrerpolicy="no-referrer" '
        "onerror=\"this.classList.add('thumb-missing');this.removeAttribute('src');this.alt=''\"></a>"
        % (esc(item.get("url") or url), esc(url), name),
        "",
    )


def name_cell(item):
    name = esc(item.get("name") or "(이름 없음)")
    url = item.get("url")
    sold_out = '<span class="tag tag-out">품절</span>' if item.get("sold_out") else ""
    inner = '<a href="%s" target="_blank" rel="noopener">%s</a>' % (esc(url), name) if url else name
    return "%s%s" % (inner, sold_out), (item.get("name") or "")


def price_cell(item):
    original = item.get("price_original")
    sale = item.get("price_sale")
    rate = item.get("discount_rate")
    if is_num(rate) and rate > 0 and is_num(original) and is_num(sale) and sale != original:
        return (
            '<span class="price-sale">%s</span><br>'
            '<span class="price-was">%s</span> <span class="tag tag-off">%d%%</span>'
            % (won(sale), won(original), int(rate)),
            sort_key(sale),
        )
    return won(sale if is_num(sale) else original), sort_key(sale if is_num(sale) else original)


CORE_COLUMNS = [
    {"label": "이미지", "type": "none", "cls": "col-img", "render": thumb},
    {"label": "상품명", "type": "text", "cls": "col-name", "render": name_cell},
    {"label": "브랜드", "type": "text", "render": lambda i: (esc(i.get("brand")), i.get("brand") or "")},
    {"label": "카테고리", "type": "text", "render": lambda i: (esc(i.get("category")), i.get("category") or "")},
    {"label": "가격", "type": "num", "cls": "col-num", "render": price_cell},
    {"label": "후기", "type": "num", "cls": "col-num", "render": lambda i: (num(i.get("review_count")), sort_key(i.get("review_count")))},
    {"label": "평점", "type": "num", "cls": "col-num", "render": lambda i: (rating_fmt(i.get("rating")), sort_key(i.get("rating")))},
    {"label": "조회수", "type": "num", "cls": "col-num", "render": lambda i: approx_cell(i, "view_count", "view_count_display")},
    {"label": "누적판매", "type": "num", "cls": "col-num", "render": lambda i: approx_cell(i, "purchase_count", "purchase_count_display")},
    {"label": "좋아요", "type": "num", "cls": "col-num", "render": lambda i: (num(i.get("like_count")), sort_key(i.get("like_count")))},
]

# 전수조사 표는 이미지 열을 뺀다 — 1만 행 규모에서 전 행을 유지하기 위한 결정(2026-07-29).
SCAN_COLUMNS = CORE_COLUMNS[1:]

# 랭킹 목록에만 있는 실시간 지표. 스토리3에서만 열이 붙는다.
LIVE_COLUMNS = [
    {"label": "보는 중", "type": "num", "cls": "col-num", "render": lambda i: (num(i.get("viewers_now"), "명"), sort_key(i.get("viewers_now")))},
    {"label": "구매 중", "type": "num", "cls": "col-num", "render": lambda i: (num(i.get("buyers_now"), "명"), sort_key(i.get("buyers_now")))},
]


def attr_column(items):
    keys = []
    for item in items:
        attrs = item.get("attributes")
        if isinstance(attrs, dict):
            for key in attrs:
                if key not in keys:
                    keys.append(str(key))
    columns = []
    for key in keys:
        columns.append(
            {
                "label": key,
                "type": "text",
                "render": (
                    lambda i, k=key: (
                        esc((i.get("attributes") or {}).get(k) or "unknown"),
                        str((i.get("attributes") or {}).get(k) or "unknown"),
                    )
                ),
            }
        )
    return columns


# ── 섹션 ────────────────────────────────────────────────────────────────────

def section(title, body, note=None):
    note_html = '<p class="section-note">%s</p>' % note if note else ""
    return '<section><h2>%s</h2>%s%s</section>' % (esc(title), note_html, body)


def banner(kind, text):
    return '<div class="banner banner-%s">%s</div>' % (kind, text)


def header_block(datasets, validations, title):
    sites = []
    for d in datasets:
        label = site_name(d["meta"].get("site"))
        if label not in sites:
            sites.append(label)
    collected = sorted({str(d["meta"].get("collected_at")) for d in datasets})
    total = sum(len(d["items"]) for d in datasets)

    banners = []
    for d in datasets:
        if d["meta"].get("incomplete"):
            note = "; ".join(str(n) for n in (d["meta"].get("notes") or [])) or "중단 지점 미기록"
            banners.append(
                banner(
                    "critical",
                    "<strong>부분 수집 데이터다.</strong> %s 수집이 끝까지 진행되지 않았다. 사유: %s"
                    % (esc(site_name(d["meta"].get("site"))), esc(note)),
                )
            )
    for v in validations:
        missing = v.get("missing_overall_pct")
        if is_num(missing) and missing > 30:
            banners.append(
                banner(
                    "critical",
                    "<strong>사이트 구조 변경 의심.</strong> 필수 필드 결측률 %.1f%% — 수집 로직을 점검할 것"
                    % missing,
                )
            )
        elif is_num(missing) and missing > 5:
            banners.append(
                banner("warning", "필수 필드 결측률 %.1f%% — 기준 5%%를 넘었다" % missing)
            )
        gap = v.get("total_gap_pct")
        if is_num(gap) and gap > 5:
            banners.append(
                banner(
                    "warning",
                    "사이트 노출 총계 %s건 대비 %s건 수집 (오차 %.1f%%)"
                    % (esc(v.get("source_total")), esc(v.get("item_count")), gap),
                )
            )

    meta_rows = [
        ("사이트", " · ".join(sites)),
        ("수집 시각", " / ".join(collected)),
        ("수집 항목", "%s건" % format(total, ",")),
    ]
    exposure = {}
    for v in validations:
        for field, rate in (v.get("exposure_by_field_pct") or {}).items():
            exposure.setdefault(field, []).append(rate)
    if exposure:
        averaged = {f: sum(r) / len(r) for f, r in exposure.items()}
        shown = ["%s %.0f%%" % (f, v) for f, v in averaged.items() if v > 0]
        hidden = [f for f, v in averaged.items() if v == 0]
        if shown:
            meta_rows.append(("지표 노출률", " · ".join(shown)))
        if hidden:
            meta_rows.append(("이 사이트가 안 주는 지표", ", ".join(hidden)))

    meta_html = "".join(
        '<div class="meta-item"><dt>%s</dt><dd>%s</dd></div>' % (esc(k), esc(v) if not v.startswith("<") else v)
        for k, v in meta_rows
    )
    return (
        '<header><h1>%s</h1><dl class="meta">%s</dl>%s</header>'
        % (esc(title), meta_html, "".join(banners))
    )


def category_popularity_blocks(datasets):
    """플랫폼별 '어떤 품목(카테고리)이 인기인가'.

    같은 상품을 플랫폼 간에 매칭하지 않는다(비범위). 대신 품목 단위로 묶어
    사이트별 하트 합·후기 합을 나란히 보여준다 — 노출값의 합산일 뿐, 추정이 아니다.
    """
    blocks = []
    for d in datasets:
        label = site_name(d["meta"].get("site"))
        agg = {}
        for item in d["items"]:
            cat = item.get("category") or "(카테고리 없음)"
            entry = agg.setdefault(cat, {"like": 0, "review": 0, "count": 0})
            if is_num(item.get("like_count")):
                entry["like"] += item["like_count"]
            if is_num(item.get("review_count")):
                entry["review"] += item["review_count"]
            entry["count"] += 1
        for metric, metric_label in (("like", "하트 합"), ("review", "후기 합")):
            rows = sorted(
                (
                    (cat, values[metric], "%d개 상품" % values["count"])
                    for cat, values in agg.items()
                    if values[metric] > 0
                ),
                key=lambda r: -r[1],
            )
            if not rows:
                continue
            blocks.append(
                '<div class="chart-block"><h3>%s — 품목별 %s</h3>%s</div>'
                % (esc(label), esc(metric_label), hbar_chart(rows, max_rows=8))
            )
    return blocks


def linesheet_body(datasets):
    all_items = [i for d in datasets for i in d["items"]]
    discounted = [i.get("discount_rate") for i in all_items if is_num(i.get("discount_rate")) and i["discount_rate"] > 0]
    sold_out = len([i for i in all_items if i.get("sold_out")])
    reviews = sum(i.get("review_count") or 0 for i in all_items if is_num(i.get("review_count")))

    out = [
        kpi_tiles(
            [
                ("상품 수", "%s" % format(len(all_items), ","), None),
                ("할인 중", "%s" % format(len(discounted), ","),
                 "평균 %d%%" % (sum(discounted) / len(discounted)) if discounted else "할인 상품 없음"),
                ("품절", "%s" % format(sold_out, ","), "%.0f%%" % (sold_out / len(all_items) * 100) if all_items else None),
                ("후기 합계", "%s" % format(int(reviews), ","), "노출된 값만 합산"),
            ]
        )
    ]

    # 플랫폼별 인기 ① 품목(카테고리) 단위 — 어떤 품목이 어느 플랫폼에서 인기인가
    category_blocks = category_popularity_blocks(datasets)
    if category_blocks:
        out.append(
            section(
                "플랫폼별 품목 인기",
                '<div class="chart-grid">%s</div>' % "".join(category_blocks),
                note="품목 = 카테고리. 사이트별로 노출된 하트 수·후기 수를 품목 단위로 합산했다. "
                "같은 상품을 플랫폼 간에 매칭하지는 않는다.",
            )
        )

    # 플랫폼별 인기 ② 상품 단위 — 사이트마다 노출 지표가 다르므로 있는 지표만 그린다
    popularity = []
    for d in datasets:
        label = site_name(d["meta"].get("site"))
        for metric, metric_label, unit in (
            ("view_count", "조회수", "회"),
            ("like_count", "좋아요", ""),
            ("purchase_count", "누적판매", ""),
        ):
            ranked = sorted(
                (i for i in d["items"] if is_num(i.get(metric))),
                key=lambda i: -i[metric],
            )
            if not ranked:
                continue
            rows = [(i.get("name") or "(이름 없음)", i[metric], i.get("brand")) for i in ranked[:10]]
            popularity.append(
                '<div class="chart-block"><h3>%s — %s 상위 10</h3>%s</div>'
                % (esc(label), esc(metric_label), hbar_chart(rows, unit=unit))
            )
    if popularity:
        out.append(
            section(
                "플랫폼별 인기 상품",
                '<div class="chart-grid">%s</div>' % "".join(popularity),
                note="사이트가 노출하는 지표만 그린다. 미노출 지표는 차트를 만들지 않는다(추정하지 않음).",
            )
        )
    else:
        out.append(
            section(
                "플랫폼별 인기 상품",
                '<p class="empty">조회수·좋아요·누적판매가 모두 미노출이다. 인기 비교 차트를 만들지 않았다.</p>',
            )
        )

    out.append(section("전 상품", product_table(all_items, CORE_COLUMNS, "t-linesheet")))
    return "".join(out)


def market_scan_body(datasets):
    all_items = [i for d in datasets for i in d["items"]]
    prices = [i.get("price_sale") for i in all_items if is_num(i.get("price_sale"))]
    review_total = sum(
        int(i["review_count"]) for i in all_items if is_num(i.get("review_count"))
    )
    stats = product_rating_summary(all_items)
    attrs = count_attributes(all_items)
    classified = len(
        [
            i
            for i in all_items
            if isinstance(i.get("attributes"), dict)
            and any(v not in (None, "", "unknown") for v in i["attributes"].values())
        ]
    )

    out = [
        kpi_tiles(
            [
                ("상품 수", format(len(all_items), ","), None),
                ("후기 합계", format(review_total, ","), "노출된 값만 합산"),
                ("중위 가격", won(sorted(prices)[len(prices) // 2]) if prices else MISSING, None),
                (
                    "평균 평점",
                    "%.2f" % stats["weighted_avg"] if stats["weighted_avg"] is not None else MISSING,
                    "후기 수 가중",
                ),
                (
                    "속성 분류율",
                    "%.0f%%" % (classified / len(all_items) * 100) if all_items else MISSING,
                    "기준 80%",
                ),
            ]
        )
    ]

    charts = []
    if prices:
        charts.append(
            '<div class="chart-block"><h3>판매가 분포</h3>%s</div>' % dist_chart(nice_buckets(prices))
        )
    if any(count for _, count in stats["dist"]):
        charts.append(
            '<div class="chart-block"><h3>상품 평점 분포</h3>%s%s</div>'
            % (
                dist_chart(stats["dist"], unit="개"),
                '<p class="section-note">평점 미노출 상품 %s개는 제외했다.</p>'
                % format(stats["hidden"], ",")
                if stats["hidden"] else "",
            )
        )
    for key, counts in attrs.items():
        charts.append(
            '<div class="chart-block"><h3>%s 분포</h3>%s</div>'
            % (esc(key), dist_chart([(k, v) for k, v in counts]))
        )
    out.append(
        section(
            "시장 요약",
            '<div class="chart-grid">%s</div>' % "".join(charts) if charts else '<p class="empty">집계할 값이 없다</p>',
            note="만족/불만족 판단은 사이트가 노출한 평점·후기 수만 쓴다. 리뷰 본문은 수집하지 않는다. "
            "속성은 상품명·상세·대표 이미지로 분류하고, 판단이 서지 않으면 unknown으로 둔다.",
        )
    )

    out.append(
        section("전 상품", product_table(all_items, SCAN_COLUMNS + attr_column(all_items), "t-scan"))
    )
    return "".join(out)


def ranking_snapshot_body(datasets):
    items = [i for d in datasets for i in d["items"]]
    items.sort(key=lambda i: i.get("rank") if is_num(i.get("rank")) else 10 ** 9)
    columns = [
        {"label": "순위", "type": "num", "cls": "col-num", "render": lambda i: (num(i.get("rank")), sort_key(i.get("rank")))},
    ] + CORE_COLUMNS + LIVE_COLUMNS
    out = [
        banner(
            "info",
            "스냅샷 1개만 있으므로 변화 분석을 하지 않았다. 같은 카테고리를 한 번 더 수집한 뒤 "
            "<code>diff_snapshots.py</code>로 기간을 지정하면 변화 리포트를 만든다.",
        ),
        kpi_tiles([("수집 순위", "%s위까지" % format(len(items), ","), None)]),
        section("현재 랭킹", product_table(items, columns, "t-rank")),
    ]
    return "".join(out)


def ranking_diff_body(diff):
    meta = diff.get("meta") or {}
    summary = diff.get("summary") or {}
    period = meta.get("period") or {}
    snapshots = meta.get("snapshots") or []

    out = [
        kpi_tiles(
            [
                ("스냅샷", "%s개" % summary.get("snapshot_count"), "%s ~ %s" % (period.get("start"), period.get("end"))),
                ("신규 진입", format(summary.get("entered", 0), ","), None),
                ("이탈", format(summary.get("exited", 0), ","), None),
                (
                    "할인 시작",
                    format(summary.get("discount_started", 0), ","),
                    "가격 변화 %s건 중" % format(summary.get("price_change_events", 0), ","),
                ),
            ]
        )
    ]

    snap_rows = "".join(
        "<tr><td>%s</td><td class=\"col-num\">%s</td><td>%s</td></tr>"
        % (
            esc(s.get("collected_at")),
            format(s.get("item_count", 0), ","),
            '<span class="tag tag-out">부분 수집</span>' if s.get("incomplete") else "",
        )
        for s in snapshots
    )
    out.append(
        section(
            "비교에 쓴 스냅샷",
            '<div class="table-wrap"><table class="grid"><thead><tr><th>수집 시각</th>'
            "<th>항목 수</th><th>비고</th></tr></thead><tbody>%s</tbody></table></div>" % snap_rows,
        )
    )

    movers = diff.get("movers") or []
    threshold = summary.get("big_move_threshold", 10)
    risers = [m for m in movers if m["delta"] >= threshold][:10]
    fallers = [m for m in movers if m["delta"] <= -threshold][:10]
    charts = []
    if risers:
        charts.append(
            '<div class="chart-block"><h3>급상승 (%d계단 이상)</h3>%s</div>'
            % (threshold, hbar_chart([(m["name"], m["delta"], "%d위→%d위" % (m["rank_first"], m["rank_last"])) for m in risers], unit="계단"))
        )
    if fallers:
        charts.append(
            '<div class="chart-block"><h3>급하락 (%d계단 이상)</h3>%s</div>'
            % (threshold, hbar_chart([(m["name"], -m["delta"], "%d위→%d위" % (m["rank_first"], m["rank_last"])) for m in fallers], unit="계단"))
        )
    out.append(
        section(
            "변동 요약",
            '<div class="chart-grid">%s</div>' % "".join(charts) if charts else
            '<p class="empty">%d계단 이상 움직인 상품이 없다</p>' % threshold,
        )
    )

    def simple_table(rows, columns, table_id):
        if not rows:
            return '<p class="empty">해당 항목이 없다</p>'
        return product_table(rows, columns, table_id)

    entry_columns = [
        {"label": "순위", "type": "num", "cls": "col-num", "render": lambda i: (num(i.get("rank")), sort_key(i.get("rank")))},
        {"label": "이미지", "type": "none", "cls": "col-img", "render": thumb},
        {"label": "상품명", "type": "text", "cls": "col-name", "render": name_cell},
        {"label": "브랜드", "type": "text", "render": lambda i: (esc(i.get("brand")), i.get("brand") or "")},
        {"label": "가격", "type": "num", "cls": "col-num", "render": price_cell},
    ]
    out.append(section("신규 진입", simple_table(diff.get("entered") or [], entry_columns, "t-in")))
    out.append(section("이탈", simple_table(diff.get("exited") or [], entry_columns, "t-out")))

    mover_columns = [
        {"label": "이미지", "type": "none", "cls": "col-img", "render": thumb},
        {"label": "상품명", "type": "text", "cls": "col-name", "render": name_cell},
        {"label": "브랜드", "type": "text", "render": lambda i: (esc(i.get("brand")), i.get("brand") or "")},
        {"label": "처음", "type": "num", "cls": "col-num", "render": lambda i: (num(i.get("rank_first"), "위"), sort_key(i.get("rank_first")))},
        {"label": "마지막", "type": "num", "cls": "col-num", "render": lambda i: (num(i.get("rank_last"), "위"), sort_key(i.get("rank_last")))},
        {
            "label": "변동",
            "type": "num",
            "cls": "col-num",
            "render": lambda i: (
                '<span class="delta delta-%s">%s%d</span>'
                % ("up" if i["delta"] > 0 else ("down" if i["delta"] < 0 else "flat"),
                   "▲" if i["delta"] > 0 else ("▼" if i["delta"] < 0 else "—"),
                   abs(i["delta"])),
                sort_key(i["delta"]),
            ),
        },
    ]
    out.append(section("순위 변동", simple_table(movers, mover_columns, "t-move")))

    changes = diff.get("price_changes") or []
    if changes:
        change_rows = "".join(
            '<tr><td>%s</td><td><span class="tag tag-%s">%s</span></td>'
            '<td class="col-img">%s</td><td class="col-name">%s</td>'
            '<td class="col-num">%s</td><td class="col-num">%s</td></tr>'
            % (
                esc(c.get("at")),
                "off" if c.get("kind", "").startswith("discount") else "out",
                esc(PRICE_CHANGE_LABEL.get(c.get("kind"), c.get("kind"))),
                thumb(c)[0],
                '<a href="%s" target="_blank" rel="noopener">%s</a>' % (esc(c.get("url")), esc(c.get("name")))
                if c.get("url") else esc(c.get("name")),
                "%s (%s%%)" % (won((c.get("from") or {}).get("price_sale")), esc((c.get("from") or {}).get("discount_rate") or 0)),
                "%s (%s%%)" % (won((c.get("to") or {}).get("price_sale")), esc((c.get("to") or {}).get("discount_rate") or 0)),
            )
            for c in changes
        )
        body = (
            '<div class="table-wrap"><table class="grid"><thead><tr><th>시점</th><th>종류</th>'
            '<th class="col-img">이미지</th>'
            "<th>상품</th><th>이전</th><th>이후</th></tr></thead><tbody>%s</tbody></table></div>" % change_rows
        )
    else:
        body = '<p class="empty">기간 내 가격·할인 변화가 감지되지 않았다</p>'
    out.append(section("가격·할인 변화", body, note="연속한 스냅샷 쌍을 비교해 잡은 변화다. 스냅샷 사이에 일어난 변화는 잡히지 않는다."))

    all_stamps = [s.get("collected_at") for s in snapshots]
    idxs = downsample_indices(len(all_stamps))
    stamps = [all_stamps[i] for i in idxs]
    sample_note = ""
    if len(stamps) < len(all_stamps):
        sample_note = (
            " 스냅샷 %s개를 균등 구간의 대표 %s개 시점으로 요약해 그렸다"
            "(평균이 아니라 실측 스냅샷을 고른 것이다)."
            % (format(len(all_stamps), ","), format(len(stamps), ","))
        )

    trends = diff.get("trends") or []
    movers_by_id = {m["product_id"]: m for m in movers}
    picked = sorted(
        (t for t in trends if t["product_id"] in movers_by_id),
        key=lambda t: -abs(movers_by_id[t["product_id"]]["delta"]),
    )[:5]

    def series_for(field):
        result = []
        for trend in picked:
            by_stamp = {p["at"]: p.get(field) for p in trend["series"]}
            result.append(
                {"name": trend.get("name") or trend["product_id"],
                 "points": [by_stamp.get(s) for s in stamps]}
            )
        return result

    out.append(
        section(
            "순위 추이 (변동 큰 5개)",
            rank_trend_chart(series_for("rank"), stamps),
            note="계열이 5개를 넘지 않도록 변동 폭이 큰 상품만 그린다. 전체 추이는 diff JSON의 trends에 있다."
            + sample_note,
        )
    )

    # 실시간 지표 추이 — 무신사 랭킹 목록에만 있는 값이라 없으면 섹션 자체를 만들지 않는다
    live_blocks = []
    for field, title in (("viewers_now", "보는 중 인원 추이"), ("buyers_now", "구매 중 인원 추이")):
        chart = value_trend_chart(series_for(field), stamps)
        if chart:
            live_blocks.append('<div class="chart-block"><h3>%s</h3>%s</div>' % (esc(title), chart))
    if live_blocks:
        out.append(
            section(
                "실시간 지표 추이 (변동 큰 5개)",
                '<div class="chart-grid">%s</div>' % "".join(live_blocks),
                note="사이트가 반올림해 노출한 값이다(예: 1.2천명→1200). 미노출 시점은 선이 비어 있다."
                + sample_note,
            )
        )
    return "".join(out)


# ── 문서 조립 ───────────────────────────────────────────────────────────────

CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb;
  --text: #0b0b0b; --text-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
  --seq: %(seq_light)s;
  --series-1: %(s1l)s; --series-2: %(s2l)s; --series-3: %(s3l)s;
  --series-4: %(s4l)s; --series-5: %(s5l)s;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  --up: #006300; --down: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19;
    --text: #ffffff; --text-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
    --seq: %(seq_dark)s;
    --series-1: %(s1d)s; --series-2: %(s2d)s; --series-3: %(s3d)s;
    --series-4: %(s4d)s; --series-5: %(s5d)s;
    --up: #0ca30c;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19;
  --text: #ffffff; --text-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
  --seq: %(seq_dark)s;
  --series-1: %(s1d)s; --series-2: %(s2d)s; --series-3: %(s3d)s;
  --series-4: %(s4d)s; --series-5: %(s5d)s;
  --up: #0ca30c;
}

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
.empty { color: var(--muted); font-size: 14px; margin: 4px 0; }

.chart-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(440px, 1fr)); gap: 26px; }
.chart-block { min-width: 0; }
.mini-list { list-style: none; margin: 4px 0 0; padding: 0; }
.mini-list li { padding: 5px 0; border-bottom: 1px solid var(--grid); font-size: 14px; }
.mini-list li:last-child { border-bottom: 0; }
.mini-list strong { font-variant-numeric: tabular-nums; margin-right: 8px; }
.chart { width: 100%%; height: auto; overflow: visible; }
.bar { fill: var(--seq); }
.bar:hover { fill-opacity: 0.82; }
.bar-label { font-size: 11px; fill: var(--text-2); }
.bar-value { font-size: 11px; fill: var(--text-2); font-variant-numeric: tabular-nums; }
.axis { font-size: 10px; fill: var(--muted); font-variant-numeric: tabular-nums; }
.grid { stroke: var(--grid); stroke-width: 1; }
.baseline { stroke: var(--baseline); stroke-width: 1; }
.line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.dot { stroke: var(--surface); stroke-width: 2; }
.series-label { font-size: 11px; fill: var(--text-2); }

.table-tools { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.filter { flex: 0 1 300px; padding: 7px 10px; font: inherit; font-size: 13px;
  border: 1px solid var(--border); border-radius: 7px;
  background: var(--page); color: var(--text); }
.table-count { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }
table.grid { border-collapse: collapse; width: 100%%; font-size: 13px; }
table.grid th, table.grid td { padding: 8px 10px; text-align: left;
  border-bottom: 1px solid var(--grid); vertical-align: middle; }
table.grid thead th { position: sticky; top: 0; background: var(--surface);
  font-size: 12px; color: var(--text-2); font-weight: 600; white-space: nowrap; z-index: 1; }
table.grid th[data-sort]:not([data-sort="none"]) { cursor: pointer; user-select: none; }
table.grid th[data-sort]:not([data-sort="none"]):hover { color: var(--text); }
table.grid th.sorted-asc::after { content: " ▲"; font-size: 9px; }
table.grid th.sorted-desc::after { content: " ▼"; font-size: 9px; }
table.grid tbody tr:hover { background: var(--page); }
.col-num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.col-name { min-width: 240px; max-width: 380px; }
.col-img { width: 64px; }
.thumb { width: 52px; height: 66px; object-fit: cover; border-radius: 5px;
  background: var(--grid); display: block; }
.thumb-missing { background: repeating-linear-gradient(45deg, var(--grid),
  var(--grid) 4px, var(--surface) 4px, var(--surface) 8px); }
.na { color: var(--muted); font-size: 12px; }
.approx { color: var(--text-2); font-size: 12px; }
.price-sale { font-weight: 600; }
.price-was { color: var(--muted); text-decoration: line-through; font-size: 12px; }
.tag { display: inline-block; font-size: 11px; padding: 1px 6px; border-radius: 4px;
  background: var(--grid); color: var(--text-2); margin-left: 4px; white-space: nowrap; }
.tag-out { background: var(--grid); color: var(--text-2); }
.tag-off { background: color-mix(in srgb, var(--critical) 16%%, transparent); color: var(--critical); }
.delta-up { color: var(--up); } .delta-down { color: var(--down); } .delta-flat { color: var(--muted); }

#tip { position: fixed; z-index: 20; pointer-events: none; opacity: 0;
  transition: opacity .1s; background: var(--text); color: var(--surface);
  font-size: 12px; padding: 5px 9px; border-radius: 6px; max-width: 260px; }
footer { max-width: 1180px; margin: 0 auto; color: var(--muted); font-size: 12px; }
""" % {
    "seq_light": SEQ_LIGHT, "seq_dark": SEQ_DARK,
    "s1l": SERIES_LIGHT[0], "s2l": SERIES_LIGHT[1], "s3l": SERIES_LIGHT[2],
    "s4l": SERIES_LIGHT[3], "s5l": SERIES_LIGHT[4],
    "s1d": SERIES_DARK[0], "s2d": SERIES_DARK[1], "s3d": SERIES_DARK[2],
    "s4d": SERIES_DARK[3], "s5d": SERIES_DARK[4],
}

JS = """
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
      var rows = Array.prototype.slice.call(body.rows);
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
    });
  });

  document.querySelectorAll('.filter').forEach(function (input) {
    var table = document.getElementById(input.dataset.for);
    var counter = document.getElementById(input.dataset.for + '-count');
    if (!table) return;
    input.addEventListener('input', function () {
      var q = input.value.trim().toLowerCase();
      var shown = 0;
      Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
        var hit = !q || row.textContent.toLowerCase().indexOf(q) !== -1;
        row.hidden = !hit;
        if (hit) shown++;
      });
      if (counter) counter.textContent = shown + '행';
    });
  });
})();
"""


def render(title, header, body, footer_note):
    return (
        "<!doctype html>\n<html lang=\"ko\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="referrer" content="no-referrer">\n'
        "<title>%s</title>\n<style>%s</style>\n</head>\n<body>\n"
        "<main>%s%s</main>\n<footer>%s</footer>\n"
        '<div id="tip" role="status"></div>\n<script>%s</script>\n</body>\n</html>\n'
        % (esc(title), CSS, header, body, footer_note, JS)
    )


TEMPLATE_BODY = """
<!-- ─────────────────────────────────────────────────────────────────────
     코드 실행이 안 되는 환경에서 리포트를 직접 쓸 때 쓰는 뼈대다.
     이 파일은 build_report.py가 뽑아내므로 스타일이 실제 리포트와 어긋나지 않는다.
     아래 블록을 필요한 것만 골라 복사해 채우고, 나머지는 지운다.

     지켜야 할 것:
     - 사이트가 안 준 값은 <span class="na">미노출</span>. 숫자를 지어내지 않는다.
     - 구간 표기("300회 이상 (최근 1개월)")는 <span class="approx">문구 그대로</span>.
     - 만족/불만족 판단은 노출된 평점·후기 수만 쓴다. 리뷰 본문을 해석하지 않는다.
     ────────────────────────────────────────────────────────────────── -->

<!-- [1] 헤더 — 항상 넣는다 -->
<header>
  <h1>대상 + 스토리 이름</h1>
  <dl class="meta">
    <div class="meta-item"><dt>사이트</dt><dd>무신사</dd></div>
    <div class="meta-item"><dt>수집 시각</dt><dd>2026-00-00 00:00:00</dd></div>
    <div class="meta-item"><dt>수집 항목</dt><dd>0건</dd></div>
    <div class="meta-item"><dt>지표 노출률</dt><dd>review_count 100% · rating 100%</dd></div>
    <div class="meta-item"><dt>이 사이트가 안 주는 지표</dt><dd>view_count</dd></div>
  </dl>
  <!-- 부분 수집·구조 변경 의심일 때만 남긴다. 아니면 지운다. -->
  <div class="banner banner-critical"><strong>부분 수집 데이터다.</strong> 중단 사유를 여기 쓴다.</div>
  <div class="banner banner-warning">필수 필드 결측률 0.0% — 기준 5%를 넘었다</div>
  <div class="banner banner-info">스냅샷이 1개라 변화 분석을 하지 않았다.</div>
</header>

<!-- [2] KPI — 4~6개가 적당하다 -->
<div class="kpi-row">
  <div class="kpi">
    <div class="kpi-label">상품 수</div>
    <div class="kpi-value">0</div>
    <div class="kpi-note">부연</div>
  </div>
</div>

<!-- [3] 차트가 들어갈 자리.
     손으로 SVG를 그리기 어려우면 차트를 빼고 표로만 간다 — 그게 정직하다.
     비교 항목이 2개 이하면 막대그래프 대신 이 목록을 쓴다. -->
<section>
  <h2>플랫폼별 인기</h2>
  <p class="section-note">사이트가 노출하는 지표만 싣는다. 미노출 지표는 만들지 않는다.</p>
  <ul class="mini-list">
    <li><strong>11,951</strong> 상품명 <span class="na">브랜드</span></li>
  </ul>
</section>

<!-- [4] 상품 표 — 행마다 이 구조를 반복한다.
     data-k는 정렬 키다. 값이 없으면 빈 문자열로 두면 정렬 시 뒤로 밀린다.
     전수조사(스토리2) 표는 이미지 열(col-img)을 통째로 뺀다 — 1만 행 규모 대비. -->
<section>
  <h2>전 상품</h2>
  <div class="table-tools">
    <input class="filter" type="search" placeholder="상품명·브랜드로 거르기" data-for="t-main">
    <span class="table-count" id="t-main-count">0행</span>
  </div>
  <div class="table-wrap">
    <table id="t-main" class="grid">
      <thead>
        <tr>
          <th class="col-img" data-sort="none">이미지</th>
          <th class="col-name" data-sort="text">상품명</th>
          <th data-sort="text">브랜드</th>
          <th class="col-num" data-sort="num">가격</th>
          <th class="col-num" data-sort="num">후기</th>
          <th class="col-num" data-sort="num">평점</th>
          <th class="col-num" data-sort="num">조회수</th>
          <th class="col-num" data-sort="num">좋아요</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td class="col-img" data-k=""><a href="#" target="_blank" rel="noopener"><img class="thumb"
            src="https://" alt="" loading="lazy" referrerpolicy="no-referrer"></a></td>
          <td class="col-name" data-k="상품명"><a href="#" target="_blank" rel="noopener">상품명</a>
            <span class="tag tag-out">품절</span></td>
          <td data-k="브랜드">브랜드</td>
          <td class="col-num" data-k="97890"><span class="price-sale">97,890원</span><br>
            <span class="price-was">139,900원</span> <span class="tag tag-off">30%</span></td>
          <td class="col-num" data-k="212">212</td>
          <td class="col-num" data-k="4.8">4.8</td>
          <td class="col-num" data-k=""><span class="approx">300회 이상 (최근 1개월)</span></td>
          <td class="col-num" data-k="">
            <span class="na">미노출</span></td>
        </tr>
      </tbody>
    </table>
  </div>
</section>
"""


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_html(path, markup, message):
    out_dir = os.path.dirname(os.path.abspath(path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(markup)
    print(message % path)
    return 0


def main():
    parser = argparse.ArgumentParser(description="수집 JSON → HTML 리포트")
    parser.add_argument("inputs", nargs="*", help="수집 JSON 또는 diff JSON 경로")
    parser.add_argument("--out", required=True, help="HTML 저장 경로")
    parser.add_argument(
        "--emit-template",
        action="store_true",
        help="입력 없이 폴백용 구조 템플릿만 뽑는다 (assets/report-template.html 갱신용)",
    )
    parser.add_argument("--validation", action="append", default=[], help="validate_data.py --json 결과")
    parser.add_argument("--title", help="리포트 제목")
    args = parser.parse_args()

    if args.emit_template:
        return write_html(
            args.out,
            render(
                "리포트 구조 템플릿",
                "",
                TEMPLATE_BODY,
                "코드 실행이 없는 환경에서 이 구조를 따라 직접 채운다. "
                "사이트가 안 준 값은 미노출로 두고 추정하지 않는다.",
            ),
            "템플릿 생성: %s",
        )

    if not args.inputs:
        print("입력 오류 — 수집 JSON을 하나 이상 넘기거나 --emit-template를 쓸 것")
        return 2

    try:
        payloads = [load_json(p) for p in args.inputs]
    except (OSError, json.JSONDecodeError) as exc:
        print("입력 오류 — %s" % exc)
        return 2

    validations = []
    for path in args.validation:
        try:
            validations.append(load_json(path))
        except (OSError, json.JSONDecodeError) as exc:
            print("검증 JSON을 읽지 못했다(무시하고 진행): %s — %s" % (path, exc))

    stories = {(p.get("meta") or {}).get("story") for p in payloads}
    if len(stories) > 1:
        print("입력 오류 — 서로 다른 story를 한 리포트에 섞을 수 없다: %s" % ", ".join(sorted(map(str, stories))))
        return 2
    story = stories.pop()

    if story == "ranking-diff":
        diff = payloads[0]
        meta = diff.get("meta") or {}
        title = args.title or "%s %s 랭킹 변화" % (site_name(meta.get("site")), meta.get("target"))
        period = meta.get("period") or {}
        header = (
            '<header><h1>%s</h1><dl class="meta">'
            '<div class="meta-item"><dt>기간</dt><dd>%s ~ %s</dd></div>'
            '<div class="meta-item"><dt>스냅샷</dt><dd>%s개</dd></div>'
            '<div class="meta-item"><dt>사이트</dt><dd>%s</dd></div>'
            "</dl></header>"
            % (
                esc(title),
                esc(period.get("start")),
                esc(period.get("end")),
                esc((diff.get("summary") or {}).get("snapshot_count")),
                esc(site_name(meta.get("site"))),
            )
        )
        body = ranking_diff_body(diff)
    else:
        datasets = []
        for payload in payloads:
            datasets.append({"meta": payload.get("meta") or {}, "items": payload.get("items") or []})
        target = datasets[0]["meta"].get("target")
        title = args.title or "%s %s" % (target, STORY_LABEL.get(story, story or "리포트"))
        header = header_block(datasets, validations, title)
        if story == "brand-linesheet":
            body = linesheet_body(datasets)
        elif story == "market-scan":
            body = market_scan_body(datasets)
        elif story == "ranking-snapshot":
            body = ranking_snapshot_body(datasets)
        else:
            print("입력 오류 — 알 수 없는 story: %r" % story)
            return 2

    footer = (
        "수치는 사이트가 화면에 노출한 값만 기록했다. 미노출 항목은 '미노출'로 두고 추정하지 않았다. "
        "이미지는 원본 URL을 그대로 불러온다."
    )
    return write_html(args.out, render(title, header, body, footer), "리포트 생성: %s")


if __name__ == "__main__":
    sys.exit(main())
