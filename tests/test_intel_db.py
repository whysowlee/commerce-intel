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

    print("[7] 템플릿 산출 (--emit-template)")
    tpl = work / "tpl.html"
    r = run([SCRIPTS / "build_analysis_report.py", "--emit-template",
             "--out", str(tpl)], work, db)
    check("템플릿 생성 성공", r.returncode == 0, r.stderr)
    check("샘플 데이터임이 화면에 명시된다", "TEMPLATE" in tpl.read_text(encoding="utf-8"))

    shutil.rmtree(work, ignore_errors=True)
    print("-" * 56)
    print(f"통과 {passed} · 실패 {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
