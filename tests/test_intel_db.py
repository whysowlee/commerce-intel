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
        {"proxy_name": "badnum", "numeric": {"kind": "count", "pattern": "[bad"}},
        {"proxy_name": "fine", "rules": [{"any": ["[가-힣]"], "value": "한글"}]},
    ])
    names = [c["proxy_name"] for c in ok]
    check("E-PA-8 컴파일 안 되는 정규식은 그 카드만 버린다", names == ["fine"], names)
    check("E-PA-9 count인데 pattern 없으면 카드를 버린다",
          any(n == "nopat" for n, _ in bad))
    # 3R Blocker 5 — numeric.pattern도 rules와 같은 컴파일 검증을 받는다
    check("E-PA-12 numeric.pattern이 깨진 카드도 버린다 (lazy 판정이 리포트 안에서 돈다)",
          any(n == "badnum" for n, _ in bad), bad)
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


def schema_v3_tests():
    """스키마 v3 (D65) — 뷰가 원본처럼 읽히고 써지는가 (E-DB) + v3 신규 계약.

    옛 이름이 뷰라는 규약(D45)은 그대로다. v3에서 더해진 계약: 컨텍스트 접두사
    CHECK · 카테고리 계층 왕복 · 브랜드 대표명/별명 · runs 정수 PK FK · obs_attr.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    intel_db = importlib.import_module("intel_db")
    schema_v3 = importlib.import_module("schema_v3")
    work = Path(tempfile.mkdtemp(prefix="v3-"))
    db = str(work / "v3.db")
    conn = intel_db.connect(db)

    # E-DB-12 새 DB는 v3다
    tabs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    views = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    check("E-DB-12 새 DB는 v3로 만들어진다 (product_categories·obs_attr 존재)",
          {"obs_base", "product_categories", "obs_attr", "brand_aliases",
           "brand_platforms"} <= tabs and "products" in views, sorted(tabs)[:8])

    now = "2026-08-04 10:00:00"
    conn.execute("INSERT INTO runs (run_id, site, story, target, collected_at) "
                 "VALUES ('R1','29cm','brand-linesheet','T',?)", (now,))
    # E-DB-4·11 뷰로 INSERT하고 URL이 접혔다 펴진다 (v3: attributes·seen_at 없음)
    conn.execute(
        "INSERT INTO products (site, product_id, name, url, image_url, brand,"
        " static_verified_at) VALUES ('29cm','P1','상품','https://a.test/goods/1',"
        "'https://img.a.test/x.jpg','브랜드',?)", (now,))
    r = conn.execute("SELECT url, image_url, brand, static_verified_at FROM products "
                     "WHERE product_id='P1'").fetchone()
    check("E-DB-4·11 뷰 INSERT 후 URL·시각이 그대로 왕복한다",
          r["url"] == "https://a.test/goods/1"
          and r["image_url"] == "https://img.a.test/x.jpg"
          and r["static_verified_at"] == now, tuple(r))
    check("E-DB-11 호스트가 사전으로 접혔다",
          conn.execute("SELECT COUNT(*) FROM hosts").fetchone()[0] == 2)

    # E-DB-30 카테고리 계층 왕복 (D65-3) — 경로가 계층 행으로 접혔다 도로 펴진다
    schema_v3.assign_category(conn, "29cm", "P1", "여성의류 > 스커트 > 미니")
    got = conn.execute("SELECT category FROM products WHERE product_id='P1'").fetchone()[0]
    check("E-DB-30 카테고리 경로가 계층으로 접혔다 도로 펴진다",
          got == "여성의류 > 스커트 > 미니", got)
    depths = dict(conn.execute("SELECT name, depth FROM categories"))
    check("E-DB-30b depth는 플랫폼 경로 위치 그대로 (1·2·3)",
          depths == {"여성의류": 1, "스커트": 2, "미니": 3}, depths)
    check("E-DB-30c 상품 카테고리는 N:M 매핑 행이다 (source='platform')",
          conn.execute("SELECT COUNT(*) FROM product_categories "
                       "WHERE source='platform'").fetchone()[0] == 1)
    # 같은 이름이 다른 부모 아래 공존한다 — UNIQUE(name, parent)
    schema_v3.ensure_category_path(conn, "남성의류 > 스커트")
    check("E-DB-30d 같은 이름이 다른 부모 아래 두 행이다",
          conn.execute("SELECT COUNT(*) FROM categories WHERE name='스커트'")
          .fetchone()[0] == 2)

    # E-DB-31 컨텍스트 접두사 CHECK (D65-1) — 4종 밖은 물리 표가 거부한다
    bad = False
    try:
        conn.execute("INSERT INTO contexts (name) VALUES ('elsewhere:x')")
    except sqlite3.IntegrityError:
        bad = True
    check("E-DB-31 접두사 4종 밖 컨텍스트는 CHECK가 거부한다", bad)
    check("E-DB-31b 접두사 4종은 통과한다", all(
        conn.execute("INSERT OR IGNORE INTO contexts (name) VALUES (?)",
                     (p + ":t",)).rowcount == 1
        for p in ("brand", "market", "ranking", "adhoc")))

    # E-DB-32 브랜드 대표명·별명 (D65-4) — 표기 변형이 candidate 별명으로 붙는다
    rep = intel_db.resolve_brand(conn, "브랜드", "29cm", {})
    check("E-DB-32 대표명 완전일치는 그대로", rep == "브랜드", rep)
    rep2 = intel_db.resolve_brand(conn, "브 랜 드", "29cm", {})
    check("E-DB-32b 표기 변형은 대표명으로 풀리고 별명이 등록된다",
          rep2 == "브랜드" and conn.execute(
              "SELECT verify_status FROM brand_aliases WHERE notation='브 랜 드'")
          .fetchone()[0] == "candidate", rep2)
    check("E-DB-32c 한글·영문은 묶이지 않는다 (음차 매칭 없음)",
          intel_db.resolve_brand(conn, "Brand", "29cm", {}) == "Brand")

    # E-DB-33 runs 정수 PK + 정식 FK (D65-7)
    rid = conn.execute("SELECT id FROM runs WHERE run_id='R1'").fetchone()[0]
    check("E-DB-33 runs.id는 정수 PK다", isinstance(rid, int), rid)
    fk = conn.execute("PRAGMA foreign_key_list(obs_base)").fetchall()
    check("E-DB-33b obs_base.run_id가 runs(id)를 정식 FK로 가리킨다",
          any(f[2] == "runs" and f[3] == "run_id" for f in fk), fk)
    # E-DB-33c 미등록 run_id는 NULL로 새지 않고 즉시 죽는다 (PR #13 리뷰 — D59 함정)
    loud = False
    try:
        conn.execute("INSERT INTO observations (site, product_id, observed_at, "
                     "context, price_sale, run_id) VALUES "
                     "('29cm','P1','2026-08-04 11:00:00','brand:t',1,'없는런')")
    except sqlite3.IntegrityError:
        loud = True
    check("E-DB-33c 미등록 run_id는 트리거가 거부한다 (NULL로 흘리지 않는다)", loud)
    check("E-DB-33d run_id 없는 관측(NULL)은 여전히 허용된다",
          conn.execute("INSERT INTO observations (site, product_id, observed_at, "
                       "context, price_sale) VALUES "
                       "('29cm','P1','2026-08-04 11:30:00','brand:t',2)").rowcount >= 0)

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
    o = conn.execute("SELECT price_sale, rank, run_id, context FROM observations "
                     "WHERE context='ranking:t'").fetchone()
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

    # E-DB-34 obs_attr (D65-6) — 관측에 비정형 지표가 행으로 붙는다
    obs_rowid = conn.execute("SELECT _rowid FROM observations LIMIT 1").fetchone()[0]
    conn.execute("INSERT INTO obs_attr (obs_id, attr_name, value, basis) "
                 "VALUES (?,?,?,?)", (obs_rowid, "sns_언급수", "37", "api"))
    check("E-DB-34 시점 속성이 관측 id에 매달린다",
          conn.execute("SELECT value FROM obs_attr WHERE obs_id=?",
                       (obs_rowid,)).fetchone()[0] == "37")

    # E-DB-17 URL 접기 규칙이 파이썬과 트리거 SQL에서 **같아야** 한다 (PR #9 리뷰).
    # 경로 없는 URL이 갈리면 같은 호스트가 사전에 두 항목으로 쪼개진다.
    from schema_v3 import split_url, _HOST, _PATH
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
    conn.execute("INSERT INTO products (site, product_id, name, static_verified_at)"
                 " VALUES ('29cm','P1','상품','2020-01-01 00:00:00')")
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

    # ── 구간 표기 → 순서형 축 (D48) ──────────────────────────────────
    # 원시 정수를 주는 API는 robots가 막혀 쓰지 않는다. 화면 구간 표기를 쓰되
    # **하한만 정확하므로 순위로만** 쓴다.
    for disp, want in [("1.2만 회 이상 (최근 1개월)", 12000), ("2.7천 개 이상", 2700),
                       ("300회 이상", 300), ("1,500개 이상", 1500),
                       ("5억 이상", 500000000)]:
        check("E-MD-19 구간 하한을 정확히 읽는다 (%s)" % disp,
              d.band_floor(disp) == want, d.band_floor(disp))
    for bad in ("없음", "", None, "1.2만 회", "조회수"):
        check("E-MD-20 구간 표기가 아니면 만들지 않는다 (%r)" % bad,
              d.band_floor(bad) is None, d.band_floor(bad))
    b = d.add_bands({"view_count_display": "1.2만 회 이상",
                     "purchase_count_display": "2.7천 개 이상"})
    check("E-MD-21 밴드가 축으로 붙는다",
          b["view_band"] == 12000 and b["purchase_band"] == 2700 and b["like_band"] is None, b)
    # **밴드를 비율 분모로 쓰면 전환율이 부풀려진다** — 하한이라서다
    f = d.add_funnel(dict(b))
    check("E-MD-22 밴드로는 퍼널 비율을 만들지 않는다",
          f["cvr_view_like"] is None and f["cvr_view_buy"] is None, f)
    check("E-MD-23 밴드의 역할은 response",
          all(d.role_of(x) == "response"
              for x in ("view_band", "purchase_band", "like_band")))

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
    # D62 개정 — "아직 정하지 마라" 상투구 대신 **왜 보류인지 + 재확인 방법**을
    # 카드 값으로 쓴다. 계약은 "확정적으로 굴지 않는다"이지 특정 문구가 아니다:
    # 판정을 미루는 이유가 있고, 실행 지시("높여라" 류)가 없으면 된다
    got9 = ins.action_hint(weak)
    check("E-MD-9 약한 단서의 액션은 판정을 미룬다 (확정 지시 없음)",
          any(("못 가른다" in l or "우연" in l or "보류" in l) for l in got9)
          and not any("높여라" in l or "낮춰라" in l for l in got9), got9)
    resp = {"verdict": "strong", "kind": "correlation", "direction": "response_pair",
            "x_label": "하트", "y_label": "후기 수"}
    check("E-MD-10 반응끼리의 상관은 액션에서 선후를 단정하지 않는다",
          any("선후를 모른다" in l for l in ins.action_hint(resp)), ins.action_hint(resp))

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
    # action_hint가 여러 줄(list)이 됐다(D51) — 부분 문자열은 줄 단위로 본다
    check("E-MD-15 lever끼리의 상관도 액션에서 선후를 단정하지 않는다",
          any("데이터가 답하지 않는다" in l for l in got)
          and not any("폭을 정할 때 참고" in l for l in got), got)

    # E-MD-16 recheck는 코드로 가른다 (문구가 바뀌어도 안 흔들린다) — D62로
    # 문구가 카드 값 포함 여러 줄이 됐으므로, 특정 문구가 아니라 **관문에 맞는
    # 종류의 안내인지**를 본다
    r_coded = {"fails": ["문구를 바꿨다"], "fail_codes": ["sample"], "n": 3}
    check("E-MD-16 recheck가 fail_codes를 본다",
          "우연과 못 가른다" in ins.recheck_hint(r_coded), ins.recheck_hint(r_coded))
    r_old = {"fails": ["표본이 작다 (n=3 < 20)"], "n": 3}   # 코드 없는 옛 항목은 폴백
    check("E-MD-17 코드가 없으면 문구로 폴백한다",
          "우연과 못 가른다" in ins.recheck_hint(r_old), ins.recheck_hint(r_old))

    # E-MD-18 재확인 안내에 **그 카드의 값**이 들어간다 (D62) — "다음 관측으로
    # 재확인" 같은 일반론은 읽는 사람이 할 수 있는 일이 없다
    r_val = {"verdict": "weak", "kind": "group_compare",
             "fail_codes": ["effect_small"], "group_a": "진청", "group_b": "중청",
             "metric_label": "후기 수", "effect": 0.22, "n": 180}
    lines = ins.recheck_lines(r_val)
    check("E-MD-18 재확인 안내에 카드 값이 들어간다",
          any("진청" in l and "후기 수" in l and "0.22" in l for l in lines), lines)
    check("E-MD-18b 종료 조건이 있다 — 보류가 영원히 보류로 안 남게",
          any("접는다" in l or "그때 믿는다" in l or "판정이 난다" in l
              for l in lines), lines)
    check("E-MD-18c 일반론 상투구가 아니다",
          not any(l.strip() == "다음 관측으로 재확인" for l in lines))


def incremental_key_tests():
    """증분 키 경로를 **모킹 없이 실제 sqlite3로** 탄다 (PR #9 리뷰).

    기존 회귀는 `sync_sheets`를 가짜 모듈로 갈아끼워 `rows_of`/`since_rowid`를
    통째로 우회했다 — 그래서 WHERE 절이 실제로 실행되는지 한 번도 안 봤다.
    **뷰와 물리 테이블 두 경우 모두** `since_rowid > 0`으로 쿼리를 돌려 본다.
    """
    sys.modules.pop("sync_sheets", None)       # 앞 테스트의 가짜 모듈을 걷어낸다
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    intel_db = importlib.import_module("intel_db")
    sync_sheets = importlib.reload(importlib.import_module("sync_sheets"))
    schema_v3 = importlib.import_module("schema_v3")

    work = Path(tempfile.mkdtemp(prefix="rid-"))
    db = str(work / "k.db")
    conn = intel_db.connect(db)                # 새 DB = v3
    conn.execute("INSERT INTO runs (run_id, site, story, target, collected_at) "
                 "VALUES ('R','29cm','brand-linesheet','T','2026-08-04 10:00:00')")
    conn.execute("INSERT INTO products (site, product_id, name, static_verified_at)"
                 " VALUES ('29cm','P','n','2026-08-04 10:00:00')")
    # D69: proxy_cache는 본 DB에 통합 — 같은 커넥션을 쓴다
    pconn = conn
    pconn.execute("INSERT INTO proxy_defs (proxy_name, value_space, method) "
                  "VALUES ('px','[\"v\"]','rule')")
    for i in range(5):
        conn.execute("INSERT INTO observations (site, product_id, observed_at,"
                     " context, price_sale) VALUES ('29cm','P',?,'brand:t',?)",
                     ("2026-08-04 1%d:00:00" % i, 1000 + i))
        pconn.execute("INSERT INTO proxy_cache VALUES ('px','29cm',?,?,?,?,?)",
                      ("P%d" % i, "fp%d" % i, "v", "b", "2026-08-04 10:00:00"))
    conn.commit()
    pconn.commit()

    for c, table, want_type in ((conn, "observations", "view"),
                                (pconn, "proxy_cache", "table")):
        typ = c.execute("SELECT type FROM sqlite_master WHERE name=?",
                        (table,)).fetchone()[0]
        check("E-DB-20 %s는 %s다 (두 경로를 다 탄다)" % (table, want_type),
              typ == want_type, typ)
        sel, key = schema_v3.rowid_parts(c, table)
        # **WHERE에 별칭이 아니라 진짜 참조 가능한 표현식이 들어가야 한다**
        check("E-DB-20b %s의 WHERE 키가 %s" % (table, "rowid" if typ == "table" else "_rowid"),
              key == ("rowid" if typ == "table" else "_rowid"), key)
        try:
            h, d, m = sync_sheets.rows_of(c, table, since_rowid=2)
            ok, detail = (len(d) == 3 and m == 5), (len(d), m)
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
        check("E-DB-21 %s 증분 조회가 실제로 돈다 (since=2 → 3행)" % table, ok, detail)
        check("E-DB-22 %s 헤더에 _rowid가 안 섞인다" % table,
              "_rowid" not in h, h[-2:] if h else h)
    # D69: pconn=conn — 별도 close 불필요
    shutil.rmtree(work, ignore_errors=True)


def context_tests():
    """문맥 문자열 (D51) — 접두사가 두 번 붙으면 같은 대상이 두 문맥으로 갈린다."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    intel_db = importlib.import_module("intel_db")
    co = intel_db.context_of
    check("E-CX-1 target에 이름만 있으면 접두사를 붙인다",
          co({"story": "brand-linesheet", "target": "로우클래식"}) == "brand:로우클래식")
    # 2026-08-04에 실제로 `brand:brand:2000아카이브스`가 들어갔다
    check("E-CX-2 접두사가 붙어 와도 두 번 붙이지 않는다",
          co({"story": "brand-linesheet", "target": "brand:로우클래식"}) == "brand:로우클래식")
    check("E-CX-3 market도 같다",
          co({"story": "market-scan", "target": "market:데님팬츠(여성)"})
          == "market:데님팬츠(여성)")
    # 다른 접두사는 의도일 수 있으니 건드리지 않는다
    check("E-CX-4 다른 접두사는 그대로 둔다",
          co({"story": "brand-linesheet", "target": "market:X"}) == "brand:market:X")
    check("E-CX-5 target이 비어도 죽지 않는다",
          co({"story": "ranking-snapshot"}) == "ranking:")


def blocker3r_tests():
    """PR #13 3라운드 Blocker 회귀 — 이관 가드·프록시 가드·lazy 판정 격리."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    intel_db = importlib.import_module("intel_db")
    migrate_v3 = importlib.import_module("migrate_v3")
    schema_v2 = importlib.import_module("schema_v2")
    work = Path(tempfile.mkdtemp(prefix="b3r-"))

    # ── B2: 이미 v3인 DB에 재실행하면 가드가 막는다 (obs_base는 v2·v3 공통이라
    #    구 가드는 통과했다 — product_categories가 v3 표식이다)
    v3db = str(work / "v3.db")
    intel_db.connect(v3db).close()
    guarded = False
    try:
        migrate_v3.migrate(v3db, str(work / "out.db"), str(work / "px.staging"))
    except SystemExit as e:
        guarded = "이미 v3" in str(e)
    check("E-MG-1 v3 정본 재실행은 가드가 막는다 (Blocker 2)", guarded)

    # ── B1: D69에서 proxy.db 분리가 폐기됨 — 이 테스트는 본 DB 내 proxy 테이블로 대체
    v2db = str(work / "v2.db")
    src = sqlite3.connect(v2db)
    src.executescript(schema_v2.SCHEMA_V2)
    src.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, site TEXT, story TEXT,"
                " target TEXT, collected_at TEXT, item_count INTEGER,"
                " source_total INTEGER, incomplete INTEGER, notes TEXT,"
                " raw_file TEXT, loaded_at TEXT)")
    src.execute("CREATE TABLE sync_state (table_name TEXT PRIMARY KEY,"
                " last_synced_key TEXT, updated_at TEXT)")
    src.execute("INSERT INTO sync_state VALUES ('observations', '0', 'x')")
    # D69: v2 정본 안의 프록시 표 — 이관이 데이터까지 본 DB(dst)로 옮기는지 본다
    src.execute("CREATE TABLE proxy_defs (proxy_name TEXT PRIMARY KEY, question TEXT,"
                " material TEXT, value_space TEXT, method TEXT, rules TEXT,"
                " created_at TEXT)")
    src.execute("CREATE TABLE proxy_cache (proxy_name TEXT, site TEXT,"
                " product_id TEXT, fingerprint TEXT, value TEXT, basis TEXT,"
                " judged_at TEXT)")
    src.execute("INSERT INTO proxy_defs (proxy_name, question, material, value_space,"
                " method, created_at) VALUES ('name_lang','언어?','name',"
                "'[\"한국어\",\"영문\"]','rule','2026-08-05')")
    src.execute("INSERT INTO proxy_cache VALUES ('name_lang','musinsa','P1','OLD',"
                "'영문','rule','2026-08-05 09:00:00')")
    src.commit(); src.close()
    # D69: proxy.db 분리 폐기 — 프록시 표는 이관된 본 DB 안에 생긴다
    live_proxy = work / "proxy.db"  # 레거시 경로, 테스트용
    live_proxy.write_text("살아있는 프록시 — 이관이 건드리면 안 된다")
    staging = str(work / "proxy.db.staging")
    migrate_v3.migrate(v2db, str(work / "v2-out.db"), staging)
    out = sqlite3.connect(str(work / "v2-out.db"))
    out_tables = {r[0] for r in out.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    n_defs = out.execute("SELECT COUNT(*) FROM proxy_defs").fetchone()[0]
    n_cache = out.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0]
    out.close()
    check("E-MG-2 D69: proxy 표와 데이터가 본 DB로 이관된다",
          {"proxy_defs", "proxy_cache", "proxy_history"} <= out_tables
          and n_defs == 1 and n_cache == 1,
          "defs=%d cache=%d %s" % (n_defs, n_cache,
                                   sorted(t for t in out_tables
                                          if t.startswith("proxy"))))
    check("E-MG-2b 프록시 경로 인자를 넘겨도 무시되고 레거시 파일은 안 건드린다 (D69)",
          live_proxy.read_text() == "살아있는 프록시 — 이관이 건드리면 안 된다")

    # ── B3: 이관에서 행이 떨어지면 그 표의 미러 진행점을 리셋한다
    g = sqlite3.connect(str(work / "g.db"))
    g.execute("CREATE TABLE sync_state (table_name TEXT PRIMARY KEY,"
              " last_synced_key TEXT, updated_at TEXT)")
    g.execute("INSERT INTO sync_state VALUES ('observations', '500', 'x')")
    migrate_v3._guard_sync_progress(g, "observations", 10, 10)
    check("E-MG-3 행 보존이면 진행점 유지",
          g.execute("SELECT last_synced_key FROM sync_state").fetchone()[0] == "500")
    migrate_v3._guard_sync_progress(g, "observations", 10, 9)
    check("E-MG-3b 드롭이 있으면 진행점 리셋 — 다음 미러가 전량 재동기화 (Blocker 3)",
          g.execute("SELECT last_synced_key FROM sync_state").fetchone()[0] is None)

    # ── B4(D69 개정): 닿지 않는 Turso 정본이면 proxy_auto가 조용히 성공하지 않고 죽는다
    cards_f = work / "cards.json"
    cards_f.write_text(json.dumps([{"proxy_name": "t", "material": "name",
                                    "method": "rule", "value_space": ["a"],
                                    "rules": [{"any": ["."], "value": "a"}]}]),
                       encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("PROXY_DB_URL", "INTEL_PROXY_DB")}  # D69: 이 변수들은 폐기됨
    # D69: PROXY_DB_URL 가드 폐기 — 프록시는 정본 안이다. 남는 계약은
    # "닿지 않는 Turso 정본이면 조용히 성공(임시 DB 판정)하지 않고 죽는다"다.
    # 드라이버 미설치 환경은 건너난다 — ModuleNotFoundError로 죽어도 통과하면
    # 회귀가 무력화된다(리뷰 반영 — 부분 문자열 매칭도 같은 이유로 버렸다).
    try:
        import libsql_experimental  # noqa: F401
        _has_libsql = True
    except ImportError:
        _has_libsql = False
    if _has_libsql:
        r = subprocess.run([sys.executable, str(SCRIPTS / "proxy_auto.py"),
                            "--db", "libsql://fake.turso.io", "--cards", str(cards_f),
                            "--out", str(work / "o.json")],
                           capture_output=True, text=True, env=env)
        _pa13_out = (r.stdout + r.stderr)
        check("E-PA-13 닿지 않는 Turso 정본이면 임시 DB로 새지 않고 죽는다 (Blocker 4·D69 개정)",
              r.returncode != 0 and not (work / "o.json").exists()
              and "ModuleNotFoundError" not in _pa13_out,
              "exit=%d %s" % (r.returncode, _pa13_out[:80]))
    else:
        print("  SKIP  E-PA-13 (libsql_experimental 미설치 — Turso 경로 검증 불가)")

    # ── B5: 깨진 카드 규칙이 collect()를 죽이지 않는다 (lazy 판정 격리)
    lzdb = str(work / "lz.db")
    conn = intel_db.connect(lzdb)
    conn.execute("INSERT INTO products (site, product_id, name) VALUES ('s','1','상품')")
    conn.execute("INSERT INTO observations (site, product_id, observed_at, context,"
                 " price_sale) VALUES ('s','1','2026-08-05 10:00:00','brand:t',100)")
    conn.commit()
    # D69: proxy가 본 DB에 통합 — 별칭 없이 같은 커넥션에 직접 넣는다
    conn.execute("INSERT INTO proxy_defs (proxy_name, material, value_space, method,"
                 " rules) VALUES ('bad_rule','name','[\"a\"]','rule',?)",
                 (json.dumps({"rules": [{"any": ["[unclosed"], "value": "a"}]}),))
    conn.commit(); conn.close()
    import importlib as _il
    intel_data = _il.import_module("intel_data")
    # D69: INTEL_PROXY_DB 환경변수 폐기 — proxy가 본 DB에 통합
    try:
        data = intel_data.collect(lzdb, None)
        ok_lz = all(it.get("px_bad_rule") is None for it in data["items"])
        check("E-LZ-1 깨진 정규식 카드가 리포트 파이프라인을 죽이지 않는다 (Blocker 5)",
              ok_lz, [it.get("px_bad_rule") for it in data["items"]])
    except Exception as e:
        check("E-LZ-1 깨진 정규식 카드가 리포트 파이프라인을 죽이지 않는다 (Blocker 5)",
              False, "%s: %s" % (type(e).__name__, e))
    shutil.rmtree(work, ignore_errors=True)


def check_run_tests():
    """팀 중복 수집 방지 (D67) — **시간대 회귀 포함** (PR #13 리뷰 Blocker).

    collected_at은 로컬 시간 문자열이다. 컷오프를 SQL `datetime('now')`(UTC)로
    계산하면 KST에서 창이 9시간 넓어진다 — "2시간 전 수집, 1시간 창"이 중복으로
    오판된다. 그 케이스를 고정한다.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    intel_db = importlib.import_module("intel_db")
    work = Path(tempfile.mkdtemp(prefix="ckrun-"))
    conn = intel_db.connect(str(work / "r.db"))
    two_h_ago = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO runs (run_id, site, story, target, collected_at) "
                 "VALUES ('R2H','musinsa','ranking-snapshot','스커트',?)", (two_h_ago,))
    conn.commit()
    dup = intel_db.check_duplicate_run(conn, "musinsa", "ranking-snapshot", "스커트", 3)
    check("E-DR-1 창 안(2시간 전, 창 3시간)의 수집은 중복으로 잡힌다",
          dup and dup["run_id"] == "R2H", dup)
    dup = intel_db.check_duplicate_run(conn, "musinsa", "ranking-snapshot", "스커트", 1)
    check("E-DR-2 창 밖(2시간 전, 창 1시간)은 중복이 아니다 — UTC 혼용이면 "
          "KST에서 여기가 중복으로 오판된다", dup is None, dup)
    check("E-DR-3 다른 target은 남의 일이다",
          intel_db.check_duplicate_run(conn, "musinsa", "ranking-snapshot", "바지", 24) is None)
    shutil.rmtree(work, ignore_errors=True)


def history_tests():
    """상품 정적 속성 이력 + 프록시 재판정 체인 (D68 · E-DB-37·38).

    핵심 시나리오: 같은 상품을 다른 이름으로 두 번 적재 → product_history에
    전이가 남고, name 재료 프록시 캐시가 재판정 대기 이력으로 옮겨지며, 다음
    판정이 그 대기 행을 완성한다.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    intel_db = importlib.import_module("intel_db")
    schema_v3 = importlib.import_module("schema_v3")
    work = Path(tempfile.mkdtemp(prefix="hist-"))
    db = str(work / "h.db")
    conn = intel_db.connect(db)

    # name 재료 프록시: 정의 + 첫 이름 기준 판정 1건
    # D69: proxy가 본 DB에 통합
    pconn = conn
    pconn.execute(
        "INSERT INTO proxy_defs (proxy_name, question, material, value_space, "
        "method, created_at) VALUES ('name_lang','언어?','name',"
        "'[\"한국어\",\"영문\"]','rule','2026-08-05')")
    pconn.execute("INSERT INTO proxy_cache VALUES "
                  "('name_lang','musinsa','P1','OLD NAME','영문','rule',"
                  "'2026-08-05 09:00:00')")
    pconn.commit()
    # D69: pconn=conn — 별도 close 불필요

    def load(name, cat, ts):
        raw = {"meta": {"schema_version": "1.0", "site": "musinsa",
                        "story": "brand-linesheet", "target": "T",
                        "collected_at": ts, "item_count": 1},
               "items": [{"product_id": "P1", "name": name, "brand": "브랜드",
                          "category": cat, "url": "https://a.test/1",
                          "image_url": "https://img.a.test/1.jpg",
                          "price_sale": 10000}]}
        f = work / f"{ts[-8:].replace(':', '')}.json"
        f.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        intel_db.load_file(conn, f, quiet=True, db_path=db)

    load("OLD NAME", "상의 > 티셔츠", "2026-08-05 10:00:00")
    load("OLD NAME", "상의 > 티셔츠", "2026-08-05 10:30:00")   # 같은 값 재수집
    check("E-DB-37 같은 값 재적재는 이력을 만들지 않는다",
          conn.execute("SELECT COUNT(*) FROM product_history").fetchone()[0] == 0)

    load("NEW NAME", "상의 > 티셔츠", "2026-08-05 11:00:00")   # 이름 변경
    rows = conn.execute("SELECT field, old_value, new_value, run_id "
                        "FROM product_history").fetchall()
    check("E-DB-37b 다른 이름 재적재 → name 전이 1행 (run_id 연결)",
          len(rows) == 1 and rows[0]["field"] == "name"
          and rows[0]["old_value"] == "OLD NAME"
          and rows[0]["new_value"] == "NEW NAME"
          and isinstance(rows[0]["run_id"], int),
          [dict(r) for r in rows])
    v = conn.execute("SELECT field, old_value, new_value FROM product_changes "
                     "WHERE site='musinsa' AND product_id='P1'").fetchone()
    check("E-DB-37c product_changes 뷰가 (site, product_id)로 편다",
          v and v["field"] == "name" and v["old_value"] == "OLD NAME", tuple(v or ()))

    # 프록시 무효화: 캐시가 비고 대기 이력(new_value NULL)이 남았다
    pconn = conn  # D69: 본 DB 통합
    left = pconn.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0]
    ph = pconn.execute("SELECT old_value, new_value, old_fingerprint, "
                       "new_fingerprint FROM proxy_history").fetchall()
    check("E-DB-38 이름 변경이 name 재료 프록시 캐시를 무효화한다 (캐시 0행)",
          left == 0, left)
    check("E-DB-38b 옛 판정은 재판정 대기 이력으로 남는다 (new_value NULL)",
          len(ph) == 1 and ph[0]["old_value"] == "영문"
          and ph[0]["new_value"] is None
          and ph[0]["new_fingerprint"] == "NEW NAME", [dict(r) for r in ph])

    # 재판정(다음 판정)이 대기 행을 완성한다
    schema_v3.record_proxy_transition(pconn, "name_lang", "musinsa", "P1",
                                      "NEW NAME", "영문", "2026-08-05 12:00:00")
    pconn.execute("INSERT OR IGNORE INTO proxy_cache VALUES "
                  "('name_lang','musinsa','P1','NEW NAME','영문','rule',"
                  "'2026-08-05 12:00:00')")
    pconn.commit()
    ph = pconn.execute("SELECT new_value FROM proxy_history").fetchall()
    check("E-DB-38c 재판정이 대기 행을 완성한다 (행 추가 없이 new_value 채움)",
          len(ph) == 1 and ph[0][0] == "영문", [tuple(r) for r in ph])
    # 같은 값·같은 지문 재판정은 이력을 만들지 않는다
    schema_v3.record_proxy_transition(pconn, "name_lang", "musinsa", "P1",
                                      "NEW NAME", "영문", "2026-08-05 13:00:00")
    check("E-DB-38d 같은 값·지문 재판정은 이력 무변화",
          pconn.execute("SELECT COUNT(*) FROM proxy_history").fetchone()[0] == 1)
    # 같은 지문·다른 값(정정 재판정) — 이력이 남고 캐시도 새 값을 서빙해야 한다.
    # 옛 지문만 지우면 옛 값 행이 PK 충돌로 살아남아 캐시가 옛 값에 고정된다
    # (PR #14 리뷰 Blocker — 호출부 둘 다 INSERT OR IGNORE/IntegrityError 무시).
    schema_v3.record_proxy_transition(pconn, "name_lang", "musinsa", "P1",
                                      "NEW NAME", "한국어", "2026-08-05 14:00:00")
    pconn.execute("INSERT OR IGNORE INTO proxy_cache VALUES "
                  "('name_lang','musinsa','P1','NEW NAME','한국어','rule',"
                  "'2026-08-05 14:00:00')")
    pconn.commit()
    cur = pconn.execute("SELECT value FROM proxy_cache WHERE proxy_name='name_lang' "
                        "AND site='musinsa' AND product_id='P1'").fetchall()
    ph2 = pconn.execute("SELECT old_value, new_value FROM proxy_history "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    check("E-DB-38e 같은 지문·다른 값 정정 — 캐시가 새 값 하나만 서빙한다",
          len(cur) == 1 and cur[0][0] == "한국어", [tuple(r) for r in cur])
    check("E-DB-38f 정정 전이가 이력에 남는다 (영문→한국어)",
          ph2 and tuple(ph2) == ("영문", "한국어"), tuple(ph2 or ()))
    # D69: pconn=conn — 별도 close 불필요

    # 카테고리 재분류 — 기존 platform 매핑이 있는 상품에 처음 보는 리프
    load("NEW NAME", "상의 > 셔츠", "2026-08-05 14:00:00")
    cat = conn.execute("SELECT old_value, new_value FROM product_history "
                       "WHERE field='category'").fetchone()
    check("E-DB-37d 카테고리 재분류가 이력으로 남는다 (경로 문자열)",
          cat and cat["old_value"] == "상의 > 티셔츠"
          and cat["new_value"] == "상의 > 셔츠", tuple(cat or ()))

    # attr 이력 — 뷰 트리거 캡처라 어느 쓰기 경로든 잡힌다
    conn.execute("INSERT INTO product_attributes (site, product_id, attr_name, "
                 "value, basis, decided_at) VALUES ('musinsa','P1','핏','와이드',"
                 "'image','2026-08-05 10:00:00')")
    conn.execute("INSERT INTO product_attributes (site, product_id, attr_name, "
                 "value, basis, decided_at) VALUES ('musinsa','P1','핏','스트레이트',"
                 "'image','2026-08-05 15:00:00')")
    conn.commit()
    ah = conn.execute("SELECT attr_name, old_value, new_value FROM attr_changes "
                      "WHERE site='musinsa' AND product_id='P1'").fetchall()
    check("E-DB-37e attr 변경이 attr_history에 남는다 (뷰 트리거 캡처)",
          len(ah) == 1 and ah[0]["old_value"] == "와이드"
          and ah[0]["new_value"] == "스트레이트", [dict(r) for r in ah])

    # 멱등 업그레이드 — 이력 표를 지우고 다시 connect하면 되살아난다 (라이브 v3 경로).
    # D69: 스키마 마커가 proxy_history로 옮겨졌다 — 구 v3 DB(프록시 표 이전)를
    # 흉내내려면 프록시 표까지 같이 지워야 마커 부재 업그레이드가 발동한다
    conn.executescript("DROP VIEW product_changes; DROP VIEW attr_changes;"
                       "DROP TABLE product_history; DROP TABLE attr_history;"
                       "DROP TABLE proxy_history; DROP TABLE proxy_cache;"
                       "DROP TABLE proxy_defs;")
    conn.commit()
    conn.close()
    conn = intel_db.connect(db)
    tabs = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN "
        "('product_history','attr_history','product_changes','attr_changes',"
        "'proxy_defs','proxy_cache','proxy_history')")}
    check("E-DB-37f 기존 v3 DB도 connect()가 이력 표·뷰를 멱등 생성한다",
          len(tabs) == 7, sorted(tabs))
    # 업그레이드는 1회다 — 표가 갖춰진 뒤에는 connect가 SCHEMA_V3를 다시 보내지
    # 않는다 (PR #14 리뷰: 매 connect마다 DDL 27줄을 Turso로 왕복시키면 안 된다)
    check("E-DB-37g 표가 갖춰지면 스키마 업그레이드가 더는 필요 없다",
          intel_db._needs_schema_upgrade(conn) is False)
    conn.close()
    shutil.rmtree(work, ignore_errors=True)


def modeling_role_tests():
    """그룹 비교의 Y 선정 (D51) — 공급자가 정한 값은 Y로 안 쓴다."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    d = importlib.import_module("intel_data")
    check("E-MD-25 브랜드 표기 정규화 — 공백·대소문자만 걷어낸다",
          d.brand_key("2000 Archives") == d.brand_key("2000Archives")
          == d.brand_key("2000-archives"), d.brand_key("2000 Archives"))
    # 한글과 영문은 다른 키다 — 음차 매칭은 근거가 없다
    check("E-MD-26 한글·영문은 묶지 않는다",
          d.brand_key("2000아카이브스") != d.brand_key("2000Archives"))
    check("E-MD-27 빈 값은 None", d.brand_key(None) is None and d.brand_key("") is None)


def axis_hygiene_tests():
    """축 위생 (D55) — 표기 변형이 그룹을 가르지 않고, 한 청중이 요약을 독점하지 않는다."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    d = importlib.import_module("intel_data")
    an = importlib.import_module("analyze")
    ins = importlib.import_module("insight")

    # ── 카테고리 표기 변형 (2026-08-04 여성 데님 리포트 강한 주장 10번) ──
    # `데님팬츠`와 `데님 팬츠`가 다른 그룹이 되어 서로 비교됐다.
    check("E-MD-34 카테고리 안쪽 공백만 다른 값은 같은 그룹이다",
          d.brand_key("데님 팬츠") == d.brand_key("데님팬츠"))
    check("E-MD-35 계층 표기는 리프와 안 합쳐진다",
          d.brand_key("의류 > 바지 > 청/데님 팬츠") != d.brand_key("데님팬츠"))

    # 그룹 비교가 실제로 그 키를 쓰는지 — 정규화가 함수에만 있고 호출부에 없으면
    # 테스트는 통과하는데 리포트는 그대로 갈린다(이 버그가 정확히 그랬다).
    # **run_group()을 실제로 태운다** (PR #12 2차 리뷰) — 전신은 brand_key()를
    # 다시 계산해 항상 통과했다. 호출부가 정규화를 빼먹으면 여기서 잡혀야 한다.
    items = ([{"site": "s", "product_id": "a%d" % i, "category": "데님 팬츠",
               "like_count": 10 + i} for i in range(25)]
             + [{"site": "s", "product_id": "b%d" % i, "category": "데님팬츠",
                 "like_count": 200 + i} for i in range(25)]
             + [{"site": "s", "product_id": "c%d" % i, "category": "스커트",
                 "like_count": 50 + i} for i in range(25)])
    ctx = {"styles": items, "items": items,
           "eda": {"nulls": [{"field": "like_count", "label": "하트",
                              "usable": True}]},
           "cat_axes": [("category", "카테고리")],
           "num_axes": [("like_count", "하트")],
           "labels": {"like_count": "하트", "category": "카테고리"},
           "hierarchy": {"anc": set(), "parent": {}, "umbrella": set()},
           "contexts": [], "own_brands": []}
    hyps = an.run_group(ctx)
    pairs = {(h["group_a"], h["group_b"]) for h in hyps}
    check("E-MD-36 표기 변형끼리는 서로 비교되지 않는다 (run_group 실호출)",
          not any({a_, b_} == {"데님 팬츠", "데님팬츠"} for a_, b_ in pairs), pairs)
    denim = [h for h in hyps if "데님" in (h.get("group_a") or "")
             or "데님" in (h.get("group_b") or "")]
    merged_n = {h["n_a"] if "데님" in h["group_a"] else h["n_b"] for h in denim}
    check("E-MD-36b 변형 50건이 한 그룹(n=50)으로 검정에 들어간다",
          merged_n == {50}, "비교 %d건, 데님 그룹 n=%s" % (len(hyps), merged_n))

    # ── 청중 편중 (강한 주장 10개가 전부 `마케팅`이던 것) ──
    hyps = [{"kind": "group_compare", "cat_field": "ax%d" % i, "metric": "like_count",
             "audience": "마케팅", "effect": 0.9 - i * 0.01} for i in range(30)]
    hyps += [{"kind": "group_compare", "cat_field": "bx%d" % i, "metric": "discount_rate",
              "audience": "판매전략", "effect": 0.5} for i in range(4)]
    hyps += [{"kind": "group_compare", "cat_field": "cx%d" % i, "metric": "fit",
              "audience": "디자인", "effect": 0.4} for i in range(3)]
    picked = ins._select(list(hyps), 10)
    from collections import Counter
    dist = Counter(h["audience"] for h in picked)
    check("E-MD-37 자리를 다 채운다 — 상한이 빈칸을 만들지 않는다", len(picked) == 10,
          len(picked))
    check("E-MD-38 한 청중이 요약을 독점하지 않는다",
          max(dist.values()) < len(picked), dict(dist))
    check("E-MD-39 후보가 남아돌아도 청중 상한을 넘지 않는다",
          dist["마케팅"] <= ins.AUDIENCE_CAP, dict(dist))
    assert an is not None      # 임포트가 깨지면 여기서 잡힌다


def readability_tests():
    """읽는 사람 기준 (D56) — 조사·숫자 위치·축 이름·액션 플랜."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    an = importlib.import_module("analyze")
    ins = importlib.import_module("insight")

    # 와/과는 은/는·이/가와 방향이 반대다 — `데님과`, `소재 미표기와`
    check("E-RD-1 받침 있으면 `과`", an._josa("데님", "와과") == "데님과",
          an._josa("데님", "와과"))
    check("E-RD-2 받침 없으면 `와`", an._josa("하트", "와과") == "하트와",
          an._josa("하트", "와과"))
    check("E-RD-3 은/는은 방향이 반대", an._josa("데님", "은는") == "데님은")

    # 주장 한 줄에 숫자를 넣지 않는다 — 중앙값은 카드/상세가 쓴다
    h = {"kind": "group_compare", "cat_field": "category", "cat_label": "카테고리",
         "group_a": "와이드", "group_b": "쇼츠", "metric": "like_count",
         "metric_label": "하트", "median_a": 277, "median_b": 104,
         "n": 1075, "effect": 0.33, "verdict": "strong", "audience": "마케팅"}
    claim = "%s에서 %s %s보다 %s 높다" % (
        h["cat_label"], an._josa(h["group_a"], "은는"), h["group_b"],
        an._josa(h["metric_label"], "이가"))
    check("E-RD-4 주장에 중앙값이 없다", "중앙값" not in claim and "277" not in claim, claim)

    # 액션 플랜 — 축마다 달라야 하고, 기전 추측이 들어가야 한다
    a_cat = ins.action_hint(h)
    a_site = ins.action_hint(dict(h, cat_field="site", cat_label="플랫폼",
                                  group_a="musinsa", group_b="29cm"))
    check("E-RD-5 액션 플랜이 여러 줄", len(a_cat) >= 4, len(a_cat))
    check("E-RD-6 축이 다르면 액션도 다르다", set(a_cat) != set(a_site))
    check("E-RD-7 기전 추측이 조건부로 들어간다",
          any("수 있다" in x for x in a_cat), a_cat[:2])
    a_px = ins.action_hint(dict(h, cat_field="px_denim_rise",
                                cat_label="밑위(라이즈)(AI 판정)"))
    check("E-RD-8 프록시 축에도 고유 액션이 붙는다",
          set(a_px) != set(a_cat) and any("수 있다" in x for x in a_px))

    # 제3자끼리 브랜드 비교는 안 낸다 — 우리 브랜드가 한쪽에 있어야 한다.
    # **표기 변형까지 자사로 친다** — `brand:` 문맥은 실제 브랜드 값을 다 걷어 온다.
    import intel_data as d
    own = {d.brand_key(b) for b in
           ["2000아카이브스", "2000Archives", "2000 Archives"]}
    check("E-RD-9 제3자 쌍은 걸린다",
          d.brand_key("에잇세컨즈") not in own and d.brand_key("잠뱅이") not in own)
    check("E-RD-10 영문 표기도 자사로 친다",
          d.brand_key("2000 Archives") in own and d.brand_key("2000archives") in own)
    check("E-RD-11 한글 표기도 자사로 친다", d.brand_key("2000아카이브스") in own)

    # 그룹 비교 근거 줄 — "상품 1개당"과 그룹별 n·중앙값이 카드에 박힌다 (D60)
    gh = {"kind": "group_compare", "group_a": "화이트/아이보리", "group_b": "유채색",
          "n_a": 83, "n_b": 208, "median_a": 12, "median_b": 45, "n": 291}
    note = ins._n_note(gh)
    check("E-RD-12 근거 줄이 상품 1개당임을 말한다", "상품 1개당" in note, note)
    check("E-RD-13 그룹별 n과 중앙값이 병기된다",
          "83" in note and "208" in note and "12" in note and "45" in note, note)
    check("E-RD-14 쌍체는 쌍 비교로 표기", "쌍" in ins._n_note({"kind": "paired", "n": 119}))

    # 코드 스팬은 폰트를 바꾸지 않는다 (D60 후속) — Courier에는 한글 글리프가 없어
    # `데님`이 ■■로 그려졌다(2026-08-04 실물 스크린샷). 「」로 감싼다.
    pd = importlib.import_module("pdf_doc")
    rendered = pd.md("`데님` 대 `소재 미표기`")
    check("E-RD-15 코드 스팬에 폰트 전환 없음", "Courier" not in rendered, rendered)
    check("E-RD-16 코드 스팬은 「」로 감싼다", "「데님」" in rendered, rendered)

    # 차이없음 문장의 조사 — "데님와"가 아니라 "데님과"
    nc = ins._null_claim({"kind": "group_compare", "cat_label": "상품명 속 소재(AI 판정)",
                          "group_a": "데님", "group_b": "소재 미표기",
                          "metric_label": "평점"})
    check("E-RD-17 차이없음 문장 조사", "데님과" in nc and "데님와" not in nc, nc)

    # 지표 성격(절대값/비율)이 근거 줄에 붙는다
    note = ins._n_note({"kind": "group_compare", "group_a": "a", "group_b": "b",
                        "n_a": 30, "n_b": 40, "median_a": 1, "median_b": 2,
                        "metric": "like_count", "metric_label": "하트", "n": 70})
    check("E-RD-18 하트는 누적 절대값으로 표기", "누적 절대값" in note, note)
    note2 = ins._n_note({"kind": "correlation", "x": "discount_rate", "y": "cvr_view_buy",
                         "x_label": "할인율(%)", "y_label": "조회→구매 전환(%)", "n": 100})
    check("E-RD-19 전환 축은 비율로 표기", "비율(%)" in note2, note2)


def proxy_space_tests():
    """값 공간 위생 3겹 (D53) — 적재 가드 · proxy-audit exit 계약 · 읽기 안전망.

    셋 다 [자동] 표기인데 자동 테스트가 없었다(PR #12 리뷰 3). exit 코드는
    subprocess로 본다 — cmd_proxy_audit의 **반환값**이 아니라 **프로세스 종료
    코드**가 계약이고, 정확히 그 배선이 끊겨 있었다(리뷰 1 — main()이 반환값을
    버려 오염을 찾아도 exit 0이었다).
    """
    import tempfile
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    work = Path(tempfile.mkdtemp())
    db = str(work / "px.db")
    run([SCRIPTS / "intel_db.py", "--db", db, "init"], work, db)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO products (site, product_id, name) VALUES ('musinsa','1','상품 A')")
    con.commit(); con.close()
    # D69: 프록시 표는 본 DB에 통합 — 새 커넥션으로 넣는다
    pcon = sqlite3.connect(db)
    pcon.execute("INSERT INTO proxy_defs (proxy_name, question, material, value_space, method, created_at) "
                 "VALUES ('name_lang','q','name','[\"영문\",\"한국어\"]','rule','2026-08-05')")
    pcon.execute("INSERT INTO proxy_cache VALUES ('name_lang','musinsa','1','상품 A','화성어','b','2026-08-05')")
    pcon.commit(); pcon.close()

    # E-PXS-2 — 오염이면 exit 2, --fix 후 0. 반환값이 아니라 프로세스 코드다
    r = run([SCRIPTS / "intel_db.py", "--db", db, "proxy-audit"], work, db)
    check("E-PXS-2 오염 DB에서 proxy-audit exit 2", r.returncode == 2,
          "exit=%d stdout=%s" % (r.returncode, r.stdout.strip()[:60]))
    r = run([SCRIPTS / "intel_db.py", "--db", db, "proxy-audit", "--fix"], work, db)
    check("E-PXS-2b --fix는 지우고 exit 0", r.returncode == 0, r.returncode)
    r = run([SCRIPTS / "intel_db.py", "--db", db, "proxy-audit"], work, db)
    check("E-PXS-2c 청소 후 exit 0", r.returncode == 0, r.returncode)

    # E-PXS-1 — 순서만 바뀐 재적재는 캐시를 안 지우고, 실질 변경은 지운다
    pcon = sqlite3.connect(db)  # D69: 본 DB 통합
    pcon.execute("INSERT INTO proxy_cache VALUES ('name_lang','musinsa','1','상품 A','영문','b','2026-08-05')")
    pcon.commit(); pcon.close()
    reorder = work / "reorder.json"
    reorder.write_text(json.dumps({
        "proxy": {"proxy_name": "name_lang", "question": "q", "material": "name",
                  "value_space": ["한국어", "영문"], "method": "rule"},
        "judgments": []}, ensure_ascii=False), encoding="utf-8")
    run([SCRIPTS / "intel_db.py", "--db", db, "proxy-load", reorder], work, db)
    pcon = sqlite3.connect(db)  # D69: 본 DB 통합
    n = pcon.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0]
    pcon.close()
    check("E-PXS-1 원소 순서만 바뀐 값 공간은 캐시를 지우지 않는다", n == 1, n)
    real = work / "real.json"
    real.write_text(json.dumps({
        "proxy": {"proxy_name": "name_lang", "question": "q", "material": "name",
                  "value_space": ["한국어", "영문", "혼합"], "method": "rule"},
        "judgments": []}, ensure_ascii=False), encoding="utf-8")
    r = run([SCRIPTS / "intel_db.py", "--db", db, "proxy-load", real], work, db)
    pcon = sqlite3.connect(db)  # D69: 본 DB 통합
    n = pcon.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0]
    pcon.close()
    check("E-PXS-1b 실질 변경은 옛 캐시를 버리고 그 사실을 찍는다",
          n == 0 and "버린다" in r.stdout, "n=%d stdout=%s" % (n, r.stdout.strip()[:60]))

    # E-PXS-3 — 오염이 남은 채 분석에 들어가면 그 축을 통째로 뺀다 (읽기 안전망)
    pcon = sqlite3.connect(db)  # D69: 본 DB 통합
    pcon.execute("INSERT INTO proxy_cache VALUES "
                 "('name_lang','musinsa','1','상품 A','금성어','b','2026-08-05')")
    pcon.commit(); pcon.close()
    con = sqlite3.connect(db)
    con.execute("INSERT INTO observations (site, product_id, observed_at, context, price_sale) "
                "VALUES ('musinsa','1','2026-08-05 10:00:00','brand:테스트',1000)")
    con.commit(); con.close()
    intel_data = importlib.import_module("intel_data")
    data = intel_data.collect(db, None)
    meta = next(p for p in data["meta"]["proxies"] if p["name"] == "name_lang")
    check("E-PXS-3 값 공간 밖 값이 남은 축은 전 상품 미판정 처리",
          all(it.get("px_name_lang") is None for it in data["items"]),
          [it.get("px_name_lang") for it in data["items"]])
    check("E-PXS-3b 왜 빠졌는지 off_space로 남긴다", "금성어" in (meta.get("off_space") or []),
          meta.get("off_space"))
    shutil.rmtree(work, ignore_errors=True)


def survival_tests():
    """생존 편향 (D54) — 이탈 판정과 동적 속성 축."""
    sys.path.insert(0, str(SCRIPTS))
    import importlib.util
    path = ROOT / "data" / ".tools" / "musinsa_brand_survival.py"
    if not path.exists():
        print("  SKIP  생존 — 파일 없음")
        return
    spec = importlib.util.spec_from_file_location("bs", str(path))
    bs = importlib.util.module_from_spec(spec); spec.loader.exec_module(bs)

    items = [{"name": "A", "survival": "survivor"}, {"name": "B", "survival": "dropped"}]
    prods = [{"product_id": "1", "brand": "A"}, {"product_id": "2", "brand": "B"},
             {"product_id": "3", "brand": "모르는브랜드"}]
    rows = bs.to_attrs(items, prods)
    check("E-SV-1 생존 상태를 상품 속성으로 편다", len(rows) == 2, len(rows))
    check("E-SV-2 랭킹에 없는 브랜드는 만들지 않는다 (모른다 ≠ 이탈)",
          all(r["product_id"] != "3" for r in rows))
    check("E-SV-3 이탈 상품도 포함된다 — 이걸 빼면 생존자만 본다",
          any(r["value"] == "dropped" for r in rows))
    # 랭킹은 계속 바뀐다 — 오래 들고 있으면 옛 상태로 분석하게 된다
    check("E-SV-4 TTL이 짧다 (7일)", all(r["ttl_days"] == 7 for r in rows))

    d = importlib.import_module("intel_data")
    data = {"items": [{"attr_brand_survival": "survivor"},
                      {"attr_brand_survival": "dropped"},
                      {"attr_only_one": "x"}, {"attr_only_one": "x"}]}
    ax = dict(d.attr_axes(data))
    check("E-SV-5 값이 2종 이상인 속성만 축이 된다",
          "attr_brand_survival" in ax and "attr_only_one" not in ax, list(ax))


def collector_tests():
    """수집기 (D48 · E-CO) — 네트워크에 붙지 않고 순수 함수만 검증한다.

    `plp()`는 HTTP를 타므로 여기서 부르지 않는다. **네트워크 없이 확인할 수 있는
    것**을 고정한다: 쿼리 인코딩, 커버리지 판정, 카드 매핑.
    """
    import importlib.util
    import urllib.parse
    path = ROOT / "data" / ".tools" / "musinsa_collect.py"
    if not path.exists():
        print("  SKIP  수집기 — 파일 없음")
        return
    spec = importlib.util.spec_from_file_location("musinsa_collect", str(path))
    mc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mc)

    # E-CO-4 값을 그대로 붙이면 한글·& 에서 쿼리가 깨진다
    q = urllib.parse.urlencode({"brand": "한글슬러그", "sortCode": "A&B", "page": "1"})
    check("E-CO-4 쿼리 파라미터가 인코딩된다",
          "%" in q and "&B" not in q.replace("%26B", ""), q)

    # E-CO-3 총계 0에서 커버리지 판정이 죽지 않는다
    for total, items, want in [(None, 0, "모름"), (0, 0, "0"), (100, 50, "50.0"),
                               (100, 100, "100.0")]:
        cov = (100.0 * items / total) if total else None
        try:
            if cov is None:
                note = "" if total is None else "총계 0"
            else:
                note = "%.1f%%%s" % (cov, "" if cov >= 99.0 else " 미완주")
            ok = True
        except Exception as e:
            ok, note = False, "%s: %s" % (type(e).__name__, e)
        check("E-CO-3 커버리지 판정이 total=%r에서 안 죽는다" % total, ok, note)

    # E-CO 카드 매핑 — reviewScore는 100점 척도다(94 = 4.7)
    it = mc._plp_item({"goodsNo": 1, "goodsName": "n", "reviewScore": 94,
                       "reviewCount": 5, "normalPrice": 1000, "finalPrice": 700,
                       "finalDiscount": 30, "isSoldOut": False, "isAd": True})
    check("E-CO 평점을 20으로 나눈다 (94 → 4.7)", it["rating"] == 4.7, it["rating"])
    check("E-CO 광고 슬롯을 raw_extras에 남긴다",
          it["raw_extras"]["is_ad"] is True, it["raw_extras"])
    none_score = mc._plp_item({"goodsNo": 2, "reviewScore": None})
    check("E-CO 평점이 없으면 만들지 않는다 (0으로 채우지 않는다)",
          none_score["rating"] is None, none_score["rating"])


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
    # 위 [6]은 폐기됨 HTML(build_analysis_report)만 봐서, D27 리팩터링 때 새
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

    print("[16] 스키마 v3 — 뷰 읽기·쓰기 + v3 계약 (D45·D65)")
    schema_v3_tests()

    print("[17] 솎기 — 변화 순간 보존 (D45-a)")
    prune_tests()

    print("[18] 모델링 — Y 선정·퍼널·무영향 (D47)")
    modeling_tests()

    print("[19] 증분 키 — 실제 sqlite3 (뷰·물리 테이블)")
    incremental_key_tests()

    print("[20] 수집기 — 인코딩·커버리지·카드 매핑 (D48)")
    collector_tests()

    print("[21-a] 생존 편향 — 이탈 판정·동적 축 (D54)")
    survival_tests()

    print("[21] 문맥 문자열·역할 규칙 (D51)")
    context_tests()
    modeling_role_tests()

    print("[21c] 팀 중복 수집 방지 — 시간대 회귀 (D67 · E-DR)")
    check_run_tests()

    print("[21d] 3라운드 Blocker 회귀 — 이관·프록시 가드·lazy 격리 (E-MG·E-PA-13·E-LZ)")
    blocker3r_tests()

    print("[21e] 상품 이력 + 프록시 재판정 체인 (D68 · E-DB-37·38)")
    history_tests()

    print("[21b] 프록시 값 공간 위생 (D53 · E-PXS)")
    proxy_space_tests()

    print("[22] 축 위생 — 표기 변형·청중 편중 (D55)")
    axis_hygiene_tests()

    print("[23] 읽는 사람 기준 — 조사·축 이름·액션 플랜 (D56)")
    readability_tests()

    shutil.rmtree(work, ignore_errors=True)
    print("-" * 56)
    print(f"통과 {passed} · 실패 {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
