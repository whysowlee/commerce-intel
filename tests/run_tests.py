#!/usr/bin/env python3
"""commerce-intel 스킬 스크립트 회귀 테스트.

실제 사이트에 붙지 않고 픽스처로 scripts/의 동작만 검증한다.
사이트를 실제로 도는 트리거/기능 테스트는 docs/TEST-CASES.md에 있다.

usage: python3 tests/run_tests.py
"""

import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "commerce-intel", "scripts")
TESTS = os.path.join(ROOT, "tests")
WORK = os.path.join(TESTS, ".work")
FIX = os.path.join(WORK, "fixtures")
OUT = os.path.join(WORK, "output")

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  PASS  %s" % name)
    else:
        FAILED.append((name, detail))
        print("  FAIL  %s%s" % (name, ("\n        " + detail) if detail else ""))


def run(script, *args):
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, script)] + list(args),
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def fx(*parts):
    return os.path.join(FIX, *parts)


def out(name):
    return os.path.join(OUT, name)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(OUT, exist_ok=True)

    sys.path.insert(0, TESTS)
    import make_fixtures

    make_fixtures.build(FIX)

    print("\n[1] validate_data.py — 스키마·결측 판정")

    code, log = run("validate_data.py", fx("musinsa-brand-linesheet-good.json"),
                    "--json", out("v-good.json"))
    check("V1 정상 데이터는 PASS(0)", code == 0, "exit=%d\n%s" % (code, log[-500:]))

    summary = json.loads(read(out("v-good.json")))
    check("V1b 검증 요약 JSON에 노출률이 들어간다",
          summary["exposure_by_field_pct"].get("like_count", 0) > 0
          and summary["verdict"] == "PASS",
          json.dumps(summary.get("exposure_by_field_pct"), ensure_ascii=False))
    check("V1c 구간 표기 조회수는 display 칸으로 집계된다",
          summary["exposure_by_field_pct"].get("view_count_display", 0) > 0
          and summary["exposure_by_field_pct"].get("view_count", 0) == 0,
          json.dumps(summary.get("exposure_by_field_pct"), ensure_ascii=False))

    code, log = run("validate_data.py", fx("musinsa-brand-linesheet-broken.json"))
    check("V2 결측 30% 초과는 FAIL(2)", code == 2, "exit=%d" % code)
    check("V2b 구조 변경 의심을 말한다", "구조 변경" in log, log[-400:])

    code, log = run("validate_data.py", fx("musinsa-brand-linesheet-empty.json"))
    check("V3 수집 0건은 FAIL(2)", code == 2, "exit=%d" % code)
    check("V3b 빈 리포트를 만들지 말라고 한다", "빈 리포트" in log, log[-300:])

    code, log = run("validate_data.py", fx("musinsa-brand-linesheet-partial.json"))
    check("V4 부분 수집은 WARN(1)", code == 1, "exit=%d" % code)
    check("V4b 총계 오차와 중단을 짚는다",
          "incomplete" in log and "노출 총계" in log, log[-400:])
    check("V4c 구간 표기를 정수로 바꿔 담으면 잡아낸다",
          "구간 표기를 정수로" in log, log[-400:])

    # SPEC v15 — 축약 표기(`1.2천`)는 정수 파싱이 허용되므로 원문과 병기해도 경고하지 않는다.
    # 구간 표기(`300회 이상`)만 여전히 경고 대상이다. 둘을 한 픽스처에 같이 넣어 가른다.
    abbrev_doc = json.loads(read(fx("musinsa-market-scan.json")))
    abbrev_doc["items"][0]["purchase_count"] = 90000
    abbrev_doc["items"][0]["purchase_count_display"] = "판매 9만개"
    abbrev_doc["items"][1]["like_count"] = 1200
    abbrev_doc["items"][1]["like_count_display"] = "1.2천"
    write(out("scan-abbrev.json"), json.dumps(abbrev_doc, ensure_ascii=False))
    code, log = run("validate_data.py", out("scan-abbrev.json"))
    check("V4d 축약 표기는 정수와 원문을 같이 담아도 경고하지 않는다",
          "구간 표기를 정수로" not in log, log[-400:])

    abbrev_doc["items"][2]["view_count"] = 300
    abbrev_doc["items"][2]["view_count_display"] = "300회 이상 (최근 1개월)"
    write(out("scan-abbrev.json"), json.dumps(abbrev_doc, ensure_ascii=False))
    code, log = run("validate_data.py", out("scan-abbrev.json"))
    check("V4e 같은 파일이어도 구간 표기는 여전히 경고한다",
          "구간 표기를 정수로" in log, log[-400:])

    code, log = run("validate_data.py", fx("musinsa-market-scan.json"),
                    "--json", out("v-scan.json"))
    check("V5 시장 전수조사 데이터 검증 통과", code in (0, 1), "exit=%d\n%s" % (code, log[-500:]))
    scan_summary = json.loads(read(out("v-scan.json")))
    check("V5b 속성 분류율이 계산된다",
          scan_summary["attribute_coverage_pct"] is not None,
          str(scan_summary.get("attribute_coverage_pct")))
    check("V5c 리뷰 본문이 없는 게 정상이다",
          scan_summary["unexpected_review_bodies"] == 0
          and not any("리뷰 본문" in w for w in scan_summary["warnings"]),
          json.dumps(scan_summary.get("warnings"), ensure_ascii=False)[-300:])

    # 실수로 리뷰 본문을 담아 오면 검증기가 짚어야 한다
    tainted = json.loads(read(fx("musinsa-market-scan.json")))
    tainted["items"][0]["reviews"] = [{"date": "2026-01-01", "rating": 5, "text": "좋아요"}]
    with open(out("scan-tainted.json"), "w", encoding="utf-8") as fh:
        json.dump(tainted, fh, ensure_ascii=False)
    code, log = run("validate_data.py", out("scan-tainted.json"))
    check("V5d 리뷰 본문이 담겨 있으면 경고한다",
          code == 1 and "리뷰 본문을 수집하지 않는다" in log, "exit=%d\n%s" % (code, log[-300:]))

    # 인코딩이 깨진 채 들어오면 결측률은 통과해도 FAIL이어야 한다 (v5)
    code, log = run("validate_data.py", fx("musinsa-brand-linesheet-mojibake.json"))
    check("V7 인코딩이 깨진 문자열을 FAIL로 잡는다",
          code == 2 and "인코딩이 깨졌다" in log and "latin-1" in log,
          "exit=%d\n%s" % (code, log[-400:]))
    code, log = run("validate_data.py", fx("musinsa-brand-linesheet-good.json"))
    check("V7b 정상 한글은 mojibake로 오탐하지 않는다",
          code == 0 and "인코딩이 깨졌다" not in log, "exit=%d" % code)

    snap = fx("snapshots", "musinsa-ranking-바지-20260301.json")
    code, log = run("validate_data.py", snap)
    check("V6 랭킹 스냅샷 검증 통과", code == 0, "exit=%d\n%s" % (code, log[-500:]))

    print("\n[2] build_report.py — HTML 생성")

    code, log = run("build_report.py", fx("musinsa-brand-linesheet-good.json"),
                    fx("29cm-brand-linesheet-good.json"),
                    "--validation", out("v-good.json"),
                    "--out", out("linesheet.html"))
    check("B1 크로스 플랫폼 라인시트 생성", code == 0 and os.path.exists(out("linesheet.html")),
          "exit=%d\n%s" % (code, log[-400:]))
    page = read(out("linesheet.html"))
    check("B1b 인기 상품 상위 10 섹션을 만들지 않는다 (전 상품을 좋아요로 정렬하면 같은 답)",
          "플랫폼별 인기 상품" not in page and "상위 10 상세" not in page)
    check("B1b2 품목별 인기 비교가 있다",
          "플랫폼별 품목 인기" in page and "품목별 하트 합" in page and "품목별 후기 합" in page
          and page.count("품목별 하트 합") == 2)  # 무신사·29CM 각각
    check("B1c 무신사·29CM가 모두 나온다", "무신사" in page and "29CM" in page)
    check("B1d 미노출 값을 추정하지 않고 표기한다", "미노출" in page)
    check("B1g 구간 표기 조회수는 문구 그대로 싣는다",
          "회 이상 (최근 1개월)" in page)
    check("B1h 구간 표기 값에는 정렬 키를 주지 않는다",
          'class="approx"' in page
          and re.search(r'<td class="col-num" data-k="">\s*<span class="approx"', page) is not None)
    check("B1e 외부 리소스를 부르지 않는다",
          "http://" not in page.replace("http://www.w3.org", "")
          or "cdn" not in page.lower(),
          "외부 스크립트/스타일 참조 의심")
    check("B1f 표 정렬·거르기가 붙는다", 'class="filter"' in page and "sorted-asc" in page)
    # 상품 표에서만 따진다 — 품목 성적표는 한 행이 한 품목이라 품목명이 행 식별자이고,
    # 거기서는 정렬이 정상이다. 규칙이 걸리는 곳은 값이 반복되는 상품 표다.
    linesheet_head = page.split('id="t-linesheet"')[1].split("</thead>")[0]
    check("B1i 상품 표에서 품목·입점은 정렬 열이 아니라 다중 선택 칩이다 (품목은 상위·세부 계층)",
          'data-sort="text">카테고리' not in linesheet_head
          and 'data-sort="text">품목' not in linesheet_head
          and '<span class="facet-label">품목 · 대분류</span>' in page
          and '<span class="facet-label">품목 · 중분류</span>' in page
          and '<span class="facet-label">품목 · 소분류</span>' in page
          and 'data-clevel="2"' in page
          and '<span class="facet-label">입점 수식</span>' in page
          and 'class="expr-input"' in page
          and page.count('class="chip"') >= 2)

    # ── 멀티 플랫폼 합집합·매칭 (SPEC v6 §4 스토리1) ────────────────────────
    check("B1m 합집합 표가 나온다 (매칭된 상품은 1행)",
          "전 상품 (플랫폼 합집합)" in page
          # 무신사 24 + 29CM 14 = 38건이지만 10건이 매칭돼 합집합은 28행이다
          and '<span class="table-count" id="t-linesheet-count">28행</span>' in page,
          "합집합 행수가 28이 아니다")
    check("B1n 입점 칼럼이 세 가지 값을 구분한다",
          "양쪽 입점" in page and "무신사 단독" in page and "29CM 단독" in page)
    check("B1o 헤더가 단순 합과 합집합을 구분해 싣는다",
          "단순 합 38건" in page and "647" not in page.split("<body")[0]
          and "28건 — 양쪽 입점 10" in page,
          "헤더에 합집합 요약이 없다")
    check("B1p 비교 레이어 3블록이 있다 (구 4개 축 — 커버리지+단독 성격을 합쳤다)",
          "품목별 입점 차이" in page and "같은 상품 반응 강도 비교" in page
          and "가격·할인 포지셔닝 차이" in page
          and "<h2>단독 입점 상품 성격</h2>" not in page
          and "단독 중위가" in page)
    check("B1p2 같은 답을 두 번 그리지 않는다 (상품 표는 전 상품 하나뿐)",
          page.count('<table id="t-linesheet"') == 1
          and "t-price-gap" not in page
          and "단독 입점이 많은 품목" not in page and "단독 — 품목 분포" not in page)
    check("B1q 반응 강도는 매칭된 상품만 센다고 밝힌다",
          "단독 입점 상품은 이 비교에서 빠진다" in page and "매칭 상품" in page)
    check("B1r 품목 통합축의 대응 근거를 싣는다",
          "품목 대응" in page and "니트웨어" in page and "동일 상품" in page,
          "통합축 근거(details.axis-map)가 없다")
    check("B1s 추정 매칭을 하지 않는다고 밝힌다",
          "정규화 상품명 완전일치" in page and "추정 매칭" in page)
    check("B1t 한쪽에만 있는 칸은 미노출이 아니라 —로 구분한다",
          "그 플랫폼에 그 상품이 없다" in page and "—" in page)

    # ── 플랫폼 3개 (SPEC v14 §4 스토리1) ───────────────────────────────────
    #
    # 규칙은 "사이트 수와 무관하게 성립해야 한다"인데, 3개가 되어야만 드러나는 버그가 있었다.
    # 2-플랫폼 픽스처만으로는 영영 안 잡히므로 여기서 세 개를 넘긴다.
    code, _ = run("build_report.py",
                  fx("musinsa-brand-linesheet-good.json"),
                  fx("29cm-brand-linesheet-good.json"),
                  fx("insilence-brand-linesheet.json"),
                  "--out", out("linesheet-3p.html"))
    p3 = read(out("linesheet-3p.html"))

    def coverage_sums(page):
        """`품목별 입점 차이` 표에서 합집합 합계와 단독 합계를 읽는다."""
        sec = page.split("품목별 입점 차이")[1].split("</section>")[0]
        body = sec.split("<tbody>")[1].split("</tbody>")[0]
        total = solo = 0
        for tr in re.findall(r"<tr.*?</tr>", body, re.S):
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<td.*?</td>", tr, re.S)]
            if len(cells) >= 8:
                total += int(cells[1])
                solo += sum(int(cells[i]) for i in (5, 6, 7))
        return total, solo

    p3_total, p3_solo = coverage_sums(p3)
    # 검산: 단독 합계 + 2곳 이상 입점 = 합집합. 구 구조는 `정확히 2곳`을 잃어 26 ≠ 28이었다.
    check("B1v 플랫폼 3개에서 상품이 입점 표에서 사라지지 않는다",
          code == 0 and p3_total == 28 and p3_solo == 18,
          "합집합 %d · 단독 %d (기대 28 / 18 — 정확히 2곳 입점 4건이 빠지면 실패)"
          % (p3_total, p3_solo))
    check("B1w 입점 표 열이 플랫폼별 입점·단독 두 쌍이다",
          "무신사 입점" in p3 and "29CM 입점" in p3 and "insilence.co.kr 입점" in p3
          and "무신사 단독" in p3 and "insilence.co.kr 단독" in p3
          and "양쪽 입점" not in p3,          # 3개일 때 '양쪽'은 틀린 말이다
          "플랫폼별 입점/단독 열이 아니다")
    check("B1x 3개 이상이면 '2곳 이상'과 '전 플랫폼'을 따로 센다",
          "2곳 이상 입점" in p3 and "전 플랫폼 입점" in p3,
          "두 값은 다른데 한 이름으로 뭉뚱그렸다")
    check("B1y 입점 축은 불리언 수식(AND·OR·NOT·괄호)이다",
          'facet-expr' in p3 and 'class="expr-input"' in p3
          and 'class="chip expr-op" data-ins=" AND "' in p3
          and 'class="chip expr-op" data-ins=" OR "' in p3
          and 'class="chip expr-op" data-ins=" NOT "' in p3
          and 'data-ins="insilence.co.kr"' in p3,
          "입점 축이 수식 입력이 아니거나 연산자·플랫폼 삽입 칩이 없다")
    check("B1z 자사몰은 반응 지표가 null이라 비교 축에서 빠진다",
          "insilence.co.kr 좋아요" not in p3 and "insilence.co.kr 후기" not in p3
          and "insilence.co.kr 평점" not in p3
          # 조용히 빼면 그 플랫폼이 애초에 없었다고 읽는다 — 빠진 사실을 밝혀야 한다
          and "노출하지 않아 수집하지 않았다" in p3,
          "수집하지 않은 지표로 열을 만들었거나, 빠진 사실을 밝히지 않았다")
    # 둘 다 지표를 내는 2-플랫폼 리포트에는 제외 각주가 붙으면 안 된다 (오탐 방지)
    check("B1z2 지표를 내는 플랫폼만 있으면 제외 각주를 붙이지 않는다",
          "노출하지 않아 수집하지 않았다" not in page,
          "빠진 플랫폼이 없는데 제외 각주가 붙었다")

    # 플랫폼이 1개면 비교 섹션을 만들지 않는다 — 빈 섹션을 내지 않는 것이 규칙이다.
    code, log = run("build_report.py", fx("musinsa-brand-linesheet-good.json"),
                    "--out", out("linesheet-solo.html"))
    solo_page = read(out("linesheet-solo.html"))
    check("B1u 플랫폼 1개면 비교 섹션을 만들지 않는다",
          code == 0
          and "플랫폼 비교 요약" not in solo_page
          and "품목별 입점 커버리지 갭" not in solo_page
          and "전 상품 (플랫폼 합집합)" not in solo_page
          and '<span class="facet-label">카테고리 · 대분류</span>' in solo_page,
          "단일 플랫폼 리포트에 비교 섹션이 붙었다")
    check("B1j 품목 성적표에 규모 대비 지수가 있다",
          "품목 성적표" in page and "규모 대비 하트" in page and "규모 대비 후기" in page
          and 'class="th-tip"' in page)
    check("B1l 파생 지표의 계산식이 리포트에 적혀 있다",
          "지표 비중 ÷ 상품 수 비중" in page)
    check("B1m 전 상품 표에 좋아요 열이 있고 정렬된다 (플랫폼이 2개면 사이트별로)",
          re.search(r'<th class="col-num" data-sort="num">(?:<span[^>]*>)?[^<]*좋아요', page) is not None)

    code, log = run("build_report.py", fx("musinsa-market-scan.json"),
                    "--validation", out("v-scan.json"),
                    "--out", out("scan.html"))
    check("B2 시장 전수조사 리포트 생성", code == 0, "exit=%d\n%s" % (code, log[-400:]))
    page = read(out("scan.html"))
    check("B2b 리뷰 본문을 안 쓴다고 명시한다",
          "리뷰 본문은 수집하지 않는다" in page and "평점·후기 수만" in page)
    check("B2c 속성 분포 차트가 있다", "핏 분포" in page)
    check("B2d 판매가 분포 차트가 있다", "판매가 분포" in page)
    check("B2e 상품 평점 분포 차트가 있다",
          "상품 평점 분포" in page and "평점 미노출 상품" in page)
    check("B2f 후기 수 가중 평균 평점이 KPI에 뜬다",
          "평균 평점" in page and "후기 수 가중" in page)
    check("B2g 전수조사 표에도 이미지 열이 있다 (v5 결정으로 뒤집힘)",
          '<th class="col-img"' in page and 'class="thumb' in page
          and 'loading="lazy"' in page)
    check("B2i 핏은 정렬 열이 아니라 다중 선택 칩이다",
          'data-sort="text">핏' not in page
          and '<span class="facet-label">핏</span>' in page)
    check("B2h 후기 0건과 평점 미노출이 구분된다",
          'data-k="0">0<' in page and "미노출" in page)

    code, _ = run("build_report.py", snap, "--out", out("rank-one.html"))
    page = read(out("rank-one.html"))
    check("B4 스냅샷 1개면 변화 분석을 하지 않는다",
          code == 0 and "변화 분석을 하지 않았다" in page)
    check("B4b 실시간 지표(보는 중/구매 중) 열이 붙는다",
          "보는 중" in page and "구매 중" in page)

    code, log = run("validate_data.py", fx("musinsa-brand-linesheet-partial.json"),
                    "--json", out("v-partial.json"))
    code, _ = run("build_report.py", fx("musinsa-brand-linesheet-partial.json"),
                  "--validation", out("v-partial.json"), "--out", out("partial.html"))
    check("B5 부분 수집은 리포트 상단에 경고가 뜬다",
          code == 0 and "부분 수집 데이터다" in read(out("partial.html")))

    run("validate_data.py", fx("musinsa-brand-linesheet-broken.json"),
        "--json", out("v-broken.json"))
    code, _ = run("build_report.py", fx("musinsa-brand-linesheet-broken.json"),
                  "--validation", out("v-broken.json"), "--out", out("broken.html"))
    check("B6 결측 과다는 구조 변경 배너로 드러난다",
          code == 0 and "사이트 구조 변경 의심" in read(out("broken.html")))

    code, log = run("build_report.py", fx("musinsa-brand-linesheet-good.json"),
                    fx("musinsa-market-scan.json"), "--out", out("mixed.html"))
    check("B7 서로 다른 story를 섞으면 거부한다", code == 2 and "섞을 수 없다" in log,
          "exit=%d\n%s" % (code, log[-300:]))

    code, log = run("build_report.py", "--emit-template", "--out", out("template.html"))
    template = read(out("template.html")) if code == 0 else ""
    check("B8 폴백 템플릿을 뽑을 수 있다",
          code == 0 and "리포트 구조 템플릿" in template, "exit=%d\n%s" % (code, log[-300:]))
    check("B8b 템플릿이 노출값 원칙과 미노출 규칙을 안내한다",
          "노출된 평점" in template and "미노출" in template and "approx" in template)
    shipped = os.path.join(ROOT, "skills", "commerce-intel", "assets", "report-template.html")
    check("B8c 배포된 템플릿이 생성기와 어긋나지 않는다",
          os.path.exists(shipped) and read(shipped) == template,
          "assets/report-template.html이 낡았다. "
          "build_report.py --emit-template --out skills/commerce-intel/assets/report-template.html 로 다시 뽑을 것")

    code, _ = run("build_report.py", "--out", out("noinput.html"))
    check("B9 입력 없이 리포트를 만들려 하면 거부한다", code == 2, "exit=%d" % code)

    # E-OUT-1: 사이트 텍스트에 스크립트가 섞여 와도 실행되지 않아야 한다
    linesheet_page = read(out("linesheet.html"))
    check("B10 사이트 텍스트의 HTML이 이스케이프된다",
          "&lt;script&gt;" in linesheet_page
          and linesheet_page.count("<script>") == 1,  # 리포트 자체 JS 하나뿐이어야 한다
          "raw <script> %d개" % linesheet_page.count("<script>"))

    print("\n[2.5] group_variants.py — 색상 변형 묶기")

    scan_copy = out("scan-group.json")
    shutil.copy(fx("musinsa-market-scan.json"), scan_copy)
    plan_path = out("plan.json")
    code, log = run("group_variants.py", scan_copy, "--attr", "핏", "--plan", plan_path)
    plan = json.loads(read(plan_path)) if code == 0 else {}
    ps = plan.get("summary", {})
    check("G1 계획을 뽑는다 — 그룹 수 < 상품 수",
          code == 0 and 0 < ps.get("groups", 0) < ps.get("items", 0),
          "exit=%d %s" % (code, json.dumps(ps, ensure_ascii=False)))
    check("G1b 판단 횟수가 미분류 건수보다 적다 (절감이 실제로 생긴다)",
          ps.get("groups_needing_decision", 0) < ps.get("unknown_items", 0)
          and ps.get("saved_ratio_pct", 0) > 0, json.dumps(ps, ensure_ascii=False))
    check("G1c 형제가 이미 분류된 그룹은 판단 없이 전파 대상이 된다",
          ps.get("propagatable_items", 0) >= 1, str(ps.get("propagatable_items")))
    check("G1d 한 그룹에 값이 섞이면 충돌로 표시한다",
          ps.get("conflict_groups", 0) >= 1, str(ps.get("conflict_groups")))
    before = json.loads(read(scan_copy))
    check("G1e --plan은 수집 JSON을 건드리지 않는다",
          before == json.loads(read(fx("musinsa-market-scan.json"))))

    groups = plan.get("groups", [])
    color_group = [g for g in groups if "벨티드" in (g.get("base") or "")]
    check("G2 색상만 다른 상품이 한 그룹으로 묶인다 (BLACK·IVORY·(Charcoal))",
          len(color_group) == 1 and color_group[0]["size"] == 3,
          json.dumps(color_group, ensure_ascii=False)[:200])

    # 대표 1건을 정하면 구성원 전체가 채워진다
    for g in groups:
        if "벨티드" in (g.get("base") or ""):
            g["value"] = "와이드"
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False)
    code, log = run("group_variants.py", scan_copy, "--attr", "핏", "--apply", plan_path)
    after = json.loads(read(scan_copy))
    filled = [i for i in after["items"] if "벨티드" in (i.get("name") or "")]
    check("G3 대표 판단이 구성원 전체에 전파된다",
          code == 0 and len(filled) == 3
          and all((i.get("attributes") or {}).get("핏") == "와이드" for i in filled),
          "exit=%d %s" % (code, [(i["name"], i["attributes"]) for i in filled]))
    check("G3b 전파로 채운 것은 근거를 group으로 남긴다 (직접 본 것과 구분)",
          any(i.get("attributes_basis") == "group" for i in filled),
          str([i.get("attributes_basis") for i in filled]))
    check("G3c 판단이 안 된 그룹은 unknown으로 남는다 (찍지 않는다)",
          any((i.get("attributes") or {}).get("핏") == "unknown" for i in after["items"]))
    check("G3d 전파 사실을 meta.notes에 남긴다",
          any("group_variants" in str(n) for n in after["meta"]["notes"]))

    print("\n[3] diff_snapshots.py — 기간 비교")

    snap_dir = fx("snapshots")
    code, log = run("diff_snapshots.py", snap_dir, "--from", "2026-03-01", "--to", "2026-03-05",
                    "--out", out("diff-short.json"))
    check("D1 스냅샷 1개면 비교 대상 부족(1)", code == 1, "exit=%d\n%s" % (code, log[-300:]))
    check("D1b 빈 diff를 만들지 않는다", not os.path.exists(out("diff-short.json")))

    code, log = run("diff_snapshots.py", snap_dir, "--from", "2026-03-01", "--to", "2026-03-31",
                    "--site", "musinsa", "--target", "바지", "--out", out("diff.json"))
    check("D2 기간 diff 생성(0)", code == 0, "exit=%d\n%s" % (code, log[-400:]))
    diff = json.loads(read(out("diff.json")))
    s = diff["summary"]
    check("D2b 기간 밖 스냅샷은 제외된다", s["snapshot_count"] == 3, str(s["snapshot_count"]))
    check("D2c 신규 진입 2건", s["entered"] == 2, str(s["entered"]))
    check("D2d 이탈 1건", s["exited"] == 1, str(s["exited"]))
    check("D2e 급상승 1건 잡힘", s["big_risers"] == 1, str(s["big_risers"]))
    check("D2f 급하락 1건 잡힘", s["big_fallers"] == 1, str(s["big_fallers"]))
    # 픽스처에 할인 시작이 2건 있다: 전 시점 상주 상품 1건 + 순위권 재진입 상품 1건.
    # 구 규칙(연속 쌍)은 재진입 쪽을 못 잡아 1건만 보고했다.
    check("D2g 할인 시작 2건 잡힘 (상주 1 + 재진입 1)", s["discount_started"] == 2,
          str(s["discount_started"]))
    # v5: 순위권을 나갔다 돌아온 상품의 변화도 잡아야 한다 (구 규칙은 0건으로 보고했다)
    changes = diff["price_changes"]
    gapped = [c for c in changes if not c["exact_at"]]
    check("D2j 순위권을 나갔다 돌아온 상품의 가격 변화를 잡는다",
          len(gapped) >= 1, "결석 구간에 걸친 사건 %d건 / 전체 %d건" % (len(gapped), len(changes)))
    check("D2k 사건마다 관측 창을 기록한다",
          all({"from_at", "to_at", "gap_snapshots", "exact_at"} <= set(c) for c in changes)
          and any(c["exact_at"] for c in changes)
          and s["price_change_exact"] == len([c for c in changes if c["exact_at"]]),
          json.dumps(changes[0], ensure_ascii=False)[:200] if changes else "사건 0건")
    top = diff["movers"][0]
    check("D2h 최대 변동이 25위→1위", top["rank_first"] == 25 and top["rank_last"] == 1,
          "%s→%s" % (top.get("rank_first"), top.get("rank_last")))

    code, log = run("diff_snapshots.py", snap_dir, "--from", "2026-03-31", "--to", "2026-03-01",
                    "--out", out("diff-bad.json"))
    check("D3 기간이 거꾸로면 입력 오류(2)", code == 2, "exit=%d" % code)

    # E-OUT-9: 아무것도 변하지 않은 기간 — 빈약한 diff라도 정직하게 만든다
    same_dir = os.path.join(WORK, "snap-same")
    os.makedirs(same_dir, exist_ok=True)
    base_snap = json.loads(read(fx("snapshots", "musinsa-ranking-바지-20260301.json")))
    for stamp, name in (("2026-04-01 09:00:00", "a.json"), ("2026-04-02 09:00:00", "b.json")):
        base_snap["meta"]["collected_at"] = stamp
        with open(os.path.join(same_dir, name), "w", encoding="utf-8") as fh:
            json.dump(base_snap, fh, ensure_ascii=False)
    code, log = run("diff_snapshots.py", same_dir, "--out", out("diff-same.json"))
    same = json.loads(read(out("diff-same.json"))) if code == 0 else {}
    s2 = same.get("summary", {})
    check("D6 변화 0건이면 diff에 0으로 집계된다",
          code == 0 and s2.get("entered") == 0 and s2.get("exited") == 0
          and s2.get("price_change_events") == 0, "exit=%d %s" % (code, json.dumps(s2)))
    code, _ = run("build_report.py", out("diff-same.json"), "--out", out("ranking-same.html"))
    same_page = read(out("ranking-same.html"))
    check("D6b 변화 없음이 명시된 리포트가 나온다",
          code == 0 and "감지되지 않았다" in same_page
          and "변화 없음" in same_page and 'class="delta delta-flat"' in same_page)

    check("D2i 급등락 임계값이 랭킹 길이의 10%(최소 3)로 계산된다",
          s["big_move_threshold"] == 3, str(s.get("big_move_threshold")))

    code, log = run("build_report.py", out("diff.json"), "--out", out("ranking.html"))
    check("D4 변화 리포트 생성", code == 0, "exit=%d\n%s" % (code, log[-400:]))
    page = read(out("ranking.html"))
    check("D4b 신규 진입·이탈을 따로 표로 두지 않고 끝점 축으로 흡수한다",
          "<h2>신규 진입</h2>" not in page and "<h2>이탈</h2>" not in page
          and "신규 진입" in page and "이탈" in page
          and '<span class="facet-label">끝점</span>' in page)
    check("D4b2 급상승·급하락 막대를 만들지 않는다 (표 정렬로 대체)",
          "변동 요약" not in page and "급상승 (" not in page)
    check("D4b3 KPI가 등장 상품·교체율이다",
          "등장 상품" in page and "교체율" in page and "인접 스냅샷 평균 신규 유입" in page)
    check("D4c 할인 시작이 표에 뜬다", "할인 시작" in page)
    check("D4c3 순위·가격·할인이 한 표에 있다",
          "상품별 추이" in page and "순위 변동" in page and "가격 변화" in page
          and "할인율 변화" in page and "감지된 변화" in page
          and "<h2>순위 변동</h2>" not in page)
    check("D4c4 구간으로만 아는 사건은 관측 창을 밝힌다",
          "사이" in page and "결석" in page and "시점 확정" in page)
    check("D4c2 스토리3 표에 이미지 열이 있다",
          page.count('<th class="col-img"') >= 2,  # 주 표 + 시점별 원자료
          "col-img 헤더 %d개" % page.count('<th class="col-img"'))
    check("D4d 순위·가격 추이 스파크라인 자리가 행마다 붙는다",
          page.count('data-spark="rank"') >= 3 and page.count('data-spark="price"') >= 3
          and page.count('data-spark="rank"') == page.count('data-spark="price"'))
    check("D4e 계열 데이터를 문서에 한 번만 심는다",
          page.count('<script id="series-data"') == 1)
    check("D4f 추이 차트 조작부가 1~100 계열을 받는다",
          'id="trend-n"' in page and 'min="1" max="100"' in page
          and 'id="trend-mode"' in page and 'id="trend-host"' in page
          and "표의 현재 정렬·필터 순서대로" in page)
    check("D4f2 겹쳐 보기/나란히 보기 전환 기준을 밝힌다",
          "5개까지는 겹쳐" in page and "6개 이상은 나란히" in page
          and "인과를 주장하지 않는다" in page)
    check("D4f3 '전 구간·일부 구간' 라벨을 쓰지 않는다 (랭킹 체류 숫자 + 끝점 배지)",
          "일부 구간" not in page and "전 구간" not in page
          and "랭킹 체류" in page and "기간 중만" in page)
    check("D4g 실시간 지표는 별도 섹션이 아니라 계열 데이터에 실린다",
          "<h2>실시간 지표 추이</h2>" not in page and '"stamps"' in page)

    payload = json.loads(
        re.search(r'<script id="series-data"[^>]*>(.*?)</script>', page, re.S).group(1)
    )
    obs = sum(len(v["p"]) for v in payload["products"].values())
    check("D4h 계열 JSON은 관측된 시점만 담는다 (널로 채우지 않는다)",
          obs > 0 and obs < len(payload["products"]) * len(payload["stamps"]),
          "관측점 %d / 전 칸 %d" % (obs, len(payload["products"]) * len(payload["stamps"])))
    check("D4i 표에 등장 상품 전부가 실린다",
          len(payload["products"]) == page.count('<tr data-f0'),
          "JSON %d / 행 %d" % (len(payload["products"]), page.count('<tr data-f0')))

    sys.path.insert(0, SCRIPTS)
    import build_report as br
    ds = br.downsample_indices(336)
    check("D7 추이 다운샘플 — 48구간 대표만 남는다",
          len(ds) <= br.MAX_TREND_POINTS + 1 and ds[0] == 0 and ds[-1] == 335
          and ds == sorted(set(ds)), "len=%d" % len(ds))
    check("D7b 48개 이하면 전부 그린다", br.downsample_indices(48) == list(range(48)))

    print("\n%s" % ("-" * 56))
    print("통과 %d · 실패 %d" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("\n실패 목록:")
        for name, detail in FAILED:
            print("  - %s" % name)
        return 1
    print("결과물: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
