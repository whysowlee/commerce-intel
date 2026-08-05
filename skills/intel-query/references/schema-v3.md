# commerce-intel DB 스키마 v3 — 상세 레퍼런스

정본 DDL은 `commerce-intel/scripts/schema_v3.py`다(D65). 여기는 그 DDL을 조회자
관점에서 정리한 것 — **쿼리는 뷰로만** 하고, 물리 구조는 조인·집계를 정확히 짜는 데
필요한 만큼만 이해하면 된다.

## 설계 한 줄 요약

물리 저장은 정수 사전·대리키로 접혀 있다(반복 텍스트 절약 — D45). 옛 이름
그대로의 **뷰**가 그걸 도로 펴서 사람이 아는 컬럼으로 보여준다. 시각은 물리적으로
unix epoch(INTEGER)지만 뷰에서는 `'YYYY-MM-DD HH:MM:SS'` TEXT로 나온다.

## 조회용 뷰 (이것만 쓴다)

### products — 상품 정적 속성 (상품당 1행)

| 컬럼 | 타입 | 설명 |
|---|---|---|
| site | TEXT | 사이트 식별자 ('musinsa', '29cm', 자사몰 도메인) |
| product_id | TEXT | 사이트 안의 상품 ID. **(site, product_id)가 상품의 논리 키** |
| name | TEXT | 상품명 |
| url | TEXT | 상품 페이지 URL (호스트+경로 재조립됨) |
| image_url | TEXT | 대표 이미지 URL |
| brand | TEXT | 브랜드 **대표명** (brands.representative_name) |
| category | TEXT | 플랫폼 카테고리 — 계층을 도로 편 경로 `'상위 > 하위 > 리프'` 또는 단일명. 파생 컬럼(원본은 product_categories N:M) |
| static_verified_at | TEXT | 정적 속성 마지막 확인 시각 |

### observations — 시점별 관측 (append only)

| 컬럼 | 타입 | 설명 |
|---|---|---|
| site, product_id | TEXT | products와 조인 키 |
| observed_at | TEXT | 관측 시각 'YYYY-MM-DD HH:MM:SS' — ISO 형식이라 문자열 비교·정렬이 시간순이다 |
| context | TEXT | 관측이 나온 화면. `brand:`/`market:`/`ranking:`/`adhoc:` 접두사 4종 고정 |
| price_original | INTEGER | 정가 (원) |
| price_sale | INTEGER | 판매가 (원) — 전 회원 공통 쿠폰적용가 기준 |
| discount_rate | INTEGER | 할인율 (%) |
| review_count | INTEGER | 후기 수 |
| rating | REAL | 평점 (5점 만점 통일) |
| view_count / view_count_display | INTEGER / TEXT | 조회수 정수 / 구간 표기 원문("1.2만 회 이상"). 구간만 노출되면 정수는 NULL |
| purchase_count / purchase_count_display | INTEGER / TEXT | 누적판매 (위와 같은 규칙) |
| like_count / like_count_display | INTEGER / TEXT | 하트 (위와 같은 규칙) |
| viewers_now | INTEGER | "N명이 보는 중" (랭킹 화면에서만 노출) |
| buyers_now | INTEGER | "N명이 구매 중" |
| sold_out | INTEGER | 1=품절, 0=판매중, NULL=미노출 |
| rank | INTEGER | 랭킹 순위 (랭킹 스냅샷에서만) |
| run_id | TEXT | 이 관측을 만든 수집 실행 (runs.run_id) |

- 논리 키: (site, product_id, observed_at, context) — 같은 시각·같은 화면 중복 없음
- **NULL은 미노출이지 0이 아니다.** AVG·SUM 전에 `IS NOT NULL`로 거른다
- `viewers_now`·`rank`는 랭킹 문맥(context LIKE 'ranking:%')에서만 의미가 있다 — 다른 문맥과 섞어 집계하지 않는다

### variants — 옵션(SKU) 구성

| 컬럼 | 설명 |
|---|---|
| site, product_id | 상품 키 |
| option_id | 옵션 ID. (site, product_id, option_id)가 논리 키 |
| option_name, color, size | 옵션 표기·색상·사이즈 |

### variant_observations — 옵션별 재고 관측 (append only)

| 컬럼 | 설명 |
|---|---|
| site, product_id, option_id | 옵션 키 |
| observed_at | 관측 시각 (TEXT) |
| sold_out | 옵션 품절 여부 |
| stock_qty / stock_display | 재고 수량 / 표시 원문 — 수량 미노출 사이트는 NULL |
| stock_basis | 판정 방법 (option_api/probe_read/probe_cart) |
| run_id | 수집 실행 |

### product_attributes — 동적 속성 (속성당 1행)

| 컬럼 | 설명 |
|---|---|
| site, product_id | 상품 키 |
| attr_name | 속성 이름 — '핏', '컬러', 'lifecycle', 'ai_카테고리_대/중/소', 'brand_survival' 등 |
| value | 판정 값 (판정 실패는 아예 행이 없다 — NULL 행을 저장하지 않는 규칙) |
| basis | 판정 근거 (name/detail/image/llm/own-brand-list 등) |
| decided_at | 판정 시각 |
| ttl_days | 속성별 유효 기간 (NULL=기본 90일) |

## 물리·지원 테이블 (조인에 필요할 때만)

### 딕셔너리

```sql
sites      (site_id INTEGER PK, name TEXT UNIQUE)
contexts   (context_id INTEGER PK, name TEXT UNIQUE
            CHECK (name LIKE 'brand:%' OR 'market:%' OR 'ranking:%' OR 'adhoc:%'))
brands     (brand_id INTEGER PK, representative_name TEXT UNIQUE)
categories (category_id INTEGER PK, name TEXT,
            parent_category_id INTEGER → categories.category_id,   -- 계층 self-FK
            depth INTEGER,                                          -- 1=최상위
            UNIQUE (name, parent_category_id))                      -- 같은 이름도 부모가 다르면 다른 행
hosts      (host_id INTEGER PK, prefix TEXT UNIQUE)                 -- URL 호스트 사전
```

### 브랜드 정규화 (D65-4·5)

```sql
brand_aliases   (alias_id PK, brand_id → brands, notation TEXT,     -- 플랫폼별 표기
                 site_id → sites, source TEXT,                      -- platform/transliteration/manual
                 verify_status TEXT,                                -- confirmed/candidate/rejected
                 verified_at TEXT, UNIQUE (brand_id, notation))
brand_platforms (brand_id → brands, platform_key → platforms,       -- 브랜드-입점처 N:M
                 brand_page_url TEXT, discovered_at TEXT, product_count INTEGER,
                 PK (brand_id, platform_key))
```

별명이 묶는 것은 **표기 변형**(공백·대소문자·하이픈 — brand_key 정규화, D51)뿐이다.
한글·영문 표기는 음차 매칭을 하지 않으므로 서로 다른 브랜드 행이다 — 같은 실제
브랜드의 한/영 표기를 다 잡으려면 두 검색어를 모두 건다.

브랜드 검색 패턴 — 대표명이 안 잡히면 별명까지:

```sql
SELECT p.* FROM products p
WHERE p.brand = :검색어
   OR p.brand IN (SELECT b.representative_name
                  FROM brand_aliases a JOIN brands b ON b.brand_id = a.brand_id
                  WHERE a.notation = :검색어
                    AND COALESCE(a.verify_status,'') != 'rejected')
```

### 상품-카테고리 매핑 (D65-3)

```sql
product_categories (pk → product_base.pk,                -- 상품 물리 키 (뷰 밖 조인용)
                    category_id → categories,
                    source TEXT,                          -- 'platform'만 (AI 분류는 product_attributes)
                    site_id → sites,
                    PK (pk, category_id, source))
```

products 뷰의 `category` 컬럼이 이 매핑을 이미 경로로 펴서 주므로, 보통은 뷰만으로
충분하다. 카테고리 **트리 자체**를 다룰 때(형제 나열, 깊이별 집계)만 categories를
직접 조인한다.

### 수집 이력·기타

```sql
runs      (id INTEGER PK,                    -- 정수 PK (D65-7). 관측이 FK로 가리킨다
           run_id TEXT UNIQUE,               -- 12자리 hex — observations.run_id가 이 값
           site, story, target, collected_at, item_count, source_total,
           incomplete, notes, raw_file, loaded_at)
obs_attr  (id PK, obs_id → obs_base.id,      -- 시점별 비정형 지표 (SNS 언급수 등)
           attr_name TEXT, value TEXT, basis TEXT, UNIQUE (obs_id, attr_name))
platforms (platform_key TEXT PK, name, url, engine, recon, skill_status, updated_at)
insights  (run_stamp, target, context, verdict,   -- strong/weak/rejected
           idx, claim, audience, effect, n, p, ..., PK (run_stamp, target, verdict, idx))
```

### 변경 이력 (D68)

정적 속성·AI 속성이 바뀔 때마다 append-only 이력이 남는다. 조회는 뷰로:

```sql
product_changes (site, product_id, 현재명,        -- 뷰 — product_history를 편 것
                 field,                           -- name/brand/category/url/image_url
                 old_value, new_value, changed_at, run_id)
attr_changes    (site, product_id, attr_name,     -- 뷰 — attr_history를 편 것
                 old_value, new_value, old_basis, new_basis, changed_at)
```

- 같은 값 재수집은 이력을 만들지 않는다 — 행 하나가 실제 전이 하나다.
- category의 old/new_value는 `'대 > 중 > 소'` 경로 문자열이다.
- "리네이밍이 성과에 영향을 줬나"는 product_changes의 changed_at을 축으로
  같은 상품 observations를 전/후로 나눠 비교한다.

### 프록시 판정 (별도 DB — D65-8)

AI 파생 프록시(썸네일 컷 종류, 상품명 언어 등)의 정의·판정 캐시는 정본과 **다른
파일**(`proxy.db`)에 산다:

```sql
proxy_defs  (proxy_name TEXT PK, question, material, value_space, method,
             created_at, label, rules)
proxy_cache (proxy_name → proxy_defs ON DELETE CASCADE,
             site, product_id, fingerprint, value, basis, judged_at,
             PK (proxy_name, site, product_id, fingerprint))
```

Turso에 proxy.db가 별도 DB로 올라가 있으면 그 URL로 따로 쿼리한다. 안 올라가
있으면 프록시 관련 질문에는 "프록시 판정은 아직 조회 대상이 아니다"라고 답한다.

## 시각 다루기 — 요약

- **뷰의 모든 시각 컬럼은 TEXT다** (`'YYYY-MM-DD HH:MM:SS'`). 물리 저장이 epoch일 뿐, 조회자는 TEXT만 본다.
- ISO 형식이라 `>=`, `BETWEEN`, `ORDER BY`가 전부 시간순으로 성립한다.
- 상대 시각은 `datetime('now', '-7 days')` 패턴 — `strftime('%s', ...)`로 epoch 변환하지 않는다.
- 날짜 단위 집계: `date(observed_at)` 또는 `substr(observed_at, 1, 10)`.

## 규모 감각 (2026-08 기준, 쿼리 짤 때 참고)

| 테이블 | 대략 규모 |
|---|---|
| products | 4.5만 행 |
| observations | 6.5만 행 (하루 수천 행씩 증가) |
| product_attributes | 1.4만 행 |
| categories | 440행 · brands 2,900행 · runs 1,500행 |
| proxy_cache (별도 DB) | 48만 행 |

observations 전체 스캔은 아직 싸지만 계속 자란다 — 문맥(context)·기간으로 좁혀서
쿼리하는 습관이 안전하다.
