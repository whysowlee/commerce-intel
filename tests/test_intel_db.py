#!/usr/bin/env python3
"""intel_db.py · 데이터 층·차트 회귀 테스트.

    python3 tests/test_intel_db.py

기존 run_tests.py와 같은 원칙 — 픽스처만 쓰고 사이트에 붙지 않는다.
검증 대상: 적재 멱등성(중복 스킵) · 재사용 판정(check/reuse-attrs TTL) ·
가격 변경 사건 검출 · 대시보드 산출물 구조.
"""
import io
import json
import os
import shutil
import sqlite3
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



def hierarchy_tests():
    """카테고리 계층 판정 (D42) — 대등하지 않은 쌍을 검정에서 뺀다.

    실측 카탈로그가 근거다. 사이트에 붙지 않고 인라인 카탈로그로 검증한다 —
    `ranking_targets.json`이 갱신돼도 이 테스트는 규칙만 본다.
    """
    sys.path.insert(0, str(SCRIPTS))
    import intel_data

    work = Path(tempfile.mkdtemp(prefix="hier-"))
    cat = work / "catalog.json"
    cat.write_text(json.dumps({"29cm": {"entries": [
        {"path": ["여성의류", "스커트", "미니"]},
        {"path": ["여성의류", "스커트", "미디"]},
        {"path": ["여성의류", "스커트", "데님"]},
        {"path": ["여성의류", "단독", "하의"]},
        {"path": ["여성의류", "아우터", "후드"]},     # 다른 가지의 순수 리프
        {"path": ["남성의류", "하의", "데님 팬츠"]},   # 하의가 우산임을 드러낸다
    ]}}, ensure_ascii=False), encoding="utf-8")
    h = intel_data.category_hierarchy(str(work / "no.db"), catalog_path=str(cat))

    check("D42 조상-자손은 비교하지 않는다 (스커트 ⊃ 미니)",
          intel_data.incomparable("스커트", "미니", h))
    check("D42 형제는 비교한다 (미니 대 미디 — 둘 다 스커트 아래)",
          not intel_data.incomparable("미니", "미디", h))
    # 사용자가 인사이트 PDF에서 잡은 바로 그 쌍이다 (2026-08-04)
    check("D42 굵기가 다르면 비교하지 않는다 (리프 미니 대 우산 하의)",
          intel_data.incomparable("미니", "하의", h))
    check("D42 경로 표기와 조각 표기가 같은 것으로 걸린다",
          intel_data.incomparable("여성의류 > 스커트 > 미디", "미디", h))
    # **모르면 거르지 않는다** — 없는 근거로 검정을 지우면 안 본 것이 없는 것이 된다
    check("D42 트리에 없는 값은 거르지 않는다 (판단 근거가 없다)",
          not intel_data.incomparable("처음보는값", "또다른값", h))
    check("D42 계층 정보가 없으면 아무것도 거르지 않는다",
          not intel_data.incomparable("미니", "하의", {"anc": set(), "parent": {}}))
    # E-CH-1·3 사유를 나눠 센다 — 합계만 찍으면 ②로 과하게 빠져도 알 수 없다
    check("E-CH-1 조상-자손은 사유가 ancestor",
          intel_data.incomparable_reason("스커트", "미니", h) == "ancestor")
    check("E-CH-3 굵기 차이는 사유가 granularity",
          intel_data.incomparable_reason("미니", "하의", h) == "granularity")
    check("E-CH-2 형제는 사유가 없다",
          intel_data.incomparable_reason("미니", "미디", h) is None)
    # E-CH-6 카탈로그가 없어도 예외를 내지 않는다
    empty = intel_data.category_hierarchy(str(work / "no.db"),
                                          catalog_path=str(work / "없는파일.json"))
    check("E-CH-6 카탈로그 파일이 없어도 죽지 않는다",
          empty["anc"] == set() and empty["parent"] == {})
    # E-CH-10 다른 가지라는 것만으로 거르지 않는다 (2026-08-04 리뷰로 좁힌 규칙).
    # 옛 규칙("부모가 갈리면 제외")은 "스커트 계열 대 아우터 계열" 같은 정상 비교까지
    # 지웠다 — 둘 다 리프면 대등하다.
    check("E-CH-10 다른 가지의 순수 리프끼리는 비교한다 (미니 대 후드)",
          not intel_data.incomparable("미니", "후드", h))
    check("E-CH-11 우산 대 리프는 굵기가 달라 거른다 (하의 대 미니)",
          intel_data.incomparable_reason("미니", "하의", h) == "granularity")
    check("E-CH-12 우산끼리는 비교한다 (스커트 대 아우터)",
          not intel_data.incomparable("스커트", "아우터", h))
    check("E-CH-13 우산 판별은 트리 전체를 본다 (하의는 남성의류 아래서 부모)",
          "하의" in h["umbrella"] and "미니" not in h["umbrella"])
    # E-CH-14 빈 값에서 죽지 않는다 — 터지면 리포트 생성 전체가 멈춘다 (PR #8 리뷰)
    for bad in ("", "   ", ">", " > > "):
        try:
            got = intel_data.incomparable_reason(bad, "미니", h)
            ok = got is None
        except Exception as e:
            ok, got = False, "%s: %s" % (type(e).__name__, e)
        check("E-CH-14 빈 카테고리(%r)에서 예외 없이 None" % bad, ok, got)
    shutil.rmtree(work, ignore_errors=True)


def proxy_auto_tests():
    """프록시 규칙 실행기 (D43) — rule 즉석 판정과 vision 배치 묶기."""
    sys.path.insert(0, str(SCRIPTS))
    import proxy_auto

    class Row(dict):
        def keys(self):
            return dict.keys(self)

    card = {"proxy_name": "t", "material": "name", "method": "rule",
            "value_space": ["데님", "그 외"],
            "rules": [{"value": "데님", "any": ["데님", "denim"]},
                      {"value": "그 외", "any": ["."]}]}
    check("D43 규칙은 위에서부터 먼저 맞는 것이 값이다",
          proxy_auto.judge_row(card, Row(name="워시드 데님 스커트"))[0] == "데님")
    check("D43 대소문자를 가리지 않는다",
          proxy_auto.judge_row(card, Row(name="DENIM SKIRT"))[0] == "데님")
    check("D43 재료가 없으면 판정하지 않는다 (저장 대상이 아니다)",
          proxy_auto.judge_row(card, Row(name=None)) is None)
    # all은 전부 맞아야 한다 — 혼합 판정이 이 규칙에 걸려 있다
    mix = {"material": "name", "rules": [{"value": "혼합", "all": ["[가-힣]", "[A-Za-z]{2}"]}]}
    check("D43 all은 전부 맞을 때만",
          proxy_auto.judge_row(mix, Row(name="LOW 스커트"))[0] == "혼합"
          and proxy_auto.judge_row(mix, Row(name="스커트")) is None)
    num = {"material": "name", "numeric": {"kind": "char_len"}}
    check("D43 수치 프록시는 float으로 나온다",
          proxy_auto.judge_row(num, Row(name="abcde"))[0] == 5.0)

    # ── 카드 검증 (2026-08-04 리뷰) — 카드는 AI가 쓰므로 검수된 입력이 아니다
    ok, bad = proxy_auto.validate_cards([
        {"proxy_name": "broken", "rules": [{"any": ["[unclosed"], "value": "x"}]},
        {"proxy_name": "nopat", "numeric": {"kind": "count"}},
        {"proxy_name": "fine", "rules": [{"any": ["[가-힣]"], "value": "한글"}]},
    ])
    names = [c["proxy_name"] for c in ok]
    check("E-PA-8 컴파일 안 되는 정규식은 그 카드만 버린다", names == ["fine"], names)
    check("E-PA-9 count인데 pattern 없으면 카드를 버린다",
          any(n == "nopat" for n, _ in bad))
    check("E-PA-9b judge_row도 죽지 않는다 (직접 호출 경로)",
          proxy_auto.judge_row({"material": "name", "numeric": {"kind": "count"}},
                               Row(name="아무거나")) is None)
    # E-PA-10 입력을 잘라 백트래킹 폭발 여지를 줄인다 (완전 차단은 불가 — 알려진 한계)
    long_card = {"material": "name", "rules": [{"any": ["끝$"], "value": "y"}]}
    tail = "가" * (proxy_auto.MATCH_MAX_CHARS + 10) + "끝"
    check("E-PA-10 매칭 입력은 MATCH_MAX_CHARS로 잘린다",
          proxy_auto.judge_row(long_card, Row(name=tail)) is None)
    # E-PA-11 한글은 앞 경계가 없으면 "정면"이 코튼이 된다 (리뷰 발견)
    import json as _json
    cards = _json.loads((SCRIPTS.parent / "references" /
                         "proxy-cards-default.json").read_text(encoding="utf-8"))["cards"]
    mat = [c for c in cards if c["proxy_name"] == "material_word"][0]
    check("E-PA-11 '정면 컷'은 코튼으로 판정되지 않는다",
          proxy_auto.judge_row(mat, Row(name="정면 컷 스커트"))[0] == "소재 미표기")
    check("E-PA-11b '서울'은 울로 판정되지 않는다",
          proxy_auto.judge_row(mat, Row(name="서울 스토어 한정"))[0] == "소재 미표기")
    check("E-PA-11c 진짜 소재 표기는 잡는다",
          proxy_auto.judge_row(mat, Row(name="면 100% 스커트"))[0] == "코튼")


def schema_v2_tests():
    """스키마 v2 (D45) — 뷰가 원본처럼 읽히고 써지는가 (E-DB).

    옛 이름이 뷰가 됐다는 것이 이 스키마의 전부다. 뷰가 한 군데라도 어긋나면
    분석 전체가 조용히 틀린 값을 본다 — 예외가 안 나는 종류라 여기서 고정한다.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    intel_db = importlib.import_module("intel_db")
    work = Path(tempfile.mkdtemp(prefix="v2-"))
    db = str(work / "v2.db")
    conn = intel_db.connect(db)

    # E-DB-12 새 DB는 v2다
    tabs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    check("E-DB-12 새 DB는 v2로 만들어진다", "obs_base" in tabs and "products" in views,
          sorted(tabs)[:6])

    now = "2026-08-04 10:00:00"
    conn.execute("INSERT INTO runs (run_id, site, story, target, collected_at) "
                 "VALUES ('R1','29cm','brand-linesheet','T',?)", (now,))
    # E-DB-4·11 뷰로 INSERT하고 URL이 접혔다 펴진다
    conn.execute(
        "INSERT INTO products (site, product_id, name, url, image_url, brand, category,"
        " attributes, attributes_basis, static_verified_at, first_seen_at, last_seen_at,"
        " raw_extras) VALUES ('29cm','P1','상품','https://a.test/goods/1',"
        "'https://img.a.test/x.jpg','브랜드','미니',NULL,NULL,?,?,?,NULL)", (now, now, now))
    r = conn.execute("SELECT url, image_url, brand, first_seen_at FROM products "
                     "WHERE product_id='P1'").fetchone()
    check("E-DB-4·11 뷰 INSERT 후 URL·시각이 그대로 왕복한다",
          r["url"] == "https://a.test/goods/1"
          and r["image_url"] == "https://img.a.test/x.jpg"
          and r["first_seen_at"] == now, tuple(r))
    check("E-DB-11 호스트가 사전으로 접혔다",
          conn.execute("SELECT COUNT(*) FROM hosts").fetchone()[0] == 2)

    # E-DB-6 부분 SET UPDATE
    conn.execute("UPDATE products SET brand='새브랜드' WHERE site='29cm' AND product_id='P1'")
    r = conn.execute("SELECT brand, name FROM products WHERE product_id='P1'").fetchone()
    check("E-DB-6 부분 SET UPDATE가 다른 컬럼을 안 지운다",
          r["brand"] == "새브랜드" and r["name"] == "상품", tuple(r))

    # E-DB-5 중복 관측은 IntegrityError — "중복 N건" 집계가 이걸로 센다
    conn.execute("INSERT INTO observations (site, product_id, observed_at, context,"
                 " price_sale, rank, run_id) VALUES ('29cm','P1',?,'ranking:t',1000,3,'R1')",
                 (now,))
    dup = False
    try:
        conn.execute("INSERT INTO observations (site, product_id, observed_at, context,"
                     " price_sale) VALUES ('29cm','P1',?,'ranking:t',9)", (now,))
    except sqlite3.IntegrityError:
        dup = True
    check("E-DB-5 중복 관측은 IntegrityError로 남는다", dup)
    o = conn.execute("SELECT price_sale, rank, run_id, context FROM observations").fetchone()
    check("E-DB-4 관측이 뷰로 그대로 읽힌다",
          tuple(o) == (1000, 3, "R1", "ranking:t"), tuple(o))

    # E-DB-8 ttl_days=NULL은 덮어써야 한다 (set-attrs가 전역 TTL을 먹이는 방식)
    for ttl in (90, None):
        conn.execute("INSERT OR REPLACE INTO product_attributes (site, product_id,"
                     " attr_name, value, basis, decided_at, ttl_days)"
                     " VALUES ('29cm','P1','핏','와이드','name',?,?)", (now, ttl))
    got = conn.execute("SELECT ttl_days FROM product_attributes "
                       "WHERE product_id='P1'").fetchone()[0]
    check("E-DB-8 ttl_days=NULL이 덮어써진다 (COALESCE로 지키면 안 된다)",
          got is None, got)

    # E-DB-17 URL 접기 규칙이 파이썬과 트리거 SQL에서 **같아야** 한다 (PR #9 리뷰).
    # 경로 없는 URL이 갈리면 같은 호스트가 사전에 두 항목으로 쪼개진다.
    from schema_v2 import split_url, _HOST, _PATH
    for url in ("https://cdn.example.com", "https://a.test/x/y", "노프로토콜경로",
                "", None):        # 빈 문자열·NULL도 같은 결과여야 한다 (PR #9 리뷰)
        hid, path = split_url(conn, url, {})
        pref = conn.execute("SELECT prefix FROM hosts WHERE host_id=?", (hid,)).fetchone()
        pref = pref[0] if pref else None
        sql = conn.execute("SELECT %s, %s" % (_HOST.format(u="?1"), _PATH.format(u="?1")),
                           (url,)).fetchone()
        check("E-DB-17 URL 접기가 파이썬·SQL에서 같다 (%r)" % url,
              (pref, path) == tuple(sql)
              and (pref or "") + (path or "") == (url or ""),
              ((pref, path), tuple(sql)))

    # E-DB-10 증분 키는 뷰의 마지막 컬럼 _rowid다
    cols = [d[0] for d in conn.execute("SELECT * FROM observations LIMIT 1").description]
    check("E-DB-10 _rowid가 뷰의 **마지막** 컬럼이다", cols[-1] == "_rowid", cols[-3:])
    shutil.rmtree(work, ignore_errors=True)


def prune_tests():
    """솎기 (D45-a) — 값이 바뀐 순간은 안 지운다 (E-DB-13·14)."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    intel_db = importlib.import_module("intel_db")
    prune = importlib.import_module("prune")
    work = Path(tempfile.mkdtemp(prefix="prune-"))
    db = str(work / "p.db")
    conn = intel_db.connect(db)
    conn.execute("INSERT INTO products (site, product_id, name, first_seen_at, last_seen_at)"
                 " VALUES ('29cm','P1','상품','2020-01-01 00:00:00','2020-01-01 00:00:00')")
    # 10시점을 **한 시간 안에** 5분 간격으로 둔다 — 버킷(1시간)당 1개 규칙이 실제로
    # 물어야 솎기가 검증된다. 가격은 3번째와 7번째에만 바뀌고 순위는 고정이다.
    prices = [1000, 1000, 2000, 2000, 2000, 2000, 3000, 3000, 3000, 3000]
    for i, p in enumerate(prices):
        conn.execute("INSERT INTO observations (site, product_id, observed_at, context,"
                     " price_sale, rank) VALUES ('29cm','P1',?,'brand:t',?,5)",
                     ("2020-01-01 00:%02d:00" % (i * 5), p))
    conn.commit()
    counts, total = prune.plan(conn, keep_days=0, bucket=3600)
    check("E-DB-13 예행은 남길 이유를 사유별로 센다",
          counts.get("change", 0) >= 2 and counts.get("edge", 0) == 2, counts)
    n = prune.apply_prune(conn)
    kept = [r[0] for r in conn.execute(
        "SELECT price_sale FROM observations ORDER BY observed_at")]
    import itertools
    seq = [k for k, _ in itertools.groupby(kept)]
    check("E-DB-14 솎은 뒤에도 가격 변화 순서가 그대로다",
          seq == [1000, 2000, 3000], (n, seq))
    check("E-DB-14b 변화 없는 중복만 지워졌다", n > 0 and len(kept) < len(prices),
          (n, len(kept)))
    # E-DB-16 못 읽는 시각은 **즉시 걸린다** — NULL로 조용히 들어가면 그 관측은
    # 시계열에서 사라지고 아무도 모른다 (NOT NULL이 그 몫을 진다)
    bad = False
    try:
        conn.execute("INSERT INTO observations (site, product_id, observed_at, context)"
                     " VALUES ('29cm','P1','날짜아님','brand:t')")
    except sqlite3.IntegrityError:
        bad = True
    check("E-DB-16 해석 못 하는 시각은 NULL로 새지 않고 즉시 실패한다", bad)
    shutil.rmtree(work, ignore_errors=True)


def modeling_tests():
    """Y 선정·퍼널 비율·무영향 인사이트 (D47 · 2026-08-04 피드백)."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    d = importlib.import_module("intel_data")
    try:
        ins = importlib.import_module("insight")
    except ImportError:
        # insight는 reportlab에 의존한다(PDF). 없는 환경에서도 데이터 층 규칙은
        # 검증돼야 하므로 여기만 건너뛴다 — 배포 게이트는 package.sh가 지킨다.
        ins = None

    # E-MD-1 공급자가 정한 값은 Y가 될 수 없다
    check("E-MD-1 할인율·판매가는 lever(원인 쪽)",
          d.role_of("discount_rate") == "lever" and d.role_of("price_sale") == "lever")
    check("E-MD-2 하트·조회·구매는 response(결과 쪽)",
          all(d.role_of(f) == "response"
              for f in ("like_count", "view_count", "purchase_count")))

    # E-MD-3 퍼널 비율 — 분모가 없거나 0이면 만들지 않는다
    it = d.add_funnel({"like_count": 50, "view_count": 1000,
                       "purchase_count": 10, "review_count": 3})
    check("E-MD-3 퍼널 비율이 계산된다 (조회 1000 · 하트 50 → 5%)",
          it["cvr_view_like"] == 5.0 and it["cvr_view_buy"] == 1.0, it)
    it0 = d.add_funnel({"like_count": 50, "view_count": None, "purchase_count": 5})
    check("E-MD-4 분모가 없으면 비율을 만들지 않는다 (0으로 채우지 않는다)",
          it0["cvr_view_like"] is None, it0)
    itz = d.add_funnel({"like_count": 0, "view_count": 0, "purchase_count": 3})
    check("E-MD-5 분모가 0이면 만들지 않는다 (나눗셈 폭발·가짜 100%)",
          itz["cvr_view_like"] is None and itz["cvr_like_buy"] is None, itz)

    if ins is None:
        print("  SKIP  E-MD-6~10 무영향·액션 — reportlab 미설치")
        return

    # E-MD-6 무영향은 "없다"이고, 표본 부족은 "모른다"다 — 섞으면 안 된다
    small = {"verdict": "rejected", "effect": 0.02, "n": 500,
             "fails": ["표본이 작다"], "kind": "group_compare"}
    real = {"verdict": "rejected", "effect": 0.03, "n": 500,
            "fails": ["효과 크기가 작다"], "kind": "group_compare"}
    got = ins.null_findings([small, real])
    check("E-MD-6 표본 부족 기각은 '차이 없음'에 넣지 않는다",
          got == [real], [g.get("fails") for g in got])
    big = {"verdict": "rejected", "effect": 0.9, "n": 500,
           "fails": ["다중비교"], "kind": "group_compare"}
    check("E-MD-7 효과가 큰데 기각된 것도 '차이 없음'이 아니다",
          ins.null_findings([big]) == [])
    thin = {"verdict": "rejected", "effect": 0.01, "n": 10,
            "fails": ["다중비교"], "kind": "group_compare"}
    check("E-MD-8 n이 너무 적으면 '차이 없다'고 말하지 않는다",
          ins.null_findings([thin]) == [])

    # E-MD-9 액션은 약한 단서에 확정적으로 붙지 않는다
    weak = {"verdict": "weak", "kind": "group_compare", "cat_field": "brand",
            "fails": ["표본이 작다"]}
    check("E-MD-9 약한 단서의 액션은 '아직 정하지 마라'로 시작한다",
          ins.action_hint(weak).startswith("아직 정하지 마라"), ins.action_hint(weak))
    resp = {"verdict": "strong", "kind": "correlation", "direction": "response_pair",
            "x_label": "하트", "y_label": "후기 수"}
    check("E-MD-10 반응끼리의 상관은 액션에서 선후를 단정하지 않는다",
          "선후를 모른다" in ins.action_hint(resp), ins.action_hint(resp))

    # E-MD-12 관문 판정은 **코드**로 한다 — 문구가 바뀌어도 안 흔들린다 (PR #9 리뷰)
    coded = {"verdict": "rejected", "effect": 0.02, "n": 500,
             "fails": ["문구가 바뀌었다"], "fail_codes": ["sample"],
             "kind": "group_compare"}
    check("E-MD-12 표본 부족을 fail_codes로 가른다 (문구 무관)",
          ins.null_findings([coded]) == [], ins.null_findings([coded]))

    an = importlib.import_module("analyze")
    # E-MD-13 둘 다 lever면 방향을 단정하지 않는다
    check("E-MD-13 lever끼리는 lever_pair로 헤지한다",
          an._orient("price_sale", "discount_rate", "판매가", "할인율")[4] == "lever_pair")
    check("E-MD-14 lever→response는 lever가 원인 쪽으로 간다",
          an._orient("like_count", "discount_rate", "하트", "할인율")[:2]
          == ("discount_rate", "like_count"))

    # E-MD-15 claim이 헤지했으면 action도 헤지해야 한다 — 같은 카드 안에서
    # 주장과 액션이 모순되면 읽는 사람은 더 확정적인 쪽(액션)을 믿는다 (PR #9 리뷰)
    lp = {"verdict": "strong", "kind": "correlation", "direction": "lever_pair",
          "x_label": "정가", "y_label": "할인율"}
    got = ins.action_hint(lp)
    check("E-MD-15 lever끼리의 상관도 액션에서 선후를 단정하지 않는다",
          "데이터가 답하지 않는다" in got and "폭을 정할 때 참고" not in got, got)

    # E-MD-16 recheck_hint도 코드로 가른다 (문구가 바뀌어도 안 흔들린다)
    r_coded = {"fails": ["문구를 바꿨다"], "fail_codes": ["sample"]}
    check("E-MD-16 recheck_hint가 fail_codes를 본다",
          "표본이 쌓이면" in ins.recheck_hint(r_coded), ins.recheck_hint(r_coded))
    r_old = {"fails": ["표본이 작다 (n=3 < 20)"]}     # 코드 없는 옛 항목은 폴백
    check("E-MD-17 코드가 없으면 문구로 폴백한다",
          "표본이 쌓이면" in ins.recheck_hint(r_old), ins.recheck_hint(r_old))


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

    print("[14] 카테고리 계층 판정 (D42)")
    hierarchy_tests()

    print("[15] 프록시 규칙 실행기 (D43)")
    proxy_auto_tests()

    print("[16] 스키마 v2 — 뷰 읽기·쓰기 (D45)")
    schema_v2_tests()

    print("[17] 솎기 — 변화 순간 보존 (D45-a)")
    prune_tests()

    print("[18] 모델링 — Y 선정·퍼널·무영향 (D47)")
    modeling_tests()

    shutil.rmtree(work, ignore_errors=True)
    print("-" * 56)
    print(f"통과 {passed} · 실패 {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
