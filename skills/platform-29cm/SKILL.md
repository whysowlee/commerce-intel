---
name: platform-29cm
description: >-
  29CM(29cm.co.kr)에서 상품·랭킹 데이터를 수집해 commerce-intel 데이터 계약
  JSON을 만든다. 요청에 29cm/29CM/이십구센티미터가 명시되고 수집·정리·모니터링 태스크가
  함께 있을 때 쓴다. 예: "29cm에서 이 브랜드 상품 정리해줘", "29cm 여성 데님 전수조사",
  "29cm 여성슈즈 랭킹 추적". 다른 플랫폼 요청이나 상품 구매 대행에는 쓰지 않는다.
compatibility: 웹 요청(JSON API) 도구로 기본 수집이 된다. 브라우저 제어 도구는 백업
  경로(경로 B)용 — 브랜드 카탈로그 화면 총계 대조에 필요하다.
metadata:
  version: 2.0.0
  status: ready
  refresh-cycle: "랭킹(HOURLY) 60분 · 그 외 1440분"
  measured-at: "2026-07-30"
---

# platform-29cm

29CM에서 **화면에 노출된 값만** 모아 데이터 계약 JSON을 만든다. DB 적재·분석·리포트와
공통 규칙은 `commerce-intel` 오케스트레이터가 갖는다.

## 수집 절차

1. **`references/adapter.md`를 먼저 읽는다** — 검증된 엔드포인트·파라미터·필드 매핑·
   함정이 실측 일자와 함께 있다.
2. **용도에 맞는 API를 고른다** — 이 사이트는 목록 경로가 둘이고 성질이 다르다:
   - **카테고리 전수 → PLP API** (`totalCount` 제공, 완전성 검증이 API 안에서 끝난다)
   - **브랜드 카탈로그 → BEST API** (`brandFacetInputs.frontBrandNo`) — **전량이 아니다**
     (실측 435 중 422, 원인 미특정). 아래 3번이 필수다.
   - **랭킹 → BEST API** (`HOURLY` + `POPULARITY`)
3. **브랜드 카탈로그는 화면 총계 대조가 필수다** — `MONTHLY` + 정렬 종류 합집합으로
   모은 뒤 브랜드 페이지의 **화면 총계(경로 B)와 대조**하고, 어긋나면 브라우저 순회로
   보완한다. `hasNext=false` 도달과 정렬 합집합 포화는 완전성 근거가 **아니다**(실측 반박).
   빠진 상품은 "상대 플랫폼 단독 입점"으로 잘못 집계되어 비교 분석을 직접 오염시킨다.
4. **랭킹 창을 카탈로그 수집에 쓰지 마라** — `HOURLY`로 브랜드를 모으면 카탈로그의
   94%가 사라진다(실측 26/435). `MONTHS_3`·`MONTHS_6`은 HTTP 500.
5. **dedup 필수** — 랭킹순 페이지네이션 중 순위가 흔들려 중복 상품이 온다(실측 422→421).
   `product_id` 기준으로 중복을 제거한다.
6. **품절은 기본 포함이다**(필터 이름이 `품절상품 제외`) — 무신사와 비교할 때는
   무신사 쪽을 `isSoldOut=true`로 맞춘다.

## 이 플랫폼의 노출 지표

| 계약 필드 | 노출 | 주의 |
|---|---|---|
| review_count / rating | 노출 | |
| like_count | 노출(`heartCount`) | |
| view_count | **미노출**(필드 자체 없음) | 항상 `null` |
| purchase_count | 기본 꺼짐(`isDisplaySellQty: false`) | 사실상 `null` |
| viewers_now / buyers_now | 없음 | 항상 `null` |
| category | 목록에 **코드로만**(`extraInfo.categories[]`) | 트리 API로 이름 해석. `smallCategoryCode`가 `null`이면 중분류 폴백 |

## 29CM 고유 함정 (상세는 adapter.md·EVIDENCE)

- **가격이 세 개다**: `originalPrice`/`sellPrice`/`displayPrice`. `price_sale`은
  **`displayPrice`(쿠폰적용가)**, `discount_rate`는 `saleRate`(같은 기준).
  상세의 `totalDiscountedItemPrice`는 **개인화 가격**(55% 불일치 실측) — 쓰지 않는다.
- **소분류 랭킹은 독립 랭킹이다** — 중분류 필터가 아니다(데님 top 100의 84%가 바지
  top 100에 없음). 폴백으로 범위를 낮추면 `meta.target`을 낮춘 이름으로 저장한다.
- 카테고리 목록은 `&page=` 페이지네이션 — 빈 페이지 + "검색 결과가 없습니다"로
  결정적으로 끝난다. **추천순 순회 금지**(깊은 페이지 재배열로 중복·누락, 실측
  13,811/17,093) — 결정적 정렬로 돈다.
- 경로 B에서: 카드 가격에 `원`이 없다(`57,960`) — `원` 기준 카드 탐색은 이웃 카드가
  섞인다. 카드는 `li`, 카드당 `/catalog/` 링크 4개.
- 브랜드 ID 확증은 브랜드 페이지 `__NEXT_DATA__`의 `brandId`/`nameKor`로 한다.

## 옵션(컬러·사이즈) 수집 — L1 소스

옵션 축·값 형식은 리뷰 `optionValue`(`[COLOR:SIZE]IVORY:S_01`)와 상품 옵션 응답에서
확인된다. 옵션별 수량은 미노출 — 수량이 필요하면 오케스트레이터의
`references/variant-collection.md` 프로브 계층을 따른다(L3는 사용자 승인 필수).

## 참고

- `references/adapter.md` — 엔드포인트·필드 매핑·함정 전체 (수집 시작 전 필독)
- 스킴 변화 감지 시: 경로 B로 폴백해 완주하고 보고. 어댑터 개정은 보고 후.
