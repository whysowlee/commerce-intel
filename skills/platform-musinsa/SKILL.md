---
name: platform-musinsa
description: 무신사(musinsa.com)에서 상품·랭킹 데이터를 수집해 commerce-intel 데이터 계약
  JSON을 만든다. 요청에 무신사/musinsa가 명시되고 수집·정리·모니터링 태스크가 함께 있을 때
  쓴다. 예: "무신사에서 인사일런스 상품 다 모아줘", "무신사 데님팬츠 전수조사", "무신사
  바지 랭킹 모니터링 시작". 다른 플랫폼 요청이나 상품 구매 대행에는 쓰지 않는다.
compatibility: 웹 요청(JSON API) 도구로 기본 수집이 된다. 브라우저 제어 도구는 백업
  경로(경로 B)용.
metadata:
  version: 2.0.0
  status: ready
  refresh-cycle: "랭킹(실시간) 30분 · 그 외 1440분"
  measured-at: "2026-07-30"
---

# platform-musinsa

무신사에서 **화면에 노출된 값만** 모아 데이터 계약 JSON을 만든다. DB 적재·분석·리포트와
공통 규칙(속도·차단 중단·추정 금지·순회 상한)은 `commerce-intel` 오케스트레이터가 갖는다.

## 수집 절차

1. **`references/adapter.md`를 먼저 읽는다** — 검증된 엔드포인트·파라미터·필드 매핑·
   함정이 실측 일자와 함께 있다. 어댑터에 없는 스킴을 사실처럼 쓰지 않는다.
2. **동명 브랜드 확인 (절차 불변식)** — 브랜드 작업이면 slug를 이미 알아도 **반드시
   브랜드 이름으로 한 번 검색한다.** 일부 브랜드는 성별·지역 매장으로 분산돼 있다
   (예: 인사일런스 + 인사일런스 우먼). 동명 매장이 2개 이상이면 제시하고 사용자가
   고르게 한다.
3. **품절 포함** — 목록 기본값이 **품절 제외**다. `&isSoldOut=true`를 붙여야 전량이다
   (실측: 563 → 2,022). 붙이기 전/후 총계를 대조해 `meta.notes`에 필터 상태를 남긴다.
   유효한 파라미터는 `isSoldOut` 하나뿐이다 — `soldOut=true`는 조용히 무시된다.
4. **경로 A로 순회** — 목록 API가 `totalCount`를 주므로 완전성 검증이 API 안에서 끝난다.
   페이지 URL을 조립하지 말고 **항상 `nextPageUrl` 체인**을 따른다(직접 조립은 403).
5. **성별 필터는 경로 A만 신뢰한다** — 화면은 스크롤하면 `gf`를 잃고 `gf=A`로 나간다
   (실측: 여성 3,004 요청이 5,010으로 불어남). 성별 축은 PLP API로만.
6. **검증 대조** — 수집 건수와 totalCount를 나란히 보고한다.

## 이 플랫폼의 노출 지표

| 계약 필드 | 노출 | 주의 |
|---|---|---|
| review_count / rating | 노출 | **목록 API는 0~100 스케일** — 5점으로 나눠 담는다 |
| like_count | 노출 | 하트 배치 API가 **정수**를 준다(축약 파싱 불요) |
| view_count | **구간 표기**(`300회 이상 (최근 1개월)`) | 정수 변환 금지 — `view_count_display`에 원문만 |
| purchase_count | 랭킹 배지 + 실시간 지표 원시값 API | |
| viewers_now / buyers_now | **랭킹 화면에만** | 다른 스토리에서는 구조적 `null` |
| category | **목록에 없다** — 상세 `baseCategoryFullPath` | 스토리 A의 지배 비용. 상세 HTML은 charset 선언이 없다 — **UTF-8 명시 디코드** |

## 무신사 고유 함정 (상세는 adapter.md·EVIDENCE)

- **브랜드 URL이 두 개다**: `/brand/{slug}`는 홈(일부만, 실측 128/563),
  전 상품은 `/brand/{slug}/products`. 화면 총계와 대조해야 구분된다.
- **랭킹 코드가 경로별로 다르다**: 경로 A는 대분류 3자리(`003`), 화면(경로 B)은
  6자리(`003000`) — 화면에 `003`을 주면 에러 없이 **다른 랭킹**이 온다.
- 랭킹은 top 100 고정, 원본 갱신 주기 30분. 스냅샷은 한 번에 끝낸다(시점 혼합 금지).
- `price_sale` = `finalPrice`(쿠폰적용가), `discount_rate` = `finalDiscount`.
  구 `price`/`saleRate`는 쿠폰 전 값이라 쓰지 않는다.
- 검색 API의 `caller=BRAND` totalCount=0을 믿지 마라 — 브랜드 검색은 어댑터의
  전용 엔드포인트로 한다.

## 옵션(컬러·사이즈) 수집 — L1 소스

상세 `goodsOption`이 옵션 구성을 준다. 상품 단위 `isSoonOutOfStock`·`isRestock`도 함께.
옵션별 **수량**은 노출되지 않는다 — 수량이 필요하면 오케스트레이터의
`references/variant-collection.md` 프로브 계층(L2/L3)을 따른다(L3는 사용자 승인 필수).

## 참고

- `references/adapter.md` — 엔드포인트·필드 매핑·함정 전체 (수집 시작 전 필독)
- 스킴 변화 감지 시(400/404·필드 소실·결측 급증): 경로 B로 폴백해 완주하고 보고한다.
  어댑터 개정은 보고 후 지시에 따른다.
