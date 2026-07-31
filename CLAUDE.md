# commerce-intel

`commerce-browser-skill`의 **고도화판**이다(2026-07-30 분기). 무신사·29CM·자사몰(Cafe24)에서
공개 API 없이 브라우저로 상품·랭킹 데이터를 모아 리포트로 만드는 스킬과 그 도구들이 들어 있다.

## 이 저장소를 처음 여는 세션이 알아야 할 것

**결정의 근거는 세 곳에 있다. 추측하기 전에 여기를 읽어라.**

| 어디 | 무엇이 있나 |
|---|---|
| `docs/SPEC-INTEL.md` (**v1**) | **intel의 유일한 스펙이자 판단 기준.** 결정 기록 D1~D11 |
| `docs/EVIDENCE.md` | 실측 근거 아카이브 — 구 SPEC.md(v16, 삭제됨)에서 추출. 규범이 아니다 |
| `docs/TEST-CASES.md` (**v11**) | intel 기준 트리거링·완주 기준선·엣지 카탈로그 (2026-07-31 전면 개정) |
| `git log` | 왜 그렇게 했는지가 커밋 메시지에 남아 있다. 구 SPEC.md 전문도 이력에 있다 |

**⚠️ `docs/SPEC-INTEL.md`와 `docs/TEST-CASES.md`는 임의로 수정하지 않는다.** SDD로 진행하는
프로젝트라 이 둘이 기준점이다. 구현이 스펙과 어긋나면 **스펙을 고치지 말고 어긋난다는 사실을
보고하고 지시를 기다린다.** 사용자가 명시적으로 지시한 개정만 수행한다.

## 구성 (2026-07-31 재편 — 상세 근거는 SPEC-INTEL §5)

```
skills/                   intel 정본. 배포 단위 (package.sh가 이걸 묶는다)
├── commerce-intel/       오케스트레이터 — 재사용 판정·DB 적재·시트 미러·리포트·검수.
│   ├── references/       db-contract · story-catalog · analysis-report · sheets-sync · report-spec
│   └── scripts/          intel_db(정본 DB) · sync_sheets · build_analysis_report(대시보드)
│                         · 승계 4종(validate_data · build_report · diff_snapshots · group_variants)
├── platform-musinsa/     전용 플랫폼 스킬 3종 — SKILL.md + references/adapter.md(실측)
├── platform-29cm/        플랫폼 스킬은 데이터 계약 JSON 생산만 안다. DB·분석은 모른다
├── platform-ownmall/     자사몰 엔진 무관(D9) — engine-detect/cafe24/shopify(미검증)/unknown
├── platform-generic/     처음 보는 플랫폼 — recon-checklist(channel-scout와 공유) · common-traps
└── platform-skill-maker/ 정찰 실측 → platform-* 스킬 초안 생성 (템플릿 2종)
.claude/agents/channel-scout.md   입점처 리서치 서브 에이전트
data/.tools/              범용 수집기 (git 추적. 수집 데이터는 미추적)
├── scan_market_any.py    전수조사 — 이름만 대면 시작. --count-only로 규모부터 본다
├── snap_ranking_any.py   랭킹 스냅샷 — --cron으로 주기 등록까지
├── classify_fit.py       상품명 → 핏 1단계 분류
└── ranking_targets.json  두 플랫폼 카테고리 코드 실측 카탈로그 1,546개
tests/                    run_tests.py 회귀 111건 + test_intel_db.py 31건. 픽스처만 쓴다
```

- 정본 DB는 `data/intel.db`(SQLite), 구글 시트는 단방향 미러다 — 파이프라인은
  수집 → raw JSON → 검증 → DB 적재 → 시트 미러 → 리포트 (SPEC-INTEL §2·§3)
- 수집 원본은 `data/raw/`, 스냅샷은 `data/snapshots/`, 리포트는 `output/` — **전부 git 미추적**이다
- **원본의 축적 스냅샷 47개+를 그대로 가져왔다**(무신사 바지 35 · 29CM 여성슈즈 9 · 그 외).
  crontab 잡도 이 경로로 재등록됐으니 축적이 이어진다. `intel_db.py import-snapshots`로
  DB에 소급 적재할 수 있다
- 구 `commerce-research/`는 삭제됐다(D10) — 전문은 git 이력에 있다

## 원본 저장소와의 관계

```
origin   = https://github.com/whysowlee/commerce-intel.git            (private, 여기로 push)
upstream = https://github.com/whysowlee/commerce-browser-skill.git   (fetch 전용 — push URL 차단)
```

- 원본 개선 가져오기: `git fetch upstream && git merge upstream/main`
- **`git push upstream`은 일부러 막아뒀다** — 이 프로젝트 커밋이 원본 공개 레포로 새지 않게 한 것이다.
  푸시는 항상 `origin`으로 간다(`git push`가 그렇게 추적돼 있다)
- 이 레포는 **private**이다. 공개하려면 `gh repo edit --visibility public`

## 이어서 할 일 (2026-07-31 기준 열린 항목)

1. **구글 시트 미러 설정** — 서비스 계정 발급·시트 공유·`data/sheets_config.json` 작성은
   사용자 1회 작업이다(`docs/SHEETS-SETUP.md`). 설정 전까지 미러만 밀리고 수집·분석은 정상
2. **코튼 팬츠 핏 분류율 48.1%** — 성공 기준 80% 미달. 남은 1,864건은 대표 이미지
   판단 1,257회가 필요하다(색상변형 그룹으로 32.6% 절감한 수치). 진행 여부는 사용자 결정 대기.
   ※ 이제 분류 결과가 DB에 적재되므로 TTL 90일 내 재사용된다 — 같은 판단을 반복하지 않는다
3. **기존 축적 스냅샷의 DB 소급 적재** — `intel_db.py import-snapshots data/snapshots`를
   실제 축적분에 1회 실행하면 대시보드가 축적 전체를 쓴다 (미실행 상태)
4. (해소) 구 SPEC 내부 모순 2건은 SPEC.md 삭제·EVIDENCE 추출(D11)로 소멸했다 —
   현행 규범은 SPEC-INTEL과 skills/가 갖고, 순환 검증 금지·`group` basis 모두 반영돼 있다
