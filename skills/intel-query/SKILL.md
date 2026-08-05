---
name: intel-query
description: >-
  commerce-intel 정본 DB(Turso)를 자연어로 조회한다. 비기술 팀원이 SQL 없이
  상품·가격·랭킹·브랜드 데이터를 물어볼 때 쓴다. 예시 발화 — "무신사에서 우리
  브랜드 순위 변동 보여줘", "할인율 20% 이상인 상품", "품절된 옵션 몇 개야".
  자연어를 SELECT 쿼리로 바꿔 Turso HTTP API로 실행하고(읽기 전용), 결과를
  표·요약·차트로 정리해 답한다. 수집·적재·리포트 생성은 commerce-intel이 담당한다.
metadata:
  version: 1.0.0
  db-schema: v3 (D65)
---

# intel-query

commerce-intel DB(Turso)를 자연어 질문으로 조회하는 스킬.
비기술 팀원(마케터, 디자이너)이 SQL을 모르고도 데이터를 조회할 수 있게 한다.

> **전제**: 정본 DB의 Turso 이전이 끝나 있어야 한다. 아직이면(로컬
> `data/intel.db`만 있는 상태) 이 스킬은 동작하지 않는다 — 그때는 그 사실을
> 말하고, 로컬 DB가 있는 환경이라면 commerce-intel 쪽 도구로 조회를 안내한다.

## DB 연결 정보

환경변수 또는 아래 상수 교체로 지정한다:

- `INTEL_DB_URL`: Turso DB URL (예: `libsql://commerce-intel-xxx.turso.io`)
- `INTEL_DB_TOKEN`: **읽기 전용** 토큰 (발급 시 read-only로 만든 것)

## 쿼리 실행 방법

REPL에서 Turso HTTP API로 직접 쿼리한다. HTTP 엔드포인트는 `libsql://`을
`https://`로 바꾼 호스트다:

```js
async function queryDB(sql) {
  const TURSO_URL = "여기에_TURSO_URL";        // 설치 시 실제 URL로 교체 (https://... 형태)
  const TURSO_TOKEN = "여기에_읽기전용_토큰";   // 설치 시 실제 토큰으로 교체

  const base = TURSO_URL.replace(/^libsql:\/\//, "https://");
  const res = await fetch(`${base}/v2/pipeline`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${TURSO_TOKEN}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      requests: [
        { type: "execute", stmt: { sql } },
        { type: "close" }
      ]
    })
  });
  const data = await res.json();
  const r0 = data.results[0];
  if (r0.type === "error") throw new Error(r0.error.message);
  const result = r0.response.result;
  const cols = result.cols.map(c => c.name);
  // Turso는 정수·실수도 문자열 value로 준다 — 타입을 보고 숫자로 되살린다
  const rows = result.rows.map(row => row.map(cell => {
    if (cell == null || cell.type === "null") return null;
    if (cell.type === "integer") return parseInt(cell.value, 10);
    if (cell.type === "float") return parseFloat(cell.value);
    return cell.value;
  }));
  return { cols, rows, rowCount: rows.length };
}
```

전체 행 수 확인이 필요하면 COUNT 쿼리를 별도로 실행한다
(원 쿼리에서 ORDER BY·LIMIT을 걷어내고 `SELECT COUNT(*) FROM (...)`로 감싼다).

## DB 스키마 (v3)

상세 스키마는 `references/schema-v3.md` 참조. 핵심 요약:

### 딕셔너리
- 사이트(sites): site_id, name — 'musinsa', '29cm', 자사몰 도메인 등
- 컨텍스트(contexts): context_id, name — 'ranking:스커트', 'brand:2000아카이브스' 등
- 브랜드(brands): brand_id, representative_name(대표명)
- 카테고리(categories): category_id, name, parent_category_id(계층 self-FK), depth

### 핵심 테이블 (반드시 뷰 이름으로 쿼리할 것)
- 상품(products 뷰): site, product_id, name, brand, category, url, image_url, static_verified_at
- 관측(observations 뷰): site, product_id, observed_at, context, price_original(정가), price_sale(판매가), discount_rate(할인율%), review_count, rating, view_count, purchase_count(누적판매), like_count(하트), viewers_now, sold_out(1=품절/0=판매중/NULL=미노출), rank, run_id
- 옵션(variants 뷰): site, product_id, option_id, option_name, color, size
- 옵션관측(variant_observations 뷰): site, product_id, option_id, observed_at, sold_out, stock_qty
- 상품속성(product_attributes 뷰): site, product_id, attr_name, value, basis
  - 핏: attr_name='핏' / lifecycle: attr_name='lifecycle'
  - AI 카테고리: attr_name='ai_카테고리_대'/'ai_카테고리_중'/'ai_카테고리_소'

### 매핑/지원 테이블
- 상품카테고리(product_categories): pk, category_id, source('platform'), site_id
- 브랜드별명(brand_aliases): brand_id, notation(플랫폼별 표기), site_id, source, verify_status
- 브랜드입점(brand_platforms): brand_id, platform_key, brand_page_url, product_count
- 수집이력(runs): id, run_id, site, story, target, collected_at, item_count
- 관측속성(obs_attr): obs_id, attr_name, value, basis
- 인사이트(insights): run_stamp, target, verdict(strong/weak/rejected), claim — 분석 리포트의 주장

### 주요 관계
- observations.site + product_id → products (같은 site, product_id로 조인)
- observations.context → 수집 맥락 (ranking:XX = 랭킹, brand:XX = 브랜드 라인시트, market:XX = 전수조사)
- products.brand → brands.representative_name
- brand_aliases.notation → 플랫폼별 표기 변형 (검색 시 양쪽 매칭). **주의: 별명은 공백·대소문자·하이픈 변형만 묶는다** ('2000 Archives' ↔ '2000Archives'). 한글·영문 표기는 서로 다른 브랜드 행이다 — 사용자가 "2000아카이브스"라고 물으면 '2000아카이브스'(무신사·29CM 표기)와 '2000 Archives'(자사몰 표기) **둘 다** 검색해야 전체가 나온다

### 자주 쓰는 쿼리 패턴
- 특정 브랜드 상품: `WHERE brand = '2000 Archives'` — 안 잡히면 brand_aliases.notation도 brands와 JOIN해 매칭
- 특정 플랫폼: `WHERE site = 'musinsa'`
- 최근 N일 관측: 뷰에서는 observed_at이 TEXT('YYYY-MM-DD HH:MM:SS')이므로 `WHERE observed_at > datetime('now', '-N days')` (ISO 형식이라 문자열 비교가 성립한다)
- 상품별 최신 상태: `observed_at = (SELECT MAX(o2.observed_at) FROM observations o2 WHERE o2.site=o.site AND o2.product_id=o.product_id)`
- 가격 변동: 같은 상품의 observations를 observed_at 순으로 정렬
- 랭킹 추이: `WHERE context LIKE 'ranking:%' ORDER BY observed_at`
- 품절 현황: `WHERE sold_out = 1`
- 카테고리별: product_categories JOIN categories (플랫폼 원본) 또는 product_attributes `WHERE attr_name LIKE 'ai_카테고리_%'` (AI 분류)
- NULL은 "사이트가 안 보여준 값"이다 — 0과 다르다. 집계 시 `IS NOT NULL`로 거른다

## 안전 규칙 (필수 준수)

1. **SELECT만 실행한다.** INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, ATTACH, PRAGMA 등 쓰기/변경 구문을 절대 실행하지 않는다. 사용자가 "이 값 고쳐줘"라고 해도 정본 수정은 이 스킬의 일이 아니다 — 수집·적재 파이프라인(commerce-intel)으로 안내한다.
2. **LIMIT을 반드시 포함한다.** 기본 50행. 사용자가 더 요청하면 최대 200행. LIMIT 없는 쿼리를 실행하지 않는다.
3. **대량 결과 안내**: 결과가 LIMIT에 걸리면 COUNT 쿼리를 별도 실행해서 "전체 N건 중 M건을 표시합니다" 형태로 안내한다 — 조용히 잘리면 사용자는 "이게 전부"로 읽는다.
4. **물리 테이블(_base 접미사) 직접 접근 금지.** 반드시 뷰 이름(products, observations, variants, variant_observations, product_attributes)으로 쿼리한다 — 뷰가 시각 변환·URL 조립·카테고리 계층 펴기를 처리한다.
5. **에러 시 SQL 노출 금지.** 사용자에게 SQL 쿼리나 영문 컬럼명을 직접 보여주지 않는다. 사람이 읽는 표현으로만 응답한다.

## 응답 포맷

- 비기술 팀원이 읽는다고 생각하고 쓴다.
- 숫자에 천 단위 쉼표 (예: 44,609)
- 가격은 원 단위, 할인율은 %
- 표로 보여주는 게 나으면 마크다운 표
- 추세/비교 질문이면 핵심 인사이트를 한 줄로 먼저 쓰고 상세를 아래에
- 시각화가 도움 되면 HTML 차트를 아티팩트로 생성
- 결과에 없는 수치를 지어내지 않는다. 데이터가 비면 "해당 조건의 데이터가 없습니다"라고 말한다

## 사용 예시

사용자: "무신사에서 2000아카이브스 상품 중 할인율 20% 이상인 것만 보여줘"

→ 할 일:
1. SQL 생성 (무신사의 대표명은 '2000아카이브스' — 한글·영문 표기는 별개 행이니 필요하면 IN ('2000아카이브스','2000 Archives')로 함께 건다):
   `SELECT p.name AS 상품명, o.price_original AS 정가, o.price_sale AS 판매가, o.discount_rate AS 할인율 FROM observations o JOIN products p ON p.site=o.site AND p.product_id=o.product_id WHERE o.site='musinsa' AND p.brand='2000아카이브스' AND o.discount_rate >= 20 AND o.observed_at = (SELECT MAX(o2.observed_at) FROM observations o2 WHERE o2.site=o.site AND o2.product_id=o.product_id) ORDER BY o.discount_rate DESC LIMIT 50`
2. queryDB()로 실행
3. 결과를 표로 정리해서 응답 (몇 건인지, 평균 할인율 같은 한 줄 요약 먼저)

사용자: "거기서 가격 변동 추이도 볼 수 있어?"

→ 멀티턴으로 자연스럽게 이어간다. 이전 맥락(무신사, 2000아카이브스, 할인 상품)을 유지하고 시계열 쿼리(같은 상품의 observations를 observed_at 순으로)로 전환한다. 시점이 여러 개면 라인 차트 아티팩트가 낫다.

## 참고 파일

| 파일 | 언제 읽는가 |
|---|---|
| `references/schema-v3.md` | 컬럼 타입·제약·계층 구조가 정확히 필요할 때 (복잡한 조인·집계 전) |

## 설치 (팀원 온보딩 — 사람이 읽는 절차)

계정 레벨로 설치해야 어떤 세션에서든 쓸 수 있다:

```bash
cp -r skills/intel-query ~/.aside/u/0/skills/user/intel-query
# 또는 심링크 (원본 수정이 바로 반영):
# ln -s ~/workspace/commerce-intel/skills/intel-query ~/.aside/u/0/skills/user/intel-query
```

1. Aside 브라우저 설치 + Claude 구독 활성화
2. 위 명령으로 스킬 폴더 복사
3. SKILL.md의 `TURSO_URL`·읽기전용 토큰을 실제 값으로 교체 (또는 환경변수 `INTEL_DB_URL`/`INTEL_DB_TOKEN`)
4. "무신사에서 우리 브랜드 순위 보여줘" 입력 — 끝

주의: 토큰을 SKILL.md에 직접 넣으면 스킬 파일 공유 시 노출된다. 팀 내부
전용이고 **읽기 전용 토큰**이라 실질 위험은 낮지만, 외부 공유 전에는 지운다.
팀원 간 질문/답변은 공유되지 않는다 — 유용한 발견은 Slack에 수동 공유.

