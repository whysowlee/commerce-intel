#!/usr/bin/env python3
"""AI 자유 생성 분석 리포트 린터 — 필수 요소를 코드로 검사한다 (SPEC-INTEL D20).

    python3 lint_analysis_html.py output/analysis-*.html

AI가 리포트를 직접 작성하는 모드에서, 재량에 맡기면 빠지기 쉬운 것들을 강제한다.
exit 0 = 통과 · 1 = FAIL 있음. 통과 전에는 산출물을 사용자에게 완성으로 보고하지 않는다.
"""
import re
import sys
from pathlib import Path

CHECKS = [
    ("정직성 배너", lambda h: "상관은 인과가 아니다" in h,
     "헤더에 '상관은 인과가 아니다' 문구가 있어야 한다"),
    ("생존편향 고지", lambda h: "생존편향" in h,
     "이 데이터에 없는 것(판매 종료·미관측 등) 고지가 있어야 한다"),
    ("다중비교 경고", lambda h: "다중비교" in h or "가설이지 결론이 아니다" in h,
     "탐색으로 찾은 패턴은 가설이라는 경고가 있어야 한다"),
    ("데이터 아일랜드", lambda h: 'type="application/json"' in h,
     "데이터는 --emit-json 산출을 그대로 임베드해야 한다 — 손으로 옮겨 적으면 안 된다"),
    ("외부 스크립트 0", lambda h: not re.search(r'<script[^>]+src\s*=\s*["\']https?://', h),
     "CDN 등 외부 스크립트 로드는 금지다"),
    ("외부 스타일 0", lambda h: not re.search(r'<link[^>]+href\s*=\s*["\']https?://', h)
     and "@import url(http" not in h,
     "외부 스타일시트·폰트 로드는 금지다"),
    ("다크 모드", lambda h: "prefers-color-scheme" in h,
     "다크 모드 대응이 있어야 한다"),
    ("구간 표기 축 금지", lambda h: not re.search(r'(data-axis|value)\s*=\s*["\']view_count["\']', h),
     "view_count(구간 표기)는 축·정렬 키로 쓰면 안 된다"),
]


def lint(path):
    h = Path(path).read_text(encoding="utf-8")
    fails = []
    for name, ok, why in CHECKS:
        if ok(h):
            print(f"  PASS  {name}")
        else:
            fails.append(name)
            print(f"  FAIL  {name} — {why}")
    return fails


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: lint_analysis_html.py <report.html> [...]")
    any_fail = False
    for p in sys.argv[1:]:
        print(f"[{p}]")
        if lint(p):
            any_fail = True
    sys.exit(1 if any_fail else 0)


if __name__ == "__main__":
    main()
