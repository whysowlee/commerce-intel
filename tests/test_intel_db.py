#!/usr/bin/env python3
"""intel_db.py · 데이터 층·차트 회귀 테스트.

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



def chart_tests():
    """chart.py 렌더 — 엣지 케이스에서 예외 없이 Drawing이 나오는지 (D40).

    이 테스트가 있는 이유: 차트 결함은 **예외 없이 조용히 깨진다**(빈 그림·잘린 라벨).
    실제로 D40 작업에서 축 라벨 겹침·음수 값 라벨 잘림을 코드 통과 후 눈으로 찾았다.
    픽셀까지 검증할 수는 없으니 **"예외 없이 그려지고, 그릴 게 없으면 빈 Drawing"**
    이라는 계약만 고정한다 — 그것만으로 다음 변경의 크래시 회귀는 막힌다.
    """
    sys.path.insert(0, str(SCRIPTS))
    try:
        import chart
    except ImportError:
        # reportlab은 PDF 파이프라인의 필수 의존이지만(package.sh가 배포 전 검사),
        # 이 테스트 파일의 나머지는 그것 없이도 돈다. 없는 환경에서 실패로 만들면
        # DB 회귀를 못 돌리게 되므로 스킵한다 — 배포 게이트는 package.sh가 지킨다.
        print("  SKIP  chart 렌더 — reportlab 미설치 (pip install reportlab)")
        return

    # 정상 입력 — 요소가 실제로 그려진다(빈 Drawing이 아니다)
    d = chart.bar_h([("29CM", 18), ("자사몰", 0)], "할인율(%)")
    check("bar_h 정상 렌더", len(d.contents) > 2, "요소 %d" % len(d.contents))
    # 음수 혼재 — 0선 양쪽으로 그린다(이중차분이 음수로 나온다)
    d = chart.bar_h([("A", 12), ("DiD", -4)], "값")
    check("bar_h 음수 혼재", len(d.contents) > 2)
    # 전부 음수 — span 계산이 0으로 나눠지지 않는지
    d = chart.bar_h([("A", -3), ("B", -9)], "값")
    check("bar_h 전부 음수", len(d.contents) > 2)
    # 전부 같은 값 — span=0 → 나눗셈 폭발 방지
    d = chart.bar_h([("A", 5), ("B", 5)], "값")
    check("bar_h 값이 전부 같음(span=0)", len(d.contents) > 2)
    # 빈 입력·전부 None — 빈 Drawing을 돌려준다(예외 아님)
    check("bar_h 빈 입력", len(chart.bar_h([], "값").contents) == 0)
    check("bar_h 전부 None", len(chart.bar_h([("A", None)], "값").contents) == 0)

    # dist_compare — 표본이 적을 때 사분위 계산이 죽지 않는지
    check("dist_compare 표본 1개는 빈 Drawing",
          len(chart.dist_compare([1], [1, 2, 3], "a", "b", "m").contents) == 0)
    d = chart.dist_compare([1, 2], [3, 4], "a", "b", "판매가")
    check("dist_compare 표본 2개(사분위 미만)", len(d.contents) > 2)
    d = chart.dist_compare(list(range(100)), list(range(50, 150)), "a", "b", "판매가")
    check("dist_compare 정상 렌더", len(d.contents) > 4)
    check("dist_compare None 섞임",
          len(chart.dist_compare([1, None, 3, 5], [2, 4, None, 6], "a", "b", "m").contents) > 2)

    # bins_bar — 구간 1개면 추세를 말할 수 없다(빈 Drawing)
    one = [{"from": 0, "to": 10, "n": 5, "median": 3}]
    check("bins_bar 구간 1개는 빈 Drawing", len(chart.bins_bar(one, "x", "y").contents) == 0)
    two = one + [{"from": 10, "to": None, "n": 8, "median": 9}]
    d = chart.bins_bar(two, "할인 폭(%)", "증분")
    check("bins_bar 정상 + to=None(마지막 구간)", len(d.contents) > 3)
    check("bins_bar median None 제외",
          len(chart.bins_bar(two + [{"from": 90, "to": 100, "n": 1, "median": None}],
                             "x", "y").contents) > 3)

    # missing_bar — EDA nulls 구조 그대로
    nulls = [{"label": "평점", "missing_pct": 100.0}, {"label": "하트", "missing_pct": 0.0}]
    check("missing_bar 정상", len(chart.missing_bar(nulls).contents) > 2)
    check("missing_bar 빈 입력", len(chart.missing_bar([]).contents) == 0)
    check("missing_bar missing_pct 없는 항목 제외",
          len(chart.missing_bar([{"label": "x"}]).contents) == 0)




def _single_stamp_has_no_series():
    """시점이 하나뿐인 DB에서 collect()가 time_series를 만들지 않는지 (B4 이식).

    "스냅샷 1개면 변화 분석을 하지 않는다"는 규칙은 리포트 문구가 아니라 데이터 층의
    계약이다 — 시점 2개 이상인 상품만 시계열에 담긴다.
    """
    import sqlite3
    import tempfile as _tf
    sys.path.insert(0, str(SCRIPTS))
    import intel_data
    import importlib
    intel_db = importlib.import_module("intel_db")
    tmp = Path(_tf.mkdtemp()) / "one.db"
    conn = intel_db.connect(str(tmp))
    conn.execute("INSERT INTO products (site, product_id, name) VALUES ('s','1','A')")
    conn.execute("INSERT INTO observations (site, product_id, observed_at, context, "
                 "price_sale, like_count) VALUES ('s','1','2026-01-01 00:00:00','brand:x',100,5)")
    conn.commit()
    conn.close()
    d = intel_data.collect(str(tmp), ["brand:x"])
    shutil.rmtree(tmp.parent, ignore_errors=True)
    return not d.get("time_series")


def data_rule_tests():
    """B계열에서 이식한 **데이터 규칙** (HTML 폐기와 무관하게 지켜야 하는 것들).

    구 run_tests.py의 B계열 82건은 대부분 HTML 레이아웃 검증(칩·정렬·섹션 유무)이라
    D27로 함께 폐기됐다. 그러나 **소수는 형식이 아니라 데이터 규칙**이었고, 그것들이
    HTML 문자열 파싱에 얹혀 있어 생성기를 지우면 함께 사라질 참이었다.

    여기로 옮긴 것은 규칙 자체를 데이터 층에서 검증한다 — 산출이 HTML이든 PDF든
    같은 답이 나와야 한다.
    """
    sys.path.insert(0, str(SCRIPTS))
    import intel_data

    m = json.loads((FIX / "musinsa-brand-linesheet-good.json").read_text())["items"]
    c = json.loads((FIX / "29cm-brand-linesheet-good.json").read_text())["items"]
    for it in m:
        it["site"] = "musinsa"
    for it in c:
        it["site"] = "29cm"
    both = m + c

    # B1m — 매칭된 상품은 합집합에서 1행이다 (단순 합 38 ≠ 합집합 28)
    rows = intel_data.build_union_rows(both)
    check("B1m 합집합: 매칭된 상품은 1행 (38건 → 28행)",
          rows is not None and len(rows) == 28,
          "행수 %s" % (len(rows) if rows else None))
    check("B1m2 단순 합과 합집합이 다르다는 것이 계산으로 드러난다",
          len(both) == 38 and len(rows) < len(both))

    # B1n — 입점 구분: 양쪽/한쪽 단독이 행에서 갈린다
    sites_per_row = [{both[i]["site"] for i in r["i"]} for r in rows]
    both_n = sum(1 for s_ in sites_per_row if len(s_) == 2)
    solo_m = sum(1 for s_ in sites_per_row if s_ == {"musinsa"})
    solo_c = sum(1 for s_ in sites_per_row if s_ == {"29cm"})
    check("B1n 입점이 세 가지로 갈린다 (양쪽 10 · 무신사 단독 14 · 29CM 단독 4)",
          (both_n, solo_m, solo_c) == (10, 14, 4),
          "%s" % ((both_n, solo_m, solo_c),))

    # B1s — 매칭은 정규화 이름 완전일치뿐이다(유사도·가격 보조 금지)
    # EVIDENCE §5: 다른 상품인데 정가가 같아 가격이 오탐을 확증한 사례가 있다
    check("B1s 이름이 다르면 가격이 같아도 매칭되지 않는다",
          intel_data.match_key("레터링 그래픽 티셔츠") != intel_data.match_key("스탠실 그래픽 티셔츠"))
    check("B1s2 괄호 안 옵션·유통 표기는 매칭에서 뺀다",
          intel_data.match_key("REYA LACE TOP (5 COLORS)")
          == intel_data.match_key("REYA LACE TOP (PINK)"))

    # B1z — 반응 지표가 전부 null인 플랫폼은 비교 축에서 빠진다(자사몰)
    own = [dict(i, site="own.com", like_count=None, review_count=None, rating=None)
           for i in m[:5]]
    data = {"items": both + own, "meta": {"proxies": []}}
    axes = intel_data.numeric_axes(data)
    check("B1z 값이 하나라도 있는 축만 남는다", "price_sale" in axes)
    none_data = {"items": [dict(i, like_count=None) for i in both], "meta": {"proxies": []}}
    check("B1z2 전부 null인 축은 분석 축에서 빠진다",
          "like_count" not in intel_data.numeric_axes(none_data))

    # B1d·B1g — 미노출(null)과 구간 표기는 정수 축이 되지 못한다
    check("B1d·B1g 구간 표기는 정수 칸이 null이다 (오차 무한 — 축이 못 된다)",
          all(i.get("view_count") is None
              for i in both if i.get("view_count_display")))

    # ── 2차 이식: 문구 뒤에 깔린 규칙들 (B1q·B2h·B4·D4b) ──────────────────
    # 구 테스트는 리포트 문구로 확인했다("단독 입점 상품은 이 비교에서 빠진다").
    # 문구는 형식이지만 그 밑의 규칙은 형식과 무관하다 — 규칙을 직접 검증한다.

    # B1q — 플랫폼 반응 비교는 **매칭된 상품만** 센다(단독 입점은 빠진다).
    # 안 그러면 카탈로그가 큰 쪽이 이기는 비교가 된다.
    pairs = intel_data.matched_pairs(both, "like_count")
    paired_keys = set()
    for (sa, sb), lst in pairs.items():
        paired_keys |= {n for _, _, n in lst}
    check("B1q 쌍체 비교는 양쪽에 다 있는 상품만 센다",
          pairs and all(len(lst) <= 10 for lst in pairs.values()),
          "쌍 %s" % {k: len(v) for k, v in pairs.items()})

    # B2h — 후기 0건과 평점 미노출은 다른 값이다(0을 결측으로, 결측을 0으로 세지 않는다)
    mixed = [dict(both[0], review_count=0, rating=None),
             dict(both[1], review_count=5, rating=4.5)]
    data_mixed = {"items": mixed, "meta": {"proxies": []}}
    axes_mixed = intel_data.numeric_axes(data_mixed)
    check("B2h 후기 0건은 값이 있는 것이다 (축에 남는다)", "review_count" in axes_mixed)
    check("B2h2 평점은 값이 하나라도 있으면 축에 남는다", "rating" in axes_mixed)
    all_null_rating = {"items": [dict(i, rating=None) for i in mixed],
                       "meta": {"proxies": []}}
    check("B2h3 전부 미노출인 평점은 축에서 빠진다 (0으로 세지 않는다)",
          "rating" not in intel_data.numeric_axes(all_null_rating))

    # B4 — 시점이 하나면 변화를 말할 수 없다. 시계열은 시점 2개 이상인 상품만 담는다.
    check("B4 단일 시점 데이터로 시계열을 만들지 않는다",
          _single_stamp_has_no_series())



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

    print("[4] 가격 변경 사건 (데이터 층)")
    run([SCRIPTS / "intel_db.py", "import-snapshots", FIX / "snapshots"], work, db)
    sys.path.insert(0, str(SCRIPTS))
    import intel_data
    d4 = intel_data.collect(db, None)
    check("가격 변경 사건이 검출된다", len(d4.get("price_events") or []) > 0,
          "사건 %d건" % len(d4.get("price_events") or []))
    ev = (d4.get("price_events") or [{}])[0]
    check("사건에 전후 가격과 시각이 있다",
          all(k in ev for k in ("price_from", "price_to", "from_at", "to_at")), ev)
    # 구 대시보드 테스트에서 이식: 구간 표기 필드는 수치 축 후보가 아니다
    check("구간 표기 필드(view_count)는 수치 축 후보에 없다",
          "view_count" not in intel_data.numeric_axes(d4))

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

    # [6b] 새 파이프라인(eda/analyze)에서도 프록시가 축이 되는지 — D39 회귀 방어.
    # 위 [6]은 폐기 예정 HTML(build_analysis_report)만 봐서, D27 리팩터링 때 새
    # 파이프라인의 프록시 소비가 끊긴 것을 111건 회귀가 아무것도 못 잡았다. 그 구멍을 막는다.
    sys.path.insert(0, str(SCRIPTS))
    import intel_data
    data = intel_data.collect(db, None)
    check("collect가 프록시를 items에 주입한다 (px_name_lang)",
          any("px_name_lang" in it for it in data["items"]))
    cats = [f for f, _ in intel_data.cat_axes(data, [("site", "플랫폼")])]
    check("cat_axes가 범주 프록시를 축에 넣는다 (D39 — analyze 그룹 비교의 축)",
          "px_name_lang" in cats,
          "cat_axes 결과: %s" % cats)
    nums = [f for f, _ in intel_data.num_axes(data)]
    check("num_axes는 범주 프록시를 수치축에 넣지 않는다 (name_lang은 범주형)",
          "px_name_lang" not in nums)

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

    print("[11] 팀 커버리지 조회 (check --team, D32)")
    team_coverage_tests(work, db, ctx)

    print("[12] 차트 렌더 (chart.py, D40)")
    chart_tests()

    print("[13] 데이터 규칙 (B계열 이식 — HTML과 무관)")
    data_rule_tests()

    shutil.rmtree(work, ignore_errors=True)
    print("-" * 56)
    print(f"통과 {passed} · 실패 {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
