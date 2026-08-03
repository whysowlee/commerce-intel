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
  해석하지 않는다 — 파생 프록시의 재료다(`proxy-extraction.md`).
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

## 2. 정본 DB (SQLite `data/intel.db`)

파이프라인: 수집 → raw JSON → 검증 → **적재(load)** → 시트 미러. 도구는 `scripts/intel_db.py`.

| 테이블 | 내용 | 키 | 갱신 |
|---|---|---|---|
| `products` | 정적 속성 + `attributes`(핏 등 비싼 판단) + `static_verified_at` | (site, product_id) | upsert — 새 값이 비어 있지 않을 때만 덮고, 실질 attributes는 기존 값을 지킨다 |
| `observations` | 시변 값 전부 + `context` | (site, product_id, observed_at, context) | **append only** |
| `platforms` | **누적 입점처 카탈로그** — 브랜드 모드·카테고리 모드·특화 탐색 어느 경로로 발견됐든 전부 여기 쌓이고 잊히지 않는다. `recon` JSON에 정찰 결과, 카탈로그 기준 충족 여부(`fashion_catalog: true` — ①패션/해당 품목 주력 ②비로그인 열람 ③규모 확인 가능), **`specialty`(특화 품목 또는 "종합")** — 특화몰을 무관한 상품군 후보에서 거르는 데 쓴다 — 와 **생존 상태(활성/철수/폐업/차단 + 마지막 확인 시각)**. 죽은 플랫폼도 지우지 않고 상태만 바꾼다(이력도 데이터다). `skill_status`(none/candidate/recon_done/draft/ready) | platform_key | upsert |
| `variants` | 옵션(SKU) 구성 — option_id·option_name·color·size | (site, product_id, option_id) | upsert (빈 값은 기존 값 유지) |
| `variant_observations` | 옵션별 재고 관측 — sold_out·stock_qty·stock_display·`stock_basis`(option_api/probe_read/probe_cart) | (site, product_id, option_id, observed_at) | **append only** |
| `proxy_defs` | 파생 프록시 정의 카드 — question·material·value_space·method | proxy_name | upsert |
| `proxy_cache` | 프록시 판정 캐시 — value·basis. **재료 지문(fingerprint)이 현재 재료와 같을 때만 유효** — 이미지 교체 시 자동 무효화 | (proxy_name, site, product_id, fingerprint) | insert only |
| `runs` | 수집 실행 이력(raw 파일 경로 포함) | run_id | append |
| `sync_state` | 시트 미러 진행 상태 | table_name | 내부용 |

- `context` = `{brand|market|ranking|adhoc}:{target}`. **관측의 출처 화면을 보존한다** —
  랭킹에만 노출되는 `viewers_now`를 다른 문맥에 섞으면 일관성이 깨진다.
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
