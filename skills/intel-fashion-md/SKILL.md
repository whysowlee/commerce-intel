---
name: intel-fashion-md
description: 패션 MD/마케터/디자이너/CEO 의사결정용 도메인 레이어. intel-explore(EDA)와
  intel-insight(분석·인사이트)가 커머스 데이터를 해석할 때 이 스킬의 도메인 규칙
  (funnel 사고, Y 선정 규칙, 리오더·라인업 질문 템플릿, 프록시 후보)을 함께 읽는다.
  또한 리포트에 실린 모든 수치를 확정적 코드로 재검증하는 sanity check 절차
  (scripts/sanity_check.py + claims JSON)를 제공한다. "리오더", "핏별 구성",
  "라인업", "sanity check", "수치 검증" 요청이나 EDA·인사이트 단이 실행될 때
  함께 트리거된다. 데이터 수집은 하지 않는다.
compatibility: 코드 실행(python3 stdlib)과 로컬 data/intel.db가 필요하다. 외부
  라이브러리 불필요 — sanity_check.py는 stdlib만 쓴다.
metadata:
  version: 1.0.0
  source-feedback: "0804_feedback.md (2026-08-04 팀 피드백 반영)"
---

# intel-fashion-md — 패션 MD 도메인 레이어 + 수치 확정 검증

이 스킬은 두 가지를 담는다.

1. **도메인 지식**: EDA·인사이트가 "무엇을 Y로 삼고, 무엇을 물어야 하는지"에 대한
   패션 MD 관점의 규칙 (8/4 피드백 §3~§5).
2. **확정 검증**: 리포트에 실린 수치를 AI 개입 없이 결정적 코드로 재계산해
   대조하는 sanity check (8/4 피드백 §6).

---

## 원칙

- **추정 금지**: DB에 없는 값은 계산하지도, 채우지도 않는다. `null ≠ 0`.
- **비율로 판단**: funnel 지표는 절대값이 아니라 상위 단계 대비 비율로 본다.
- **'영향 없음'도 인사이트다**: 유의미한 차이가 없다는 결과도 "신경 쓰지 않아도
  된다"는 판단 근거이므로 기각하지 말고 보고한다.
- **관측 진술 + 액션 후보 분리**: 주장 본문은 관측 진술로 쓴다(intel-insight §5
  유지). 다만 각 강한 주장에는 **액션 후보** 1줄을 별도 필드로 붙인다
  (예: 관측 "27사이즈가 평균 대비 2.1배 빨리 품절 (n=12 SKU)" → 액션 후보
  "리오더 시 27 배분 상향 검토"). 액션 후보는 제안 표기이며 결정은 사람이 한다.

---

## 방법론은 여기 없다 — `analysis-context.md`가 정본이다

Y 선정 규칙·퍼널 구조·비율의 함정·내생성은 **플랫폼과 무관한 방법론**이라
`../commerce-intel/references/analysis-context.md` 한 곳에 있다. 여기 옮겨 적으면
두 문서가 어긋나는 날 다음 세션이 어느 쪽을 사실로 읽을지 알 수 없다.

한 줄로 줄이면 이렇다 — **공급자가 정한 값(가격·할인)은 Y가 아니다.** 코드로는
D47·D51이 구현했고(`intel_data.role_of`), 그룹 비교·상관 양쪽에 걸려 있다.

이 스킬이 갖는 것은 **패션 MD 도메인 지식**이다: 아래 비즈니스 질문과 프록시
후보 풀, 그리고 수치 확정 검증.

---

## 비즈니스 질문 템플릿 (피드백 §4 + MD 의사결정)

EDA·인사이트 실행 시 아래 질문을 기본 후보로 삼는다. 데이터가 부족하면
억지로 답하지 말고 "필요 수집"으로 보고한다.

1. **리오더 수량·시점**: 사이즈(옵션)별 품절 도달 속도가 다른가?
   어떤 사이즈가 먼저 소진되는가? (variant_observations, survival/depletion)
   - 액션 연결: 리오더 사이즈 배분, 리오더 트리거 시점(인기 사이즈 소진 전).
2. **핏별 라인업 구성**: 2000아카이브스 데님 상품군 확장 시 핏별로 어떻게
   구성하고 무엇부터 출시할지. 카테고리 시장에서 핏별 상품 수 비율 vs
   funnel 성과 비율의 격차(공급 과소/과잉 핏)를 본다.
   (product_attributes의 핏 + observations)
3. **가격대 포지션**: 카테고리 내 가격 분포에서 자사/경쟁 상품이 어느
   구간에 있고, 구간별 funnel 성과가 다른가?
4. **색상별 차이**: 같은 실루엣의 컬러웨이 간 성과 차이가 있는가?
   (variants + group_variants의 그룹)
5. **랭킹 이벤트 해석**: 급상승/급하락/신규진입(diff_snapshots)이 가격 변화·
   할인 시작과 같은 창에서 일어났는가?

## 프록시 후보 (피드백 §3)

proxy_defs에 카드를 만들 때 아래를 기본 후보 풀로 쓴다 (intel-insight §0의
자동 생성 규칙·커버리지 삭제 규칙은 그대로 적용):

- 썸네일 유형 — 룩북 / 제품컷 / 인플루언서 2차 활용 (vision)
- 상품명 언어 — 영문 / 한글 / 혼용 (rule)
- 셀럽 착용 배지 유무 (vision/rule)
- 초기 리뷰 존재 여부 — 발매 후 첫 관측에 review_count ≥ 1 (rule)
- 색상 전개 수 — variants 기준 (rule)
- 핏 — product_attributes 우선, 없으면 상품명/상세 (rule→vision)
- 가격대 구간 — 카테고리 내 사분위 (rule, 파생)
- 상세 페이지 구성 · 스냅 기여도 — 재료 수집이 안 돼 있으면 카드로 만들지
  말고 "필요 수집"으로 보고

---

## 수치 확정 검증 — sanity check (피드백 §6)

**규칙: 리포트(PDF·요약)에 실리는 모든 수치 주장은 claims JSON으로 내보내고,
`scripts/sanity_check.py`로 재계산해 대조한다. 대조를 통과하지 못한 수치는
리포트에 싣지 않는다.**

sanity_check.py는 결정적으로 동작한다: AI·난수·네트워크 없음. 같은
`data/intel.db` + 같은 claims 파일이면 언제, 어느 세션에서 실행해도 같은
결과가 나온다. clean context 세션에서의 재현 검증도 이 스크립트 재실행으로
한다.

### 절차

1. 분석 중 수치 주장이 확정될 때마다 claim 항목을 만든다
   (`output/claims-<대상>-<날짜>.json`). 형식은
   `references/claims-example.json` 참고.
2. 실행:
   ```bash
   python3 skills/intel-fashion-md/scripts/sanity_check.py \
     --db data/intel.db --claims output/claims-denim-20260804.json
   ```
3. exit code (하우스 규약):
   - `0` PASS — 전 claim 일치. 리포트 생성 가능.
   - `1` WARN — 일치하지만 경고 있음 (표본 n < 30 등). 리포트에 경고 병기.
   - `2` FAIL — 하나라도 불일치 또는 claim 해석 불가. **리포트 생성 금지**,
     원인(수치 오기 vs 데이터 변경) 확인 먼저.
4. 결과 표(claim별 PASS/FAIL, 재계산값, n, n_null)를 리포트 부록(detail 층)에
   그대로 싣는다.

### claim이 표현할 수 있는 것

- 지표: `count_rows`, `n`(non-null 건수), `n_null`, `median`, `mean`, `sum`,
  `min`, `max`, `distinct_count`, `share`(특정 값 비율),
  `ratio`(sum(분자)/sum(분모), 둘 다 non-null 행만), `group_median`(그룹별 중앙값)
- 대상 테이블: `observations`(기본) / `products` / `variants` / `variant_observations`
- 필터: 컬럼 등호·비교, `context_prefix`, 관측시각 범위,
  `attr:<속성명>`(product_attributes 조인, 예: `attr:핏`)
- `latest_only: true` — (site, product_id, context)별 최신 관측 1건만 사용
  (시점 혼합 방지 기본값)

### 한계 (정직 고지)

- sanity check는 "리포트 수치 = DB 재계산값" 정합성만 보장한다. 방법론
  타당성(Y 선정, 비교군 구성)은 이 스크립트가 판정하지 못하므로
  intel-insight의 5관문과 이 스킬의 Y 선정 규칙으로 다룬다.
- DB가 갱신되면(append) 재실행 값이 달라질 수 있다. claims에는 검증 시점의
  `observed_to`(관측 범위 상한)를 박아 시점을 고정한다.

---

## 참고 파일

- `scripts/sanity_check.py` — 확정 검증 스크립트 (stdlib only)
- `references/claims-example.json` — claims 형식 예시
- `../commerce-intel/references/db-contract.md` — 스키마·null 규칙 (이 스킬의 전제)
- `../intel-insight/SKILL.md` — 방법론 규칙표·5관문 (이 스킬은 그 위의 도메인 레이어)
