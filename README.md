# commerce-browser-skill

공개 API가 없는 커머스 사이트(무신사·29CM)에서 자연어 요청 하나로 상품·리뷰·랭킹을 모아
HTML 리포트로 만드는 **Claude 스킬**과, 그 스킬을 만드는 과정의 스펙·테스트다.

산출물은 `commerce-research/` 폴더 하나다. 이 폴더가 곧 스킬이다.

## 할 수 있는 일

| 스토리 | 요청 예 | 결과물 |
|---|---|---|
| 브랜드 라인시트 | "무신사에서 인사일런스 상품 전체 라인시트로 정리해줘" | 전 상품 표(이미지·가격·후기·평점·좋아요) + 플랫폼별 인기 |
| 시장 전수조사 | "무신사 데님팬츠 시장 조사해줘" | 가격·평점·핏 분포 + 후기 수 가중 평균 평점 (전 상품 표, 이미지 열 없음) |
| 랭킹 모니터링 | "무신사 바지 랭킹 top 100, 3월 한 달 변화 보여줘" | 신규 진입/이탈/급등락, 할인 시작 시점, 순위 추이 |

## 설치

스킬 폴더를 Claude가 읽는 위치에 두면 된다.

```bash
./package.sh                      # dist/commerce-research.zip 생성 (테스트 통과 시에만)

# Claude Code에서 쓰기 — 개인용
cp -r commerce-research ~/.claude/skills/

# 특정 프로젝트에서만 쓰기
cp -r commerce-research <프로젝트>/.claude/skills/
```

zip으로 배포받았다면 압축을 풀어 같은 위치에 두면 된다.
설치 후 새 대화에서 위 요청 예시를 그대로 말하면 스킬이 자동으로 로드된다.

의존성은 없다. **Python 3.9 표준 라이브러리만** 쓴다.

## 스킬 구조

```
commerce-research/
├── SKILL.md                  # 트리거, 원칙, 공통 워크플로우, 스토리별 절차
├── references/
│   ├── musinsa.md            # 무신사 URL·순회 방법·필드 매핑·함정  (작업 시작할 때 읽는다)
│   ├── 29cm.md               # 29CM 〃
│   └── report-spec.md        # 리포트 섹션 구성
├── scripts/
│   ├── validate_data.py      # 수집 JSON 검증 → 종료 코드로 진행 여부 판단
│   ├── build_report.py       # 수집/diff JSON → 단일 HTML 리포트
│   └── diff_snapshots.py     # 랭킹 스냅샷 기간 비교
└── assets/
    └── report-template.html  # 코드 실행이 안 되는 환경용 구조 템플릿
```

수집은 에이전트가, **검증·집계·리포트 생성은 코드가** 한다. 둘 사이는
[데이터 계약](docs/SPEC.md#6-데이터-계약-수집-json-스키마)(JSON 스키마)으로만 이어져 있어서,
사이트 구조가 바뀌어 수집 방법이 달라져도 리포트 쪽은 그대로다.

## 스크립트 직접 쓰기

```bash
S=commerce-research/scripts

# 1. 수집 결과 검증 — 종료 코드 0 진행 / 1 경고 동반 진행 / 2 중단
python3 $S/validate_data.py data/raw/musinsa-brand-linesheet-인사일런스-20260729-1400.json \
    --json data/validation.json

# 2. 리포트 생성 (여러 사이트를 함께 주면 플랫폼 비교가 붙는다)
python3 $S/build_report.py data/raw/*.json --validation data/validation.json \
    --out output/linesheet.html

# 3. 랭킹 기간 비교 — 종료 코드 1이면 스냅샷이 1개 이하라 비교 불가
python3 $S/diff_snapshots.py data/snapshots --site musinsa --target 바지 \
    --from 2026-03-01 --to 2026-03-31 --out data/diff.json
python3 $S/build_report.py data/diff.json --out output/ranking-diff.html
```

## 랭킹 스냅샷 축적 (스토리3)

- **공간**: 이 저장소의 `data/snapshots/` — 고정 경로이며 git에는 올라가지 않는다.
  파일 1개 = 스냅샷 1개(`<site>-ranking-<카테고리>-<YYYYMMDD-HHmm>.json`),
  데모 단계에서는 전부 보존한다
- **주기**: **30분에 1번.** Claude Code 스케줄 기능에 "무신사 바지 랭킹 스냅샷 찍어놔"를
  30분 간격으로 등록하면 된다 — 반드시 이 저장소를 작업 폴더로 실행되게 한다
- 두 사이트 모두 과거 랭킹을 소급 조회할 수 없다(2026-07-29 서버 측 검증 완료).
  변화 분석은 **축적을 시작한 시점부터, 30분 축적분만으로** 한다

## 이 스킬이 지키는 것

- **추정하지 않는다.** 사이트가 안 보여주는 값은 `null`이고 리포트에 "미노출"로 찍힌다.
  리뷰 수로 판매량을 역산하는 식의 추정은 하지 않는다
- **구간 표기를 정수로 바꾸지 않는다.** 무신사 조회수는 `"300회 이상 (최근 1개월)"`처럼
  구간으로 나온다. 이걸 `300`으로 담으면 없는 정밀도를 만드는 것이라, 문구 그대로 싣고
  순위 차트에는 쓰지 않는다
- **리뷰 본문을 수집·해석하지 않는다.** 만족/불만족 판단 재료는 사이트가 노출한
  후기 수와 평균 평점뿐이고, 가중 평균·분포 계산은 전부 코드가 한다 —
  해석은 재현되지 않으므로 개입시키지 않는다
- **수집 규모는 상품 1만 개까지 묻지 않고 진행한다.** 초과하면 수집을 시작하기 전에
  성별·세부 카테고리 같은 좁힐 축의 실제 개수를 조회해 제시하고, 좁힌 범위를
  리포트 제목에 그대로 새긴다("데님팬츠(남성)")
- **막히면 멈춘다.** 403/429/캡차를 우회하지 않고, User-Agent도 위장하지 않는다.
  거기까지 모은 것으로 부분 리포트를 만들고 어디서 멈췄는지 밝힌다
- **빈 결과를 만들지 않는다.** 검색 0건이면 그렇게 보고하고, 비교할 스냅샷이 없으면
  빈 diff 대신 사유를 말한다

## 테스트

```bash
python3 tests/run_tests.py
```

픽스처만 쓰므로 사이트에 붙지 않고 몇 초면 끝난다. **현재 61건 전건 통과.**
스크립트를 고치면 이걸 먼저 돌린다. `package.sh`도 이 테스트가 통과해야 zip을 만든다.

실제 사이트를 도는 트리거·기능 테스트는 [docs/TEST-CASES.md](docs/TEST-CASES.md)에 있다.

## 문서

| 문서 | 내용 |
|---|---|
| [docs/SPEC.md](docs/SPEC.md) | 범위/비범위, 스토리별 스펙, 데이터 계약, 강건성 정책 |
| [docs/TEST-CASES.md](docs/TEST-CASES.md) | 트리거링·기능·회귀 테스트 케이스 |
| [description.md](description.md) | 프로젝트 배경과 목표 (스킬에는 포함되지 않는다) |
