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
import re
import sys
from datetime import datetime

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

# 순위 추이 위에 겹쳐 찍는 표식. 세로선 색은 그 상품의 계열 색을 따른다.
EVENT_MARK = {
    "discount_started": "▼",
    "discount_deepened": "▼",
    "discount_reduced": "△",
    "discount_ended": "△",
    "price_up": "＋",
    "price_down": "－",
}

NO_PRICE_CHANGE = "변화 없음"

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


def median(values):
    """중위값. 노출된 값만 쓴다 — 미노출을 0으로 세지 않는다."""
    values = sorted(v for v in values if is_num(v))
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


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


def time_axis_labels(stamps, max_labels=8):
    """시점 축 라벨.

    스냅샷이 30분 간격이라 날짜만 찍으면 33개가 전부 "07-29"로 보인다.
    날짜가 바뀌는 지점에만 날짜를 붙이고 나머지는 시각만 쓴다. 겹치지 않게 솎아낸다.
    """
    if not stamps:
        return []
    every = max(1, (len(stamps) + max_labels - 1) // max_labels)
    picked = list(range(0, len(stamps), every))
    if picked[-1] != len(stamps) - 1:
        picked.append(len(stamps) - 1)
    if len(picked) > 2 and picked[-1] - picked[-2] < every / 2:
        picked.pop(-2)
    labels = []
    last_date = None
    for idx in picked:
        stamp = stamps[idx] or ""
        date, clock = stamp[5:10], stamp[11:16]
        if not clock:
            labels.append((idx, date or stamp[:10]))
            continue
        labels.append((idx, clock if date == last_date else "%s %s" % (date, clock)))
        last_date = date
    return labels


def series_legend(series_list):
    """계열 이름을 차트 아래 범례로 낸다.

    선 끝에 직접 붙이는 게 원래는 읽기 좋지만, 랭킹은 상품이 서로 다른 시점에
    순위권 밖으로 빠져서 선 끝 x가 흩어진다 — 라벨이 서로 겹치고 이름이 잘린다.
    범례로 내리면 이름을 온전히 싣고 플롯 폭도 넓게 쓴다.
    """
    if not series_list:
        return ""
    items = "".join(
        '<span class="legend-item" style="color: var(--series-%d)">'
        '<span class="legend-swatch"></span>'
        '<span class="legend-name">%s</span></span>' % (idx + 1, esc(series["name"]))
        for idx, series in enumerate(series_list)
    )
    return '<p class="legend legend-series">%s</p>' % items


def segments_of(points):
    """연속한 시점끼리만 이어 붙인 구간 목록.

    순위권 밖으로 나갔던 구간은 값이 없으므로 **선을 끊는다.** 이어 그리면 그 구간에도
    순위가 있었다고 주장하는 셈이다(report-spec: "중간에 순위권 밖으로 나갔던 구간은 끊긴다").
    """
    segments, current = [], []
    for point in points:
        if current and point[2] - current[-1][2] > 1:
            segments.append(current)
            current = []
        current.append(point)
    if current:
        segments.append(current)
    return segments


def polyline(points):
    return " ".join(
        ("M%.1f %.1f" if i == 0 else "L%.1f %.1f") % (p[0], p[1])
        for i, p in enumerate(points)
    )


def value_trend_chart(series_list, stamps, unit="명"):
    """실시간 지표 등 일반 수치의 추이 선그래프. 0을 기준선으로 둔다. 최대 5개 계열."""
    series_list = [s for s in series_list if any(is_num(p) for p in s["points"])][:5]
    if not series_list or len(stamps) < 2:
        return None

    top = max(p for s in series_list for p in s["points"] if is_num(p))
    if top <= 0:
        return None

    width, height = 720, 260
    pad_l, pad_r, pad_t, pad_b = 52, 20, 16, 44
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
    for idx, text in time_axis_labels(stamps):
        parts.append('<text class="axis" x="%.1f" y="%d" text-anchor="middle">%s</text>'
                     % (pad_l + idx * step, height - pad_b + 18, esc(text)))

    for s_idx, series in enumerate(series_list):
        color_var = "var(--series-%d)" % (s_idx + 1)
        pts = [
            (pad_l + idx * step, y_of(value), idx, value)
            for idx, value in enumerate(series["points"])
            if is_num(value)
        ]
        if not pts:
            continue
        for segment in segments_of(pts):
            if len(segment) > 1:
                parts.append('<path class="line" d="%s" style="stroke:%s"/>'
                             % (polyline(segment), color_var))
        for x, y, idx, value in pts:
            tip = "%s · %s · %s%s" % (series["name"], stamps[idx], format(int(value), ","), unit)
            parts.append(
                '<circle class="dot" cx="%.1f" cy="%.1f" r="4" style="fill:%s" '
                'data-tip="%s"><title>%s</title></circle>' % (x, y, color_var, esc(tip), esc(tip))
            )
    parts.append('<line class="baseline" x1="%d" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                 % (pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h))
    parts.append("</svg>")
    return "".join(parts) + series_legend(series_list)


def nearest_stamp_index(stamps, at):
    """추이 차트가 요약된 시점만 그릴 수 있으므로, 변화 시점을 가장 가까운 시점에 붙인다.

    ISO 문자열은 사전순 = 시간순이라 그대로 비교한다. 붙였다는 사실은 각주로 밝힌다.
    """
    if not stamps or not at:
        return None
    idx = 0
    for i, stamp in enumerate(stamps):
        if stamp <= at:
            idx = i
        else:
            break
    return idx


def rank_trend_chart(series_list, stamps, events=None):
    """순위 추이 선그래프. 순위는 작을수록 위로 간다. 최대 5개 계열.

    events가 있으면 같은 좌표계 위에 가격·할인 변화 시점을 세로선으로 겹쳐 그린다 —
    "할인을 시작하니 순위가 올랐는가"를 표 두 개 왕복 없이 보게 하려는 것이다.
    인과를 주장하는 게 아니라 두 시계열을 한 축에 놓는 것뿐이다.
    """
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
    pad_l, pad_r, pad_t, pad_b = 44, 20, 16, 44
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
    for idx, text in time_axis_labels(stamps):
        parts.append('<text class="axis" x="%.1f" y="%d" text-anchor="middle">%s</text>'
                     % (pad_l + idx * step, height - pad_b + 18, esc(text)))

    # 가격·할인 변화 시점을 선 아래 레이어에 먼저 깔아 순위 선을 가리지 않게 한다.
    #
    # 시점이 스냅샷 1칸으로 확정된 사건만 세로 점선으로 찍는다. 상품이 순위권 밖에 있던
    # 구간에 걸친 사건은 **띠**로 그린다 — 정확한 x에 선을 그으면 없는 정밀도를 주장하는 셈이다.
    for event in events or []:
        i_to = nearest_stamp_index(stamps, event.get("to_at") or event.get("at"))
        if i_to is None:
            continue
        x_to = pad_l + i_to * step
        color_var = "var(--series-%d)" % (event.get("series_idx", 0) + 1)
        label = PRICE_CHANGE_LABEL.get(event.get("kind"), event.get("kind"))
        mark = EVENT_MARK.get(event.get("kind"), "◆")

        if event.get("exact_at"):
            tip = "%s · %s · %s" % (event.get("name") or "", event.get("to_at"), label)
            parts.append(
                '<line class="event-line" x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" style="stroke:%s" '
                'data-tip="%s"><title>%s</title></line>'
                % (x_to, pad_t, x_to, pad_t + plot_h, color_var, esc(tip), esc(tip))
            )
            tick_x = x_to
        else:
            i_from = nearest_stamp_index(stamps, event.get("from_at"))
            x_from = pad_l + (i_from if i_from is not None else i_to) * step
            band_w = max(4.0, x_to - x_from)
            tip = "%s · %s ~ %s 사이 · %s (스냅샷 %s개 결석)" % (
                event.get("name") or "", event.get("from_at"), event.get("to_at"),
                label, event.get("gap_snapshots"),
            )
            parts.append(
                '<rect class="event-band" x="%.1f" y="%d" width="%.1f" height="%.1f" '
                'style="fill:%s" data-tip="%s"><title>%s</title></rect>'
                % (x_from, pad_t, band_w, plot_h, color_var, esc(tip), esc(tip))
            )
            tick_x = x_from + band_w / 2
        parts.append(
            '<text class="event-tick" x="%.1f" y="%d" text-anchor="middle" style="fill:%s" '
            'data-tip="%s">%s</text>'
            % (tick_x, pad_t - 4, color_var, esc(tip), esc(mark))
        )

    for s_idx, series in enumerate(series_list):
        color_var = "var(--series-%d)" % (s_idx + 1)
        pts = []
        for idx, rank in enumerate(series["points"]):
            if not is_num(rank):
                continue
            pts.append((pad_l + idx * step, y_of(rank), idx, rank))
        if not pts:
            continue
        # 순위권 밖이던 구간은 선을 끊는다 — 이어 그으면 없던 순위를 만들어내는 것이다
        for segment in segments_of(pts):
            if len(segment) > 1:
                parts.append('<path class="line" d="%s" style="stroke:%s"/>'
                             % (polyline(segment), color_var))
        for x, y, idx, rank in pts:
            tip = "%s · %s · %d위" % (series["name"], stamps[idx], rank)
            parts.append(
                '<circle class="dot" cx="%.1f" cy="%.1f" r="4.5" style="fill:%s" '
                'data-tip="%s"><title>%s</title></circle>' % (x, y, color_var, esc(tip), esc(tip))
            )
    parts.append("</svg>")
    return "".join(parts) + series_legend(series_list)


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

def facet_block(items, facets, table_id):
    """열 값으로 거르는 다중 선택 칩. 한 그룹 안에서는 OR, 그룹끼리는 AND다.

    **예외: `match: "all"`인 축은 그룹 안에서도 AND다** (입점 축 — presence_facet 참조).
    그 축은 칩에 `모두 만족` 표시를 달아 독자가 동작을 알 수 있게 한다.

    값이 1종뿐인 축은 거를 게 없으니 그룹을 만들지 않는다.
    """
    groups = []
    for f_idx, facet in enumerate(facets):
        tally = {}
        for item in items:
            for value in facet["values"](item):
                tally[value] = tally.get(value, 0) + 1
        if len(tally) < 2:
            continue
        order = facet.get("order")
        if order:
            keys = [k for k in order if k in tally] + sorted(k for k in tally if k not in order)
        else:
            keys = sorted(tally, key=lambda k: (-tally[k], k))
        # 기본으로 켜 둘 값이 있으면 '전체'를 끄고 그 칩만 켠다.
        # 기본 필터를 걸더라도 건수는 항상 보이게 둔다 — 조용히 숨기지 않는다.
        default = set(facet.get("default") or [])
        chips = ['<button type="button" class="chip chip-all%s" data-v="">전체</button>'
                 % ("" if default else " is-on")]
        chips += [
            '<button type="button" class="chip%s" data-v="%s">%s<em>%s</em></button>'
            % (" is-on" if key in default else "", esc(key), esc(key), format(tally[key], ","))
            for key in keys
        ]
        match = facet.get("match") or "any"
        # 동작이 다른 축이므로 화면에 그 사실을 적는다 — 안 적으면 OR로 읽는다.
        # 배지는 `facet-label` **밖**에 둔다. 안에 넣으면 라벨 텍스트가 오염된다.
        mode_badge = (
            '<span class="facet-mode" title="여러 개를 켜면 그 플랫폼에 모두 있는 상품만 남는다">모두 만족</span>'
            if match == "all" else ""
        )
        groups.append(
            '<div class="facet-group" data-for="%s" data-facet="%d" data-match="%s">'
            '<span class="facet-label">%s</span>%s%s</div>'
            % (table_id, f_idx, match, esc(facet["label"]), mode_badge, "".join(chips))
        )
    if not groups:
        return ""
    return '<div class="facets">%s</div>' % "".join(groups)


def th_label(col):
    """열 이름. tip이 있으면 점선 밑줄 + hover 뜻풀이를 붙인다.

    '반응지수'처럼 이름만 보고는 뜻을 알 수 없는 열은 리포트 안에서 스스로 설명해야 한다.
    """
    label = esc(col["label"])
    if not col.get("tip"):
        return label
    return '<span class="th-tip" data-tip="%s">%s</span>' % (esc(col["tip"]), label)


def product_table(items, columns, table_id, facets=None, placeholder="상품명·브랜드로 거르기",
                  row_id=None, selectable=False):
    head = "".join(
        '<th class="%s" data-sort="%s">%s</th>' % (col.get("cls", ""), col["type"], th_label(col))
        for col in columns
    )
    if selectable:
        head = '<th class="col-pick" data-sort="none" data-tip="차트에 그릴 상품 고르기">\u2713</th>' + head
    facets = facets or []
    rows = []
    for item in items:
        cells = []
        if selectable:
            cells.append('<td class="col-pick" data-k="">'
                         '<input type="checkbox" class="pick" aria-label="차트에 포함"></td>')
        for col in columns:
            value, key = col["render"](item)
            cells.append('<td class="%s" data-k="%s">%s</td>' % (col.get("cls", ""), esc(key), value))
        # 한 축이 값을 여러 개 가질 수 있으므로 구분자로 감싼다 (JS가 |값| 으로 찾는다).
        attrs = "".join(
            ' data-f%d="%s"' % (f_idx, esc("|%s|" % "|".join(facet["values"](item))))
            for f_idx, facet in enumerate(facets)
        )
        if row_id is not None:
            attrs += ' data-pid="%s"' % esc(row_id(item))
        rows.append("<tr%s>%s</tr>" % (attrs, "".join(cells)))
    return (
        "%s"
        '<div class="table-tools">'
        '<input class="filter" type="search" placeholder="%s" '
        'data-for="%s" aria-label="표 거르기">'
        '<span class="table-count" id="%s-count">%d행</span></div>'
        '<div class="table-wrap"><table id="%s" class="grid"><thead><tr>%s</tr></thead>'
        "<tbody>%s</tbody></table></div>"
        % (
            facet_block(items, facets, table_id),
            esc(placeholder),
            table_id,
            table_id,
            len(items),
            table_id,
            head,
            "".join(rows),
        )
    )


def category_facet(items):
    return {
        "label": "카테고리",
        "values": lambda i: [i.get("category") or "(카테고리 없음)"],
    }


SALE_ON = "판매 중"
SALE_OUT = "품절"


def sold_out_facet(items, getter=None):
    """판매 중 / 품절 필터 축.

    무신사 목록은 `isSoldOut=true`를 줘야 품절을 준다(어댑터 함정). 품절을 수집하면
    표가 두 배로 길어지므로 **기본은 판매 중만** 보여주고 품절은 칩으로 켠다.
    건수는 칩에 찍히므로 조용히 숨기는 게 아니다.
    """
    pick = getter or (lambda i: bool(i.get("sold_out")))
    has_out = any(pick(i) for i in items)
    return {
        "label": "판매",
        "values": lambda i: [SALE_OUT if pick(i) else SALE_ON],
        "order": [SALE_ON, SALE_OUT],
        "default": [SALE_ON] if has_out else [],
    }


def union_sold_out(row, sites):
    """합집합 행은 어느 플랫폼에서든 판매 중이면 판매 중으로 본다."""
    records = [row["by_site"][s] for s in sites if s in row["by_site"]]
    if not records:
        return False
    return all(bool(r.get("sold_out")) for r in records)


def attribute_facets(items):
    """스토리2 속성(핏 등)을 축마다 하나씩 다중 선택 필터로 만든다."""
    keys = []
    for item in items:
        attrs = item.get("attributes")
        if isinstance(attrs, dict):
            for key in attrs:
                if key not in keys:
                    keys.append(str(key))
    return [
        {
            "label": key,
            "values": (lambda i, k=key: [str((i.get("attributes") or {}).get(k) or "unknown")]),
        }
        for key in keys
    ]


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
    # 카테고리는 정렬 열이 아니라 **필터 축**이다 (2026-07-30 지시) — 칩으로 거른다.
    # 가나다순 정렬은 열람에 도움이 안 되고, 정렬 화살표가 필터를 가린다.
    {"label": "카테고리", "type": "none", "render": lambda i: (esc(i.get("category")), i.get("category") or "")},
    {"label": "가격", "type": "num", "cls": "col-num", "render": price_cell},
    {"label": "후기", "type": "num", "cls": "col-num", "render": lambda i: (num(i.get("review_count")), sort_key(i.get("review_count")))},
    {"label": "평점", "type": "num", "cls": "col-num", "render": lambda i: (rating_fmt(i.get("rating")), sort_key(i.get("rating")))},
    {"label": "조회수", "type": "num", "cls": "col-num", "render": lambda i: approx_cell(i, "view_count", "view_count_display")},
    {"label": "누적판매", "type": "num", "cls": "col-num", "render": lambda i: approx_cell(i, "purchase_count", "purchase_count_display")},
    {"label": "좋아요", "type": "num", "cls": "col-num", "render": lambda i: (num(i.get("like_count")), sort_key(i.get("like_count")))},
]

# 전수조사 표도 이미지 열을 싣는다 — 2026-07-30 사용자 지시로 구 결정(이미지 열 제외)을 뒤집었다.
# 전 행 유지 원칙은 그대로이고, 썸네일은 loading="lazy"로 화면에 들어올 때만 받는다.
SCAN_COLUMNS = CORE_COLUMNS

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
                # 속성도 필터 축이다 — 칩으로 거른다 (2026-07-30 지시)
                "type": "none",
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


def header_block(datasets, validations, title, union=None):
    sites = []
    for d in datasets:
        label = site_name(d["meta"].get("site"))
        if label not in sites:
            sites.append(label)
    collected = sorted({str(d["meta"].get("collected_at")) for d in datasets})
    total = sum(len(d["items"]) for d in datasets)
    multi = len(sites) > 1

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
        ("플랫폼" if multi else "사이트", " · ".join(sites)),
        ("수집 시각", " / ".join(collected)),
    ]
    if multi:
        # 사이트별 수집량과 **합집합 크기**를 따로 보여준다. 단순 합(986)을 상품 수로
        # 내놓으면 양쪽에 있는 상품이 두 번 세어진 값을 진짜 상품 수로 읽게 된다.
        per_site = " · ".join(
            "%s %s건" % (site_name(d["meta"].get("site")), format(len(d["items"]), ","))
            for d in datasets
        )
        meta_rows.append(("수집 항목", "%s (단순 합 %s건)" % (per_site, format(total, ","))))
        if union is not None and union["rows"]:
            matched = len(matched_rows(union))
            # `matched_rows`는 **2곳 이상**이다. 플랫폼이 2개면 그것이 곧 '양쪽'이지만
            # 3개 이상이면 아니므로 이름을 갈라 쓴다 — 안 그러면 표의 열과 값이 어긋난다.
            meta_rows.append((
                "합집합 상품",
                ("%s건 — 양쪽 입점 %s · 단독 %s" if len(sites) == 2
                 else "%s건 — 2곳 이상 입점 %s · 단독 %s")
                % (
                    format(len(union["rows"]), ","),
                    format(matched, ","),
                    " / ".join(
                        "%s %s" % (site_name(s), format(len(solo_rows(union, s)), ","))
                        for s in union["sites"]
                    ),
                ),
            ))
    else:
        meta_rows.append(("수집 항목", "%s건" % format(total, ",")))

    # 지표 노출률은 **사이트별로** 낸다. 평균을 내면 "한쪽만 주는 지표"가 '절반 노출'로
    # 뭉개진다 — 무신사 100% + 29CM 0%는 50%가 아니다.
    by_site = {}
    for v in validations:
        by_site.setdefault(site_name(v.get("site")), {}).update(v.get("exposure_by_field_pct") or {})
    for label, exposure in by_site.items():
        if not exposure:
            continue
        shown = ["%s %.0f%%" % (f, r) for f, r in exposure.items() if r > 0]
        hidden = [f for f, r in exposure.items() if r == 0]
        prefix = "%s 지표 노출률" % label if multi else "지표 노출률"
        if shown:
            meta_rows.append((prefix, " · ".join(shown)))
        if hidden:
            meta_rows.append(("%s가 안 주는 지표" % label, ", ".join(hidden)))

    meta_html = "".join(
        '<div class="meta-item"><dt>%s</dt><dd>%s</dd></div>' % (esc(k), esc(v) if not v.startswith("<") else v)
        for k, v in meta_rows
    )
    return (
        '<header><h1>%s</h1><dl class="meta">%s</dl>%s</header>'
        % (esc(title), meta_html, "".join(banners))
    )


def category_stats(items):
    """카테고리별 노출값 집계.

    상품당 평균은 **그 지표가 노출된 상품 수로만** 나눈다. 미노출을 0으로 세면
    평균이 조용히 낮아진다 — 없는 값을 0이라고 우기는 것과 같다.
    """
    agg = {}
    for item in items:
        cat = item.get("category") or "(카테고리 없음)"
        entry = agg.setdefault(
            cat,
            {"count": 0, "like_sum": 0, "like_n": 0, "review_sum": 0, "review_n": 0,
             "prices": [], "discounts": [], "rating_w": 0.0, "rating_n": 0},
        )
        entry["count"] += 1
        if is_num(item.get("like_count")):
            entry["like_sum"] += item["like_count"]
            entry["like_n"] += 1
        if is_num(item.get("review_count")):
            entry["review_sum"] += item["review_count"]
            entry["review_n"] += 1
        if is_num(item.get("price_sale")):
            entry["prices"].append(item["price_sale"])
        if is_num(item.get("discount_rate")):
            entry["discounts"].append(item["discount_rate"])
        if is_num(item.get("rating")) and is_num(item.get("review_count")) and item["review_count"] > 0:
            entry["rating_w"] += item["rating"] * item["review_count"]
            entry["rating_n"] += int(item["review_count"])
    return agg


def index_cell_value(value):
    """1.0을 기준으로 읽는 지수 칸. 1.30↑ ▲ / 0.70↓ ▼.

    색과 화살표는 '기준보다 크다/작다'만 말한다 — 좋다/나쁘다로 단정하지 않는다.
    """
    if not is_num(value):
        return MISSING, ""
    cls = "idx-over" if value >= 1.3 else ("idx-under" if value <= 0.7 else "")
    mark = " ▲" if value >= 1.3 else (" ▼" if value <= 0.7 else "")
    return '<span class="idx %s">%.2f%s</span>' % (cls, value, mark), value


def response_index(share_metric, share_count):
    """규모 대비 반응. 1.0 = 상품 수 비중만큼 반응한 것.

    노출값의 비중끼리 나눈 값이라 추정이 아니다. 분모(상품 수 비중)가 0이면 만들지 않는다.
    """
    if not share_count:
        return None
    return share_metric / share_count


def category_scorecard(items, table_id):
    """품목 성적표 — 규모(상품 수)와 반응(하트·후기)을 한 표에서 대조한다."""
    agg = category_stats(items)
    if len(agg) < 2:
        return None
    total_count = sum(e["count"] for e in agg.values())
    total_like = sum(e["like_sum"] for e in agg.values())
    total_review = sum(e["review_sum"] for e in agg.values())

    rows = []
    for cat, e in agg.items():
        share_count = e["count"] / total_count if total_count else 0
        rows.append(
            {
                "cat": cat,
                "count": e["count"],
                "like_sum": e["like_sum"] if e["like_n"] else None,
                "like_avg": (e["like_sum"] / e["like_n"]) if e["like_n"] else None,
                "review_sum": e["review_sum"] if e["review_n"] else None,
                "review_avg": (e["review_sum"] / e["review_n"]) if e["review_n"] else None,
                "price_mid": median(e["prices"]),
                "discount_avg": (sum(e["discounts"]) / len(e["discounts"])) if e["discounts"] else None,
                "rating": (e["rating_w"] / e["rating_n"]) if e["rating_n"] else None,
                "like_index": response_index(
                    e["like_sum"] / total_like if total_like else 0, share_count),
                "review_index": response_index(
                    e["review_sum"] / total_review if total_review else 0, share_count),
            }
        )
    rows.sort(key=lambda r: -(r["like_sum"] or 0))

    def index_cell(row, key):
        return index_cell_value(row[key])

    columns = [
        {"label": "품목", "type": "text", "cls": "col-name",
         "render": lambda r: (esc(r["cat"]), r["cat"])},
        {"label": "상품 수", "type": "num", "cls": "col-num",
         "render": lambda r: (num(r["count"]), r["count"])},
        {"label": "하트 합", "type": "num", "cls": "col-num",
         "render": lambda r: (num(r["like_sum"]), sort_key(r["like_sum"]))},
        {"label": "상품당 하트", "type": "num", "cls": "col-num",
         "tip": "하트 합 ÷ 하트가 노출된 상품 수. 미노출 상품은 분모에서 뺐다",
         "render": lambda r: (num(round(r["like_avg"])) if r["like_avg"] is not None else MISSING,
                              sort_key(r["like_avg"]))},
        {"label": "규모 대비 하트", "type": "num", "cls": "col-num",
         "tip": "이 품목의 하트 비중 ÷ 상품 수 비중. 1.00이면 상품 수만큼 하트를 받은 것이고, "
                "2.00이면 규모의 2배로 반응한 것이다. 1.30↑ ▲ / 0.70↓ ▼",
         "render": lambda r: index_cell(r, "like_index")},
        {"label": "후기 합", "type": "num", "cls": "col-num",
         "render": lambda r: (num(r["review_sum"]), sort_key(r["review_sum"]))},
        {"label": "상품당 후기", "type": "num", "cls": "col-num",
         "tip": "후기 합 ÷ 후기가 노출된 상품 수. 미노출 상품은 분모에서 뺐다",
         "render": lambda r: (num(round(r["review_avg"])) if r["review_avg"] is not None else MISSING,
                              sort_key(r["review_avg"]))},
        {"label": "규모 대비 후기", "type": "num", "cls": "col-num",
         "tip": "이 품목의 후기 비중 ÷ 상품 수 비중. 1.00이면 상품 수만큼 후기가 쌓인 것이다. "
                "1.30↑ ▲ / 0.70↓ ▼",
         "render": lambda r: index_cell(r, "review_index")},
        {"label": "중위 가격", "type": "num", "cls": "col-num",
         "render": lambda r: (won(r["price_mid"]), sort_key(r["price_mid"]))},
        {"label": "평균 할인율", "type": "num", "cls": "col-num",
         "render": lambda r: (num(round(r["discount_avg"]), "%") if r["discount_avg"] is not None else MISSING,
                              sort_key(r["discount_avg"]))},
        {"label": "평점", "type": "num", "cls": "col-num",
         "tip": "후기 수로 가중한 평균 평점. 후기 1건인 상품과 1,000건인 상품을 같게 세지 않는다",
         "render": lambda r: (rating_fmt(r["rating"]), sort_key(r["rating"]))},
    ]
    return product_table(rows, columns, table_id, placeholder="품목명으로 거르기")


def category_popularity_body(datasets):
    """플랫폼별 '어떤 품목(카테고리)이 인기인가'.

    같은 상품을 플랫폼 간에 매칭하지 않는다(비범위). 품목 단위로 묶어 사이트별
    합·상품당 평균·규모 대비 반응까지 낸다 — 전부 노출값의 합과 비율이고 추정이 아니다.
    """
    parts = []
    for d in datasets:
        label = site_name(d["meta"].get("site"))
        agg = category_stats(d["items"])
        bars = []
        for metric, metric_label in (("like_sum", "하트 합"), ("review_sum", "후기 합")):
            rows = sorted(
                (
                    (cat, values[metric], "%d개 상품" % values["count"])
                    for cat, values in agg.items()
                    if values[metric] > 0
                ),
                key=lambda r: -r[1],
            )
            if rows:
                bars.append(
                    '<div class="chart-block"><h3>%s — 품목별 %s (상위 8)</h3>%s</div>'
                    % (esc(label), esc(metric_label), hbar_chart(rows, max_rows=8))
                )
        for metric, metric_label in (("like", "상품당 하트"), ("review", "상품당 후기")):
            rows = sorted(
                (
                    (cat, values["%s_sum" % metric] / values["%s_n" % metric],
                     "%d개 상품" % values["count"])
                    for cat, values in agg.items()
                    if values["%s_n" % metric]
                ),
                key=lambda r: -r[1],
            )
            if rows:
                bars.append(
                    '<div class="chart-block"><h3>%s — 품목별 %s (상위 8)</h3>%s</div>'
                    % (esc(label), esc(metric_label), hbar_chart(rows, max_rows=8))
                )
        if bars:
            parts.append('<div class="chart-grid">%s</div>' % "".join(bars))
        table = category_scorecard(
            d["items"], "t-cat-%s" % (d["meta"].get("site") or len(parts))
        )
        if table:
            parts.append("<h3>%s — 품목 성적표</h3>%s" % (esc(label), table))
    return "".join(parts)


# ── 멀티 플랫폼 매칭 (SPEC v6 §4 스토리1) ───────────────────────────────────
#
# 규칙은 하나뿐이다: 정규화 상품명 완전일치.
# 유사도·가격 보조 매칭은 금지다(SPEC §3 비범위). 인사일런스 실측에서 유사도 0.82↑
# 후보 11건 중 대부분이 다른 상품이었고, `레터링 그래픽 티셔츠`와 `스탠실 그래픽 티셔츠`는
# 정가까지 66,000원으로 같아 **가격이 오탐을 확증**했다. 그래서 가격도 신호로 못 쓴다.

# 괄호/대괄호 안은 매칭에서 뺀다 — "[2 PACK]", "(단독)" 같은 유통 표기가 붙기 때문이다.
BRACKETS_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
# 한글·영숫자만 남긴다. 공백·`_`·`/`·하이픈은 사이트마다 다르게 넣는다.
NON_WORD_RE = re.compile(r"[^0-9a-z가-힣]")


def match_key(name):
    """동일 상품 판정에 쓰는 정규화 상품명. 이 키가 같을 때만 같은 상품이다."""
    if not name:
        return ""
    lowered = str(name).lower()
    return NON_WORD_RE.sub("", BRACKETS_RE.sub(" ", lowered))


def site_of(dataset, fallback_idx=0):
    return dataset["meta"].get("site") or ("d%d" % fallback_idx)


def build_union(datasets):
    """사이트별 items를 합집합 행으로 묶는다.

    반환하는 행 하나는 "한 상품"이고, `by_site`에 그 상품이 입점한 사이트별 레코드가 들어간다.
    두 사이트에 다 있으면 `by_site`가 2개짜리이고 표에서 1행으로 나온다.

    같은 사이트 안에서 정규화 키가 겹치면(동명이품) 첫 레코드를 대표로 쓰고 나머지는
    `collisions`에 세어 리포트가 밝힌다 — 조용히 버리지 않는다.
    """
    sites = []
    for idx, dataset in enumerate(datasets):
        site = site_of(dataset, idx)
        if site not in sites:
            sites.append(site)

    rows = {}
    order = []
    collisions = 0
    for idx, dataset in enumerate(datasets):
        site = site_of(dataset, idx)
        for item in dataset["items"]:
            key = match_key(item.get("name"))
            # 키가 빈 상품(이름 결측)은 매칭 대상이 아니다. product_id로 고유하게 둔다.
            if not key:
                key = "\x00%s\x00%s" % (site, item.get("product_id") or len(order))
            row = rows.get(key)
            if row is None:
                row = {"key": key, "by_site": {}, "extras": {}}
                rows[key] = row
                order.append(key)
            if site in row["by_site"]:
                row["extras"].setdefault(site, []).append(item)
                collisions += 1
            else:
                row["by_site"][site] = item
    return {
        "sites": sites,
        "rows": [rows[k] for k in order],
        "collisions": collisions,
    }


def union_pick(row, sites, field):
    """대표값 — 입력 순서상 먼저 온 사이트의 값을 쓴다(있는 것 중에서)."""
    for site in sites:
        item = row["by_site"].get(site)
        if item is not None:
            value = item.get(field)
            if value not in (None, ""):
                return value
    return None


def matched_rows(union):
    return [r for r in union["rows"] if len(r["by_site"]) > 1]


def solo_rows(union, site):
    return [r for r in union["rows"] if list(r["by_site"]) == [site]]


# ── 품목 통합축 (SPEC v6 §4 스토리1) ────────────────────────────────────────
#
# 사이트 카테고리 이름은 서로 다르다 — 인사일런스 실측에서 29CM 43종 / 무신사 37종 중
# 이름이 그대로 겹치는 건 8종뿐이다. 사람이 매핑 표를 관리하지 않고, **매칭된 동일 상품이
# 각 사이트에서 어느 카테고리에 들어가 있는지를 세어 코드가 대응을 만든다.**

def build_category_axis(union):
    """매칭 상품의 카테고리 동시출현으로 통합축을 만든다.

    기준축은 첫 사이트의 카테고리 이름이다. 다른 사이트의 카테고리는 **같은 상품이
    기준 사이트에서 어느 카테고리에 있었는지**를 세어 최다 대응으로 붙인다.
    대응이 안 생긴 카테고리는 억지로 붙이지 않고 자기 이름을 그대로 쓴다.
    """
    sites = union["sites"]
    if len(sites) < 2:
        return {"base": sites[0] if sites else None, "map": {}, "evidence": {}}
    base = sites[0]
    pairs = {}
    for row in matched_rows(union):
        base_item = row["by_site"].get(base)
        if base_item is None:
            continue
        base_cat = base_item.get("category")
        if not base_cat:
            continue
        for site, item in row["by_site"].items():
            if site == base:
                continue
            cat = item.get("category")
            if not cat:
                continue
            tally = pairs.setdefault((site, cat), {})
            tally[base_cat] = tally.get(base_cat, 0) + 1

    mapping, evidence = {}, {}
    for (site, cat), tally in pairs.items():
        best = max(sorted(tally), key=lambda c: tally[c])
        mapping[(site, cat)] = best
        evidence[(site, cat)] = {"count": tally[best], "total": sum(tally.values())}
    return {"base": base, "map": mapping, "evidence": evidence}


def unified_category(axis, site, category):
    """사이트별 카테고리를 통합축 이름으로 바꾼다. 대응이 없으면 원래 이름이다."""
    if not category:
        return "(카테고리 없음)"
    if site == axis.get("base"):
        return category
    return axis["map"].get((site, category), category)


def row_category(row, sites, axis):
    """합집합 행의 통합축 품목. 기준 사이트 값이 있으면 그것을, 없으면 매핑된 이름을 쓴다."""
    base = axis.get("base")
    base_item = row["by_site"].get(base)
    if base_item is not None and base_item.get("category"):
        return base_item["category"]
    for site in sites:
        item = row["by_site"].get(site)
        if item is not None and item.get("category"):
            return unified_category(axis, site, item["category"])
    return "(카테고리 없음)"


def presence_label(row, sites):
    """입점 칼럼 값. 사이트 수와 무관하게 성립해야 한다(플랫폼은 확장 축이다)."""
    present = [s for s in sites if s in row["by_site"]]
    if len(present) == len(sites) and len(sites) > 1:
        return "양쪽 입점" if len(sites) == 2 else "전 플랫폼 입점"
    return " · ".join("%s 단독" % site_name(s) for s in present)


def union_columns(sites, axis, union=None):
    """합집합 표의 열. 매칭된 상품은 1행이고 사이트별 값이 나란히 들어간다."""
    columns = [
        {"label": "이미지", "type": "none", "cls": "col-img",
         "render": lambda r, s=sites: thumb(union_repr(r, s))},
        {"label": "상품명", "type": "text", "cls": "col-name",
         "render": lambda r, s=sites: name_cell(union_repr(r, s))},
        {"label": "입점", "type": "none", "cls": "col-presence",
         "tip": "이 상품이 어느 플랫폼에 있는지. 정규화 상품명이 완전히 같을 때만 같은 상품으로 묶는다",
         "render": lambda r, s=sites: (
             '<span class="tag tag-presence%s">%s</span>'
             % (" is-all" if len(r["by_site"]) == len(s) else "", esc(presence_label(r, s))),
             presence_label(r, s))},
        {"label": "브랜드", "type": "text",
         "render": lambda r, s=sites: (esc(union_pick(r, s, "brand")), union_pick(r, s, "brand") or "")},
        {"label": "품목", "type": "none",
         "render": lambda r, s=sites, a=axis: (
             esc(row_category(r, s, a)), row_category(r, s, a))},
    ]
    # 사이트마다 가격·후기·평점·좋아요를 나란히 놓는다. 한쪽에만 있으면 그 칸은 '—'다.
    for site in sites:
        label = site_name(site)
        columns.append(
            {"label": "%s 가격" % label, "type": "num", "cls": "col-num",
             "render": lambda r, s=site: (
                 price_cell(r["by_site"][s]) if s in r["by_site"] else ("<span class=\"na\">—</span>", ""))}
        )
    if len(sites) == 2:
        a, b = sites
        columns.append(
            {"label": "가격 차이", "type": "num", "cls": "col-num",
             "tip": "%s 판매가 − %s 판매가. 양수면 %s가 더 비싸다. "
                    "한쪽에만 있는 상품은 비교 대상이 아니라 미노출이 아니다"
                    % (site_name(a), site_name(b), site_name(a)),
             "render": lambda r, s2=sites: (
                 (lambda gap: MISSING if gap is None else (
                     '<span class="na">동일</span>' if gap == 0
                     else '<span class="tag tag-%s">%s%s</span>'
                          % ("off" if gap < 0 else "out",
                             "+" if gap > 0 else "−",
                             format(abs(int(gap)), ","))))(union_price_gap(r, s2)),
                 sort_key(union_price_gap(r, s2)))}
        )
    # 후기·평점·좋아요도 **그 사이트가 실제로 노출한 지표일 때만** 열을 만든다
    # (2026-07-30). 자사몰은 이 셋을 수집하지 않으므로 열이 통째로 비는데, 빈 열을 그리면
    # "반응이 없는 플랫폼"으로 읽힌다 — 미수집과 0은 다르다(E-DQ-9). 조회수·누적판매는
    # union_metric_columns가 같은 규칙을 이미 쓰고 있었다.
    for metric, metric_label, fmt in (
        ("review_count", "후기", lambda v: num(v)),
        ("rating", "평점", lambda v: rating_fmt(v)),
        ("like_count", "좋아요", lambda v: num(v)),
    ):
        display_key = "%s_display" % metric
        for site in sites:
            if not site_exposes(union, site, metric, display_key):
                continue
            columns.append(
                {"label": "%s %s" % (site_name(site), metric_label), "type": "num", "cls": "col-num",
                 "render": lambda r, s=site, m=metric, f=fmt: (
                     (f(r["by_site"][s].get(m)), sort_key(r["by_site"][s].get(m)))
                     if s in r["by_site"] else ("<span class=\"na\">—</span>", ""))}
            )
    # 조회수·누적판매처럼 한쪽만 노출하는 지표의 열은 union_metric_columns가 붙인다
    # (노출 여부를 실제 데이터로 판정해야 해서 union 전체가 필요하다).
    return columns


def union_repr(row, sites):
    """대표 레코드 — 이미지·상품명처럼 사이트가 달라도 같은 값을 쓰는 열에 넘긴다."""
    for site in sites:
        if site in row["by_site"]:
            return row["by_site"][site]
    return {}


def site_exposes(union, site, metric, display_key=None):
    """그 사이트가 이 지표를 하나라도 노출했는가.

    `union`이 없으면(구 호출 형태) 판정할 자료가 없으므로 노출된 것으로 본다 —
    열을 잘못 지우는 쪽이 잘못 그리는 쪽보다 나쁘다.
    """
    if union is None:
        return True
    display_key = display_key or "%s_display" % metric
    return any(
        row["by_site"][site].get(metric) is not None
        or row["by_site"][site].get(display_key) is not None
        for row in union["rows"]
        if site in row["by_site"]
    )


def union_metric_columns(union, sites):
    """조회수·누적판매처럼 **한쪽만 노출하는 지표**의 열. 노출된 사이트만 열을 만든다."""
    columns = []
    for metric, metric_label, _unit in (("view_count", "조회수", "회"), ("purchase_count", "누적판매", "")):
        display_key = "%s_display" % metric
        for site in sites:
            if not site_exposes(union, site, metric, display_key):
                continue
            columns.append(
                {"label": "%s %s" % (site_name(site), metric_label), "type": "num", "cls": "col-num",
                 "render": (lambda r, s=site, m=metric, d=display_key: (
                     approx_cell(r["by_site"][s], m, d) if s in r["by_site"]
                     else ("<span class=\"na\">—</span>", "")))}
            )
    return columns


def presence_facet(sites):
    """입점 축은 **플랫폼별 칩이고 여러 개를 켜면 AND**다 (2026-07-30 개정).

    다른 축은 한 축 안에서 OR이지만(§4 리포트 공통) 이 축만 예외다. 이유는 이 축을
    쓰는 목적 자체가 **"무신사와 29CM 둘 다에 있는 상품"**을 골라보는 것이기 때문이다 —
    OR로 두면 "무신사에 있거나 29CM에 있는 것"이 되어 거의 전 행이 남아 쓸모가 없다.

    구 구조는 값이 `양쪽 입점`·`무신사 단독`처럼 조합을 미리 나열한 문자열 하나였다.
    플랫폼이 3개가 되면 조합이 폭발하고 "정확히 2곳"을 고를 방법이 없었다.
    """
    return {
        "label": "입점",
        "match": "all",
        "values": lambda r, s=sites: [site_name(x) for x in s if x in r["by_site"]],
    }


def union_category_facet(sites, axis):
    return {
        "label": "품목",
        "values": lambda r, s=sites, a=axis: [row_category(r, s, a)],
    }


# ── 고도화 4개 축 (SPEC v6 §4 스토리1) ──────────────────────────────────────

def overlap_ratio(row):
    """2곳 이상에 입점한 상품의 비율. 합집합 − 단독 합계가 곧 '2곳 이상'이다."""
    if not row["total"]:
        return 0.0
    return (row["total"] - sum(row["solo"].values())) / row["total"]


def coverage_gap_block(union, sites, axis, table_id):
    """① 품목별 입점 커버리지 갭 — 어느 품목을 어느 플랫폼에 몰아줬는가."""
    # 열은 **플랫폼별 `입점`과 플랫폼별 `단독` 두 쌍**이다 (2026-07-30 개정).
    #
    # 구 구조는 `모든 플랫폼에 있음`(both) + 플랫폼별 `단독` 뿐이었다. 플랫폼이 2개일 때는
    # 이 둘이 모든 경우를 덮지만 **3개가 되면 "정확히 2곳에만 입점"이 어느 열에도 안 들어가
    # 상품이 표에서 사라진다** (실측: 합집합 28인데 열 합계 26). 게다가 헤더의 `양쪽 입점`은
    # `2곳 이상`(matched_rows)이라 같은 이름이 다른 값을 말하고 있었다.
    #
    # 플랫폼별 `입점` 열은 **그 플랫폼에 있으면 센다** — 여러 플랫폼에 있는 상품은 여러 열에
    # 잡히므로 열 합계가 합집합보다 클 수 있다(정상). 대신 **사라지는 상품이 없고**
    # 열 수가 플랫폼 수에 선형으로만 늘어난다.
    agg = {}
    for row in union["rows"]:
        cat = row_category(row, sites, axis)
        entry = agg.setdefault(
            cat, {"total": 0, "on": {s: 0 for s in sites}, "solo": {s: 0 for s in sites}})
        entry["total"] += 1
        present = [s for s in sites if s in row["by_site"]]
        for s in present:
            entry["on"][s] += 1
        if len(present) == 1:
            entry["solo"][present[0]] += 1
    if not agg:
        return None

    rows = [dict(v, cat=k) for k, v in agg.items()]
    rows.sort(key=lambda r: -r["total"])
    columns = [
        {"label": "품목", "type": "none", "render": lambda r: (esc(r["cat"]), r["cat"])},
        {"label": "합집합", "type": "num", "cls": "col-num",
         "render": lambda r: (num(r["total"]), r["total"])},
    ]
    for site in sites:
        columns.append(
            {"label": "%s 입점" % site_name(site), "type": "num", "cls": "col-num",
             "tip": "그 플랫폼에 있는 상품 수. 여러 플랫폼에 있는 상품은 각 열에 모두 잡히므로 "
                    "열을 더하면 합집합보다 클 수 있다",
             "render": lambda r, s=site: (num(r["on"][s]), r["on"][s])}
        )
    for site in sites:
        columns.append(
            {"label": "%s 단독" % site_name(site), "type": "num", "cls": "col-num",
             "tip": "그 플랫폼에만 있는 상품 수. 단독 열들의 합 + 2곳 이상 입점 = 합집합이다",
             "render": lambda r, s=site: (num(r["solo"][s]), r["solo"][s])}
        )
    columns.append(
        {"label": "겹침 비율", "type": "num", "cls": "col-num",
         "tip": "2곳 이상에 입점한 상품 ÷ 그 품목 합집합. 낮을수록 플랫폼별로 다른 상품을 내놓은 품목이다",
         "render": lambda r: (
             '<span class="idx">%.0f%%</span>' % (overlap_ratio(r) * 100) if r["total"] else MISSING,
             overlap_ratio(r) if r["total"] else "")}
    )

    # 구 `단독 입점 상품 성격` 블록을 여기로 합쳤다 (2026-07-30).
    # 그 블록의 품목 분포 막대는 아래 표의 `단독` 열과 **같은 값을 같은 순서로** 그리고 있었다
    # (실측: 동점 tie-break만 달랐다). 표를 그 열로 정렬하면 같은 답이므로 막대를 없앤다.
    # 남는 고유 정보는 단독 상품의 **가격대**뿐이라 KPI와 분포로 낸다.
    tiles, charts = [], []
    for site in sites:
        solo_items = [r["by_site"][site] for r in solo_rows(union, site)]
        if not solo_items:
            continue
        prices = [i.get("price_sale") for i in solo_items if is_num(i.get("price_sale"))]
        cats = {}
        for row in solo_rows(union, site):
            cat = row_category(row, sites, axis)
            cats[cat] = cats.get(cat, 0) + 1
        top = sorted(cats.items(), key=lambda kv: -kv[1])
        tiles.append((
            "%s 단독" % site_name(site), format(len(solo_items), ","),
            "합집합의 %.0f%%" % (len(solo_items) / len(union["rows"]) * 100) if union["rows"] else None))
        tiles.append((
            "%s 단독 중위가" % site_name(site),
            won(median(prices)) if prices else MISSING,
            "품목 %d종 · 최다 %s" % (len(cats), top[0][0] if top else "-")))
        if prices:
            charts.append(
                '<div class="chart-block"><h3>%s 단독 — 판매가 분포</h3>%s</div>'
                % (esc(site_name(site)), dist_chart(nice_buckets(prices))))
    head = kpi_tiles(tiles) if tiles else ""
    chart = '<div class="chart-grid">%s</div>' % "".join(charts) if charts else ""
    return head + chart + product_table(rows, columns, table_id, placeholder="품목명으로 거르기")


def response_excluded_note(union, sites):
    """반응 비교에서 빠진 플랫폼을 각주로 밝힌다 (자사몰 등 — SPEC v14).

    조용히 빼면 독자는 그 플랫폼이 애초에 없었다고 읽는다.
    """
    dropped = [s for s in sites
               if not any(site_exposes(union, s, m) for m, _ in COMPARE_METRICS)]
    if not dropped:
        return ""
    return (
        " <strong>%s은(는) 이 비교에서 빠진다</strong> — 그 플랫폼이 좋아요·후기를 "
        "노출하지 않아 수집하지 않았다. <em>반응이 없다는 뜻이 아니라 값이 없다는 뜻이다.</em>"
        % esc(" · ".join(site_name(s) for s in dropped))
    )


def response_strength_block(union, sites, axis, table_id):
    """② 같은 상품 반응 강도 비교 — 매칭된 상품만.

    동일 상품이므로 **상품 구성 차이가 통제된 비교**다. 단독 입점은 제외한다.
    두 사이트가 모두 노출하는 지표(하트·후기)만 쓴다.
    """
    pairs = matched_rows(union)
    # 지표를 아예 수집하지 않는 플랫폼은 이 비교에서 뺀다 (SPEC v14 — 자사몰).
    # 빈 열을 그리면 "반응이 없는 플랫폼"으로 읽힌다. 미수집과 0은 다르다.
    sites = [s for s in sites
             if any(site_exposes(union, s, m) for m, _ in COMPARE_METRICS)]
    if not pairs or len(sites) < 2:
        return None
    agg = {}
    for row in pairs:
        cat = row_category(row, sites, axis)
        entry = agg.setdefault(
            cat,
            {"n": 0, "sums": {(s, m): 0 for s in sites for m, _ in COMPARE_METRICS},
             "ns": {(s, m): 0 for s in sites for m, _ in COMPARE_METRICS}},
        )
        entry["n"] += 1
        for site in sites:
            item = row["by_site"].get(site)
            if item is None:
                continue
            for metric, _label in COMPARE_METRICS:
                if is_num(item.get(metric)):
                    entry["sums"][(site, metric)] += item[metric]
                    entry["ns"][(site, metric)] += 1
    rows = [dict(v, cat=k) for k, v in agg.items()]
    rows.sort(key=lambda r: -r["n"])

    # 전체 매칭 상품의 플랫폼 배율 = 기준선.
    # 플랫폼 규모 자체가 다르면(무신사 하트가 29CM의 8배라면) 품목 배율은 전부 8 근처에서
    # 흔들린다. 그 8을 '무신사가 이 품목에 강하다'로 읽으면 규모 차이를 품목 특성으로
    # 착각하는 것이다. 그래서 **기준선 대비**를 같이 낸다 — 그게 품목의 신호다.
    baseline = {}
    for metric, _label in COMPARE_METRICS:
        totals = {s: sum(r["sums"][(s, metric)] for r in rows) for s in sites}
        if len(sites) == 2:
            a, b = sites
            baseline[metric] = (totals[a] / totals[b]) if totals[b] else None

    columns = [
        {"label": "품목", "type": "none", "render": lambda r: (esc(r["cat"]), r["cat"])},
        {"label": "매칭 상품", "type": "num", "cls": "col-num",
         "tip": "두 플랫폼에 모두 있는 상품 수. 이 표는 이 상품들만 센다",
         "render": lambda r: (num(r["n"]), r["n"])},
    ]
    for metric, metric_label in COMPARE_METRICS:
        for site in sites:
            columns.append(
                {"label": "%s %s" % (site_name(site), metric_label), "type": "num", "cls": "col-num",
                 "render": lambda r, s=site, m=metric: (
                     num(r["sums"][(s, m)]), r["sums"][(s, m)])}
            )
        if len(sites) == 2:
            a, b = sites
            columns.append(
                {"label": "%s 배율" % metric_label, "type": "num", "cls": "col-num",
                 "tip": "%s %s 합 ÷ %s %s 합. 플랫폼 규모가 다르면 이 값은 전 품목에서 함께 커진다 — "
                        "옆의 '기준 대비'로 읽어라"
                        % (site_name(a), metric_label, site_name(b), metric_label),
                 "render": lambda r, m=metric, x=a, y=b: ratio_cell(
                     r["sums"][(x, m)], r["sums"][(y, m)])}
            )
            columns.append(
                {"label": "%s 기준 대비" % metric_label, "type": "num", "cls": "col-num",
                 "tip": "이 품목의 배율 ÷ 전체 매칭 상품의 배율(%s). 1.00이면 브랜드 평균만큼 "
                        "%s에 쏠린 것이고, 1.30↑면 이 품목이 유별나게 %s에서 반응이 좋다는 뜻이다"
                        % (("%.1f배" % baseline[metric]) if baseline.get(metric) else "산출 불가",
                           site_name(a), site_name(a)),
                 "render": lambda r, m=metric, x=a, y=b: index_cell_value(
                     (r["sums"][(x, m)] / r["sums"][(y, m)] / baseline[m])
                     if r["sums"][(y, m)] and baseline.get(m) else None)}
            )

    bars = []
    if len(sites) == 2:
        a, b = sites
        for metric, metric_label in COMPARE_METRICS:
            ranked = sorted(
                (
                    (r["cat"], r["sums"][(a, metric)] / r["sums"][(b, metric)],
                     "매칭 %d개 · %s %s / %s %s"
                     % (r["n"], site_name(a), format(r["sums"][(a, metric)], ","),
                        site_name(b), format(r["sums"][(b, metric)], ",")))
                    for r in rows if r["sums"][(b, metric)] > 0
                ),
                key=lambda x: -x[1],
            )
            if ranked:
                bars.append(
                    '<div class="chart-block"><h3>%s 배율 — %s ÷ %s (상위 8)</h3>%s</div>'
                    % (esc(metric_label), esc(site_name(a)), esc(site_name(b)),
                       hbar_chart(ranked, unit="배", max_rows=8))
                )
    chart = '<div class="chart-grid">%s</div>' % "".join(bars) if bars else ""
    return chart + product_table(rows, columns, table_id, placeholder="품목명으로 거르기")


def price_gap_rows(union, sites):
    """매칭 상품의 플랫폼 간 판매가 차이. 표는 만들지 않는다 — 전 상품 표가 곧 그 표다."""
    if len(sites) != 2:
        return []
    a, b = sites
    out = []
    for row in matched_rows(union):
        ia, ib = row["by_site"].get(a), row["by_site"].get(b)
        if ia is None or ib is None:
            continue
        sa, sb = ia.get("price_sale"), ib.get("price_sale")
        if not (is_num(sa) and is_num(sb)):
            continue
        out.append({"row": row, "gap": sa - sb,
                    "rate_a": ia.get("discount_rate"), "rate_b": ib.get("discount_rate")})
    return out


def union_price_gap(row, sites):
    """합집합 행의 판매가 차이. 매칭이 아니거나 값이 없으면 None."""
    if len(sites) != 2:
        return None
    a, b = sites
    ia, ib = row["by_site"].get(a), row["by_site"].get(b)
    if ia is None or ib is None:
        return None
    sa, sb = ia.get("price_sale"), ib.get("price_sale")
    if not (is_num(sa) and is_num(sb)):
        return None
    return sa - sb


PRICE_SAME = "판매가 동일"
PRICE_DIFF = "판매가 다름"
PRICE_SOLO = "비교 불가"


def price_gap_facet(rows, sites):
    def bucket(row):
        gap = union_price_gap(row, sites)
        if gap is None:
            return [PRICE_SOLO]
        return [PRICE_SAME if gap == 0 else PRICE_DIFF]
    return {"label": "가격", "values": bucket,
            "order": [PRICE_DIFF, PRICE_SAME, PRICE_SOLO]}


def price_gap_summary(union, sites, axis):
    """③ 가격·할인 포지셔닝 — **집계만**. 상품 목록은 전 상품 표에 합쳤다(2026-07-30).

    구 구조는 매칭 상품 전 건을 표로 또 실어서 같은 상품이 리포트에 두 번 나왔다.
    """
    if len(sites) != 2:
        return None
    a, b = sites
    rows = price_gap_rows(union, sites)
    if not rows:
        return None
    for r in rows:
        r["cat"] = row_category(r["row"], sites, axis)
    same_price = len([r for r in rows if r["gap"] == 0])

    # 품목별 평균 할인율 대조 — 가격 정책이 품목 단위로 갈리는지 본다.
    cat_agg = {}
    for r in rows:
        entry = cat_agg.setdefault(r["cat"], {"n": 0, "ra": [], "rb": [], "gaps": []})
        entry["n"] += 1
        if is_num(r["rate_a"]):
            entry["ra"].append(r["rate_a"])
        if is_num(r["rate_b"]):
            entry["rb"].append(r["rate_b"])
        entry["gaps"].append(r["gap"])
    cat_rows = sorted(
        (
            (cat, sum(v["gaps"]) / len(v["gaps"]),
             "매칭 %d개 · 평균 할인 %s %.0f%% / %s %.0f%%"
             % (v["n"], site_name(a), (sum(v["ra"]) / len(v["ra"]) if v["ra"] else 0),
                site_name(b), (sum(v["rb"]) / len(v["rb"]) if v["rb"] else 0)))
            for cat, v in cat_agg.items()
        ),
        key=lambda x: x[1],
    )
    chart = ""
    if cat_rows:
        chart = (
            '<div class="chart-grid"><div class="chart-block">'
            '<h3>품목별 평균 판매가 차이 (%s − %s)</h3>%s</div></div>'
            % (esc(site_name(a)), esc(site_name(b)), hbar_chart(cat_rows, unit="원", max_rows=10))
        )
    note = (
        '<p class="section-note">매칭 %s개 중 <strong>판매가가 같은 상품 %s개</strong>, '
        "다른 상품 %s개다. 판매가는 두 사이트 모두 <strong>전 회원 공통 쿠폰적용가</strong>다"
        "(무신사 <code>finalPrice</code> · 29CM <code>displayPrice</code> — SPEC v8). "
        "<strong>상품별 가격 차이는 아래 '전 상품' 표의 <code>가격 차이</code> 열에서 본다</strong> — "
        "같은 상품 목록을 두 번 싣지 않는다.</p>"
        % (format(len(rows), ","), format(same_price, ","), format(len(rows) - same_price, ","))
    )
    return chart + note


def axis_evidence_note(axis, union):
    """통합축의 근거를 밝힌다 — 독자가 검증할 수 있어야 한다."""
    if not axis.get("map"):
        return ""
    weak, lines = [], []
    for (site, cat), best in sorted(axis["map"].items()):
        ev = axis["evidence"][(site, cat)]
        if cat == best:
            continue
        line = "%s <code>%s</code> → <code>%s</code> (동일 상품 %d건)" % (
            esc(site_name(site)), esc(cat), esc(best), ev["count"])
        lines.append(line)
        if ev["count"] < 3:
            weak.append(line)
    if not lines:
        return ""
    body = (
        "품목 축은 <strong>기준 플랫폼 %s의 카테고리 이름</strong>이다. 다른 플랫폼의 카테고리는 "
        "<em>같은 상품이 기준 플랫폼에서 어느 카테고리에 있었는지</em>를 세어 최다 대응으로 붙였다 — "
        "사람이 매핑 표를 쓰지 않았다. 대응이 생기지 않은 카테고리는 자기 이름을 그대로 쓴다."
        % esc(site_name(axis["base"]))
    )
    if weak:
        body += (
            " <strong>근거 3건 미만인 약한 대응이 %d개</strong> 있다 — 그 품목 행은 신뢰도가 낮다."
            % len(weak)
        )
    return (
        '%s<details class="axis-map"><summary>품목 대응 %d건 보기</summary><ul>%s</ul></details>'
        % (body, len(lines), "".join("<li>%s</li>" % l for l in lines))
    )


def platform_compare_body(union, sites, axis):
    """플랫폼 비교 요약 — 고도화 4개 축을 한 섹션으로."""
    out = []
    matched = matched_rows(union)
    tiles = [("합집합 상품", format(len(union["rows"]), ","), "매칭으로 묶은 뒤의 고유 상품 수")]
    for site in sites:
        n = len([r for r in union["rows"] if site in r["by_site"]])
        tiles.append((site_name(site), format(n, ","), "단독 %s개" % format(len(solo_rows(union, site)), ",")))
    # `matched`는 2곳 이상이다. 플랫폼이 3개 이상이면 `전 플랫폼 입점`이라고 쓰면 안 된다 —
    # 2곳에만 있는 상품이 그 값에 들어 있기 때문이다(구 라벨의 오류).
    tiles.append((
        "양쪽 입점" if len(sites) == 2 else "2곳 이상 입점",
        format(len(matched), ","),
        "합집합의 %.0f%%" % (len(matched) / len(union["rows"]) * 100) if union["rows"] else None,
    ))
    if len(sites) > 2:
        every = len([r for r in union["rows"] if len(r["by_site"]) == len(sites)])
        tiles.append((
            "전 플랫폼 입점", format(every, ","),
            "합집합의 %.0f%%" % (every / len(union["rows"]) * 100) if union["rows"] else None,
        ))
    out.append(kpi_tiles(tiles))

    if union["collisions"]:
        out.append(banner(
            "warn",
            "한 사이트 안에서 정규화 상품명이 겹치는 레코드가 <strong>%d건</strong> 있다. "
            "첫 레코드를 대표로 썼고 나머지는 표에 나오지 않는다 — 동명이품인지 중복 수집인지 확인이 필요하다."
            % union["collisions"],
        ))

    # 설명문의 '양쪽'은 플랫폼 2개 가정이다. 3개 이상이면 '2곳 이상'으로 바꿔 쓴다.
    both_word = "양쪽에 다 있는 상품" if len(sites) == 2 else "2곳 이상에 있는 상품"

    blocks = [
        ("품목별 입점 차이",
         coverage_gap_block(union, sites, axis, "t-coverage"),
         "품목마다 <strong>플랫폼별 입점 수</strong>와 <strong>그 플랫폼에만 있는 수</strong>를 센다. "
         "브랜드가 어느 품목을 어느 플랫폼에 몰아줬는지가 보인다. "
         "<code>입점</code> 열은 여러 플랫폼에 있는 상품을 각 열에 모두 세므로 "
         "<strong>열을 더하면 합집합보다 클 수 있다</strong>(정상). "
         "<code>단독</code> 열들의 합 + 2곳 이상 입점 = 합집합이다. "
         "구 <code>단독 입점 상품 성격</code> 섹션을 이 블록에 합쳤다(2026-07-30) — "
         "그 섹션의 품목 분포 막대는 아래 표의 <code>단독</code> 열과 같은 값이었다. "
         "단독 입점의 고유한 정보는 <strong>가격대</strong>뿐이라 KPI와 판매가 분포로 남겼다. "
         "매칭 실패가 아니라 <strong>유통 전략</strong>으로 읽는다. "
         "매칭은 정규화 상품명 완전일치이므로, 이름이 조금이라도 다르면 단독으로 잡힌다 — "
         "그것이 실제 유통 차이인지 표기 차이인지는 상품명을 눌러 원본을 확인하라."),
        ("같은 상품 반응 강도 비교",
         response_strength_block(union, sites, axis, "t-response"),
         "<strong>%s만</strong> 센다. 같은 상품을 비교하므로 " % both_word +
         "<em>상품 구성 차이가 통제된 비교</em>가 된다 — 한쪽에 상품이 더 많아서 생기는 차이가 아니다. "
         "두 사이트가 모두 정수로 노출하는 지표(좋아요·후기)만 쓴다. "
         "<strong>단독 입점 상품은 이 비교에서 빠진다.</strong>"
         + response_excluded_note(union, sites)),
        ("가격·할인 포지셔닝 차이",
         price_gap_summary(union, sites, axis),
         "%s의 판매가·할인율을 <strong>품목 단위로 집계</strong>한다. " % both_word +
         "정가가 같은데 판매가만 다르면 그것이 플랫폼별 할인 정책 차이다. "
         "<strong>상품별 대조는 '전 상품' 표에 합쳤다</strong>(2026-07-30) — "
         "구 구조는 매칭 상품 전 건을 여기서 또 실어서 같은 상품이 리포트에 두 번 나왔다."),
    ]
    for title, body, note in blocks:
        if body:
            out.append(section(title, body, note=note))
    return "".join(out)


def linesheet_body(datasets):
    all_items = [i for d in datasets for i in d["items"]]
    discounted = [i.get("discount_rate") for i in all_items if is_num(i.get("discount_rate")) and i["discount_rate"] > 0]
    sold_out = len([i for i in all_items if i.get("sold_out")])
    reviews = sum(i.get("review_count") or 0 for i in all_items if is_num(i.get("review_count")))

    union = build_union(datasets)
    sites = union["sites"]
    axis = build_category_axis(union)
    multi = len(sites) > 1

    out = [
        kpi_tiles(
            [
                ("수집 상품", "%s" % format(len(all_items), ","),
                 "%d개 플랫폼 합산" % len(sites) if multi else None),
                ("할인 중", "%s" % format(len(discounted), ","),
                 "평균 %d%%" % (sum(discounted) / len(discounted)) if discounted else "할인 상품 없음"),
                ("품절", "%s" % format(sold_out, ","), "%.0f%%" % (sold_out / len(all_items) * 100) if all_items else None),
                ("후기 합계", "%s" % format(int(reviews), ","), "노출된 값만 합산"),
            ]
        )
    ]

    # 플랫폼이 2개 이상일 때만 만드는 비교 레이어. 1개면 비교 대상이 없으니 만들지 않는다.
    if multi:
        out.append(
            section(
                "플랫폼 비교 요약",
                platform_compare_body(union, sites, axis),
                note="같은 상품 판정은 <strong>정규화 상품명 완전일치</strong>뿐이다 — 상품명을 소문자로 바꾸고 "
                "대괄호·괄호 안을 지우고 한글·영숫자만 남겨 비교한다. "
                "<strong>유사도나 가격으로 추정 매칭하지 않는다</strong>(실측에서 정가까지 같은 다른 상품이 "
                "확인됐다). 그래서 매칭되지 않은 상품은 오류가 아니라 <em>단독 입점 사실</em>이다. "
                + axis_evidence_note(axis, union),
            )
        )

    # 플랫폼별 인기 ① 품목(카테고리) 단위 — 사이트별 원본 카테고리 기준
    category_body = category_popularity_body(datasets)
    if category_body:
        out.append(
            section(
                "플랫폼별 품목 인기",
                category_body,
                note="품목 = <strong>사이트 원본 카테고리</strong>다(통합축이 아니다 — 원본을 그대로 보존한다). "
                "사이트별로 노출된 하트 수·후기 수를 품목 단위로 합산했다. "
                "<strong>상품당 평균</strong>은 그 지표가 노출된 상품 수로만 나눴다(미노출은 분모에서 뺐다 — "
                "0으로 세면 평균이 조용히 낮아진다). "
                "<strong>규모 대비 하트·후기</strong>는 <em>그 품목의 지표 비중 ÷ 상품 수 비중</em>이다. "
                "예를 들어 상품 수가 전체의 5%인 품목이 하트의 20%를 받았다면 4.00이고, "
                "'상품 수에 비해 4배로 반응이 왔다'는 뜻이다. 1.00이면 규모만큼, 1.30 이상 ▲, 0.70 이하 ▼. "
                "상품 수가 1~2개인 품목은 이 값이 크게 튀니 <strong>상품 수 열과 같이 보라.</strong> "
                "전부 노출값의 합과 비율이며 추정이 아니다. "
                + ("이 섹션은 <strong>사이트별 전 상품</strong>을 세므로 단독 입점도 포함된다 — "
                   "매칭 상품만 보는 비교는 위 '플랫폼 비교 요약'에 있다." if multi else ""),
            )
        )

    if multi:
        # 합집합 표 — 매칭된 상품은 1행이고 `입점` 칼럼에 어느 플랫폼에 있는지가 찍힌다.
        columns = union_columns(sites, axis, union) + union_metric_columns(union, sites)
        out.append(
            section(
                "전 상품 (플랫폼 합집합)",
                product_table(
                    union["rows"], columns, "t-linesheet",
                    facets=[sold_out_facet(union["rows"], lambda r, s=sites: union_sold_out(r, s)),
                            price_gap_facet(union["rows"], sites),
                            presence_facet(sites), union_category_facet(sites, axis)],
                ),
                note="<strong>%s개 행</strong> = 플랫폼 합집합이다. 양쪽에 있는 상품은 1행이고 "
                "플랫폼별 값이 나란히 들어간다. 한쪽에만 있는 칸은 <span class=\"na\">—</span>다 "
                "(미노출이 아니라 <em>그 플랫폼에 그 상품이 없다</em>는 뜻이다). "
                "<strong>가격 차이</strong> 열은 양쪽에 다 있는 상품의 판매가 차이다 — "
                "구 <code>가격·할인 포지셔닝 차이</code> 섹션의 상품 표를 이 표에 합쳤다(2026-07-30). "
                "같은 상품 목록이 리포트에 두 번 나오지 않는다. 품목 단위 집계는 그 섹션에 남아 있다. "
                "<strong>판매</strong>·<strong>가격</strong>·<strong>입점</strong>·<strong>품목</strong> 칩으로 "
                "걸러 볼 수 있고, 한 축에서 여러 개를 켜면 OR다. "
                "품목은 통합축이며 대응 근거는 '플랫폼 비교 요약' 각주에 있다."
                % format(len(union["rows"]), ","),
            )
        )
    else:
        out.append(
            section(
                "전 상품",
                product_table(
                    all_items, CORE_COLUMNS, "t-linesheet",
                    facets=[sold_out_facet(all_items), category_facet(all_items)],
                ),
                note="<strong>기본은 판매 중만</strong> 보여준다 — 품절을 함께 수집하면 표가 두 배로 길어져 "
                "훑기 어렵다. <code>판매</code> 칩으로 품절을 켜면 된다(건수는 칩에 찍힌다). "
                "카테고리 칩은 여러 개를 켜면 OR로 걸린다. 열 머리를 누르면 오름차순↔내림차순으로 정렬한다 "
                "(값이 없는 행은 방향과 무관하게 뒤로 간다).",
            )
        )
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

    facets = [sold_out_facet(all_items)] + attribute_facets(all_items) + [category_facet(all_items)]
    out.append(
        section(
            "전 상품",
            product_table(
                all_items, SCAN_COLUMNS + attr_column(all_items), "t-scan", facets=facets
            ),
            note="<strong>기본은 판매 중만</strong>이다 — <code>판매</code> 칩으로 품절을 켤 수 있다. "
            "속성 칩은 여러 개를 켜면 OR로 걸리고, 서로 다른 축끼리는 AND로 걸린다. "
            "열 머리를 누르면 오름차순↔내림차순으로 정렬한다(값이 없는 행은 방향과 무관하게 뒤로 간다).",
        )
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


def series_bounds(series, field):
    """추이에서 그 필드의 첫 값과 마지막 값. 미노출 시점은 건너뛴다."""
    values = [p.get(field) for p in series if is_num(p.get(field))]
    if not values:
        return None, None
    return values[0], values[-1]


# 끝점 상태 — '전 구간/일부 구간'을 대신한다 (2026-07-30 사용자 지적).
# 그 라벨은 "첫·마지막 스냅샷 둘 다에 있었나"라는 구현 사정을 말하는데 읽는 사람은
# "관측이 온전한가"로 읽었다. 사건 언어로 바꾸고, 온전함은 체류 횟수 숫자로 따로 낸다.
ENDPOINT_LABEL = {
    "stay": "유지",
    "in": "신규 진입",
    "out": "이탈",
    "mid": "기간 중만",
}
ENDPOINT_ORDER = [ENDPOINT_LABEL[k] for k in ("stay", "in", "out", "mid")]

OBS_MANY = "관측 2회 이상"
OBS_ONCE = "1회만 스침"


def observed_indices(series, stamp_index):
    """그 상품이 순위권에 있던 시점 인덱스. 순위가 있는 시점만 센다."""
    found = []
    for point in series:
        idx = stamp_index.get(point.get("at"))
        if idx is not None and is_num(point.get("rank")):
            found.append(idx)
    return sorted(set(found))


def endpoint_state(observed, last_index):
    if not observed:
        return "mid"
    has_first = observed[0] == 0
    has_last = observed[-1] == last_index
    if has_first and has_last:
        return "stay"
    if has_last:
        return "in"
    if has_first:
        return "out"
    return "mid"


def churn_rate(trends, stamps):
    """인접 스냅샷 평균 신규 유입률(%).

    첫·마지막 두 시점만 비교하는 '신규 진입/이탈'은 기간이 길어지면 실제 교체를
    과소 표기한다(33개 스냅샷 실측: 등장 679개 vs 랭킹 크기 100). 인접 쌍마다
    "이번에 새로 들어온 비율"을 재서 평균한다.
    """
    stamp_index = {stamp: idx for idx, stamp in enumerate(stamps)}
    present = [set() for _ in stamps]
    for trend in trends:
        pid = str(trend.get("product_id"))
        for idx in observed_indices(trend.get("series") or [], stamp_index):
            present[idx].add(pid)
    rates = []
    for before, after in zip(present, present[1:]):
        if not after:
            continue
        rates.append(len(after - before) / len(after))
    if not rates:
        return None
    return sum(rates) / len(rates) * 100


# 플랫폼 반응 강도 비교에 쓸 수 있는 지표.
# **두 사이트가 모두 정확한 정수로 노출하는 것만** 넣는다 — 조회수·누적판매는 29CM이
# 노출하지 않아 비교가 성립하지 않는다(SPEC §4 스토리1). 평점·가격은 합산이 뜻이 없어
# 별도 섹션(가격·할인 포지셔닝)에서 다룬다.
COMPARE_METRICS = (
    ("like_count", "하트"),
    ("review_count", "후기"),
)


def ratio_cell(value, base):
    """기준값 대비 몇 배인지. 기준이 0이거나 없으면 만들지 않는다.

    플랫폼 비교(같은 상품 반응 강도)가 쓴다. 평균이 아니라 중위값을 기준으로 넘겨야
    상위 몇 개에 기준이 흔들리지 않는다.
    """
    if not is_num(value) or not is_num(base) or base <= 0:
        return MISSING, ""
    ratio = value / base
    return '<span class="idx">×%.1f</span>' % ratio, ratio


def move_bucket(delta, threshold):
    if delta >= threshold:
        return "급상승"
    if delta > 0:
        return "상승"
    if delta <= -threshold:
        return "급하락"
    if delta < 0:
        return "하락"
    return "유지"


def duration_text(minutes):
    if minutes is None:
        return ""
    if minutes < 60:
        return "%d분" % minutes
    hours = minutes / 60.0
    if abs(hours - round(hours)) < 0.05:
        return "%d시간" % round(hours)
    return "%.1f시간" % hours


def gap_minutes(from_at, to_at):
    """관측 창의 길이. 파싱이 안 되면 만들지 않는다."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            start = datetime.strptime(from_at, fmt)
            end = datetime.strptime(to_at, fmt)
        except (TypeError, ValueError):
            continue
        return int((end - start).total_seconds() // 60)
    return None


def stay_cell(row):
    """랭킹 체류 — '28/33회 (85%)'. 숫자가 라벨보다 직관적이다."""
    obs, total = row["_obs"], row["_total"]
    pct = (obs / total * 100) if total else 0
    return (
        '%d/%d회<span class="vs">%.0f%%</span>' % (obs, total, pct),
        obs,
    )


def price_range_text(row):
    lo, hi = row.get("_p_lo"), row.get("_p_hi")
    if not is_num(lo) or not is_num(hi):
        return "가격 미노출"
    if lo == hi:
        return "%s 유지" % won(lo)
    return "%s ~ %s" % (won(lo), won(hi))


def spark_cell(row, kind, tail):
    """스파크라인 자리. 실제 그리기는 인라인 JS가 한다(계열 데이터는 한 번만 심는다)."""
    return (
        '<span class="spark" data-spark="%s" data-pid="%s"></span>'
        '<span class="vs">%s</span>' % (kind, esc(row["product_id"]), tail),
        "",
    )


def rank_price_body(diff, rows, changes, threshold, stamps):
    """상품별 추이 표 — 한 행 = 한 상품.

    구 구조는 신규 진입 표 · 이탈 표 · 순위 변동 표 · 가격 변화 표를 따로 뒀는데,
    열 구성이 거의 같았고 그러면서도 등장 상품 679개 중 158개(23%)만 실렸다.
    한 표로 합치고 끝점 상태를 필터 축으로 낸다(2026-07-30 사용자 승인).
    """
    if not rows:
        return '<p class="empty">기간 내 순위권에 오른 상품이 없다</p>'

    def price_change_cell(row):
        first, last = row["_p_first"], row["_p_last"]
        if not is_num(first) or not is_num(last):
            return MISSING, ""
        if first == last:
            return '%s <span class="na">유지</span>' % won(last), 0
        gap = last - first
        return (
            '%s<span class="vs">%s%s원 · 처음 %s</span>'
            % (won(last), "+" if gap > 0 else "−", format(abs(int(gap)), ","), won(first)),
            gap,
        )

    def discount_cell(row):
        first, last = row["_d_first"], row["_d_last"]
        if not is_num(first) or not is_num(last):
            return MISSING, ""
        if first == last:
            return '%d%% <span class="na">유지</span>' % int(first), 0
        return (
            '%d%% <span class="arrow">→</span> <strong>%d%%</strong>' % (int(first), int(last)),
            last - first,
        )

    def kinds_cell(row):
        if not row["_kinds"]:
            return '<span class="na">%s</span>' % NO_PRICE_CHANGE, ""
        tags = "".join(
            '<span class="tag tag-off">%s</span>' % esc(kind) for kind in row["_kinds"]
        )
        window = row["_window"] or {}
        if window.get("exact"):
            when = '<span class="vs">%s 확정</span>' % esc((window.get("to_at") or "")[5:16])
        else:
            span = duration_text(gap_minutes(window.get("from_at"), window.get("to_at")))
            when = '<span class="vs">%s ~ %s 사이%s · 스냅샷 %s개 결석</span>' % (
                esc((window.get("from_at") or "")[5:16]),
                esc((window.get("to_at") or "")[5:16]),
                " (%s)" % esc(span) if span else "",
                esc(window.get("gap")),
            )
        count = '<span class="vs">%d회</span>' % row["_events"] if row["_events"] > 1 else ""
        return tags + when + count, window.get("to_at") or ""

    def rank_delta_cell(row):
        if not is_num(row["delta"]):
            return MISSING, ""
        delta = row["delta"]
        return (
            '<span class="delta delta-%s">%s%d</span>'
            % ("up" if delta > 0 else ("down" if delta < 0 else "flat"),
               "▲" if delta > 0 else ("▼" if delta < 0 else "—"),
               abs(delta)),
            sort_key(delta),
        )

    columns = [
        {"label": "이미지", "type": "none", "cls": "col-img", "render": thumb},
        {"label": "상품명", "type": "text", "cls": "col-name", "render": name_cell},
        {"label": "브랜드", "type": "text",
         "render": lambda r: (esc(r.get("brand")), r.get("brand") or "")},
        {"label": "끝점", "type": "none",
         "tip": "기간 양 끝에 있었는지. 유지=처음·마지막 다 있음 / 신규 진입=마지막에만 / "
                "이탈=처음에만 / 기간 중만=양 끝에 없고 중간에만 순위권",
         "render": lambda r: ('<span class="tag tag-%s">%s</span>'
                              % ({"out": "out2"}.get(r["_end"], r["_end"]),
                                 ENDPOINT_LABEL[r["_end"]]), "")},
        {"label": "랭킹 체류", "type": "num", "cls": "col-num",
         "tip": "스냅샷 몇 번 중 몇 번 순위권에 있었나. 끊긴 구간은 순위 추이 선이 끊겨 보인다",
         "render": stay_cell},
        {"label": "순위 추이", "type": "none", "cls": "col-spark",
         "tip": "왼쪽이 기간 시작. 위로 갈수록 상위. 순위권 밖이던 구간은 선이 끊긴다. "
                "가로 축과 순위 축은 전 행이 같은 범위라 모양을 서로 비교할 수 있다",
         "render": lambda r: spark_cell(
             r, "rank",
             "최고 %s · 마지막 %s" % (
                 num(r["_best"], "위") if is_num(r["_best"]) else "-",
                 num(r["rank_last"], "위") if is_num(r["rank_last"]) else "-"))},
        {"label": "순위 변동", "type": "num", "cls": "col-num",
         "tip": "첫 관측 순위 − 마지막 관측 순위", "render": rank_delta_cell},
        {"label": "가격 추이", "type": "none", "cls": "col-spark",
         "tip": "상품마다 자기 가격 범위로 그린다(행끼리 높이를 비교하지 말 것). "
                "뒤에 깔린 띠는 할인 구간이다. 꼬리표는 관측된 최저~최고 가격이다",
         "render": lambda r: spark_cell(r, "price", price_range_text(r))},
        {"label": "가격 변화", "type": "num", "cls": "col-num", "render": price_change_cell},
        {"label": "할인율 변화", "type": "num", "cls": "col-num", "render": discount_cell},
        {"label": "감지된 변화 · 시점", "type": "text", "render": kinds_cell},
    ]

    once = [r for r in rows if r["_obs"] < 2]
    facets = [
        {
            "label": "끝점",
            "values": lambda r: [ENDPOINT_LABEL[r["_end"]]],
            "order": ENDPOINT_ORDER,
        },
        {
            "label": "관측",
            "values": lambda r: [OBS_ONCE if r["_obs"] < 2 else OBS_MANY],
            "order": [OBS_MANY, OBS_ONCE],
            # 1회만 스친 상품은 추이가 점 하나라 기본에서 빼 둔다. 건수는 칩에 보인다.
            "default": [OBS_MANY] if once and len(once) < len(rows) else [],
        },
        {
            "label": "순위",
            "values": lambda r: [r["_move"]],
            "order": ["급상승", "상승", "유지", "하락", "급하락", "비교 불가"],
        },
        {
            "label": "가격",
            "values": lambda r: r["_kinds"] or [NO_PRICE_CHANGE],
            "order": [PRICE_CHANGE_LABEL[k] for k in
                      ("discount_started", "discount_deepened", "discount_reduced",
                       "discount_ended", "price_up", "price_down")] + [NO_PRICE_CHANGE],
        },
    ]

    body = product_table(
        rows, columns, "t-move", facets=facets,
        row_id=lambda r: r["product_id"], selectable=True,
    )

    if changes:
        def when_cell(change):
            if change.get("exact_at"):
                return "%s<span class=\"vs\">시점 확정</span>" % esc(change.get("to_at"))
            span = duration_text(gap_minutes(change.get("from_at"), change.get("to_at")))
            return "%s ~ %s<span class=\"vs\">%s· 스냅샷 %s개 결석</span>" % (
                esc(change.get("from_at")),
                esc((change.get("to_at") or "")[11:16]),
                "%s · " % esc(span) if span else "",
                esc(change.get("gap_snapshots")),
            )

        change_rows = "".join(
            '<tr><td>%s</td><td><span class="tag tag-%s">%s</span></td>'
            '<td class="col-img">%s</td><td class="col-name">%s</td>'
            '<td class="col-num">%s</td><td class="col-num">%s</td></tr>'
            % (
                when_cell(c),
                "off" if (c.get("kind") or "").startswith("discount") else "out",
                esc(PRICE_CHANGE_LABEL.get(c.get("kind"), c.get("kind"))),
                thumb(c)[0],
                '<a href="%s" target="_blank" rel="noopener">%s</a>' % (esc(c.get("url")), esc(c.get("name")))
                if c.get("url") else esc(c.get("name")),
                "%s (%s%%)" % (won((c.get("from") or {}).get("price_sale")),
                               esc((c.get("from") or {}).get("discount_rate") or 0)),
                "%s (%s%%)" % (won((c.get("to") or {}).get("price_sale")),
                               esc((c.get("to") or {}).get("discount_rate") or 0)),
            )
            for c in changes
        )
        exact = len([c for c in changes if c.get("exact_at")])
        body += (
            '<details class="raw"><summary>시점별 가격·할인 변화 원자료 %s건 '
            "(시점 확정 %s · 구간으로만 아는 것 %s)</summary>"
            '<div class="table-wrap"><table class="grid"><thead><tr><th>관측 창</th><th>종류</th>'
            '<th class="col-img">이미지</th><th>상품</th><th>이전</th><th>이후</th></tr></thead>'
            "<tbody>%s</tbody></table></div></details>"
            % (format(len(changes), ","), format(exact, ","),
               format(len(changes) - exact, ","), change_rows)
        )
    else:
        body += (
            '<details class="raw"><summary>시점별 가격·할인 변화 원자료 0건</summary>'
            '<p class="empty">기간 내 가격·할인 변화가 감지되지 않았다 '
            "(상품마다 마지막으로 관측한 상태와 비교했다). "
            "위 표의 '가격 변화'·'할인율 변화' 열은 첫 관측과 마지막 관측의 노출값 비교다.</p></details>"
        )
    return body


def timeline_strip(snapshots):
    """스냅샷 33행 표를 한 줄 스트립으로 압축한다. 원자료는 접어 둔다."""
    if not snapshots:
        return '<p class="empty">스냅샷 정보가 없다</p>'
    width, height = 720, 54
    pad_l, pad_r = 12, 12
    plot_w = width - pad_l - pad_r
    step = plot_w / max(1, len(snapshots) - 1)
    parts = ['<svg class="chart chart-strip" viewBox="0 0 %d %d" role="img" '
             'preserveAspectRatio="xMinYMin meet" aria-label="스냅샷 타임라인">' % (width, height)]
    parts.append('<line class="baseline" x1="%d" y1="20" x2="%d" y2="20"/>'
                 % (pad_l, pad_l + plot_w))
    for idx, snap in enumerate(snapshots):
        x = pad_l + idx * step
        broken = bool(snap.get("incomplete"))
        tip = "%s · %s건%s" % (
            snap.get("collected_at"), format(snap.get("item_count", 0), ","),
            " · 부분 수집" if broken else "",
        )
        parts.append(
            '<circle class="dot%s" cx="%.1f" cy="20" r="4" data-tip="%s"><title>%s</title></circle>'
            % (" dot-warn" if broken else "", x, esc(tip), esc(tip))
        )
    stamps = [s.get("collected_at") for s in snapshots]
    for idx, text in time_axis_labels(stamps, max_labels=6):
        parts.append('<text class="axis" x="%.1f" y="42" text-anchor="middle">%s</text>'
                     % (pad_l + idx * step, esc(text)))
    parts.append("</svg>")

    rows = "".join(
        "<tr><td>%s</td><td class=\"col-num\">%s</td><td>%s</td></tr>"
        % (
            esc(s.get("collected_at")),
            format(s.get("item_count", 0), ","),
            '<span class="tag tag-out">부분 수집</span>' if s.get("incomplete") else "",
        )
        for s in snapshots
    )
    return (
        '<div class="chart-solo">%s</div>'
        '<details class="raw"><summary>스냅샷 %s개 원자료</summary>'
        '<div class="table-wrap"><table class="grid"><thead><tr><th>수집 시각</th>'
        "<th>항목 수</th><th>비고</th></tr></thead><tbody>%s</tbody></table></div></details>"
        % (parts and "".join(parts), format(len(snapshots), ","), rows)
    )


def trend_controls(row_count):
    """계열 수를 사용자가 정한다(1~100). 대상은 표의 현재 정렬·필터 순서다."""
    return (
        '<div class="trend-tools">'
        '<label class="trend-field">계열 수'
        '<input id="trend-n" class="trend-n" type="number" min="1" max="100" value="5" '
        'aria-label="차트에 그릴 상품 수">'
        "</label>"
        '<label class="trend-field">보기'
        '<select id="trend-mode" class="trend-mode" aria-label="차트 형태">'
        '<option value="auto">자동 (5개까지 겹쳐 보기)</option>'
        '<option value="overlay">겹쳐 보기</option>'
        '<option value="grid">나란히 보기</option>'
        "</select></label>"
        '<label class="trend-field trend-check">'
        '<input id="trend-picked" type="checkbox"> 표에서 고른 행만'
        "</label>"
        '<span class="trend-status" id="trend-status"></span>'
        "</div>"
        '<div id="trend-host" class="trend-host"></div>'
    )


def series_payload(diff, rows, changes, stamps):
    """계열 데이터를 한 번만 심는다.

    스파크라인 · 추이 차트 · 행 펼침 상세가 **같은 데이터 하나**를 쓴다.
    모든 시점을 null까지 채우면 22,000칸이 되므로 관측된 시점만 담는다
    (실측 3,300건 → 약 100KB).
    """
    stamp_index = {stamp: idx for idx, stamp in enumerate(stamps)}
    trends = {str(t["product_id"]): t for t in (diff.get("trends") or [])}
    events = {}
    for change in changes:
        events.setdefault(str(change.get("product_id")), []).append(change)

    products = {}
    rank_values = []
    for row in rows:
        pid = str(row["product_id"])
        series = (trends.get(pid) or {}).get("series") or []
        points = []
        for point in series:
            idx = stamp_index.get(point.get("at"))
            if idx is None:
                continue
            rank = point.get("rank")
            if not is_num(rank):
                continue
            rank_values.append(rank)
            points.append([
                idx, int(rank),
                point.get("price_sale") if is_num(point.get("price_sale")) else None,
                point.get("discount_rate") if is_num(point.get("discount_rate")) else None,
                point.get("viewers_now") if is_num(point.get("viewers_now")) else None,
                point.get("buyers_now") if is_num(point.get("buyers_now")) else None,
            ])
        marks = []
        for change in events.get(pid) or []:
            marks.append({
                "f": nearest_stamp_index(stamps, change.get("from_at")),
                "t": nearest_stamp_index(stamps, change.get("to_at") or change.get("at")),
                "x": bool(change.get("exact_at")),
                "k": change.get("kind"),
                "l": PRICE_CHANGE_LABEL.get(change.get("kind"), change.get("kind")),
            })
        products[pid] = {
            "n": row.get("name") or pid,
            "u": row.get("url") or "",
            "p": points,
            "e": marks,
        }
    return {
        "stamps": stamps,
        "rankMax": max(rank_values) if rank_values else 1,
        "rankMin": min(rank_values) if rank_values else 1,
        "marks": {k: PRICE_CHANGE_LABEL[k] for k in PRICE_CHANGE_LABEL},
        "glyph": EVENT_MARK,
        "products": products,
    }


def ranking_diff_body(diff):
    meta = diff.get("meta") or {}
    summary = diff.get("summary") or {}
    period = meta.get("period") or {}
    snapshots = meta.get("snapshots") or []
    changes = diff.get("price_changes") or []
    threshold = summary.get("big_move_threshold", 10)

    all_stamps = [s.get("collected_at") for s in snapshots]
    idxs = downsample_indices(len(all_stamps))
    stamps = [all_stamps[i] for i in idxs]
    stamp_index = {stamp: idx for idx, stamp in enumerate(stamps)}
    last_index = len(stamps) - 1

    trends = diff.get("trends") or []
    movers_by_id = {str(m["product_id"]): m for m in (diff.get("movers") or [])}
    events = {}
    for change in changes:
        events.setdefault(str(change.get("product_id")), []).append(change)

    # 한 표에 등장 상품 전부를 담는다. 구 구조는 신규/이탈/유지를 세 표로 나눠
    # 등장 679개 중 158개(23%)만 실었다.
    rows = []
    for trend in trends:
        pid = str(trend.get("product_id"))
        series = trend.get("series") or []
        observed = observed_indices(series, stamp_index)
        if not observed:
            continue
        ranks = [p.get("rank") for p in series
                 if stamp_index.get(p.get("at")) is not None and is_num(p.get("rank"))]
        r_first, r_last = (ranks[0], ranks[-1]) if ranks else (None, None)
        p_first, p_last = series_bounds(series, "price_sale")
        d_first, d_last = series_bounds(series, "discount_rate")
        prices = [p.get("price_sale") for p in series
                  if stamp_index.get(p.get("at")) is not None and is_num(p.get("price_sale"))]
        mover = movers_by_id.get(pid)
        delta = mover.get("delta") if mover else None
        if delta is None and is_num(r_first) and is_num(r_last):
            delta = int(r_first - r_last)
        mine = events.get(pid) or []
        kinds = []
        for change in mine:
            label = PRICE_CHANGE_LABEL.get(change.get("kind"), change.get("kind"))
            if label not in kinds:
                kinds.append(label)
        window = None
        if mine:
            latest = max(mine, key=lambda c: c.get("to_at") or c.get("at") or "")
            window = {
                "from_at": latest.get("from_at"),
                "to_at": latest.get("to_at") or latest.get("at"),
                "gap": latest.get("gap_snapshots"),
                "exact": bool(latest.get("exact_at")),
            }
        sample = (mine or [{}])[0]
        rows.append({
            "product_id": pid,
            "name": trend.get("name") or sample.get("name"),
            "brand": trend.get("brand") or sample.get("brand"),
            "url": trend.get("url") or sample.get("url"),
            "image_url": trend.get("image_url") or (mover or {}).get("image_url")
                         or sample.get("image_url"),
            "sold_out": False,
            "price_sale": p_last,
            "price_original": None,
            "discount_rate": d_last,
            "rank_first": r_first,
            "rank_last": r_last,
            "delta": delta,
            "_best": min(ranks) if ranks else None,
            "_p_first": p_first, "_p_last": p_last,
            "_p_lo": min(prices) if prices else None,
            "_p_hi": max(prices) if prices else None,
            "_d_first": d_first, "_d_last": d_last,
            "_kinds": kinds, "_events": len(mine), "_window": window,
            "_obs": len(observed), "_total": len(stamps),
            "_end": endpoint_state(observed, last_index),
            "_move": move_bucket(delta or 0, threshold) if is_num(delta) else "비교 불가",
        })
    rows.sort(key=lambda r: (-r["_events"], -abs(r["delta"]) if is_num(r["delta"]) else 0,
                             -r["_obs"]))

    churn = churn_rate(trends, stamps)
    ranking_size = max([s.get("item_count") or 0 for s in snapshots] or [0])
    out = [
        kpi_tiles(
            [
                ("스냅샷", "%s개" % format(len(snapshots), ","),
                 "%s ~ %s" % (period.get("start"), period.get("end"))),
                ("등장 상품", format(len(rows), ","),
                 "랭킹 %s위까지 · 자리보다 %.1f배" % (
                     format(ranking_size, ","),
                     (len(rows) / ranking_size) if ranking_size else 0)),
                ("교체율", "%.0f%%" % churn if churn is not None else MISSING,
                 "인접 스냅샷 평균 신규 유입"),
                (
                    "가격·할인 변화",
                    format(len(changes), ","),
                    "시점 확정 %s건 · 할인 시작 %s건"
                    % (format(summary.get("price_change_exact", 0), ","),
                       format(summary.get("discount_started", 0), ",")),
                ),
            ]
        )
    ]

    out.append(
        section(
            "기간 개요",
            timeline_strip(snapshots),
            note="점 하나가 스냅샷 하나다. 부분 수집된 시점은 색이 다르다. "
            "<strong>교체율</strong>은 인접 스냅샷마다 '이번에 새로 들어온 비율'을 재서 평균한 값이다 — "
            "첫·마지막 두 시점만 비교하면 기간이 길어질수록 실제 교체를 과소 표기한다.",
        )
    )

    exact_changes = len([c for c in changes if c.get("exact_at")])
    once = len([r for r in rows if r["_obs"] < 2])
    out.append(
        section(
            "상품별 추이",
            rank_price_body(diff, rows, changes, threshold, stamps),
            note="한 행 = 한 상품. 기간에 순위권에 오른 상품 <strong>%s개 전부</strong>가 들어 있다 "
            "(신규 진입·이탈도 <code>끝점</code> 칩으로 거른다 — 표를 따로 두지 않는다). "
            "<strong>랭킹 체류</strong>는 스냅샷 몇 번 중 몇 번 순위권에 있었나이고, "
            "끊긴 구간은 순위 추이 선이 끊겨 보인다. "
            "%s"
            "가격·할인은 상품마다 <strong>마지막으로 관측한 상태와 비교</strong>해 잡는다 — "
            "순위권 밖에 있던 구간에 걸친 변화도 잡히지만 <strong>시점은 관측 창으로만</strong> 안다"
            "(%s건 중 %s건은 스냅샷 1칸으로 확정). "
            "한계 두 가지: 결석 구간 안에서 여러 번 변했으면 <strong>순변화 1건으로 뭉쳐지고</strong>, "
            "변했다 원래대로 돌아왔으면 <strong>아예 잡히지 않는다</strong>(순위권 밖은 관측 자체가 없다)."
            % (
                format(len(rows), ","),
                ("<strong>1회만 스친 상품 %s개는 기본에서 빼 뒀다</strong>(추이가 점 하나다). "
                 "<code>관측</code> 칩으로 켤 수 있다. " % format(once, ",")) if once else "",
                format(len(changes), ","),
                format(exact_changes, ","),
            ),
        )
    )

    sample_note = ""
    if len(stamps) < len(all_stamps):
        sample_note = (
            " 스냅샷 %s개를 균등 구간의 대표 %s개 시점으로 요약해 그렸다"
            "(평균이 아니라 실측 스냅샷을 고른 것이다)."
            % (format(len(all_stamps), ","), format(len(stamps), ","))
        )
    out.append(
        section(
            "추이 차트",
            trend_controls(len(rows)),
            note="<strong>위 표의 현재 정렬·필터 순서대로 상위 N개</strong>를 그린다 — "
            "차트가 따로 선택 규칙을 갖지 않는다. 표를 정렬하거나 칩을 바꾸면 차트도 따라 바뀐다. "
            "계열 수는 1~100이고, <strong>5개까지는 겹쳐</strong> 그려 상품끼리 비교하고 "
            "<strong>6개 이상은 나란히</strong> 그린다(순위선을 100개 겹치면 읽을 수 없고 계열 색도 5개뿐이다). "
            "나란히 보기의 각 칸은 <strong>순위(왼쪽 축)·가격(오른쪽 축)·할인 구간(배경 띠)</strong>을 "
            "한 좌표계에 놓는다. 순위 축은 전 칸이 같은 범위라 모양을 비교할 수 있고, "
            "가격은 상품마다 자기 범위로 그린다. "
            "표식은 가격·할인 변화다(▼ 할인 시작·확대, △ 축소·종료, ＋－ 인상·인하) — "
            "시점이 확정된 것은 세로 점선, 순위권 밖이던 구간에 걸친 것은 <strong>띠</strong>다. "
            "같은 축에 겹쳐 놓은 것이고 <strong>인과를 주장하지 않는다</strong>."
            + sample_note,
        )
    )

    payload = series_payload(diff, rows, changes, stamps)
    out.append(
        '<script id="series-data" type="application/json">%s</script>'
        % json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
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

/* auto-fill이어야 블록이 1개일 때 트랙이 합쳐지지 않는다.
   auto-fit은 빈 트랙을 접어서 520px 차트를 컨테이너 폭까지 늘려 버린다 — 옆 섹션과
   크기가 안 맞는 원인이었다(2026-07-30). */
.chart-grid { display: grid; gap: 26px;
  grid-template-columns: repeat(auto-fill, minmax(min(100%%, 440px), 1fr)); }
.chart-block { min-width: 0; }
/* 그리드 밖에 홀로 놓이는 차트(추이 등)도 본문 폭까지 늘어나지 않게 묶는다. */
.chart-solo { max-width: 880px; }
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

/* 다중 선택 필터 — 여러 개를 켜면 OR, 서로 다른 그룹끼리는 AND로 걸린다. */
.facets { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
.facet-group { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px; }
.facet-label { font-size: 12px; color: var(--muted); margin-right: 4px;
  flex: 0 0 auto; min-width: 52px; }
.chip { font: inherit; font-size: 12px; padding: 3px 9px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--page); color: var(--text-2);
  cursor: pointer; white-space: nowrap; }
.chip:hover { color: var(--text); border-color: var(--baseline); }
.chip.is-on { background: var(--seq); border-color: var(--seq); color: #fff; }
.facet-mode { margin-left: 6px; padding: 1px 6px; border-radius: 8px;
  background: var(--line); color: var(--muted); font-size: 11px; font-weight: 600; }
.chip em { font-style: normal; opacity: 0.65; margin-left: 4px;
  font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }
table.grid { border-collapse: collapse; width: 100%%; font-size: 13px; }
table.grid th, table.grid td { padding: 8px 10px; text-align: left;
  border-bottom: 1px solid var(--grid); vertical-align: middle; }
table.grid thead th { position: sticky; top: 0; background: var(--surface);
  font-size: 12px; color: var(--text-2); font-weight: 600; white-space: nowrap; z-index: 1; }
/* 정렬 가능한 열은 눌러 보기 전에도 눌리는 줄 알아야 한다 — 중립 화살표를 늘 띄운다. */
table.grid th[data-sort]:not([data-sort="none"]) { cursor: pointer; user-select: none; }
table.grid th[data-sort]:not([data-sort="none"])::after {
  content: " \\2195"; font-size: 10px; opacity: 0.3; }
table.grid th[data-sort]:not([data-sort="none"]):hover { color: var(--text); }
table.grid th[data-sort]:not([data-sort="none"]):hover::after { opacity: 0.7; }
table.grid th.sorted-asc::after { content: " \\25B2"; font-size: 9px; opacity: 1; }
table.grid th.sorted-desc::after { content: " \\25BC"; font-size: 9px; opacity: 1; }
/* 이름만으로 뜻이 안 통하는 열은 점선 밑줄로 표시하고 hover에 풀이를 붙인다 */
.th-tip { text-decoration: underline dotted; text-decoration-color: var(--muted);
  text-underline-offset: 3px; cursor: help; }

/* 스토리3 — 표 안 스파크라인. 그리기는 인라인 JS가 하고 여기선 자리와 크기만 잡는다. */
.col-spark { width: 124px; }
.col-spark .vs { white-space: nowrap; }
.col-pick { width: 30px; text-align: center; }
.spark { display: block; width: 104px; height: 24px; }
.spark svg { display: block; width: 100%%; height: 100%%; overflow: visible; }
.spark-line { fill: none; stroke: var(--seq); stroke-width: 1.6;
  stroke-linejoin: round; stroke-linecap: round; }
.spark-dot { fill: var(--seq); }
.spark-band { fill: color-mix(in srgb, var(--critical) 9%%, transparent); }
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
  grid-template-columns: repeat(auto-fill, minmax(min(100%%, 300px), 1fr)); }
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
.approx { color: var(--text-2); font-size: 12px; }
.price-sale { font-weight: 600; }
.price-was { color: var(--muted); text-decoration: line-through; font-size: 12px; }
.tag { display: inline-block; font-size: 11px; padding: 1px 6px; border-radius: 4px;
  background: var(--grid); color: var(--text-2); margin-left: 4px; white-space: nowrap; }
.tag-out { background: var(--grid); color: var(--text-2); }
.tag-off { background: color-mix(in srgb, var(--critical) 16%%, transparent); color: var(--critical); }
.tag-stay { background: color-mix(in srgb, var(--seq) 14%%, transparent); color: var(--seq); }
.tag-in { background: color-mix(in srgb, var(--good) 16%%, transparent); color: var(--up); }
.tag-out2 { background: color-mix(in srgb, var(--critical) 14%%, transparent); color: var(--down); }
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
.tag-presence.is-all { background: color-mix(in srgb, var(--seq) 18%%, transparent);
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
  background: color-mix(in srgb, currentColor 20%%, transparent); }
/* 계열 범례 — 이름은 본문 색으로 읽고 색은 스와치만 쓴다 */
.legend-series { gap: 4px 18px; }
.legend-series .legend-name { color: var(--text-2); }

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
    if (!state[id]) state[id] = { q: '', picked: {}, mode: {} };
    return state[id];
  }

  function apply(id) {
    var table = document.getElementById(id);
    if (!table) return;
    var st = stateOf(id);
    var counter = document.getElementById(id + '-count');
    var shown = 0;
    Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
      if (row.classList.contains('detail-row')) { row.hidden = false; return; }
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
      row.hidden = !hit;
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

  var pending = {};
  document.querySelectorAll('.facet-group').forEach(function (group) {
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
})();

// ── 스토리3 추이 렌더 ───────────────────────────────────────────────────────
// 계열 데이터는 문서에 한 번만 심겨 있고(#series-data) 스파크라인·추이 차트·
// 행 펼침 상세가 그 하나를 같이 쓴다. 같은 데이터를 세 번 그리지 않는다.
(function () {
  var holder = document.getElementById('series-data');
  if (!holder) return;
  var SD;
  try { SD = JSON.parse(holder.textContent); } catch (e) { return; }
  var STAMPS = SD.stamps || [], PROD = SD.products || {};
  var N = Math.max(1, STAMPS.length - 1);
  var RMIN = SD.rankMin || 1, RMAX = SD.rankMax || 1;
  var RSPAN = Math.max(1, RMAX - RMIN);
  var GLYPH = SD.glyph || {};

  function esc(t) {
    return String(t == null ? '' : t).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function won(v) {
    return v == null ? '-' : v.toLocaleString('ko-KR') + '원';
  }
  // 연속한 시점끼리만 잇는다. 순위권 밖이던 구간은 선을 끊는다 —
  // 이어 그리면 그 구간에도 순위가 있었다고 주장하는 것이다.
  function segments(pts) {
    var out = [], cur = [];
    for (var i = 0; i < pts.length; i++) {
      if (cur.length && pts[i][0] - cur[cur.length - 1][0] > 1) { out.push(cur); cur = []; }
      cur.push(pts[i]);
    }
    if (cur.length) out.push(cur);
    return out;
  }
  function pathOf(seg, xf, yf) {
    var d = '';
    for (var i = 0; i < seg.length; i++) {
      d += (i ? 'L' : 'M') + xf(seg[i]).toFixed(1) + ' ' + yf(seg[i]).toFixed(1);
    }
    return d;
  }

  // ── 스파크라인 ────────────────────────────────────────────────────────────
  function spark(host) {
    var d = PROD[host.dataset.pid];
    if (!d) return;
    var kind = host.dataset.spark, pts = d.p, W = 104, H = 24, pad = 2;
    if (!pts.length) { host.innerHTML = ''; return; }
    var xf = function (p) { return pad + p[0] / N * (W - pad * 2); };
    var yf, body = '';
    if (kind === 'rank') {
      // 순위 축은 전 행이 같은 범위다 — 행끼리 모양을 비교할 수 있어야 한다.
      yf = function (p) { return pad + (p[1] - RMIN) / RSPAN * (H - pad * 2); };
    } else {
      var vals = pts.filter(function (p) { return p[2] != null; }).map(function (p) { return p[2]; });
      if (!vals.length) { host.innerHTML = '<svg viewBox="0 0 104 24"></svg>'; return; }
      var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
      if (hi === lo) {   // 값이 하나면 가운데 — 바닥에 그리면 '최저'로 오해된다
        yf = function () { return H / 2; };
      } else {
        yf = function (p) { return pad + (1 - (p[2] - lo) / (hi - lo)) * (H - pad * 2); };
      }
      // 할인 구간을 뒤에 띠로 깐다 (가격이 내려간 구간과 겹쳐 보인다)
      var runs = [], open = null;
      for (var i = 0; i < pts.length; i++) {
        var on = pts[i][3] != null && pts[i][3] > 0;
        if (on && open === null) open = pts[i][0];
        if (!on && open !== null) { runs.push([open, pts[i][0]]); open = null; }
      }
      if (open !== null) runs.push([open, pts[pts.length - 1][0]]);
      for (var r = 0; r < runs.length; r++) {
        var x0 = pad + runs[r][0] / N * (W - pad * 2);
        var x1 = pad + runs[r][1] / N * (W - pad * 2);
        body += '<rect class="spark-band" x="' + x0.toFixed(1) + '" y="0" width="' +
                Math.max(1.5, x1 - x0).toFixed(1) + '" height="' + H + '"/>';
      }
    }
    var segs = segments(pts.filter(function (p) { return kind === 'rank' || p[2] != null; }));
    for (var s2 = 0; s2 < segs.length; s2++) {
      if (segs[s2].length > 1) {
        body += '<path class="spark-line" d="' + pathOf(segs[s2], xf, yf) + '"/>';
      } else {
        body += '<circle class="spark-dot" cx="' + xf(segs[s2][0]).toFixed(1) +
                '" cy="' + yf(segs[s2][0]).toFixed(1) + '" r="1.8"/>';
      }
    }
    host.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-hidden="true">' +
                     body + '</svg>';
  }
  document.querySelectorAll('[data-spark]').forEach(spark);

  // ── 상품 1개 패널 — 순위(좌축) + 가격(우축) + 할인 띠 ─────────────────────
  function panel(pid, W, H, opts) {
    opts = opts || {};
    var d = PROD[pid];
    if (!d) return '';
    var pts = d.p;
    var padL = 34, padR = 46, padT = 12, padB = 24;
    var pw = W - padL - padR, ph = H - padT - padB;
    if (!pts.length) return '<p class="empty">관측된 순위가 없다</p>';
    var xf = function (p) { return padL + p[0] / N * pw; };
    var ry = function (p) { return padT + (p[1] - RMIN) / RSPAN * ph; };
    var pv = pts.filter(function (p) { return p[2] != null; }).map(function (p) { return p[2]; });
    var lo = pv.length ? Math.min.apply(null, pv) : 0;
    var hi = pv.length ? Math.max.apply(null, pv) : 1;
    var py = hi === lo
      ? function () { return padT + ph / 2; }
      : function (p) { return padT + (1 - (p[2] - lo) / (hi - lo)) * ph; };
    var out = '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" ' +
              'preserveAspectRatio="xMinYMin meet" aria-label="순위·가격 추이">';

    // 할인 구간 띠
    var runs = [], open = null;
    for (var i = 0; i < pts.length; i++) {
      var on = pts[i][3] != null && pts[i][3] > 0;
      if (on && open === null) open = pts[i][0];
      if (!on && open !== null) { runs.push([open, pts[i][0]]); open = null; }
    }
    if (open !== null) runs.push([open, pts[pts.length - 1][0]]);
    for (var r = 0; r < runs.length; r++) {
      var x0 = padL + runs[r][0] / N * pw, x1 = padL + runs[r][1] / N * pw;
      out += '<rect class="spark-band" x="' + x0.toFixed(1) + '" y="' + padT +
             '" width="' + Math.max(2, x1 - x0).toFixed(1) + '" height="' + ph + '"/>';
    }
    // 순위 축 (좌) — 3칸
    for (var g = 0; g < 3; g++) {
      var rv = RMIN + RSPAN * g / 2, gy = padT + (rv - RMIN) / RSPAN * ph;
      out += '<line class="grid" x1="' + padL + '" y1="' + gy.toFixed(1) + '" x2="' +
             (padL + pw) + '" y2="' + gy.toFixed(1) + '"/>';
      out += '<text class="axis" x="' + (padL - 6) + '" y="' + (gy + 4).toFixed(1) +
             '" text-anchor="end">' + Math.round(rv) + '위</text>';
    }
    // 가격 축 (우)
    if (pv.length && hi !== lo) {
      out += '<text class="axis axis-right" x="' + (padL + pw + 6) + '" y="' + (padT + 4) +
             '">' + Math.round(hi / 1000) + '천</text>';
      out += '<text class="axis axis-right" x="' + (padL + pw + 6) + '" y="' + (padT + ph) +
             '">' + Math.round(lo / 1000) + '천</text>';
    } else if (pv.length) {
      out += '<text class="axis axis-right" x="' + (padL + pw + 6) + '" y="' + (padT + ph / 2 + 4) +
             '">' + Math.round(hi / 1000) + '천</text>';
    }
    // 변화 표식
    for (var m = 0; m < d.e.length; m++) {
      var ev = d.e[m], gl = GLYPH[ev.k] || '◆';
      if (ev.x) {
        var ex = padL + ev.t / N * pw;
        out += '<line class="event-line" x1="' + ex.toFixed(1) + '" y1="' + padT + '" x2="' +
               ex.toFixed(1) + '" y2="' + (padT + ph) + '" style="stroke:var(--series-4)" ' +
               'data-tip="' + esc(ev.l + ' · 시점 확정') + '"/>';
        out += '<text class="event-tick" x="' + ex.toFixed(1) + '" y="' + (padT - 3) +
               '" text-anchor="middle" style="fill:var(--series-4)">' + gl + '</text>';
      } else {
        var bx = padL + ev.f / N * pw, bw = Math.max(3, (ev.t - ev.f) / N * pw);
        out += '<rect class="event-band" x="' + bx.toFixed(1) + '" y="' + padT + '" width="' +
               bw.toFixed(1) + '" height="' + ph + '" style="fill:var(--series-4)" ' +
               'data-tip="' + esc(ev.l + ' · 구간으로만 아는 변화') + '"/>';
        out += '<text class="event-tick" x="' + (bx + bw / 2).toFixed(1) + '" y="' + (padT - 3) +
               '" text-anchor="middle" style="fill:var(--series-4)">' + gl + '</text>';
      }
    }
    // 가격 선 (우축, 점선)
    if (hi !== lo) {
      var pseg = segments(pts.filter(function (p) { return p[2] != null; }));
      for (var q = 0; q < pseg.length; q++) {
        if (pseg[q].length > 1) out += '<path class="price-line" d="' + pathOf(pseg[q], xf, py) + '"/>';
      }
    }
    // 순위 선 (좌축, 실선) + 점
    var rseg = segments(pts);
    for (var t = 0; t < rseg.length; t++) {
      if (rseg[t].length > 1) {
        out += '<path class="line" d="' + pathOf(rseg[t], xf, ry) + '" style="stroke:var(--seq)"/>';
      }
    }
    for (var u = 0; u < pts.length; u++) {
      var p = pts[u];
      var tip = STAMPS[p[0]] + ' · ' + p[1] + '위' + (p[2] != null ? ' · ' + won(p[2]) : '') +
                (p[3] ? ' · 할인 ' + p[3] + '%' : '') +
                (p[4] != null ? ' · 보는 중 ' + p[4] + '명' : '');
      out += '<circle class="dot" cx="' + xf(p).toFixed(1) + '" cy="' + ry(p).toFixed(1) +
             '" r="3" style="fill:var(--seq)" data-tip="' + esc(tip) + '"/>';
    }
    // x축
    var every = Math.max(1, Math.ceil(STAMPS.length / (opts.wide ? 7 : 3)));
    var lastDate = null;
    for (var v = 0; v < STAMPS.length; v += every) {
      var st = STAMPS[v] || '', dt = st.slice(5, 10), ck = st.slice(11, 16);
      var lab = dt === lastDate ? ck : dt + ' ' + ck;
      lastDate = dt;
      out += '<text class="axis" x="' + (padL + v / N * pw).toFixed(1) + '" y="' +
             (H - padB + 15) + '" text-anchor="middle">' + esc(lab) + '</text>';
    }
    out += '<line class="baseline" x1="' + padL + '" y1="' + (padT + ph) + '" x2="' +
           (padL + pw) + '" y2="' + (padT + ph) + '"/></svg>';

    var head = '<h4>' + (d.u ? '<a href="' + esc(d.u) + '" target="_blank" rel="noopener">' +
               esc(d.n) + '</a>' : esc(d.n)) + '</h4>';
    var ranks = pts.map(function (p) { return p[1]; });
    var note = '<p class="panel-note">최고 ' + Math.min.apply(null, ranks) + '위 · 마지막 ' +
               ranks[ranks.length - 1] + '위 · ' + pts.length + '/' + STAMPS.length + '회 관측' +
               (pv.length ? ' · ' + won(pv[pv.length - 1]) : '') + '</p>';
    return '<div class="panel">' + head + note + out + '</div>';
  }

  // ── 겹쳐 보기 (계열 5개까지) ──────────────────────────────────────────────
  function overlay(pids) {
    var W = 720, H = 300, padL = 44, padR = 20, padT = 16, padB = 30;
    var pw = W - padL - padR, ph = H - padT - padB;
    var out = '<svg class="chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" ' +
              'preserveAspectRatio="xMinYMin meet" aria-label="순위 추이">';
    for (var g = 0; g < 4; g++) {
      var rv = RMIN + RSPAN * g / 3, gy = padT + (rv - RMIN) / RSPAN * ph;
      out += '<line class="grid" x1="' + padL + '" y1="' + gy.toFixed(1) + '" x2="' +
             (padL + pw) + '" y2="' + gy.toFixed(1) + '"/>';
      out += '<text class="axis" x="' + (padL - 8) + '" y="' + (gy + 4).toFixed(1) +
             '" text-anchor="end">' + Math.round(rv) + '위</text>';
    }
    var every = Math.max(1, Math.ceil(STAMPS.length / 8)), lastDate = null;
    for (var v = 0; v < STAMPS.length; v += every) {
      var st = STAMPS[v] || '', dt = st.slice(5, 10), ck = st.slice(11, 16);
      var lab = dt === lastDate ? ck : dt + ' ' + ck;
      lastDate = dt;
      out += '<text class="axis" x="' + (padL + v / N * pw).toFixed(1) + '" y="' + (H - padB + 16) +
             '" text-anchor="middle">' + esc(lab) + '</text>';
    }
    var legend = '';
    for (var i = 0; i < pids.length; i++) {
      var d = PROD[pids[i]];
      if (!d || !d.p.length) continue;
      var color = 'var(--series-' + ((i % 5) + 1) + ')';
      var xf = function (p) { return padL + p[0] / N * pw; };
      var yf = function (p) { return padT + (p[1] - RMIN) / RSPAN * ph; };
      for (var m = 0; m < d.e.length; m++) {
        var ev = d.e[m], gl = GLYPH[ev.k] || '◆';
        if (ev.x) {
          var ex = padL + ev.t / N * pw;
          out += '<line class="event-line" x1="' + ex.toFixed(1) + '" y1="' + padT + '" x2="' +
                 ex.toFixed(1) + '" y2="' + (padT + ph) + '" style="stroke:' + color +
                 '" data-tip="' + esc(d.n + ' · ' + ev.l + ' · 시점 확정') + '"/>';
        } else {
          var bx = padL + ev.f / N * pw, bw = Math.max(4, (ev.t - ev.f) / N * pw);
          out += '<rect class="event-band" x="' + bx.toFixed(1) + '" y="' + padT + '" width="' +
                 bw.toFixed(1) + '" height="' + ph + '" style="fill:' + color +
                 '" data-tip="' + esc(d.n + ' · ' + ev.l + ' · 구간으로만 아는 변화') + '"/>';
        }
        out += '<text class="event-tick" x="' +
               (ev.x ? padL + ev.t / N * pw : padL + (ev.f + (ev.t - ev.f) / 2) / N * pw).toFixed(1) +
               '" y="' + (padT - 3) + '" text-anchor="middle" style="fill:' + color + '">' + gl + '</text>';
      }
      var segs = segments(d.p);
      for (var s2 = 0; s2 < segs.length; s2++) {
        if (segs[s2].length > 1) {
          out += '<path class="line" d="' + pathOf(segs[s2], xf, yf) + '" style="stroke:' + color + '"/>';
        }
      }
      for (var u = 0; u < d.p.length; u++) {
        var p = d.p[u];
        var tip = d.n + ' · ' + STAMPS[p[0]] + ' · ' + p[1] + '위' +
                  (p[2] != null ? ' · ' + won(p[2]) : '');
        out += '<circle class="dot" cx="' + xf(p).toFixed(1) + '" cy="' + yf(p).toFixed(1) +
               '" r="3.5" style="fill:' + color + '" data-tip="' + esc(tip) + '"/>';
      }
      legend += '<span class="legend-item" style="color:' + color + '">' +
                '<span class="legend-swatch"></span><span class="legend-name">' +
                esc(d.n) + '</span></span>';
    }
    out += '<line class="baseline" x1="' + padL + '" y1="' + (padT + ph) + '" x2="' +
           (padL + pw) + '" y2="' + (padT + ph) + '"/></svg>';
    return '<div class="chart-solo">' + out +
           '<p class="legend legend-series">' + legend + '</p>' +
           '<p class="legend"><span class="legend-item"><span class="legend-swatch dashed"></span>' +
           '시점 확정된 가격·할인 변화</span>' +
           '<span class="legend-item"><span class="legend-swatch band"></span>' +
           '구간으로만 아는 변화</span></p></div>';
  }

  // ── 표의 현재 정렬·필터를 따른다 ─────────────────────────────────────────
  var table = document.getElementById('t-move');
  var host = document.getElementById('trend-host');
  var nInput = document.getElementById('trend-n');
  var modeSel = document.getElementById('trend-mode');
  var pickOnly = document.getElementById('trend-picked');
  var status = document.getElementById('trend-status');
  if (!table || !host) return;

  function visiblePids() {
    var rows = Array.prototype.slice.call(table.tBodies[0].rows);
    var out = [];
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (r.hidden || r.classList.contains('detail-row')) continue;
      if (pickOnly && pickOnly.checked) {
        var box = r.querySelector('.pick');
        if (!box || !box.checked) continue;
      }
      if (r.dataset.pid) out.push(r.dataset.pid);
    }
    return out;
  }

  function render() {
    var want = Math.max(1, Math.min(100, parseInt(nInput.value, 10) || 1));
    var pool = visiblePids();
    var pids = pool.slice(0, want);
    var mode = modeSel.value;
    if (mode === 'auto') mode = pids.length <= 5 ? 'overlay' : 'grid';
    if (!pids.length) {
      host.innerHTML = '<p class="empty">' +
        (pickOnly && pickOnly.checked ? '표에서 고른 행이 없다' : '거를 조건에 맞는 상품이 없다') +
        '</p>';
      status.textContent = '';
      return;
    }
    if (mode === 'overlay') {
      var shown = pids.slice(0, 5);
      host.innerHTML = overlay(shown);
      status.textContent = '대상 ' + pool.length + '개 중 ' + shown.length + '개 · 겹쳐 보기' +
        (pids.length > 5 ? ' (겹쳐 보기는 5개까지 — 나머지는 나란히 보기로)' : '');
    } else {
      host.innerHTML = '<div class="panel-grid">' + pids.map(function (p) {
        return panel(p, 300, 150, {});
      }).join('') + '</div>';
      status.textContent = '대상 ' + pool.length + '개 중 ' + pids.length + '개 · 나란히 보기' +
        (want > pool.length ? ' (요청 ' + want + '개, 대상이 ' + pool.length + '개다)' : '');
    }
  }

  nInput.addEventListener('input', render);
  nInput.addEventListener('change', render);
  modeSel.addEventListener('change', render);
  if (pickOnly) pickOnly.addEventListener('change', render);
  table.addEventListener('click', function (e) {
    if (e.target.classList && e.target.classList.contains('pick')) render();
  });
  // 정렬·필터가 바뀌면 차트도 따라간다
  document.addEventListener('tableview', render);

  // ── 행을 누르면 그 상품의 상세를 펼친다 ──────────────────────────────────
  table.tBodies[0].addEventListener('click', function (e) {
    if (e.target.closest('a') || e.target.classList.contains('pick')) return;
    var row = e.target.closest('tr');
    if (!row || !row.dataset.pid || row.classList.contains('detail-row')) return;
    var next = row.nextElementSibling;
    if (next && next.classList.contains('detail-row')) {
      next.remove();
      row.classList.remove('is-open');
      return;
    }
    var span = row.cells.length;
    var tr = document.createElement('tr');
    tr.className = 'detail-row';
    var td = document.createElement('td');
    td.colSpan = span;
    td.innerHTML = panel(row.dataset.pid, 700, 240, { wide: true });
    tr.appendChild(td);
    row.parentNode.insertBefore(tr, row.nextSibling);
    row.classList.add('is-open');
  });

  render();
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
        if story == "brand-linesheet":
            # 헤더가 합집합 크기를 싣도록 매칭을 먼저 돈다(순수 계산이라 부작용이 없다).
            header = header_block(datasets, validations, title, union=build_union(datasets))
            body = linesheet_body(datasets)
        elif story == "market-scan":
            header = header_block(datasets, validations, title)
            body = market_scan_body(datasets)
        elif story == "ranking-snapshot":
            header = header_block(datasets, validations, title)
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
