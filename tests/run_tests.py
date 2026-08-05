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

    print("\n[2] (HTML 리포트 검증 — D27로 폐기, 데이터 규칙은 test_intel_db [13]으로 이식)")

    # ── B 계열(HTML 리포트 검증)은 2026-08-03에 삭제됐다 ────────────────────
    # D27로 HTML 산출을 폐기하고 생성기 4종을 실제로 지우면서 함께 들어냈다.
    # 그 안에 있던 **데이터 규칙**(합집합 매칭·입점 구분·미노출과 0의 구분·
    # 매칭분만 비교·단일 시점 시계열 금지)은 형식과 무관하므로
    # tests/test_intel_db.py [13]으로 이식했다 — 13건이 HTML 없이 같은 규칙을 지킨다.
    # 나머지는 칩·정렬·섹션 유무 같은 레이아웃 검증이라 폐기 대상이었다.

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

    check("D2i 급등락 임계값이 랭킹 길이의 10%(최소 3)로 계산된다",
          s["big_move_threshold"] == 3, str(s.get("big_move_threshold")))

    # D4~D6의 리포트 표시 검증(신규 진입·이탈 축·급등락 막대·할인 시작 열 등)은
    # HTML 레이아웃이라 D27 폐기와 함께 들어냈다. **diff 계산 자체**는 위 D1~D6이
    # diff.json으로 검증한다 — 끝점·교체율·급등락 임계는 형식과 무관한 데이터 규칙이다.

    # D7 — 시계열 다운샘플은 D24 규범이다. build_report(폐기)에 있던 함수를
    # intel_data로 옮겼고, 규칙은 그대로다: 균등 구간 대표만·평균 금지·첫끝 포함.
    sys.path.insert(0, SCRIPTS)
    import intel_data as idat
    ds = idat.downsample_indices(336)
    check("D7 추이 다운샘플 — 48구간 대표만 남는다",
          len(ds) <= idat.MAX_TREND_POINTS + 1 and ds[0] == 0 and ds[-1] == 335
          and ds == sorted(set(ds)), "len=%d" % len(ds))
    check("D7b 48개 이하면 전부 그린다", idat.downsample_indices(48) == list(range(48)))

    print("\n[E] 배포 가능성 — 스킬 프론트매터 (D57)")

    # 프론트매터가 깨지면 팀원 환경에 **에러 없이 설치되지 않는다**. 2026-08-04에 7종이
    # 그렇게 빠졌다. 배포 전에 여기서 잡는다.
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import validate_skills

    broken = {d.name: errs for d in sorted((validate_skills.ROOT / "skills").iterdir())
              if d.is_dir() and (errs := validate_skills.check(d))}
    check("E1 모든 SKILL.md 프론트매터가 파싱된다", not broken, repr(broken)[:400])

    fm_bad = 'name: x\ndescription: 어쩌고 예: "저쩌고"\n'
    fm_ok = 'name: x\ndescription: >-\n  어쩌고 예: "저쩌고"\nmetadata:\n  cycle: "랭킹: 30분"\n'
    check("E2 인용 없는 `: `를 잡는다", validate_skills.lint_plain_scalars(fm_bad))
    check("E3 블록 스칼라·인용 값은 오탐하지 않는다",
          not validate_skills.lint_plain_scalars(fm_ok),
          repr(validate_skills.lint_plain_scalars(fm_ok)))

    print("\n[F] 정본 부분 배포 — seed 범위 (D58)")

    # 정본에서 **허용 범위만** 떼는 도구다. 새면 남의 데이터가 팀원에게 나간다 —
    # 픽스처 DB(범위 안 2건 · 범위 밖 2건)로 범위 판정과 검산을 둘 다 확인한다.
    import sqlite3
    sys.path.insert(0, SCRIPTS)
    import intel_db

    src_p, out_p = out("seed-src.db"), out("seed-out.db")
    c = intel_db.connect(src_p)          # 정본과 같은 경로로 스키마를 짓는다
    c.execute("INSERT INTO runs (run_id, site, story, target, collected_at) "
              "VALUES ('r-in','musinsa','market-scan','데님팬츠(여성·브랜드랭킹 상위30)','2026-08-04 00:00:00')")
    c.execute("INSERT INTO runs (run_id, site, story, target, collected_at) "
              "VALUES ('r-out','musinsa','market-scan','데님팬츠(남성)','2026-08-04 00:00:00')")
    for pid, brand in (("p1", "2000 Archives"),      # 브랜드 범위 (표기 변형)
                       ("p2", "남의브랜드"),           # 런 범위로만 들어와야 한다
                       ("p3", "또다른브랜드"),         # 범위 밖 — 나가면 안 된다
                       ("p4", "2000아카이브스")):
        c.execute("INSERT INTO products (site, product_id, name, brand, category) "
                  "VALUES ('musinsa',?,?,?,'데님팬츠')", (pid, "n-" + pid, brand))
    c.execute("INSERT INTO observations (site, product_id, observed_at, context, run_id) "
              "VALUES ('musinsa','p2','2026-08-04 00:00:00','market','r-in')")
    c.execute("INSERT INTO observations (site, product_id, observed_at, context, run_id) "
              "VALUES ('musinsa','p3','2026-08-04 00:00:00','market','r-out')")
    c.execute("INSERT INTO insights (run_stamp, target, context, verdict, idx, claim) "
              "VALUES ('s','자사','brand:2000아카이브스','strong',1,'주장')")
    c.execute("INSERT INTO insights (run_stamp, target, context, verdict, idx, claim) "
              "VALUES ('s','모자','ranking:모자','strong',1,'남의 주장')")
    c.commit()
    c.close()

    rc = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "make_seed_db.py"),
                         "--src", src_p, "--out", out_p], capture_output=True, text=True)
    check("F1 seed 생성이 성공하고 검산을 통과한다",
          rc.returncode == 0 and "검산 통과" in rc.stdout, rc.stdout + rc.stderr)

    got = sqlite3.connect(out_p)
    pids = {r[0] for r in got.execute("SELECT product_id FROM products")}
    check("F2 브랜드 범위는 표기 변형까지 들어온다", {"p1", "p4"} <= pids, str(pids))
    check("F3 허용 런에서 관측된 상품은 브랜드가 달라도 들어온다", "p2" in pids, str(pids))
    check("F4 범위 밖 상품은 나가지 않는다", "p3" not in pids, str(pids))
    obs_pids = {r[0] for r in got.execute("SELECT DISTINCT product_id FROM observations")}
    check("F5 범위 밖 관측도 따라오지 않는다", "p3" not in obs_pids, str(obs_pids))
    ctxs = {r[0] for r in got.execute("SELECT DISTINCT context FROM insights")}
    check("F6 인사이트는 배포 범위 문맥만 간다", ctxs == {"brand:2000아카이브스"}, str(ctxs))
    check("F7 시트 미러 상태(sync_state)는 배포하지 않는다",
          got.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0] == 0)
    got.close()

    # 검산이 실제로 잡는지 — seed에 범위 밖 상품을 손으로 넣고 --check를 돌린다
    tainted = sqlite3.connect(out_p)
    tainted.execute("INSERT INTO products (site, product_id, name, brand) "
                    "VALUES ('musinsa','p9','n-p9','몰래끼운브랜드')")
    tainted.commit()
    tainted.close()
    rc2 = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "make_seed_db.py"),
                          "--src", src_p, "--out", out_p, "--check"],
                         capture_output=True, text=True)
    check("F8 범위 밖 행이 섞이면 검산이 실패한다(exit 1)",
          rc2.returncode == 1 and "범위 밖 상품" in (rc2.stdout + rc2.stderr),
          rc2.stdout + rc2.stderr)

    print("\n[M] merge — 새로 만든 DB(v2)에 합쳐지는가 (D59)")

    # 새 DB는 항상 v2로 지어지고, v2에서 옛 이름은 뷰다(D45). 그래서
    # ① 뷰에는 업서트를 못 쓰고 ② 관측은 상품·런보다 **나중에** 들어가야 하며
    # ③ 속성 판정은 애초에 merge 대상에서 빠져 있었다. 팀원이 seed를 합치는
    # 바로 그 첫 명령이 여기에 전부 걸렸다 — 실측으로 잡은 것을 고정한다.
    msrc, mdst = out("merge-src.db"), out("merge-dst.db")
    s = intel_db.connect(msrc)
    s.execute("INSERT INTO runs (run_id, site, story, target, collected_at) "
              "VALUES ('r1','musinsa','market-scan','데님팬츠(여성)','2026-08-04 00:00:00')")
    s.execute("INSERT INTO products (site, product_id, name, brand, category) "
              "VALUES ('musinsa','x1','이름','브랜드','데님팬츠')")
    s.execute("INSERT INTO observations (site, product_id, observed_at, context, "
              "price_sale, run_id) VALUES ('musinsa','x1','2026-08-04 00:00:00','market',10000,'r1')")
    s.execute("INSERT INTO product_attributes (site, product_id, attr_name, value, basis, "
              "decided_at) VALUES ('musinsa','x1','핏','와이드','image','2026-08-04 00:00:00')")
    s.commit()
    s.close()

    def merge_counts():
        rc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "intel_db.py"),
                             "--db", mdst, "merge", msrc], capture_output=True, text=True)
        d = intel_db.connect(mdst)
        got = {t: d.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
               for t in ("products", "observations", "product_attributes", "runs")}
        d.close()
        return rc, got

    rc, got = merge_counts()
    check("M1 merge가 예외 없이 끝난다 (뷰에 업서트를 던지지 않는다)",
          rc.returncode == 0 and "cannot UPSERT" not in (rc.stdout + rc.stderr),
          rc.stdout + rc.stderr)
    check("M2 관측이 실제로 들어간다 — 상품·런보다 나중에 넣는다",
          got["observations"] == 1, str(got))
    check("M3 속성 판정도 함께 온다 (전에는 merge 대상이 아니었다)",
          got["product_attributes"] == 1, str(got))
    rc2, got2 = merge_counts()
    check("M4 두 번 합쳐도 부풀지 않는다", got2 == got, "%s → %s" % (got, got2))

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
