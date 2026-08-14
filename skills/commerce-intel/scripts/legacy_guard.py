#!/usr/bin/env python3
"""폐기된 Turso 경로를 실수로 실행하는 것을 막는다 (2026-08-13).

## 왜 있나

정본 DB가 **Turso(libSQL) → Google Sheets로 옮겨졌다** (intel-query SKILL.md v2.0,
2026-08-10). 전환 사유는 Turso 무료 티어의 읽기 차단이었다.

그런데 이 레포의 코드는 전환을 따라오지 않았다 — `sync_sheets.py`·`prune.py`·
`intel_db.py load`는 여전히 `schema_v3.default_db_target()`(=`INTEL_DB_URL`,
`libsql://...`)을 정본으로 보고 동작한다. 지금 이것들을 돌리면:

  · `sync_sheets.py` — **Turso → 시트** 방향으로 쓴다. 정본이 시트가 된 지금은
    방향이 거꾸로다. Turso에 멈춰 있는 옛 스냅샷(마지막 성공 2026-08-07)으로
    최신 시트를 덮어쓴다. **읽기 차단으로 실패해온 것이 오히려 사고를 막았다.**
  · `prune.py` — 시트가 아니라 Turso의 관측을 솎는다. 정본에 아무 효과가 없다.
  · `intel_db.py load` — 수집 결과를 Turso에 적재한다. 시트 정본에 반영되지 않는다.

## 지금의 대체 경로

전부 Aside 루틴 + `intel-query` 스킬(Google Sheets API)로 넘어갔다:

  · 수집·적재      → 루틴 `JRVcsK61lr4EOhwi` (매일 02·08·14·20시)
  · 솎기(용량 관리) → 루틴 `vlWHt4HN0nYwK0vj` (매일 04시, 계산·보고만 — 삭제는 승인 후)
  · 백업           → 루틴 `6pUxuVdZ2rzhz43e` (일요일 05시, 레포 밖에 CSV)
  · 시트 미러링    → **불필요**. 시트가 곧 정본이다.

## 그래도 돌려야 한다면

과거 Turso DB를 되살려 조회하는 등 의도가 분명할 때만:

    INTEL_ALLOW_TURSO_LEGACY=1 python3 sync_sheets.py ...

**시트를 덮어쓸 수 있다는 것을 이해한 상태에서만 쓴다.**

## 삭제하지 않고 남겨둔 이유

코드 자체는 멀쩡하고, 팀원이 여러 명 붙어 동시 수집하는 단계가 되면 SQL 정본이
다시 필요해질 수 있다(시트는 10M 셀 한도와 동시 쓰기 제약이 있다). 그때 이
경로를 되살리는 편이 새로 쓰는 것보다 싸다 — pipeline/pi/sync.sh를 폐기 주석만
달고 남겨둔 것과 같은 판단이다.
"""
import os
import sys

_ENV_OVERRIDE = "INTEL_ALLOW_TURSO_LEGACY"

_MESSAGE = """\
✗ {name}은(는) 폐기된 경로다 — 정본 DB는 더 이상 Turso가 아니다.

  정본: Google Sheets (intel-query SKILL.md v2.0, 2026-08-10~)
        스프레드시트 ID는 data/sheets_config.json에 있다
        (이 레포는 public이라 .gitignore로 빼둔다 — 문서·코드에 적지 말 것)

  {risk}

  대체 경로:
    수집·적재       Aside 루틴 JRVcsK61lr4EOhwi (매일 02·08·14·20시)
    솎기/용량 관리   Aside 루틴 vlWHt4HN0nYwK0vj (매일 04시, 보고만)
    백업            Aside 루틴 6pUxuVdZ2rzhz43e (일요일 05시)
    조회·편집       intel-query 스킬 (~/.aside/u/0/skills/user/intel-query/SKILL.md)

  의도한 실행이라면:  {env}=1 을 붙여라.
  자세한 배경은 scripts/legacy_guard.py 문서 주석에 있다.
"""

_RISKS = {
    "sync_sheets.py": (
        "위험: 이 스크립트는 Turso → 시트 방향으로 쓴다. 정본이 시트인 지금은\n"
        "  방향이 거꾸로이고, Turso에 멈춰 있는 2026-08-07 스냅샷으로 최신\n"
        "  시트를 덮어쓴다. 시트가 곧 정본이므로 미러링 자체가 불필요하다."
    ),
    "prune.py": (
        "위험: 시트가 아니라 Turso의 관측을 솎는다 — 정본에 아무 효과가 없다.\n"
        "  시트 쪽 솎기는 루틴 vlWHt4HN0nYwK0vj가 계산해 보고한다."
    ),
    "intel_db.py load": (
        "위험: 수집 결과를 Turso에 적재한다 — 시트 정본에 반영되지 않는다.\n"
        "  적재는 수집 루틴이 Sheets API로 직접 한다."
    ),
}


def block_if_legacy(name: str, target: "str | None" = None) -> None:
    """폐기된 경로면 안내를 찍고 exit 4. 환경변수 %s=1로 우회한다.

    target을 주면 **그게 libsql:// URL일 때만** 막는다 — 로컬 SQLite를
    대상으로 하는 개발·회귀 실행은 정본과 무관하므로 막을 이유가 없다.
    (회귀 테스트가 `intel_db.py --db <임시파일> proxy-load`를 서브프로세스로
    부른다 — 무조건 막으면 그게 깨진다.)

    target을 생략하면 무조건 막는다 — sync_sheets.py처럼 소스 DB와 무관하게
    라이브 스프레드시트를 덮어쓰는 경우가 그쌄다.
    """ % _ENV_OVERRIDE
    if target is not None and not str(target).startswith(("libsql://", "wss://", "https://")):
        return  # 로컬 SQLite 대상 — 정본과 무관하다
    if os.environ.get(_ENV_OVERRIDE) == "1":
        print(
            "⚠ %s=1 — 폐기된 Turso 경로를 의도적으로 실행한다. "
            "시트 정본과 어긋날 수 있다." % _ENV_OVERRIDE,
            file=sys.stderr,
        )
        return
    risk = _RISKS.get(name, "위험: 시트 정본과 어긋난 결과가 나온다.")
    print(_MESSAGE.format(name=name, risk=risk, env=_ENV_OVERRIDE), file=sys.stderr)
    sys.exit(4)
