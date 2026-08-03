#!/usr/bin/env python3
"""intel_db.py · build_analysis_report.py 회귀 테스트.

    python3 tests/test_intel_db.py

기존 run_tests.py와 같은 원칙 — 픽스처만 쓰고 사이트에 붙지 않는다.
검증 대상: 적재 멱등성(중복 스킵) · 재사용 판정(check/reuse-attrs TTL) ·
가격 변경 사건 검출 · 대시보드 산출물 구조.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "commerce-intel" / "scripts"
FIX = ROOT / "tests" / ".work" / "fixtures"

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def run(args, cwd, env_db):
    env = dict(os.environ, INTEL_DB=env_db)
    return subprocess.run([sys.executable, *args], cwd=cwd, env=env,
                          capture_output=True, text=True)


class FakeSheet:
    """gspread 스프레드시트 흉내 — 네트워크 없이 runs 탭 조회를 검증한다."""

    def __init__(self, tabs):
        self.tabs = tabs          # {탭이름: [[헤더...], [행...]]}

    def worksheet(self, title):
        if title not in self.tabs:
            raise Exception(f"no such worksheet: {title}")
        sheet = self

        class WS:
            def get_all_values(self):
                return sheet.tabs[title]
        return WS()


def with_fake_sheet(sh, err=None, raises=None):
    """team_coverage가 함수 안에서 import하는 sync_sheets를 가짜로 갈아끼운다."""
    import types
    mod = types.ModuleType("sync_sheets")
    mod.open_spreadsheet = lambda config, creds: (sh, err)

    def fetch_tab(spreadsheet, title):
        if raises:
            raise Exception(raises)
        if title not in spreadsheet.tabs:
            return None          # 진짜 fetch_tab과 같은 규약: 없는 탭은 None
        return _as_dicts(spreadsheet.tabs[title])
    mod.fetch_tab = fetch_tab
    sys.modules["sync_sheets"] = mod


def _as_dicts(values):
    return [dict(zip(values[0], r)) for r in values[1:] if any(c.strip() for c in r)]


RUNS_HEADER = ["run_id", "site", "story", "target", "collected_at", "item_count"]


def team_coverage_tests(work, db, ctx):
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    intel_db = importlib.import_module("intel_db")
    conn = intel_db.connect(db)
    target = ctx.split(":", 1)[1]
    now = datetime.now()
    recent = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    old = (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

    def coverage(rows, **kw):
        with_fake_sheet(FakeSheet({"runs": [RUNS_HEADER, *rows]}), **kw)
        return intel_db.team_coverage(conn, "musinsa", ctx, 30, "cfg", "creds")

    # 팀원이 방금 수집 → 중복이다
    c = coverage([["teammate-1", "musinsa", "brand-linesheet", target, recent, "440"]])
    check("팀원의 최근 수집을 잡아낸다", c["consulted"] and c["fresh"], c)
    check("근거로 수집 시각을 돌려준다", c["last_collected_at"] == recent, c)

    # 오래된 팀 수집분은 스킵 사유가 아니다 — 재수집이 정당하다
    c = coverage([["teammate-1", "musinsa", "brand-linesheet", target, old, "440"]])
    check("주기 밖 팀 수집분은 fresh=false", c["consulted"] and not c["fresh"], c)
    check("이력 자체는 보고한다", c["run_count"] == 1, c)

    # 다른 site·다른 target은 남의 일이다
    c = coverage([["t2", "29cm", "brand-linesheet", target, recent, "1"],
                  ["t3", "musinsa", "brand-linesheet", "다른브랜드", recent, "1"],
                  ["t4", "musinsa", "ranking-snapshot", target, recent, "1"]])
    check("site·story·target이 다르면 무시한다", c["run_count"] == 0 and not c["fresh"], c)

    # 내가 올린 run은 팀 수집분이 아니다 (로컬 판정이 이미 봤다)
    mine = conn.execute("SELECT run_id FROM runs LIMIT 1").fetchone()
    if mine:
        c = coverage([[mine[0], "musinsa", "brand-linesheet", target, recent, "440"]])
        check("내 run_id는 팀 수집분에서 제외된다", c["run_count"] == 0 and not c["fresh"], c)

    # 실패 3종 — 전부 consulted=False로 떨어지고 예외를 던지지 않는다
    c = coverage([], err=("서비스 계정 키가 없다", 3))
    check("시트 열기 실패는 consulted=false", not c["consulted"] and c.get("error"), c)
    with_fake_sheet(FakeSheet({}))            # runs 탭 자체가 없다
    c = intel_db.team_coverage(conn, "musinsa", ctx, 30, "cfg", "creds")
    check("runs 탭 없음은 consulted=false", not c["consulted"] and "runs 탭이 없다" in c["error"], c)
    c = coverage([], raises="quota exceeded")
    check("조회 예외는 consulted=false로 흡수된다", not c["consulted"] and "quota" in c["error"], c)

    # 폴백: 팀 조회가 실패해도 수집은 진행돼야 한다 (exit 1 = 수집 필요)
    r = run([SCRIPTS / "intel_db.py", "check", "--site", "musinsa", "--context",
             "brand:없는브랜드", "--cycle-minutes", "30", "--team",
             "--config", str(work / "nope.json"), "--creds", str(work / "nope.json")],
            work, db)
    check("시트 미설정이어도 수집 판정은 나온다 (exit 1)", r.returncode == 1, r.stdout + r.stderr)
    check("팀 미확인이 판정 근거에 남는다",
          "팀 커버리지 미확인" in r.stdout, r.stdout)
    sys.modules.pop("sync_sheets", None)


def main():
    work = Path(tempfile.mkdtemp(prefix="intel-db-test-"))
    db = str(work / "intel.db")
    fixture = FIX / "musinsa-brand-linesheet-good.json"

    print("[1] 적재 멱등성")
    r1 = run([SCRIPTS / "intel_db.py", "load", fixture], work, db)
    check("첫 적재 성공", r1.returncode == 0, r1.stderr)
    check("첫 적재는 전건 신규", "중복 0건" in r1.stdout, r1.stdout)
    r2 = run([SCRIPTS / "intel_db.py", "load", fixture], work, db)
    check("같은 파일 재적재는 관측 0건 신규", "관측 0건 적재" in r2.stdout, r2.stdout)
    n_items = len(json.loads(fixture.read_text())["items"])
    check(f"재적재 전건({n_items})이 중복 스킵", f"중복 {n_items}건" in r2.stdout, r2.stdout)

    print("[2] 시변 스킵 판정 (check)")
    ctx = "brand:" + json.loads(fixture.read_text())["meta"]["target"]
    # 픽스처 collected_at은 과거다 → 어떤 주기로도 신선하지 않아야 한다
    r = run([SCRIPTS / "intel_db.py", "check", "--site", "musinsa",
             "--context", ctx, "--cycle-minutes", "30"], work, db)
    check("과거 관측은 skip=false (exit 1)", r.returncode == 1, r.stdout)
    # 방금 관측을 심으면 skip=true여야 한다
    fresh = json.loads(fixture.read_text())
    fresh["meta"]["collected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fresh_f = work / "fresh.json"
    fresh_f.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
    run([SCRIPTS / "intel_db.py", "load", fresh_f], work, db)
    r = run([SCRIPTS / "intel_db.py", "check", "--site", "musinsa",
             "--context", ctx, "--cycle-minutes", "30"], work, db)
    check("방금 관측은 skip=true (exit 0)", r.returncode == 0, r.stdout)
    out = json.loads(r.stdout)
    check("판정 JSON에 근거가 있다", out.get("last_observed_at") and "cycle_minutes" in out)

    print("[3] 정적 속성 재사용 (reuse-attrs, TTL)")
    scan = json.loads((FIX / "musinsa-market-scan.json").read_text())
    # 1차: 분류가 있는 채로 적재 → DB에 속성이 남는다
    run([SCRIPTS / "intel_db.py", "load", FIX / "musinsa-market-scan.json"], work, db)
    # 2차 수집이 미분류로 왔다고 가정
    blank = json.loads((FIX / "musinsa-market-scan.json").read_text())
    had_attrs = 0
    for it in blank["items"]:
        if it.get("attributes") and any(v not in (None, "", "unknown")
                                        for v in it["attributes"].values()):
            had_attrs += 1
            it["attributes"] = {"핏": "unknown"}
            it["attributes_basis"] = "unknown"
    blank_f = work / "blank.json"
    blank_f.write_text(json.dumps(blank, ensure_ascii=False), encoding="utf-8")
    r = run([SCRIPTS / "intel_db.py", "reuse-attrs", str(blank_f),
             "--out", str(work / "reused.json")], work, db)
    check("reuse-attrs 실행 성공", r.returncode == 0, r.stderr)
    reused = json.loads((work / "reused.json").read_text())
    n_filled = sum(1 for it in reused["items"]
                   if it.get("attributes") and any(v not in (None, "", "unknown")
                                                   for v in it["attributes"].values()))
    check(f"미분류 {had_attrs}건이 DB에서 채워졌다", n_filled == had_attrs,
          f"filled={n_filled}")
    check("재사용 사실이 notes에 남는다",
          any("DB 재사용" in n for n in reused["meta"]["notes"]))
    r = run([SCRIPTS / "intel_db.py", "reuse-attrs", str(blank_f),
             "--out", str(work / "expired.json"), "--ttl-days", "0"], work, db)
    expired = json.loads((work / "expired.json").read_text())
    n_exp = sum(1 for it in expired["items"]
                if it.get("attributes") and any(v not in (None, "", "unknown")
                                                for v in it["attributes"].values()))
    check("TTL 0일이면 만료로 채우지 않는다", n_exp == 0, f"filled={n_exp}")

    print("[4] 가격 변경 사건 + 대시보드 산출")
    run([SCRIPTS / "intel_db.py", "import-snapshots", FIX / "snapshots"], work, db)
    out_html = work / "analysis.html"
    r = run([SCRIPTS / "build_analysis_report.py", "--db", db,
             "--out", str(out_html)], work, db)
    check("대시보드 생성 성공", r.returncode == 0, r.stderr)
    check("가격 변경 사건이 검출된다", "가격 변경 사건 0건" not in r.stdout, r.stdout)
    html = out_html.read_text(encoding="utf-8")
    for token, why in [
        ("상관은 인과가 아니다", "정직성 문구"),
        ("심슨의 역설", "세그먼트 경고"),
        ("생존편향", "생존편향 경고"),
        ('type="application/json"', "데이터 1회 임베드"),
        ("prefers-color-scheme", "다크 모드"),
    ]:
        check(f"산출물에 {why}", token in html)
    check("외부 리소스 0 (http 로드 없음)",
          'src="http' not in html and 'href="http' not in html.replace('href="https://www.musinsa', 'X'))
    check("구간 표기 필드(view_count)는 축 후보에 없다", '"view_count"' not in html.split("AXES=")[1][:400])

    print("[5] 옵션(variants) 적재")
    vraw = {
        "meta": {"site": "musinsa", "story": "brand-linesheet", "target": "옵션테스트",
                 "collected_at": "2026-07-31 12:00:00", "item_count": 1,
                 "source_total": 1, "incomplete": False, "notes": []},
        "items": [{
            "product_id": "V1", "name": "옵션 상품", "url": "https://x", "image_url": "https://x",
            "brand": "b", "category": "c", "price_original": 10000, "price_sale": 9000,
            "discount_rate": 10, "sold_out": False,
            "variants": [
                {"option_id": "V1-BK-M", "option_name": "BLACK / M", "color": "BLACK",
                 "size": "M", "sold_out": False, "stock_qty": 7, "stock_display": None,
                 "stock_basis": "probe_read"},
                {"option_id": "V1-BK-L", "option_name": "BLACK / L", "color": "BLACK",
                 "size": "L", "sold_out": True, "stock_qty": 0, "stock_display": None,
                 "stock_basis": "option_api"},
            ],
        }],
    }
    vf = work / "variants.json"
    vf.write_text(json.dumps(vraw, ensure_ascii=False), encoding="utf-8")
    r = run([SCRIPTS / "intel_db.py", "load", vf], work, db)
    check("옵션 관측 2건 적재", "옵션 관측 2건" in r.stdout, r.stdout)
    r = run([SCRIPTS / "intel_db.py", "load", vf], work, db)
    check("옵션 재적재는 멱등(관측 0건)", "옵션 관측 2건" not in r.stdout, r.stdout)
    r = run([SCRIPTS / "intel_db.py", "export", "--table", "variants",
             "--format", "json"], work, db)
    vs = json.loads(r.stdout)
    check("variants 정적 2행 (재적재에도 불변)", len(vs) == 2, f"{len(vs)}행")
    check("stock_basis가 관측에 남는다", "probe_read" in run(
        [SCRIPTS / "intel_db.py", "export", "--table", "variant_observations",
         "--format", "json"], work, db).stdout)

    print("[6] 파생 프록시 (proxy-load → 대시보드 주입)")
    fixture_data = json.loads(fixture.read_text())
    subj = fixture_data["items"][0]
    pj = {
        "proxy": {"proxy_name": "name_lang", "question": "상품명이 영문인가 한국어인가",
                  "material": "name", "value_space": ["영문", "한국어", "혼합", "unknown"],
                  "method": "rule"},
        "judgments": [
            {"site": "musinsa", "product_id": subj["product_id"],
             "fingerprint": subj["name"], "value": "한국어", "basis": "한글 비율 우세"},
            {"site": "musinsa", "product_id": subj["product_id"],
             "fingerprint": subj["name"], "value": "화성어", "basis": "값 공간 밖"},
        ],
    }
    pf = work / "proxy.json"
    pf.write_text(json.dumps(pj, ensure_ascii=False), encoding="utf-8")
    r = run([SCRIPTS / "intel_db.py", "proxy-load", pf], work, db)
    check("판정 적재 + 값 공간 밖 거부", "판정 1건 적재" in r.stdout and "거부" in r.stdout, r.stdout)
    r = run([SCRIPTS / "intel_db.py", "proxy-load", pf], work, db)
    check("proxy-load 멱등(중복 스킵)", "판정 0건 적재" in r.stdout, r.stdout)
    px_html = work / "px.html"
    r = run([SCRIPTS / "build_analysis_report.py", "--db", db, "--out", str(px_html)], work, db)
    check("프록시 포함 대시보드 생성", r.returncode == 0, r.stderr)
    html = px_html.read_text(encoding="utf-8")
    check("px_ 열이 주입된다", '"px_name_lang"' in html)
    check("AI 판정 표기·미판정 구분이 있다", "AI 판정" in html and "(미판정)" in html)

    print("[7] AI 생성 모드 도구 (--emit-json · 린터)")
    ej = work / "analysis.json"
    r = run([SCRIPTS / "build_analysis_report.py", "--db", db, "--emit-json",
             "--out", str(ej)], work, db)
    check("--emit-json 산출", r.returncode == 0 and ej.exists(), r.stderr)
    d = json.loads(ej.read_text(encoding="utf-8"))
    check("JSON에 meta·items·price_events", set(d) >= {"meta", "items", "price_events"})
    r = run([SCRIPTS / "lint_analysis_html.py", str(px_html)], work, db)
    check("생성 대시보드는 린터 통과", r.returncode == 0, r.stdout)
    bad = work / "bad.html"
    bad.write_text('<html><script src="https://cdn.x/c.js"></script></html>', encoding="utf-8")
    r = run([SCRIPTS / "lint_analysis_html.py", str(bad)], work, db)
    check("결격 HTML은 린터 FAIL", r.returncode == 1, r.stdout)

    print("[8] 표본 계획 (plan_sample)")
    plan_f = work / "plan.json"
    r = run([SCRIPTS / "plan_sample.py", "plan", "--population", "24673",
             "--per-stratum", "32", "--out", str(plan_f)], work, db)
    check("계획 생성", r.returncode == 0, r.stderr)
    plan = json.loads(plan_f.read_text(encoding="utf-8"))
    check("로그 층이 여러 개다", len(plan["strata"]) >= 5, str(len(plan["strata"])))
    idx0 = {i for st in plan["strata"] for i in st["indices"]}
    exp_f = work / "plan2.json"
    r = run([SCRIPTS / "plan_sample.py", "expand", str(plan_f), "--out", str(exp_f)], work, db)
    check("확장 성공", r.returncode == 0, r.stderr)
    plan2 = json.loads(exp_f.read_text(encoding="utf-8"))
    idx1 = {i for st in plan2["strata"] for i in st["indices"]}
    check("확장이 기존 표본을 전부 포함(중첩)", idx0 <= idx1)
    check("확장으로 표본이 늘었다", len(idx1) > len(idx0), f"{len(idx0)}→{len(idx1)}")

    print("[9] 검증기 표본 분기 (meta.sampling)")
    sm = json.loads(fixture.read_text())
    sm["meta"]["sampling"] = {"design": "log-rank-strata", "planned": len(sm["items"])}
    sm["meta"]["source_total"] = 99999   # 모집단 총계 — 표본 모드에선 대조 대상이 아니다
    sm_f = work / "sampled.json"
    sm_f.write_text(json.dumps(sm, ensure_ascii=False), encoding="utf-8")
    r = run([SCRIPTS / "validate_data.py", str(sm_f)], work, db)
    check("표본 모드: 총계 불일치가 경고가 아니다(계획 완주로 판정)",
          "계획" in r.stdout and "오차" not in r.stdout.split("계획")[0][-200:], r.stdout[-500:])

    print("[10] 템플릿 산출 (--emit-template)")
    tpl = work / "tpl.html"
    r = run([SCRIPTS / "build_analysis_report.py", "--emit-template",
             "--out", str(tpl)], work, db)
    check("템플릿 생성 성공", r.returncode == 0, r.stderr)
    check("샘플 데이터임이 화면에 명시된다", "TEMPLATE" in tpl.read_text(encoding="utf-8"))

    print("[11] 팀 커버리지 조회 (check --team, D32)")
    team_coverage_tests(work, db, ctx)

    shutil.rmtree(work, ignore_errors=True)
    print("-" * 56)
    print(f"통과 {passed} · 실패 {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
