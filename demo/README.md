# commerce-intel 4단계 데모

수집 요청 → DB 업데이트 → 분석 요청 → 레포트 생성을 한 화면에서 시연하는 로컬 웹 UI.
의존성 없음(파이썬 표준 라이브러리 + `claude` CLI).

```bash
python3 demo/server.py            # http://127.0.0.1:8765
python3 demo/server.py --port N
INTEL_DEMO_DB=/path/copy.db python3 demo/server.py   # 리허설용 DB 복사본 지정
```

| 단계 | 입력 | 실행 주체 |
|---|---|---|
| ① 수집 요청 | 자연어 | `claude -p` 헤드리스가 해석 — 재사용 판정(TTL·스킵 창)이 먼저고, 필요할 때만 수집(랭킹은 `snap_ranking_any.py`, 라인시트는 플랫폼 스킬 절차) |
| ② DB 업데이트 | 버튼 2개 | **정본 DB 적재**: `intel_db.py import-snapshots`(중복 스킵·멱등) + `load`(①의 라인시트 등) + 안전망(보고 안 된 `data/raw/` 라인시트를 검증 후 적재) + `stats`. **구글 시트 업로드**: `sync_sheets.py`(단방향 미러) — **정본 적재를 마쳐야 활성화되고**(화면·서버 양쪽에서 검사), 실패해도 정본은 유효하며 다음 업로드가 따라잡는다 |
| ③ 분석 요청 | 자연어 | `claude -p`가 문맥 선정 → `--emit-json`으로 데이터 확인 → 소견 요약 |
| ④ 레포트 생성 | 문맥(③에서 자동 입력, 비면 가장 최근 수집 문맥) | `build_analysis_report.py`(분석 대시보드 — DB)는 항상, `brand:…` 문맥이면 `build_report.py`(라인시트 — `data/raw/`의 사이트별 최신 수집분)도 함께 → `/output/…html` 링크 1~2개 |

## 트러블슈팅

모든 잡(4단계 전부)은 완료 시 `data/demo-logs/jobs.jsonl`에 한 줄씩 남는다 —
단계 번호·사용자 입력·상태·시각·로그 전문·결과, 그리고 ①③은 헤드리스 세션 ID까지.
데모 후 Claude Code 세션에서 "데모 로그 트러블슈팅해줘"라고 하면 이 파일로 원인을 추적할 수 있고,
헤드리스 세션의 전체 트랜스크립트는
`~/.claude/projects/-Users-2000atelier-workspace-commerce-intel/<세션ID>.jsonl`에 있다.

주의:
- 헤드리스 세션은 권한 프롬프트에 멈추지 않도록 `--allowedTools Bash Read Glob Grep Write Edit`로
  실행된다. 로컬 데모 전제 — 서버는 127.0.0.1에만 바인딩되며 외부 노출 금지.
- ①은 세 스토리(라인시트·전수조사·랭킹)를 모두 지원한다. 전수조사는 `--count-only`로 규모를
  먼저 보고하고 진행하며, 표본 수집은 사용자가 명시했을 때만 `plan_sample.py`로 계획한다(D21·D22).
- ②를 실제 DB에 처음 실행하면 기존 축적분 소급 적재(CLAUDE.md 열린 항목 3)가 함께 수행된다.
