# 데이터 계약과 정본 DB

## 1. 수집 JSON 계약

플랫폼 스킬의 산출물은 항상 이 모양이다. 하류 스크립트 전부가 이걸 전제한다.

```json
{
  "meta": {
    "site": "musinsa",
    "story": "brand-linesheet",
    "target": "인사일런스",
    "collected_at": "2026-07-31 14:00:00",
    "item_count": 2022,
    "source_total": 2022,
    "incomplete": false,
    "notes": ["경로 A: 목록 API 순회. 품절 포함(isSoldOut=true) 상태의 totalCount 2,022와 대조"]
  },
  "items": [{
    "product_id": "4297589",
    "name": "...", "url": "https://...", "image_url": "https://...",
    "brand": "인사일런스", "category": "데님팬츠",
    "price_original": 139900, "price_sale": 97890, "discount_rate": 30,
    "review_count": 212, "rating": 4.8,
    "view_count": null, "view_count_display": "300회 이상 (최근 1개월)",
    "purchase_count": 5500, "purchase_count_display": null,
    "like_count": 84, "like_count_display": null,
    "viewers_now": null, "buyers_now": null,
    "sold_out": false,
    "rank": null,
    "attributes": {"핏": "와이드"}, "attributes_basis": "name"
  }]
}
```

| 필드 | 언제 필수인가 |
|---|---|
| `product_id` `name` `url` `image_url` `brand` `category` `price_original` `price_sale` `discount_rate` `sold_out` | 항상. 결측률 5% 초과 경고, 30% 초과 수집 실패 |
| `review_count` `rating` `view_count` `purchase_count` `like_count` `viewers_now` `buyers_now` | 사이트가 보여줄 때만. **안 보이면 `null`** — 0과 다르다 |
| `view_count_display` `purchase_count_display` `like_count_display` | 구간·축약 표기일 때 원문 보존 |
| `rank` | 랭킹 스냅샷 |
| `attributes` `attributes_basis` | 전수조사. basis는 `name`/`detail`/`image`/`group`/`unknown` |

- `site`는 사이트 식별자다 — `musinsa`, `29cm`, 자사몰은 도메인 문자열(`insilence.co.kr`).
  값의 종류를 코드가 제한하지 않는다 — 새 플랫폼은 새 값으로 들어온다.
- `reviews[]`(리뷰 본문)는 수집하지 않는다. 담겨 있으면 검증기가 경고한다.
- **`raw_extras`는 선택 필드다**(재료 보존 — D19). 화면에 노출된 부가 표시물(배지·
  라벨 문자열)을 원문 그대로 담는다: `{"badges": ["[아이유 착용]"], "labels": [...]}`.
  해석하지 않는다 — 파생 프록시의 재료다(`proxy-extraction.md`). DB에 컬럼으로
  저장되지는 않는다(D65-2) — raw JSON이 원문 보존처다.
- **`obs_attrs`는 선택 필드다**(D65-6). 계약 밖 **시점 지표**(SNS 언급수·트렌드 점수
  등)를 `{"이름": 값}` 또는 `{"이름": {"value": 값, "basis": "api"}}`로 담으면
  적재가 관측에 매달아(obs_attr) 시계열로 쌓는다. 값 `null`은 저장하지 않는다.
- **`variants[]`는 선택 필드다**(옵션 정교화 — SPEC-INTEL §6). 스키마·프로브 계층·
  판매수량 계산 규칙은 `variant-collection.md`가 정본이다. 없음/`null` = 미수집,
  `[]` = 옵션 없는 상품 — 구분한다. 옵션별 판매수량은 계약에 없다(재고 감소분으로 계산).
- **멀티 플랫폼도 계약을 바꾸지 않는다** — 수집 JSON은 언제나 사이트 하나짜리고,
  합집합·매칭은 리포트 생성기가 여러 JSON을 받아 계산한다.
- `meta.target`은 실제 수집 범위를 정직하게 쓴다 — 좁혀 놓고 "전수"라고 쓰지 않는다.

### 값 규칙 (실측 근거는 docs/EVIDENCE.md §4)

- **`price_sale` = 전 회원 공통 쿠폰적용가.** 개인화 가격(「나의 구매 가능 가격」 류)은
  담지 않는다. `discount_rate`도 같은 기준이어야 한다.
- **`rating`은 5점 만점 통일.** 무신사 목록 API는 0~100이므로 나눠 담는다.
- **축약 표기(`1.2천`·`판매 9만개`)는 정수로 파싱해 담는다**(`천`=×1,000 · `만`=×10,000,
  오차 ≤±4% 유계). 원문은 `*_display`에 병기 가능.
- **구간 표기(`300회 이상`)는 정수로 바꾸지 않는다**(오차 무한). 정수 칸 `null`,
  원문만 `*_display`에. 정수와 병기하면 검증기가 경고한다.
- **`source_total`에 수집 건수를 넣지 않는다** — 자기 자신과 비교하는 순환 검증이 된다.
  독립 총계(API totalCount 또는 화면 총계)만 담고, 없으면 `null` + notes에 근거.
  총계를 읽은 시점의 **필터 상태(품절 포함 여부 등)를 notes에 함께 적는다.**

## 2. 정본 DB (SQLite `data/intel.db` — 스키마 v3, D65 · 프록시 표 포함 D69)

파이프라인: 수집 → raw JSON → 검증 → **적재(load)** → 시트 미러. 도구는 `scripts/intel_db.py`.
아래 표 이름은 전부 **뷰**다(D45) — 물리 저장은 정수 사전·대리키(`schema_v3.py`)이고,
읽고 쓰는 계약은 뷰가 진다. v1·v2 파일은 열리지 않는다 — `migrate_v3.py`로 이관한다.

**클라우드 정본 (Turso — D67).** `INTEL_DB_URL=libsql://...` + `INTEL_DB_TOKEN`이
있으면 모든 도구가 클라우드 정본을 본다(프록시 표도 같은 정본 안이다 — D69).
환경변수가 없으면 지금처럼 로컬 파일이고, **명시적 `--db` 인자는 항상 환경변수를
이긴다**(테스트·리허설 격리). DB는 반드시 `schema_v3.open_db()`/`intel_db.connect()`로
연다 — libsql 드라이버의 비호환(행 튜플·예외 타입 등)을 호환 래퍼가 흡수하는
유일한 통로다. 세팅·이관·팀 온보딩은 `docs/TURSO-SETUP.md`. 수집 시작 전
`intel_db.py check-run`으로 공유 runs의 최근 중복 수집을 확인한다.

| 테이블 | 내용 | 키 | 갱신 |
|---|---|---|---|
| `products` | 정적 속성 — 이름·URL·이미지·브랜드(대표명)·카테고리(계층에서 도로 편 파생 값)·`static_verified_at`. **attributes JSON·first/last_seen_at·raw_extras는 v3에서 제거**(D65-2 — 속성은 product_attributes가 유일 정본, 원문 부가 정보는 raw JSON에 있다) | (site, product_id) | upsert — 새 값이 비어 있지 않을 때만 덮는다 |
| `product_attributes` (D35) | **동적 속성 — 축을 행으로.** attr_name(핏·컬러·소재·시즌·`ai_카테고리_대/중/소`)·value·basis·decided_at·**ttl_days**(속성별 만료). 수집 JSON의 attributes도 적재 때 여기로 직행한다. AI 분류 카테고리는 `ai_카테고리_*` 행(basis=llm)이다 — 플랫폼 카테고리(product_categories)와 섞지 않는다(D65-3). **판정 실패(null)는 저장하지 않는다** | (site, product_id, attr_name) | upsert (set-attrs) |
| `categories` | **계층 카테고리** — name·parent_category_id(self-FK)·depth(플랫폼이 정해준 깊이, 1=최상위). 같은 이름이 다른 부모 아래 공존한다(UNIQUE(name, parent)) | category_id | 적재가 경로를 분해해 자동 생성 |
| `product_categories` | 상품-카테고리 **N:M** — source='platform'(플랫폼 원본만). 쓰기는 파이썬 `assign_category()`가 유일한 통로다(뷰 트리거는 경로 분해를 못 한다) | (pk, category_id, source) | insert or ignore |
| `brands` + `brand_aliases` | 브랜드 **대표명**(representative_name) + 플랫폼별 표기 별명. 수집 표기가 대표명·별명에 없고 brand_key(D51)로 기존 브랜드와 일치하면 **candidate 별명**이 자동 등록된다(`resolve_brand`). 확정/기각은 사람이 verify_status로. 음차 매칭은 하지 않는다 | brand_id / (brand_id, notation) | 적재가 자동 |
| `brand_platforms` | 브랜드-입점처 **N:M** — brand_page_url·discovered_at·product_count. 구 `platforms.discovered_for_brand`(쉼표 텍스트)를 대체(D65-5) | (brand_id, platform_key) | channel-scout 결과 적재 |
| `observations` | 시변 값 전부 + `context` + `run_id`(runs.id 정식 FK — D65-7) | (site, product_id, observed_at, context) | **append only** |
| `obs_attr` | **시점별 비정형 지표**(D65-6) — SNS 언급수·트렌드 점수 등 간헐 지표를 관측 id에 key-value로. 고정 지표는 observations 컬럼 그대로. 수집 JSON의 `items[].obs_attrs`가 여기로 온다 | (obs_id, attr_name) | upsert |
| `platforms` | **누적 입점처 카탈로그** — 브랜드 모드·카테고리 모드·특화 탐색 어느 경로로 발견됐든 전부 여기 쌓이고 잊히지 않는다. `recon` JSON에 정찰 결과, 카탈로그 기준 충족 여부(`fashion_catalog: true` — ①패션/해당 품목 주력 ②비로그인 열람 ③규모 확인 가능), **`specialty`(특화 품목 또는 "종합")** 와 **생존 상태(활성/철수/폐업/차단 + 마지막 확인 시각)**. 죽은 플랫폼도 지우지 않고 상태만 바꾼다. `skill_status`(none/candidate/recon_done/draft/ready) | platform_key | upsert |
| `variants` | 옵션(SKU) 구성 — option_id·option_name·color·size (seen_at 제거 — D65-9) | (site, product_id, option_id) | upsert (빈 값은 기존 값 유지) |
| `variant_observations` | 옵션별 재고 관측 — sold_out·stock_qty·stock_display·`stock_basis` + `run_id` FK | (site, product_id, option_id, observed_at) | **append only** |
| `runs` | 수집 실행 이력(raw 파일 경로 포함) — **정수 PK `id`**(관측이 FK로 가리킨다), run_id TEXT는 UNIQUE | id | append |
| `sync_state` | 시트 미러 진행 상태 (proxy_cache 진행점도 여기 — 미러 상태는 미러를 도는 쪽 것) | table_name | 내부용 |
| `product_history` | **정적 속성 변경 이력** (D68) — pk·field(name/brand/category/url/image_url)·old_value·new_value·changed_at·run_id. 적재(`_upsert_product`)가 기존 값과 다른 실질값이 오면 덮기 전에 남긴다. 값→NULL은 안 생긴다(빈 값은 덮지 않으므로). 조회는 `product_changes` 뷰((site, product_id)·현재명·run_id 포함) | id | **append only — 삭제·수정 금지** |
| `attr_history` | **AI 속성 변경 이력** (D68) — attr_name·old/new_value·old/new_basis. `product_attributes` 뷰 트리거가 캡처하므로 어떤 쓰기 경로든 잡힌다. 조회는 `attr_changes` 뷰 | id | **append only** (트리거 자동) |

**프록시 표 (본 DB 통합 — D69).** D65-8에서 별도 `proxy.db`로 갈랐던 프록시
표는 D69로 본 DB(intel.db)에 재통합됐다 — lazy 판정 전환 이후 캐시가 천천히
쌓여 분리의 이점이 약해졌고, FK·트리거·자동동기화를 못 하는 대가가 더 커졌다.
`PROXY_DB_URL`·`PROXY_DB_TOKEN`·`INTEL_PROXY_DB`는 평시 런타임에서는 폐기 — 같은
커넥션으로 직접 조인한다. 단 `migrate_proxy_merge.py`(레거시 proxy.db 1회성 이관)만은
이 변수들을 레거시 소스 위치 지정용으로 읽는다(PROXY_DB_URL > INTEL_PROXY_DB > 정본 옆 proxy.db).

| 테이블 | 내용 | 키 |
|---|---|---|
| `proxy_defs` | 정의 카드 — question·material·value_space·method·label + **rules**(rule 카드 본문 JSON — lazy 판정 재료) | proxy_name |
| `proxy_cache` | 판정 캐시 — value·basis. **재료 지문이 현재 재료와 같을 때만 유효**. `proxy_defs`에 **ON DELETE CASCADE** — 정의를 지우면 그 계약의 판정도 함께 사라진다(proxy-audit --fix는 보조 수단) | (proxy_name, site, product_id, fingerprint) |
| `proxy_history` | **판정 전이 이력** (D68) — old/new_value·old/new_fingerprint. **new_value NULL = 재판정 대기**: 재료(이름·이미지) 변경 감지가 옛 판정을 여기로 옮기고 캐시에서 지운다 → 다음 lazy/proxy-load 판정이 그 행을 완성한다(`record_proxy_transition`). 강제 즉시 재판정은 없다 — lazy 원칙대로 분석이 요구할 때 판정된다. **같은 재료에서 나온 AI 속성 판정(attr_base, basis=재료명)도 함께 무효화된다**(D71 — attr_history에 new_value NULL 행을 남기고 삭제, 같은 트랜잭션) | id |

- **판정은 method 불문 lazy가 기본이다**(D65-8·D66): 정의는 즉시 등록하되(image
  카드는 실물 샘플 1~2장 접지 — D66) 판정은 미룬다. rule은 분석(`intel_data.collect`)이
  캐시 miss를 defs.rules로 그 자리에서 판정해 캐시에 남기고(전량 선행은
  `proxy_auto.py --eager`), vision·llm은 분석이 그 축을 요구할 때 미캐시분만
  **협의 규칙(비용 보고 → 대규모면 범위 합의)** 을 거쳐 배치(proxy-extractor)로 돈다.
- `context` = `{brand|market|ranking|adhoc}:{target}`. **접두사 4종은 contexts CHECK
  제약이 강제한다**(D65-1) — 모르는 story는 적재가 `adhoc:`으로 접는다. 관측의 출처
  화면을 보존한다 — 랭킹에만 노출되는 `viewers_now`를 다른 문맥에 섞으면 일관성이 깨진다.
- 기존 축적 스냅샷은 `intel_db.py import-snapshots data/snapshots`로 소급 적재한다.

## 3. 재사용 정책 (SPEC-INTEL §2-2)

| 대상 | 규칙 | 명령 |
|---|---|---|
| 정적 속성 | `static_verified_at` 90일 이내면 재확인하지 않는다. 만료분은 다음 수집에서 재확인 | `reuse-attrs`(수집 JSON에 채움) · `stale-static`(만료 목록) |
| 시변 값 | 같은 (site, context) 최신 관측이 갱신 주기 이내면 수집 생략 | `check --cycle-minutes N` (exit 0 = 스킵 가능) |
| **플랫폼 정찰** | `platforms.updated_at` 90일 이내면 channel-scout가 재정찰하지 않는다 — 오케스트레이터가 기존 정찰을 스폰 프롬프트로 넘긴다. 브랜드별 **입점 여부**는 매번 새로 확인한다(시변) | `export --table platforms --format json` |
| **팀원 수집분** (D32) | 로컬이 신선하지 않을 때만 시트 `runs` 탭을 본다. 같은 (site, context)를 팀원이 갱신 주기 안에 수집했으면 중복. 주기 밖이면 이력만 알리고 통과 | `check --team` (내 `run_id`는 제외된다) |
| 사용자 명시 재수집 | 두 규칙 모두 무시 | — |

**스킵은 반드시 보고한다** — "마지막 관측이 N분 전이라 재수집을 생략했다. 새로 받으려면
말해달라." 조용히 옛 데이터를 쓰지 않는다.

### `check --team` 출력 읽는 법

```jsonc
{
  "skip": true, "source": "team",          // local = 내 DB / team = 팀원 수집분
  "team": {
    "consulted": true,                     // false면 시트를 못 봤다 → 로컬 판정만으로 진행
    "fresh": true,                         // 갱신 주기 이내인 팀 수집이 있다
    "last_collected_at": "2026-08-03 11:33:17",
    "run_count": 3                         // 내 run_id를 뺀 팀 수집 이력 건수
  }
}
```

- `consulted: false`는 **오류가 아니라 상태**다(키 미설정·쿼터·`runs` 탭 없음). `error`에
  사유가 들어오고 exit 코드는 로컬 판정 그대로다 — 수집은 막히지 않는다.
- 시트는 **완료된 수집만** 안다. `runs` 행은 적재 후 미러 때 올라가므로 "지금 수집 중"은
  잡히지 않는다 — 중복 창이 미러 주기만큼 남는다.
