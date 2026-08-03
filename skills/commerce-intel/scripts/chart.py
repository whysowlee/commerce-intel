#!/usr/bin/env python3
"""PDF 차트 — reportlab Drawing을 직접 그린다 (insight-spec §8 시각 규칙 구현).

## 왜 직접 그리나

reportlab에 차트 모듈(`reportlab.graphics.charts`)이 있지만 쓰지 않는다. 우리 시각
규칙(0 시작·색약 안전·순위 축 반전·구간 끊기·직접 라벨)을 맞추려면 결국 저수준
`Drawing`으로 내려와야 하고, 그럴 바엔 처음부터 필요한 형태만 그리는 쪽이 코드가 짧다.
외부 차트 라이브러리도 쓰지 않는다 — 배포 의존은 reportlab 하나로 족하다.

## 지키는 규칙 (insight-spec §8)

- **파이·도넛·3D 없음.** 각도 비교는 사람이 못 한다 — 막대로 그린다
- **막대는 0에서 시작한다.** 아니면 차이가 과장된다
- **색만으로 구분하지 않는다**(적록색약 8%) — 명도 차 + 직접 라벨을 병용한다
- **흑백 인쇄에서 읽혀야 한다** — 채도가 아니라 명도로 갈리는 두 톤만 쓴다
- 축 라벨·단위·n을 생략하지 않는다

## 쓰는 법

    from chart import bar_h, dist_compare, bins_bar
    d.figure(bar_h([("29CM", 18), ("자사몰", 0)], "할인율(%) 중앙값"),
             caption="플랫폼별 할인율 — n=1,317")
"""
from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors

# 색: 명도로 갈린다(흑백 인쇄·색약 대응). 채도 대비에 기대지 않는다.
INK = colors.HexColor("#16181D")
DARK = colors.HexColor("#3D4450")     # 계열 A — 진함
LIGHT = colors.HexColor("#AAB2BD")    # 계열 B — 연함
RULE = colors.HexColor("#D4D9E0")
MUTED = colors.HexColor("#5B6472")
FONT = "HYGothic-Medium"              # pdf_doc가 등록한 한글 CID 폰트


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".") if abs(v) < 100 else "{:,.0f}".format(v)
    return "{:,}".format(v)


def _txt(d, x, y, s, size=7.5, fill=MUTED, anchor="start"):
    t = String(x, y, str(s), fontName=FONT, fontSize=size, fillColor=fill)
    t.textAnchor = anchor
    d.add(t)


def bar_h(pairs, axis_label, width=440, row_h=20, pad_left=110):
    """가로 막대. 범주 비교·순위에 쓴다(값으로 정렬된 채로 넘겨라).

    **0에서 시작한다.** 음수가 섞이면 0선을 가운데 두고 양쪽으로 그린다 —
    이중차분처럼 부호가 의미인 값이 있다.
    """
    pairs = [(k, v) for k, v in pairs if v is not None]
    if not pairs:
        return Drawing(width, 10)
    h = row_h * len(pairs) + 42   # 아래 축 라벨 자리(겹침 방지)
    d = Drawing(width, h)
    vals = [v for _, v in pairs]
    lo, hi = min(0, min(vals)), max(0, max(vals))
    span = (hi - lo) or 1
    # 음수가 있으면 0선 왼쪽에도 값 라벨이 붙으므로 그만큼 여백을 둔다.
    # 없으면 0선이 라벨 열 바로 옆이라 여백이 필요 없다(가로 폭을 아낀다).
    neg_pad = 34 if min(vals) < 0 else 0
    plot_w = width - pad_left - 46 - neg_pad
    zero_x = pad_left + neg_pad + (0 - lo) / span * plot_w

    # 0 기준선 — 막대가 0에서 시작한다는 것을 눈으로 보이게
    d.add(Line(zero_x, 26, zero_x, h - 8, strokeColor=RULE, strokeWidth=0.8))
    for i, (k, v) in enumerate(pairs):
        y = h - 18 - (i + 1) * row_h + 5
        bw = abs(v) / span * plot_w
        x = zero_x if v >= 0 else zero_x - bw
        d.add(Rect(x, y, max(bw, 0.6), row_h - 8,
                   fillColor=DARK if v >= 0 else LIGHT, strokeColor=None))
        _txt(d, pad_left - 6, y + 3, k, anchor="end", fill=INK)
        # 값은 막대 끝에 직접 라벨 — 범례를 읽게 만들지 않는다
        _txt(d, (x + bw + 4) if v >= 0 else (x - 4), y + 3, _fmt(v),
             anchor="start" if v >= 0 else "end", fill=INK)
    _txt(d, 2, 8, axis_label, size=7)   # 좌측 끝 — 막대·값 라벨과 겹치지 않는다
    return d


def dist_compare(a, b, label_a, label_b, metric_label, width=440, height=96):
    """두 그룹 분포 비교 — 사분위 박스 + 중앙값. 그룹 비교 가설의 근거 그림.

    박스 플롯을 쓰는 이유: 평균 막대 두 개로는 **겹침**이 안 보인다. 중앙값이 갈려도
    분포가 크게 겹치면 "다르다"가 약해지는데, 그 사실이 그림에 나와야 정직하다.
    """
    import statistics as st

    def q(xs):
        s = sorted(xs)
        if len(s) < 4:
            return (s[0], st.median(s), s[-1], s[0], s[-1])
        qs = st.quantiles(s, n=4, method="inclusive")
        return (qs[0], qs[1], qs[2], s[0], s[-1])

    a = [x for x in a if x is not None]
    b = [x for x in b if x is not None]
    if len(a) < 2 or len(b) < 2:
        return Drawing(width, 10)
    qa, qb = q(a), q(b)
    lo = min(qa[3], qb[3])
    hi = max(qa[4], qb[4])
    span = (hi - lo) or 1
    pad_left, plot_w = 110, width - 110 - 46
    d = Drawing(width, height)

    for i, (qq, lab, col, n) in enumerate(
            [(qa, label_a, DARK, len(a)), (qb, label_b, LIGHT, len(b))]):
        y = height - 28 - i * 34
        x1, med, x3, mn, mx = [pad_left + (v - lo) / span * plot_w for v in qq]
        # 수염(최소~최대) — 이상치를 지우지 않는다는 원칙을 그림에서도 지킨다
        d.add(Line(mn, y + 7, mx, y + 7, strokeColor=RULE, strokeWidth=0.8))
        d.add(Rect(x1, y, max(x3 - x1, 0.6), 14, fillColor=col, strokeColor=None))
        d.add(Line(med, y - 2, med, y + 16, strokeColor=INK, strokeWidth=1.6))
        _txt(d, pad_left - 6, y + 4, "%s (n=%s)" % (lab, "{:,}".format(n)),
             anchor="end", fill=INK)
        _txt(d, med + 3, y + 17, _fmt(qq[1]), size=6.8, fill=INK)
    _txt(d, pad_left, 8, "%s — 상자=사분위, 굵은 선=중앙값, 가는 선=최소~최대" % metric_label,
         size=6.8)
    return d


def bins_bar(bins, x_label, y_label, width=440):
    """구간별 세로 막대 — 구간별 추이·용량반응("얼마나 주면 얼마나")에 쓴다.

    상관계수는 방향만 말한다. **어느 구간인지**를 보여주는 것이 이 그림의 일이다.
    각 막대에 n을 적는다 — 구간마다 표본이 다르다는 사실을 숨기지 않는다.
    """
    bins = [b for b in bins if b.get("median") is not None]
    if len(bins) < 2:
        return Drawing(width, 10)
    h = 150
    d = Drawing(width, h)
    vals = [b["median"] for b in bins]
    lo, hi = min(0, min(vals)), max(0, max(vals))
    span = (hi - lo) or 1
    pad_l, base_y = 46, 34
    plot_w = width - pad_l - 12
    plot_h = h - base_y - 24
    bw = plot_w / len(bins) * 0.62
    gap = plot_w / len(bins)
    zero_y = base_y + (0 - lo) / span * plot_h

    d.add(Line(pad_l, zero_y, width - 12, zero_y, strokeColor=RULE, strokeWidth=0.8))
    for i, b in enumerate(bins):
        x = pad_l + i * gap + (gap - bw) / 2
        bh = abs(b["median"]) / span * plot_h
        y = zero_y if b["median"] >= 0 else zero_y - bh
        d.add(Rect(x, y, bw, max(bh, 0.6), fillColor=DARK, strokeColor=None))
        _txt(d, x + bw / 2, y + bh + 3, _fmt(b["median"]), size=6.8, fill=INK, anchor="middle")
        rng = "%s~%s" % (_fmt(b.get("from")), _fmt(b.get("to")) if b.get("to") is not None else "")
        _txt(d, x + bw / 2, base_y - 10, rng, size=6.2, anchor="middle")
        _txt(d, x + bw / 2, base_y - 19, "n=%s" % "{:,}".format(b["n"]), size=6, anchor="middle")
    _txt(d, pad_l, h - 12, y_label, size=7)
    _txt(d, width - 12, 8, x_label, size=7, anchor="end")
    return d


def missing_bar(nulls, width=440, top=8):
    """결측률 막대 — EDA 프로파일용. 미노출이 많은 축부터 보여준다.

    **미노출과 0은 다르다**는 원칙을 그림에서도 지킨다: 결측률만 그리고 0 건수는
    표에 남긴다(둘을 한 막대에 쌓으면 같은 종류로 읽힌다).
    """
    rows = sorted([n for n in nulls if n.get("missing_pct") is not None],
                  key=lambda n: -n["missing_pct"])[:top]
    if not rows:
        return Drawing(width, 10)
    pairs = [(r["label"], r["missing_pct"]) for r in rows]
    return bar_h(pairs, "결측률(%) — 사이트가 보여주지 않은 비율", width=width)
