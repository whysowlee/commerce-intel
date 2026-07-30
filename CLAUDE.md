# commerce-intel

`commerce-browser-skill`의 **고도화판**이다(2026-07-30 분기). 무신사·29CM·자사몰(Cafe24)에서
공개 API 없이 브라우저로 상품·랭킹 데이터를 모아 리포트로 만드는 스킬과 그 도구들이 들어 있다.

## 이 저장소를 처음 여는 세션이 알아야 할 것

**결정의 근거는 세 곳에 있다. 추측하기 전에 여기를 읽어라.**

| 어디 | 무엇이 있나 |
|---|---|
| `docs/SPEC.md` (**v15**) | 스펙 정본. 머리에 버전별 변경 이력이 있고 **결정마다 실측 근거가 붙어 있다** |
| `docs/TEST-CASES.md` (**v9**) | 트리거링·완주 기준선·엣지 케이스 카탈로그 |
| `git log` | 왜 그렇게 했는지가 커밋 메시지에 남아 있다. 원본 이력 4개가 그대로 딸려왔다 |

**⚠️ `docs/SPEC.md`와 `docs/TEST-CASES.md`는 임의로 수정하지 않는다.** SDD로 진행하는
프로젝트라 이 둘이 기준점이다. 구현이 스펙과 어긋나면 **스펙을 고치지 말고 어긋난다는 사실을
보고하고 지시를 기다린다.** 사용자가 명시적으로 지시한 개정만 수행한다.

## 구성

```
commerce-research/        스킬 본체 (SKILL.md · references/<site>.md · scripts/ · assets/)
├── SKILL.md              절차서. 플랫폼 중립으로 쓴다 — 특정 도구 이름에 의존하지 않는다
├── references/           사이트 어댑터 — musinsa · 29cm · cafe24. 작업 시작 전에 먼저 읽는다
└── scripts/              validate_data · build_report · diff_snapshots · group_variants
data/.tools/              범용 수집기 (git 추적. 수집 데이터는 미추적)
├── scan_market_any.py    전수조사 — 이름만 대면 시작. --count-only로 규모부터 본다
├── snap_ranking_any.py   랭킹 스냅샷 — --cron으로 주기 등록까지
├── classify_fit.py       상품명 → 핏 1단계 분류
└── ranking_targets.json  두 플랫폼 카테고리 코드 실측 카탈로그 1,546개
tests/run_tests.py        회귀 111건. 픽스처만 쓰고 사이트에 붙지 않는다
```

- 수집 원본은 `data/raw/`, 스냅샷은 `data/snapshots/`, 리포트는 `output/` — **전부 git 미추적**이다
- **원본의 축적 스냅샷 47개를 그대로 가져왔다**(무신사 바지 35 · 29CM 여성슈즈 9 · 그 외).
  crontab 잡도 이 경로로 재등록됐으니 축적이 끊기지 않고 이어진다 —
  `diff_snapshots.py`로 바지 35개 구간 diff가 성립하는 것까지 확인했다

## 원본 저장소와의 관계

```
origin   = https://github.com/whysowlee/commerce-intel.git            (private, 여기로 push)
upstream = https://github.com/whysowlee/commerce-browser-skill.git   (fetch 전용 — push URL 차단)
```

- 원본 개선 가져오기: `git fetch upstream && git merge upstream/main`
- **`git push upstream`은 일부러 막아뒀다** — 이 프로젝트 커밋이 원본 공개 레포로 새지 않게 한 것이다.
  푸시는 항상 `origin`으로 간다(`git push`가 그렇게 추적돼 있다)
- 이 레포는 **private**이다. 공개하려면 `gh repo edit --visibility public`

## 이어서 할 일 (2026-07-30 기준 열린 항목)

1. **코튼 팬츠 핏 분류율 48.1%** — SPEC 성공 기준 80% 미달. 남은 1,864건은 대표 이미지
   판단 1,257회가 필요하다(색상변형 그룹으로 32.6% 절감한 수치). 진행 여부는 사용자 결정 대기
2. **SPEC 내부 모순 2건** — ① §6 `source_total` 폴백 문장이 v6 결정(순환 검증 금지)과 어긋난다
   ② §6 `attributes_basis` 값 목록에 v12가 추가한 `group`이 빠져 있다. 둘 다 보고만 한 상태다
