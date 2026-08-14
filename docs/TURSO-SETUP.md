# Turso 이전·팀원 온보딩 (D67) — ⚠️ 폐기됨 (2026-08-13)

> **이 문서는 역사적 기록이다. 따라 하지 말 것.**
>
> 정본 DB는 **Google Sheets**로 옮겨갔다 (`intel-query` SKILL.md v2.0,
> 2026-08-10). 전환 사유가 바로 여기 적힌 Turso 무료 티어의 **읽기 차단**이다
> (`SQL read operations are forbidden`). 즉 그 차단은 고쳐야 할 장애가 아니라
> 이미 전환을 끝낸 원인이다 — **대시보드 확인이나 플랜 업그레이드로
> 되살리려 하지 말 것.**
>
> 현재 운영 경로는 전부 Aside 루틴 + `intel-query` 스킬(Google Sheets API)이다:
> 수집·적재 `JRVcsK61lr4EOhwi` / 용량 감시·솞기 제안 `vlWHt4HN0nYwK0vj` /
> 주간 백업 `6pUxuVdZ2rzhz43e`. 시트 미러링은 시트가 곳 정본이므로 불필요하다.
>
> `sync_sheets.py`·`prune.py`·`intel_db.py load`는 `legacy_guard.py`가 막고 있다
> (Turso URL을 대상으로 할 때만). 삭제하지 않고 남긴 이유는 그 파일 주석에 있다.

--- 이하 원문 (2026-08-05 작성) ---

정본 DB를 로컬 SQLite에서 Turso(libSQL) 클라우드로 옮겨 **팀원 여러 명이 하나의
정본에 수집·조회**하게 한다. 환경변수가 없으면 모든 도구는 지금처럼 로컬
`data/intel.db`로 돈다 — 테스트·CI·오프라인 개발은 아무것도 바뀌지 않는다.

## 0. 전제

- 로컬 정본이 **v3 스키마**여야 한다 (`migrate_v3.py` — SPEC D65). v2 파일을 올리면 안 된다.
- 파이썬 드라이버 `libsql-experimental`은 **시스템 파이썬 3.9에서 빌드가 실패한다**
  (실측 2026-08-05). `uv`로 3.12 이상을 쓴다:

  ```bash
  uv venv -p 3.12 ~/.venvs/intel && ~/.venvs/intel/bin/pip install libsql-experimental gspread
  # 이후 python3 대신 ~/.venvs/intel/bin/python 사용 (Turso 모드일 때만 필요)
  ```

## 1. Turso 세팅 (관리자 1회)

```bash
brew install tursodatabase/tap/turso
turso auth login                                  # 브라우저 로그인
turso db create commerce-intel
# D69: commerce-intel-proxy 별도 DB 폐기 — 프록시 테이블이 본 DB에 통합됨

# 토큰 — 용도별로 가른다
turso db tokens create commerce-intel             # 쓰기+읽기 → 수집·적재·시트 미러용
turso db tokens create commerce-intel --read-only # 읽기 전용 → 챗 인터페이스(intel-query)용

turso db show commerce-intel --url                # libsql://commerce-intel-xxx.turso.io

# (선택) 실연결 테스트 전용 DB — tests/test_turso_connect.py C층이 여기에만 쓴다.
# 공유 정본에 테스트가 표를 만들었다 지우게 하지 않는다
turso db create commerce-intel-test
```

> **시트 미러는 읽기 전용 토큰으로 못 돈다** — `sync_state`(미러 진행점)를 DB에
> 쓴다. 미러를 도는 계정은 쓰기 토큰을 쓴다. 읽기 전용 토큰의 용도는
> intel-query(챗 조회)와 순수 조회 도구다.

## 2. 데이터 이관 (관리자 1회)

```bash
# 정본 (v3 파일 → Turso). uv 파이썬으로 실행
~/.venvs/intel/bin/python tools/upload_to_turso.py --src data/intel.db \
    --url libsql://commerce-intel-xxx.turso.io --token <쓰기토큰>

# D69: 프록시 테이블은 본 DB에 포함 — 별도 업로드 불필요
```

행 수 검산까지 통과해야 이관 완료다. 원격이 비어 있지 않으면 스크립트가 거부한다
(이중 업로드 방지).

## 3. 환경변수 (팀원 각자)

`~/.config/intel/env` 파일로 관리한다:

```bash
# ~/.config/intel/env — 수집·적재를 하는 사람 (쓰기 토큰)
export INTEL_DB_URL=libsql://commerce-intel-xxx.turso.io
export INTEL_DB_TOKEN=eyJ...
# D69: PROXY_DB_URL/PROXY_DB_TOKEN 폐기 — 프록시가 본 DB에 통합됨
```

셸 rc에 `source ~/.config/intel/env`를 넣거나, 작업 전에 수동으로 source 한다.

우선순위: **명시적 `--db` 인자 > `INTEL_DB_URL` > `INTEL_DB`(로컬 경로) >
`data/intel.db`**. 즉:

- 환경변수를 지우면(또는 `--db data/intel.db`를 명시하면) 로컬 SQLite 모드 —
  개발·테스트·리허설이 클라우드를 건드리지 않는다
- 테스트가 임시 파일을 `--db`로 넘기므로, 환경변수가 있어도 테스트는 로컬로 돈다

## 4. 팀 중복 수집 방지

클라우드 정본에서는 `runs`가 팀 전체의 수집 이력이다. 수집 시작 전에:

```bash
python3 skills/commerce-intel/scripts/intel_db.py check-run \
    --site musinsa --story ranking-snapshot --target 스커트   # exit 1이면 최근 수집 있음
```

기존 `check --team`(시트 우회 조회 — D32)은 공유 DB에서는 불필요해진다 —
`check`의 로컬 판정이 곧 팀 판정이다.

## 5. 로컬 SQLite 모드로 전환 (개발·테스트)

```bash
unset INTEL_DB_URL INTEL_DB_TOKEN  # D69: PROXY_DB_* 폐기
# 또는 한 번만: python3 ... --db data/intel.db
```

`data/intel.db`는 지우지 않는다 — 로컬 백업 겸 개발용이다. Turso 이전 후에도
`upload_to_turso.py --verify-only`로 로컬 대비 행 수를 대조할 수 있다.

## 알려진 제약 (실측 포함)

- **Turso는 ATTACH DATABASE 미지원** — D69에서 프록시가 본 DB에 통합돼
  별도 커넥션이 불필요해졌다.
- **드라이버(libsql-experimental)는 sqlite3과 다르다** — row_factory 없음(튜플만),
  제약 위반이 IntegrityError가 아니라 ValueError, total_changes 없음, lastrowid
  불안정. `schema_v3.open_db()`의 호환 래퍼가 전부 흡수하므로 **DB는 반드시
  open_db()/intel_db.connect()로 열어라.** `libsql.connect()`를 직접 부르면
  이 지뢰들을 그대로 밟는다.
- PRAGMA foreign_keys는 커넥션마다 켠다 — open_db()가 모든 커넥션(로컬·libsql)에
  켠다(B2·B7). connect()는 open_db()를 거치므로 따로 켜지 않는다.
- 쓰기는 직렬화된다(서버 단일 라이터) — 대량 적재는 배치 커밋(5,000행)으로.
