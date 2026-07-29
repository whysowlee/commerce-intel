#!/usr/bin/env python3
"""commerce-research 스크립트 회귀 테스트.

실제 사이트에 붙지 않고 픽스처로 scripts/의 동작만 검증한다.
사이트를 실제로 도는 트리거/기능 테스트는 docs/TEST-CASES.md에 있다.

usage: python3 tests/run_tests.py
"""

import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "commerce-research", "scripts")
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
    check("B1b 플랫폼별 인기 섹션이 있다", "플랫폼별 인기 상품" in page)
    check("B1b2 품목별 인기 비교가 있다",
          "플랫폼별 품목 인기" in page and "품목별 하트 합" in page and "품목별 후기 합" in page
          and page.count("품목별 하트 합") == 2)  # 무신사·29CM 각각
    check("B1c 무신사·29CM가 모두 나온다", "무신사" in page and "29CM" in page)
    check("B1d 미노출 값을 추정하지 않고 표기한다", "미노출" in page)
    check("B1g 구간 표기 조회수는 문구 그대로 싣는다",
          "회 이상 (최근 1개월)" in page)
    check("B1h 구간값으로는 조회수 순위 차트를 만들지 않는다",
          "조회수 상위" not in page and "좋아요 상위" in page)
    check("B1e 외부 리소스를 부르지 않는다",
          "http://" not in page.replace("http://www.w3.org", "")
          or "cdn" not in page.lower(),
          "외부 스크립트/스타일 참조 의심")
    check("B1f 표 정렬·거르기가 붙는다", 'class="filter"' in page and "sorted-asc" in page)

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
    check("B2g 전수조사 표에는 이미지 열이 없다",
          '<th class="col-img"' not in page and 'class="thumb' not in page)
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
    shipped = os.path.join(ROOT, "commerce-research", "assets", "report-template.html")
    check("B8c 배포된 템플릿이 생성기와 어긋나지 않는다",
          os.path.exists(shipped) and read(shipped) == template,
          "assets/report-template.html이 낡았다. "
          "build_report.py --emit-template --out commerce-research/assets/report-template.html 로 다시 뽑을 것")

    code, _ = run("build_report.py", "--out", out("noinput.html"))
    check("B9 입력 없이 리포트를 만들려 하면 거부한다", code == 2, "exit=%d" % code)

    # E-OUT-1: 사이트 텍스트에 스크립트가 섞여 와도 실행되지 않아야 한다
    linesheet_page = read(out("linesheet.html"))
    check("B10 사이트 텍스트의 HTML이 이스케이프된다",
          "&lt;script&gt;" in linesheet_page
          and linesheet_page.count("<script>") == 1,  # 리포트 자체 JS 하나뿐이어야 한다
          "raw <script> %d개" % linesheet_page.count("<script>"))

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
    check("D2g 할인 시작 1건 잡힘", s["discount_started"] == 1, str(s["discount_started"]))
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
          code == 0 and "감지되지 않았다" in same_page and "움직인 상품이 없다" in same_page)

    check("D2i 급등락 임계값이 랭킹 길이의 10%(최소 3)로 계산된다",
          s["big_move_threshold"] == 3, str(s.get("big_move_threshold")))

    code, log = run("build_report.py", out("diff.json"), "--out", out("ranking.html"))
    check("D4 변화 리포트 생성", code == 0, "exit=%d\n%s" % (code, log[-400:]))
    page = read(out("ranking.html"))
    check("D4b 신규 진입/이탈 섹션", "신규 진입" in page and "이탈" in page)
    check("D4c 할인 시작이 표에 뜬다", "할인 시작" in page)
    check("D4c2 스토리3 표마다 이미지 열이 있다",
          page.count('<th class="col-img"') >= 4,  # 신규/이탈/순위변동/가격변화
          "col-img 헤더 %d개" % page.count('<th class="col-img"'))
    check("D4d 순위 추이 선그래프", "순위 추이" in page and 'class="line"' in page)
    check("D4e 결측 구간이 있어도 그린다", "series-label" in page)
    check("D4g 실시간 지표 추이 섹션이 뜬다",
          "실시간 지표 추이" in page and "보는 중 인원 추이" in page)

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
