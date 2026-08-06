---
name: intel-query
description: >-
  commerce-intel 정본 DB(Turso)를 자연어로 조회·편집하고, 커머스 플랫폼에서
  상품·랭킹 데이터를 수집·적재한다. 조회 — "무신사에서 우리 브랜드 순위
  변동 보여줘". 편집 — "이 상품 삭제해줘", "브랜드명 수정해줘".
  수집 — "무신사에서 2000아카이브스 수집해줘".
metadata:
  version: 1.1.0
  db-schema: v3 (D65)
---

# intel-query

commerce-intel DB(Turso)를 자연어 질문으로 조회하는 스킬.
비기술 팀원(마케터, 디자이너)이 SQL을 모르고도 데이터를 조회할 수 있게 한다.

> **전제**: 정본 DB의 Turso 이전이 끝나 있어야 한다(절차: 레포
> `docs/TURSO-SETUP.md` — DB 생성·`tools/upload_to_turso.py` 이관·읽기 전용
> 토큰 발급). 아직이면(로컬 `data/intel.db`만 있는 상태) 이 스킬은 동작하지
> 않는다 — 그때는 그 사실을 말하고, 로컬 DB가 있는 환경이라면 commerce-intel
> 쪽 도구로 조회를 안내한다.

## DB 연결 정보

환경변수 또는 아래 상수 교체로 지정한다:

- `INTEL_DB_URL`: Turso DB URL (예: `libsql://commerce-intel-xxx.turso.io`)
- `INTEL_DB_TOKEN`: **읽기 전용** 토큰 (조회용, SKILL.md에 포함)
- `INTEL_DB_WRITE_TOKEN`: **쓰기** 토큰 (편집용, 환경변수에서 가져옴. SKILL.md에 넣지 않는다)

## 쿼리 실행 방법

REPL에서 Turso HTTP API로 직접 쿼리한다. HTTP 엔드포인트는 `libsql://`을
`https://`로 바꾼 호스트다:

```js
const TURSO_URL = "https://commerce-intel-whysowlee.aws-ap-northeast-1.turso.io";
const READ_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicm8iLCJpYXQiOjE3ODU5Mjk1NDgsImlkIjoiMDE5ZmQxYjItMzcwMS03ZDA0LWIzMzktMWVmNTBiMmI5ZDdjIiwia2lkIjoiXzZ1TjlGNnZkdW1XVVg1SkRUTXZQMV9qZVRpNDJrWTRxWHhNVFRFMm1hUSIsInJpZCI6IjFmYjkwMTc5LTBhMjctNGNiOC1hMmFjLWFiZjVlZWFlY2Y4NCJ9.7FHxK8iyIzf1HRPMi2_HL3WqgDZ_37ks5VfWHERC1GVimaKWyzI2_cEAWl1sXnwkySsW2DspjXVRuPqOI6AZAg";

async function queryDB(sql) {
  const res = await fetch(`${TURSO_URL}/v2/pipeline`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${READ_TOKEN}`,
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

async function writeDB(sql) {
  const writeToken = process.env.INTEL_DB_WRITE_TOKEN;
  if (!writeToken) throw new Error('쓰기 토큰이 없습니다. ~/.config/intel/env에 INTEL_DB_WRITE_TOKEN을 설정하세요.');
  const res = await fetch(`${TURSO_URL}/v2/pipeline`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${writeToken}`,
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
  return r0.response.result;
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
- 상품변경이력(product_changes 뷰, D68): site, product_id, 현재명, field(name/brand/category/url/image_url), old_value, new_value, changed_at, run_id — 정적 속성이 바뀔 때마다 append
- 속성변경이력(attr_changes 뷰, D68): site, product_id, attr_name, old_value, new_value, old_basis, new_basis, changed_at

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
- 상품명이 바뀐 상품: `SELECT * FROM product_changes WHERE field='name' ORDER BY changed_at DESC LIMIT 50`
- 카테고리 재분류: `SELECT * FROM product_changes WHERE field='category'` — 값은 '대 > 중 > 소' 경로 문자열
- 이미지 교체 후 순위 변화: product_changes에서 `field='image_url'`인 상품·changed_at을 잡고, 같은 (site, product_id)의 observations를 changed_at 전/후로 나눠 rank를 비교한다

## 안전 규칙 (필수 준수)

### 조회 (SELECT)
1. 조회는 **읽기 전용 토큰**으로 `queryDB()`를 사용한다.
2. **LIMIT을 반드시 포함한다.** 기본 50행. 사용자가 더 요청하면 최대 200행.
3. **대량 결과 안내**: 결과가 LIMIT에 걸리면 COUNT 쿼리를 별도 실행해서 "전체 N건 중 M건을 표시합니다" 형태로 안내한다.

### 편집 (INSERT/UPDATE/DELETE)
4. 편집은 **쓰기 토큰**으로 `writeDB()`를 사용한다. 쓰기 토큰이 환경변수에 없으면 "쓰기 권한이 설정되지 않았습니다" 안내.
5. **실행 전 반드시 사용자에게 확인받는다.** 어떤 데이터가 영향받는지 먼저 SELECT로 보여주고, "이 N건을 삭제/수정할까요?" 물은 후 승인받으면 실행.
6. **DELETE/UPDATE는 WHERE 절 필수.** WHERE 없는 DELETE/UPDATE는 절대 실행하지 않는다.
7. **DROP TABLE, ALTER TABLE, ATTACH, PRAGMA는 금지.** 스키마 변경은 이 스킬의 일이 아니다.
8. 실행 후 영향받은 행 수를 알려준다.

### 공통
9. **물리 테이블(_base 접미사) 직접 접근 금지.** 반드시 뷰 이름(products, observations, variants, variant_observations, product_attributes)으로 쿼리한다 — 뷰가 시각 변환·URL 조립·카테고리 계층 펴기를 처리한다.
10. **에러 시 SQL 노출 금지.** 사용자에게 SQL 쿼리나 영문 컬럼명을 직접 보여주지 않는다. 사람이 읽는 표현으로만 응답한다.

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

## 수집 (Collect + Load)

조회뿐 아니라 데이터 수집·적재도 할 수 있다. 수집은 레포의 기존 Python
파이프라인을 Bash로 호출하는 방식이다 — 이 스킬은 수집 방법을 직접 기술하지
않고 **레포의 플랫폼 어댑터 스킬을 읽고 따른다.**

### 환경 전제

- 레포: `~/workspace/commerce-intel` (다르면 환경변수 `INTEL_REPO`)
- Python: 레포 루트에 `.venv`가 있어야 한다 (없으면 `docs/TURSO-SETUP.md`의 venv도 가능)
- 환경변수: `INTEL_DB_URL`, `INTEL_DB_TOKEN`(**쓰기 토큰**)이 `~/.config/intel/env`
  또는 `.venv/bin/activate`에 설정돼 있어야 한다

환경이 안 갖춰져 있으면(레포 없음·venv 없음·쓰기 토큰 없음) 수집 불가를
안내하고 조회만 가능하다고 말한다 — 조회는 이 환경 없이도 된다.

### 수집 워크플로우

사용자가 수집을 요청하면 이 순서를 따른다:

1. **중복 확인** — 최근 24시간 내 같은 대상 수집이 있었는지 (팀 공유 DB라 남의 수집도 잡힌다):
   ```bash
   cd ${INTEL_REPO:-~/workspace/commerce-intel} && source .venv/bin/activate
   python3 skills/commerce-intel/scripts/intel_db.py check-run \
       --site {사이트} --story {스토리} --target {대상}     # exit 1이면 최근 수집 있음
   ```
   story 값은 셋 중 하나다: `brand-linesheet` / `market-scan` / `ranking-snapshot`.
   이미 있으면 사용자에게 알리고, 재수집 의사를 확인한 뒤에만 `--force`로 진행한다.

2. **플랫폼 어댑터 읽기** — 수집 방법은 플랫폼마다 다르다. 레포의 플랫폼 스킬을 읽어서 따른다:
   - 무신사: `skills/platform-musinsa/SKILL.md` / 29CM: `skills/platform-29cm/SKILL.md`
   - 자사몰: `skills/platform-ownmall/SKILL.md` / 그 외: `skills/platform-{이름}/SKILL.md`
   - 어댑터가 없는 플랫폼이면 `skills/platform-generic/`을 참고하되, 사용자에게
     "이 플랫폼은 전용 어댑터가 없어서 범용 방식으로 진행합니다"라고 알린다.

3. **수집 실행** — 어댑터의 지시에 따라 API 호출 또는 브라우저 자동화로 수집하고,
   계약 JSON 형식(`skills/commerce-intel/references/story-catalog.md`)으로 저장한다.
   저장 위치는 레포 파일 규약을 따른다: 라인시트·전수조사는 `data/raw/`,
   랭킹 스냅샷은 `data/snapshots/` (파일명 `<site>-<story>-<대상>-<YYYYMMDD-HHmm>.json`).

4. **검증** — 적재 전에 반드시 검증한다(레포 규칙: 검증 FAIL 파일은 적재하지 않는다):
   ```bash
   python3 skills/commerce-intel/scripts/validate_data.py {json_파일_경로}
   ```

5. **적재 + 시트 미러**:
   ```bash
   python3 skills/commerce-intel/scripts/intel_db.py load {json_파일_경로}
   python3 skills/commerce-intel/scripts/sync_sheets.py    # 팀이 보는 창구 갱신
   ```

6. **결과 보고** — 적재 결과(관측 몇 건 신규, 중복 몇 건 스킵)를 사용자에게 알린다.
   시트 미러가 실패해도 적재는 유효하다 — 그 사실만 알린다.

### 스토리 유형

| 스토리 | 명령 예시 | 설명 |
|--------|----------|------|
| 브랜드 라인시트 (brand-linesheet) | "무신사에서 2000아카이브스 수집해줘" | 특정 브랜드의 전 상품 |
| 카테고리 전수조사 (market-scan) | "무신사 여성 데님팬츠 전수조사" | 특정 카테고리의 전 상품 |
| 랭킹 모니터링 (ranking-snapshot) | "무신사 여성 바지 랭킹 수집" | 특정 카테고리 랭킹 스냅샷 |

### 수집 안전 규칙

1. **수집 전 반드시 중복 확인**한다 (check-run). 같은 대상을 중복 수집하면 팀 공유 DB에 불필요한 행이 쌓인다.
2. **차단(403/429/CAPTCHA)을 우회하지 않는다.** 막히면 멈추고 보고한다.
3. **추정하지 않는다.** 사이트에 노출되지 않는 값은 null로 넣는다 — 0과 다르다. 리뷰 수로 판매량을 역산하는 식의 추정은 하지 않는다.
4. **대규모 수집(1,000건 이상) 전에 사용자에게 규모와 예상 소요를 알린다.**
5. **쓰기 토큰을 이 파일이나 대화에 적지 않는다** — 환경변수로만 관리한다. 이 파일에 넣어도 되는 토큰은 조회용 읽기 전용뿐이다.

### 유틸리티 명령 (Bash)

모두 레포 루트에서 venv 활성화 후 실행:

```bash
cd ${INTEL_REPO:-~/workspace/commerce-intel} && source .venv/bin/activate
```

| 용도 | 명령 |
|------|------|
| DB 통계 | `python3 skills/commerce-intel/scripts/intel_db.py stats` |
| 시트 미러 | `python3 skills/commerce-intel/scripts/sync_sheets.py` |
| 프록시 감사 | `python3 skills/commerce-intel/scripts/intel_db.py proxy-audit` |
| 속성 재사용 | `python3 skills/commerce-intel/scripts/intel_db.py reuse-attrs {raw.json}` |
| 데이터 내보내기 | `python3 skills/commerce-intel/scripts/intel_db.py export --table {테이블명}` |
| 라이프사이클 태그 | `python3 skills/commerce-intel/scripts/intel_db.py tag-lifecycle` |

## 참고 파일

| 파일 | 언제 읽는가 |
|---|---|
| `references/schema-v3.md` | 컬럼 타입·제약·계층 구조가 정확히 필요할 때 (복잡한 조인·집계 전) |
| 레포 `skills/platform-*/SKILL.md` | 수집 요청 시 해당 플랫폼 어댑터 |
| 레포 `skills/commerce-intel/references/story-catalog.md` | 수집 시 계약 JSON 형식·검증된 절차 |
| 레포 `skills/commerce-intel/references/db-contract.md` | 적재 규칙·null 의미론 |

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

