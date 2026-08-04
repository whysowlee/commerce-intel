#!/usr/bin/env python3
"""PDF 문서 빌더 — 이 프로젝트의 모든 리포트가 여기서 나온다 (D27 · 2026-08-03).

## 왜 PDF고, 왜 reportlab인가

리포트를 읽는 자리가 모바일이고 보고·공유에 쓰인다(사용자 지시 2026-08-03).
인터랙티브 HTML 대시보드는 폐기됐다 — 조작 규약(D26)·모듈 토글(D25)은 PDF에서
의미가 없다. 대신 조건을 바꿔가며 확인하는 일은 사람 손에서 **AI 쪽으로 넘어왔다**.

엔진은 Chrome을 먼저 택했다가 되돌렸다. 이 환경의 Chrome 150은 `--dump-dom`·
`--screenshot`은 정상인데 `--print-to-pdf`만 무한 대기한다(2026-08-03 실측:
절대경로·플래그 최소화·샌드박스 해제 전부 실패). 렌더 엔진이 아니라 인쇄 경로
문제이고, 우회하려면 CDP 웹소켓을 직접 물어야 해서 배포 일정 안에 넣을 위험이 아니었다.

reportlab은 **한글 CID 폰트가 내장**이라 폰트 파일을 번들하지 않아도 되고
(`HYGothic-Medium`), 결정적이며, 크로스 플랫폼이다. 대가는 `pip install reportlab`
하나가 생긴다는 것 — skills/README.md의 "설치할 패키지 없음"이 깨진다.

## 쓰는 법

    from pdf_doc import Doc
    d = Doc("인사이트 — 로우클래식", subtitle="2026-08-03 · 관측 07-01~08-02")
    d.honesty(["상관은 인과가 아니다", "지금 팔리는 것만 보고 있다"])
    d.h2("강한 주장")
    d.card("할인 20% 구간에서 판매 증분이 3배였다", audience="판매전략",
           evidence="n=42 · 관측 창 2026-07-01~07-28")
    d.save("output/insight-0803.pdf")
"""
import os
import re
import sys

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, KeepTogether,
                                    PageBreak, PageTemplate, Paragraph, Spacer, Table,
                                    TableStyle)
except ImportError:
    sys.stderr.write(
        "reportlab이 필요하다. PDF 리포트를 만드는 유일한 의존이다.\n"
        "    python3 -m pip install reportlab\n")
    raise


# ── 폰트 ────────────────────────────────────────────────────────────────────
# 한글은 reportlab 내장 CID 폰트를 쓴다. 폰트 파일을 번들하지 않는 이유는 배포 무게이고,
# 시스템 폰트에 의존하지 않는 이유는 사원마다 OS가 달라서다.
#
# CID 폰트에는 볼드 자형이 없다. 굵게는 획을 덧그려 흉내낸다(BOLD 스타일의 textColor·
# strokeWidth). 자형을 바꾸는 게 아니라 두께만 주는 것이라 한글에서도 깨지지 않는다.
FONT = "HYGothic-Medium"
_registered = False


def _ensure_font():
    global _registered
    if not _registered:
        pdfmetrics.registerFont(UnicodeCIDFont(FONT))
        _registered = True


# ── 색 ──────────────────────────────────────────────────────────────────────
# 흑백 인쇄에서도 읽혀야 한다(분석 리포트 스펙 §7 공유 전 점검). 그래서 정보는 색이
# 아니라 굵기·테두리·위치로 나른다. 색은 보조일 뿐이다.
INK = colors.HexColor("#16181D")
MUTED = colors.HexColor("#5B6472")
RULE = colors.HexColor("#D4D9E0")
RULE_STRONG = colors.HexColor("#AAB2BD")
BG_SOFT = colors.HexColor("#F7F8FA")
BG_ROW = colors.HexColor("#FAFBFC")
BG_HEAD = colors.HexColor("#F2F4F7")

# 장식 전용 악센트 — 제목 밑줄·카드 왼쪽 바·통계 스트립. 여기 한 줄만 바꾸면 된다.
ACCENT = colors.HexColor("#3A5060")

# 청중 배지. **라벨을 지우지 않으므로** 색을 못 봐도 읽힌다(색약 규칙 D12-d).
AUDIENCE_BADGE = {
    "판매전략": (colors.HexColor("#1B4F9C"), colors.white),   # 파랑
    "마케팅":  (colors.HexColor("#B3541E"), colors.white),   # 주황
    "디자인":  (colors.HexColor("#2E6B4F"), colors.white),   # 초록
}
BADGE_WEAK = (colors.HexColor("#E7EAEE"), colors.HexColor("#4A515C"))   # 약한 단서 — 채우지 않는다
BADGE_DEFAULT = (colors.HexColor("#4A515C"), colors.white)


def _styles():
    _ensure_font()
    base = dict(fontName=FONT, textColor=INK, alignment=TA_LEFT,
                wordWrap="CJK")   # 한글은 글자 단위가 아니라 어절로 끊는다
    return {
        "h1": ParagraphStyle("h1", **base, fontSize=21, leading=26, spaceAfter=3),
        "sub": ParagraphStyle("sub", **dict(base, textColor=MUTED),
                              fontSize=8.5, leading=12, spaceAfter=8),
        "h2": ParagraphStyle("h2", **base, fontSize=13.5, leading=18,
                             spaceBefore=16, spaceAfter=4),
        "h3": ParagraphStyle("h3", **base, fontSize=11, leading=15,
                             spaceBefore=8, spaceAfter=3),
        "body": ParagraphStyle("body", **base, fontSize=9.8, leading=14.5, spaceAfter=5),
        "small": ParagraphStyle("small", **dict(base, textColor=MUTED),
                                fontSize=8.2, leading=11.5, spaceAfter=3),
        "cell": ParagraphStyle("cell", **base, fontSize=8.6, leading=11.8),
        "cellhead": ParagraphStyle("cellhead", **base, fontSize=8.6, leading=11.8),
        "claim": ParagraphStyle("claim", **base, fontSize=10.8, leading=15.5, spaceAfter=3),
        "evidence": ParagraphStyle("evidence", **dict(base, textColor=MUTED),
                                   fontSize=8.2, leading=11.5),
        # 통계 스트립 — 큰 숫자 + 작은 라벨 (pdf-design stats strip)
        "stat_num": ParagraphStyle("stat_num", **base, fontSize=17, leading=20, spaceAfter=1),
        "stat_label": ParagraphStyle("stat_label", **dict(base, textColor=MUTED),
                                     fontSize=7.6, leading=10),
    }


# ── 글리프 치환 ─────────────────────────────────────────────────────────────
# 내장 CID 폰트에 없는 글자는 **조용히 엉뚱한 한글 음절로 그려진다** — 빈칸이나 두부(tofu)가
# 아니라서 눈으로 보기 전에는 모른다. 2026-08-03 실측으로 걸러낸 목록이다.
#
# 상품명·수집 notes에 이 글자들이 섞여 들어오므로 리포트 코드가 아니라 esc()에서 막는다.
# 정상 확인된 것(치환하지 않는다): → ← ↑ ↓ ⇒ ▶ ▲ ▼ ★ ☆ ※ ◆ ● ≥ ≤ ± × ÷ ≈ ─ │ ① ② ⓐ ㈜
BAD_GLYPHS = {
    "·": "・",   # · 가운뎃점 → ・ (가장 가까운 자형. 원본은 깨진다)
    "⚠": "※",   # ⚠ → ※
    "✅": "○",   # ✅ → ○
    "❌": "×",   # ❌ → ×
    "₩": "원",        # ₩ → 원
    "€": "EUR",      # € → EUR
    "¥": "JPY",      # ¥ → JPY
    "­": "",         # soft hyphen — ↓ 로 그려진다. 지운다
}


def _glyphs(text):
    for bad, good in BAD_GLYPHS.items():
        if bad in text:
            text = text.replace(bad, good)
    return text


def esc(value):
    """상품명에 &, < 가 실제로 들어온다. Paragraph는 마크업을 해석하므로 반드시 막는다.

    폰트에 없는 글자 치환도 여기서 같이 한다 — 통과 지점을 하나로 모아야 빠지지 않는다.
    """
    if value is None:
        return ""
    text = _glyphs(str(value))
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def b(text):
    """굵게. CID 폰트에 볼드 자형이 없어 reportlab의 <b>는 획 덧그리기로 처리된다."""
    return "<b>%s</b>" % text


class Doc:
    """리포트 하나. 요소를 순서대로 쌓고 save()로 굽는다."""

    def __init__(self, title, subtitle="", pagesize=A4,
                 margins=(13 * mm, 12 * mm, 15 * mm, 12 * mm)):
        self.title = title
        self.subtitle = subtitle
        self.pagesize = pagesize
        self.margins = margins          # top, right, bottom, left
        self.s = _styles()
        self.flow = []
        self._anchors = {}              # 앵커 이름 → 페이지 (save 후 채워진다)
        self.h1(title)
        if subtitle:
            self.flow.append(Paragraph(esc(subtitle), self.s["sub"]))
            # 제목 블록 마감 — 전폭 얇은 선 대신 짧고 굵은 악센트 바(pdf-design 패턴).
            # 가는 전폭 선보다 시선이 제목에 모이고, 본문 표의 선들과 위계가 갈린다.
            self.flow.append(_Rule(2.6, ACCENT, width=74))
            self.flow.append(Spacer(1, 12))

    # ── 텍스트 ──────────────────────────────────────────────────────────
    def h1(self, text):
        self.flow.append(Paragraph(esc(text), self.s["h1"]))

    def h2(self, text, anchor=None):
        # 제목 아래 짧은 악센트 바 — 전폭 밑줄보다 깔끔하고 표 머리선과 혼동되지 않는다
        el = [Paragraph(esc(text), self.s["h2"]), _Rule(2.0, ACCENT, width=52), Spacer(1, 7)]
        if anchor:
            el.insert(0, _Anchor(anchor, self))
        # 제목만 남고 내용이 다음 장으로 넘어가는 꼴을 막는다
        self.flow.append(KeepTogether(el))

    def h3(self, text):
        self.flow.append(Paragraph(esc(text), self.s["h3"]))

    def para(self, text, style="body"):
        # 마크업(<b> 등)이 이미 들어 있는 문자열은 이스케이프하지 않는다 —
        # 다만 글리프 치환은 양쪽 다 거쳐야 한다(빠지면 그 줄만 깨진 채 나간다).
        body = _glyphs(text) if _has_markup(text) else esc(text)
        self.flow.append(Paragraph(md(body), self.s[style]))

    def small(self, text):
        self.para(text, style="small")

    def spacer(self, height=6):
        self.flow.append(Spacer(1, height))

    def page_break(self):
        self.flow.append(PageBreak())

    # ── 정직성 배너 ─────────────────────────────────────────────────────
    def honesty(self, points, title="이 리포트를 읽기 전에"):
        """분석 리포트 스펙 §4. 형식이 HTML에서 PDF로 바뀌어도 이건 빠지지 않는다.

        요청과 무관하게 필수다 — 무엇을 보고 있고 무엇이 데이터에 없는지 먼저 말한다.
        """
        rows = [[Paragraph(b(esc(title)), self.s["small"])]]
        for p in points:
            # 글머리표는 esc()를 거치지 않으므로 안전한 글자를 직접 쓴다(BAD_GLYPHS 참조)
            rows.append([Paragraph(md("・ " + esc(p)), self.s["small"])])
        t = Table(rows, colWidths=[self._content_width()])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), BG_SOFT),
            ("BOX", (0, 0), (-1, -1), 0.6, RULE_STRONG),
            ("LINEBEFORE", (0, 0), (0, -1), 2.5, RULE_STRONG),   # 카드와 같은 문법의 왼쪽 바
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (0, 0), 7),
            ("BOTTOMPADDING", (0, -1), (-1, -1), 7),
        ]))
        self.flow.append(t)
        self.flow.append(Spacer(1, 10))

    # ── 통계 스트립 (pdf-design stats strip 이식 · 2026-08-04) ──────────
    def stats(self, items):
        """핵심 수치 3~4개를 한 줄로 — 왼쪽 악센트 바 + 큰 숫자 + 작은 라벨.

        요약 리포트 머리에서 "이 리포트에 뭐가 몇 개 있나"를 첫눈에 답한다.
        숫자와 라벨이 전부 텍스트라 색이 없어도(흑백 인쇄) 그대로 읽힌다.
        items: [(라벨, 값)] — 값이 str이면 그대로, 수면 천 단위 구분해 그린다.
        """
        items = [(l, v) for l, v in items if v is not None]
        if not items:
            return
        n = len(items)
        cells = []
        for label, value in items:
            text = value if isinstance(value, str) else "{:,}".format(value)
            cells.append([Paragraph(b(esc(text)), self.s["stat_num"]),
                          Paragraph(esc(label), self.s["stat_label"])])
        t = Table([cells], colWidths=[self._content_width() / n] * n)
        style = [("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("TOPPADDING", (0, 0), (-1, -1), 2),
                 ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                 ("LEFTPADDING", (0, 0), (-1, -1), 8),
                 ("RIGHTPADDING", (0, 0), (-1, -1), 6)]
        for i in range(n):
            style.append(("LINEBEFORE", (i, 0), (i, 0), 2.5, ACCENT))
        t.setStyle(TableStyle(style))
        self.flow.append(t)
        self.flow.append(Spacer(1, 12))

    # ── 배지 ────────────────────────────────────────────────────────────
    def _badge_row(self, badges):
        """색이 다른 작은 라벨 여러 개를 한 줄로. 각 배지는 글자 폭만큼만 차지한다.

        폭을 stringWidth로 재서 준다 — 고정 폭을 주면 「디자인」과 「판매전략」이 같은
        칸에 들어가 짧은 쪽에 빈 공간이 남는다. 마지막 칸은 남는 폭을 먹여 왼쪽 정렬.
        """
        cells, widths = [], []
        for text, (bg, fg) in badges:
            st = ParagraphStyle("badge", parent=self.s["small"], textColor=fg,
                                fontSize=7.6, leading=9.5, spaceAfter=0, alignment=TA_CENTER)
            cells.append(Paragraph(b(esc(text)), st))
            widths.append(pdfmetrics.stringWidth(text, FONT, 7.6) + 14)
        cells.append("")
        widths.append(max(self._content_width() - sum(widths) - 18, 1))
        t = Table([cells], colWidths=widths, rowHeights=[13.5])
        style = [("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                 ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                 ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
        for i, (_, (bg, _fg)) in enumerate(badges):
            style.append(("BACKGROUND", (i, 0), (i, 0), bg))
            style.append(("RIGHTPADDING", (i, 0), (i, 0), 5))   # 배지 사이 간격
        t.setStyle(TableStyle(style))
        return t

    # ── 카드 (가설 하나 = 카드 하나) ────────────────────────────────────
    def card(self, claim, audience=None, evidence=None, weak=False,
             detail_link=None, anchor=None):
        """가설 카드.

        weak=True면 약한 단서다 — 왼쪽 굵은 선이 회색으로 바뀌고 배경이 옅어진다.
        색이 아니라 **선 굵기와 라벨**로 구분되므로 흑백 인쇄에서도 살아남는다.
        """
        inner = []
        badges = ([(audience, AUDIENCE_BADGE.get(audience, BADGE_DEFAULT))] if audience else []) \
            + ([("약한 단서", BADGE_WEAK)] if weak else [])
        if badges:
            inner.append(self._badge_row(badges))
        inner.append(Paragraph(md(esc(claim)), self.s["claim"]))
        if detail_link:
            inner.append(Paragraph("→ 상세: %s" % esc(detail_link), self.s["small"]))
        if evidence:
            inner.append(_Rule(0.4, RULE))
            inner.append(Spacer(1, 3))
            inner.append(Paragraph(esc(evidence), self.s["evidence"]))

        t = Table([[inner]], colWidths=[self._content_width()])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), BG_ROW if weak else colors.white),
            ("BOX", (0, 0), (-1, -1), 0.6, RULE),
            # 강한 주장은 악센트 색 + 더 굵은 바. 색을 못 봐도 굵기(3.0 대 2.5)와
            # 배경·「약한 단서」배지로 갈리므로 흑백 원칙은 그대로다.
            ("LINEBEFORE", (0, 0), (0, -1),
             2.5 if weak else 3.0, RULE_STRONG if weak else ACCENT),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        el = [t, Spacer(1, 6)]
        if anchor:
            el.insert(0, _Anchor(anchor, self))
        self.flow.append(KeepTogether(el))

    # ── 표 ──────────────────────────────────────────────────────────────
    def table(self, header, rows, widths=None, align_right=()):
        """표. 값 없는 칸은 빈칸이 아니라 '—'로 그린다 — 미노출과 0은 다르다.

        align_right에 열 인덱스를 주면 그 열은 우측 정렬 + 자릿수 정렬된다(수치 열).
        """
        cw = self._col_widths(len(header), widths)
        data = [[Paragraph(b(esc(h)), self.s["cellhead"]) for h in header]]
        for r in rows:
            # 셀도 마크다운 굵게를 받는다 — para()·card()만 지원하면 표에서만 별표가
            # 그대로 인쇄돼 같은 문서 안에서 표기가 갈린다(2026-08-03 실측)
            data.append([Paragraph(md(esc("—" if v is None or v == "" else v)), self.s["cell"])
                         for v in r])
        t = Table(data, colWidths=cw, repeatRows=1)   # 페이지 넘어가면 머리행 반복
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), BG_HEAD),
            ("LINEBELOW", (0, 0), (-1, 0), 1.2, INK),   # 머리행은 본문 선보다 확실히 굵게
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        for i in range(2, len(data), 2):
            style.append(("BACKGROUND", (0, i), (-1, i), BG_ROW))
        for c in align_right:
            style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
        t.setStyle(TableStyle(style))
        self.flow.append(t)
        self.flow.append(Spacer(1, 8))

    def figure(self, drawing, caption=None):
        """차트 한 장. chart.py가 만든 Drawing을 받는다."""
        el = [drawing]
        if caption:
            el.append(Spacer(1, 2))
            el.append(Paragraph(esc(caption), self.s["small"]))
        el.append(Spacer(1, 8))
        self.flow.append(KeepTogether(el))

    # ── 저장 ────────────────────────────────────────────────────────────
    def save(self, out_path):
        out_path = os.path.abspath(out_path)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        top, right, bottom, left = self.margins
        doc = BaseDocTemplate(
            out_path, pagesize=self.pagesize,
            topMargin=top, rightMargin=right, bottomMargin=bottom, leftMargin=left,
            title=self.title, author="commerce-intel")
        frame = Frame(left, bottom,
                      self.pagesize[0] - left - right,
                      self.pagesize[1] - top - bottom, id="body",
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        doc.addPageTemplates([PageTemplate(id="main", frames=[frame],
                                           onPage=self._footer)])
        doc.build(self.flow)
        return out_path

    def _footer(self, canvas, doc):
        """쪽번호와 출처. 인쇄물은 어디서 났는지 항상 적혀 있어야 한다.

        위에 얇은 선을 그어 본문과 시각적으로 끊는다(pdf-design 푸터 패턴).
        문서 제목을 함께 적어 낱장으로 돌아다녀도 출처를 안다.
        """
        canvas.saveState()
        w = self.pagesize[0]
        _, right, bottom, left = self.margins
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.6)
        canvas.line(left, bottom - 4, w - right, bottom - 4)
        canvas.setFont(FONT, 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(left, bottom - 14, _glyphs("commerce-intel · %s" % self.title))
        canvas.drawRightString(w - right, bottom - 14, "%d" % doc.page)
        canvas.restoreState()

    # ── 내부 ────────────────────────────────────────────────────────────
    def _content_width(self):
        return self.pagesize[0] - self.margins[1] - self.margins[3]

    def _col_widths(self, n, widths):
        total = self._content_width()
        if not widths:
            return [total / n] * n
        s = float(sum(widths))
        return [total * w / s for w in widths]


_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
# 강조·인라인 코드. **굵게를 먼저 치환한 뒤** 남은 홑별표만 기울임으로 본다 —
# 순서를 바꾸면 `**굵게**`의 바깥 별표 한 쌍이 기울임으로 잘못 잡힌다.
_MD_ITALIC = re.compile(r"(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)")
_MD_CODE = re.compile(r"`([^`\n]+?)`")


def md(text):
    """마크다운 인라인 표기를 reportlab 마크업으로 바꾼다 — `**굵게**` · `*기울임*` · `` `코드` ``.

    리포트 문구를 쓸 때 마크다운이 손에 익어 그냥 치게 되는데, 변환하지 않으면
    별표와 백틱이 그대로 인쇄된다(2026-08-04 사용자 지적 — 상세 PDF에 `*안 한 것*`이
    별표째 찍혔다). 이스케이프 뒤에 돌려야 본문의 `<`가 태그로 새지 않는다.
    """
    text = _MD_BOLD.sub(r"<b>\1</b>", text)
    text = _MD_ITALIC.sub(r"<i>\1</i>", text)
    return _MD_CODE.sub(r'<font face="Courier">\1</font>', text)


def _has_markup(text):
    return "<b>" in text or "<i>" in text or "<a " in text


class _Rule(Flowable):
    """가로줄. 제목 밑 악센트 바와 카드 안 구분선에 쓴다.

    width를 주면 그 폭(pt)만큼만 왼쪽 정렬로 그린다 — 제목 아래 짧은 악센트 바
    (pdf-design 패턴). 안 주면 전폭.
    """

    def __init__(self, thickness=0.5, color=RULE, width=None):
        Flowable.__init__(self)
        self.thickness = thickness
        self.color = color
        self.fixed_width = width
        self.width = 0
        self.height = thickness

    def wrap(self, availWidth, availHeight):
        self.width = min(self.fixed_width, availWidth) if self.fixed_width else availWidth
        return (self.width, self.thickness)

    def draw(self):
        self.canv.setStrokeColor(self.color)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(0, 0, self.width, 0)


class _Anchor(Flowable):
    """앵커. 인사이트 PDF가 상세 PDF의 특정 위치를 가리킬 때 쓴다.

    자리를 차지하지 않는다 — 그려질 때 자기가 놓인 쪽 번호를 doc에 기록해 두고,
    인사이트 쪽이 그 번호로 "상세 N쪽"을 적는다. PDF 뷰어마다 파일 간 앵커 점프
    지원이 제각각이라, 쪽 번호를 본문에 적는 쪽이 어디서나 통한다.
    """

    def __init__(self, name, doc):
        Flowable.__init__(self)
        self.name = name
        self.doc = doc
        self.width = self.height = 0

    def wrap(self, availWidth, availHeight):
        return (0, 0)

    def draw(self):
        self.canv.bookmarkPage(self.name)
        self.doc._anchors[self.name] = self.canv.getPageNumber()


if __name__ == "__main__":
    d = Doc("PDF 렌더 스모크", subtitle="한글·표·카드·페이지 넘김 확인 · 2026-08-03")
    d.honesty([
        "이 문서는 렌더 확인용이며 실제 관측 데이터가 아니다.",
        "상관은 인과가 아니다 — 여러 조합을 훑다 발견한 패턴은 가설이지 결론이 아니다.",
        "지금 팔리고 있는 것만 보고 있다(판매 종료 상품은 수집 시점에 이미 없다).",
    ])
    d.stats([("강한 주장", 2), ("약한 단서", 1), ("상품", 1317), ("검정한 가설", 24)])
    d.h2("강한 주장")
    d.card("할인율 20% 구간에서 판매 증분이 다른 구간의 3배였다.",
           audience="판매전략", evidence="n=42 · 효과크기 1.4 · 관측 창 2026-07-01~07-28",
           detail_link="detail-0803.pdf 4쪽")
    d.card("스쿱넥 상의는 같은 가격대 라운드넥보다 하트 증분이 높았다.",
           audience="디자인", evidence="n=61 · 효과크기 0.7 · 관측 창 2026-07-10~08-02")
    d.h2("약한 단서")
    d.card("민트 컬러가 블랙보다 품절 속도가 빨랐다.", audience="마케팅", weak=True,
           evidence="n=11 · 다중비교 보정 미통과 — 다음 주 관측으로 재확인 가능")
    d.h2("표 렌더")
    d.table(
        ["상품명", "플랫폼", "판매가", "할인율", "품절"],
        [["REYA LACE TOP (PINK)", "자사몰", "59,000", "30%", "판매중"],
         ["SNYDER WIDE DENIM (INDIGO)", "무신사", "128,000", "15%", "판매중"],
         ["LUNA BOOTCUT LEGGINGS (ASH)", "29CM", "48,000", None, "품절"]],
        widths=[38, 14, 16, 14, 12], align_right=(2, 3))
    out = d.save("output/_smoke.pdf")
    print("OK:", out, os.path.getsize(out), "bytes")
