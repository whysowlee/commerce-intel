---
name: platform-musinsa-global
description: 무신사 글로벌(global.musinsa.com, 무신사 해외몰)에서 상품 데이터를 수집해
  commerce-intel 데이터 계약 JSON을 만든다. 요청에 무신사 글로벌/musinsa global/글로벌
  무신사/해외 무신사가 명시되고 수집·정리 태스크가 함께 있을 때 쓴다. 예: "무신사 글로벌
  US 데님 카테고리 정리해줘", "musinsa global에서 이 브랜드 훑어줘", "무신사 글로벌 재팬
  가격 뽑아줘". **국내 무신사(www.musinsa.com) 요청은 platform-musinsa를 쓴다** — 이 스킬은
  해외몰 전용이다. **사용자 지시 건별 수집만 한다(D30)** — 크론 무인 축적에는 쓰지 않는다.
  다른 플랫폼 요청이나 상품 구매 대행에도 쓰지 않는다.
compatibility: 웹 요청 도구로 수집이 된다(서버 렌더 HTML 임베드 JSON 파싱 — /api/는 robots
  금지라 쓰지 않는다). 브라우저 제어 도구는 백업 경로용.
metadata:
  version: 0.1.0
  status: draft            # draft → ready 승격은 사용자가 한다
  refresh-cycle: unverified  # brands는 updatedAt 노출(1회 관측), items는 항상 null (§R)
  measured-at: "2026-08-03"
---

# platform-musinsa-global

무신사 글로벌(해외 판매몰)에서 **화면에 노출된 값만** 모아 데이터 계약 JSON을 만든다.
DB 적재·분석·리포트와 공통 규칙(속도·차단 중단·추정 금지·순회 상한)은 `commerce-intel`
오케스트레이터가 갖는다. `meta.site` 값은 `musinsa-global`이다.

> ⚠️ **이 스킬은 초안(draft)이다.** `「미검증」` 표기 항목은 사실이 아니라 확인 과제다.
> 미검증 항목에 의존하는 수집을 하게 되면 그 사실을 사용자에게 먼저 알린다.

> **국내 무신사(`platform-musinsa`)와 별개다.** 카탈로그·상품ID는 같은 계열이지만
> 도메인·수집 경로·통화·가격 체계·평점 스케일이 다르다. "국내와 동일"인 규칙은
> `platform-musinsa`를 참조하고, **다른 부분만** 여기서 다룬다.

## ⚠️ 수집 신분 — 건별 수집만 (D30, 2026-08-03 실측)

- 도메인은 **`global.musinsa.com`**, URL은 **지역 접두** `/{region}/...` 꼴이다
  (region 13종: jp·hk·id·my·ph·sg·tw·th·vn·ca·us·au·nz — **국내 KR은 없다**).
- robots.txt: `Claude-User`·`ClaudeBot` 등 Claude 봇을 **명시 허용하되 `/api/`만 제외**
  (`Disallow: /api/` `Allow: /`). `User-agent: *`는 `Disallow: /`. **국내와 달리 Claude
  봇에도 `/api/`를 막는다** — 그래서 **XHR/백엔드 API를 부르지 않고 서버 렌더 HTML의
  임베드 JSON만 쓴다.** (2026-08-03 실측)
- **정직한 Claude 신원(`Claude-User`)으로 접근한다. UA 위장 금지.** 403/429/캡차/CF 챌린지가
  뜨면 **즉시 중단·보고**한다(우회 금지 — SPEC-INTEL D30 선).
- **수집 방식은 ⓐ 사용자 지시 건별 수집만** — robots `*: Disallow: /` 구조가 W컨셉·EQL·SSF와
  같으므로 D30을 적용한다. **크론 무인 축적은 사용자 명시 승인 전까지 하지 않는다.** 랭킹
  축적은 국내 무신사·29CM 몫이다. ("기본 플랫폼 스킬"이 축적 허가는 아니다 — EQL·SSF도 그렇다.)

## 수집 절차

1. **`references/adapter.md`를 먼저 읽는다** — 검증된 요청 스킴·필드 매핑·함정이 실측
   일자와 함께 있다. 어댑터에 없는 스킴을 사실처럼 쓰지 않는다.
2. **region 확정** — 사용자가 어느 시장(US·JP·…)인지 밝히지 않으면 묻는다. **region마다
   카탈로그·가격·통화가 다르다.** `meta`에 region과 통화(`currency`)를 반드시 남긴다.
3. **카테고리 코드 확인** — 코드 스킴은 **국내와 동일**(3자리 대분류 + 6자리 소분류,
   `003002`=데님팬츠 등). 전체 목록은 `/sitemap/category/sitemap-1.xml.gz`(robots 허용).
   브랜드 단위 수집은 「미검증」이다(adapter.md 미검증 목록).
4. **목록 순회(경로 A)** — `GET /{region}/category/{code}?page=N`. **서버 렌더 HTML에
   상품 배열 + `totalCount`가 임베드**된다. 페이지네이션은 **`?page=N` 단순 증가**다
   (국내의 hmac nextPageUrl 체인이 아니다). 정렬은 `?sortCode=RECOMMEND|RANK|NEW|LOW_PRICE|HIGH_PRICE`.
   **단 `sortCode=RANK`("Top rated in Korea")는 수집하지 않는다** — 무신사 글로벌은
   지역 랭킹과 **한국 랭킹**을 함께 노출하는데, 한국 랭킹은 국내 `platform-musinsa`에서
   이미 수집한다. 글로벌에서 RANK까지 담으면 같은 한국 데이터가 중복된다. 글로벌의
   가치는 **지역별 랭킹**(위 §국가별 랭킹)이지 한국 랭킹의 재수집이 아니다.
5. **총계 확보** — `source_total`은 임베드 **`totalCount`** 다(US 데님 14,858 실측).
   총계를 읽은 시점의 필터 상태(품절 포함 여부 — 미확인)를 `meta.notes`에 병기한다.
6. **PLP 리스트만 담는다** — 한 페이지 HTML에 추천·큐레이션 모듈 상품이 섞일 수 있다.
   종료 판정은 카드 수가 아니라 **누적 distinct `goodsNo`가 `totalCount`에 도달**하는지로 한다.
   빈/무증가 페이지가 나오면 끝낸다.
7. **검증 대조** — 수집 건수와 `totalCount`를 나란히 보고한다. 다르면 그 차이를 밝힌다.

## ★ 국가별 랭킹 — 이 스킬의 고유 feature (사용자 지시 2026-08-03)

무신사 글로벌은 **region마다 인기 랭킹이 다르다.** 이건 국내 무신사엔 없는 축이다 —
"이 상품이 어느 나라에서 뜨나"를 볼 수 있어 **자사 브랜드 해외 반응 추적**에 직결된다.

- 경로: `GET /{region}/trending/items?category1DepthCode={대}&category2DepthCodes={중}`
  (사용자 스크린샷으로 확정 — 정렬·페이지네이션·순위 필드는 실제 수집 시 확인, adapter §4-1b)
- **region을 `meta.target`에 넣어 나눠 축적한다** — `무신사글로벌 여성데님(HK)` 형식.
  국가마다 context가 갈려 시계열이 안 섞인다. HK·JP를 한 축적으로 뭉개지 않는다
- 국가 비교는 여러 region 스냅샷을 **분석 단에서 나란히** 본다(각각 한 나라의 한 시점)
- crontab 무인 축적 금지(D30) — 사용자가 그 나라 랭킹을 물을 때 그 자리에서 스냅샷

## 이 플랫폼의 노출 지표

| 계약 필드 | 노출 여부 | 국내와 다른 점 (adapter.md §4 참조) |
|---|---|---|
| review_count | 노출 — `estimateCount` | 국내는 `reviewCount` |
| rating | 노출 — `estimateAverage`, **이미 0~5** | **국내 PLP는 0~100(÷20). 글로벌은 변환 불요** |
| like_count | **노출 — PLP에 `likeCount` 정수 직접** | 국내는 하트 배치 API 필요. 글로벌은 PLP에 바로 옴 |
| view_count | 미노출(항상 null) | — |
| purchase_count | 미노출(항상 null) | — |
| viewers_now / buyers_now | 미노출(항상 null) | trending 랭킹에 실시간 지표가 있는지는 「미검증」 |

미노출 필드는 항상 `null`이다 — 다른 출처에서 끌어와 채우지 않는다.

## 무신사 글로벌 고유 함정 (상세는 adapter.md)

- **통화가 외화·region별이다.** `price`/`normalPrice`는 정수지만 **KRW가 아니다**
  (US=USD, JP=JPY). `currencyCode`를 반드시 보존하고 `meta.currency`에 남긴다. 국내 데이터와
  같은 칸에 섞으면 조용히 오염된다. **db-contract는 정수 KRW를 전제하므로 이 스킬은 통화
  문제를 제기한 상태다**(adapter.md §4-1 — 계약 개정은 SPEC 소관).
- **쿠폰가(`finalPrice`)가 없다.** 국내 함정5(쿠폰적용가를 담아라)는 **글로벌에 해당 없다** —
  `price_sale=price`, `discount_rate=saleRate`가 화면 최종가다. 국내 습관대로 `finalPrice`를
  찾지 마라(없다).
- **`/api/`를 부르지 마라** — robots가 Claude 봇에도 막는다. 상품은 서버 렌더 HTML 임베드에서만.
- **`rating`을 ÷20 하지 마라** — 글로벌은 `estimateAverage`가 이미 0~5다.
- **품절 포함 여부가 미확인**이다 — PLP는 `status:"SALE"`만 관측됐다. PLP 단독 수집이면
  "품절이 목록에서 빠졌을 수 있다"를 `meta.notes`에 남긴다(품절 상품 `status` 값·포함
  파라미터는 「미검증」).
- **`product_id`(goodsNo)는 국내와 같은 체계다** — 국내 수집분과 ID로 직접 교차 매칭이
  된다(글로벌은 `globalYn:"Y"`인 국내 상품의 부분집합). 단 가격은 통화가 달라 매칭 키로
  쓰지 마라.

## 참고

- `references/adapter.md` — 엔드포인트·필드 매핑·함정·미검증 목록 전체 (수집 시작 전 필독)
- 스킴이 바뀐 것을 감지하면(임베드 구조 변경·필드 소실·결측 급증): 브라우저(경로 B)로
  폴백해 완주하고 `meta.notes`에 기록한 뒤 사용자에게 보고한다. 어댑터 개정은 보고 후의 일이다.

## 미검증 목록 (다음 실측의 할 일)

adapter.md 미검증 목록이 정본이다. 요약: 품절 세부·브랜드 모드·검색·랭킹·옵션 재고·
소수점 통화·크론 축적 승인·페이지네이션 종료 정밀화.
