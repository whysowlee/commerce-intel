# 무신사 글로벌 어댑터

- 사이트: https://global.musinsa.com (무신사 해외 판매몰) · `meta.site` 값: `musinsa-global`
- 최초 실측: **2026-08-03** · 최근 확인: 2026-08-03
- **국내 무신사(`platform-musinsa`)와 별개 스킬이다.** 카탈로그·상품ID는 같은 계열이지만
  도메인·수집 경로·통화·가격 체계·평점 스케일이 다르다. 아래는 **국내와 다른 부분만**
  실측으로 적는다. "국내와 동일"이라고 명시한 것은 `platform-musinsa/references/adapter.md`를
  본다.

**규칙: 여기 적힌 것만 검증된 스킴이다.** 항목마다 실측 일자를 붙이고, 확인 안 된 것은
`「미검증」`으로 표기한다. 미검증 항목을 지우지 말 것 — 다음 실측의 할 일 목록이다.

## 0. 도메인 실측 (2026-08-03)

| 후보 | 결과 |
|---|---|
| **`global.musinsa.com`** | **200 (살아 있음)** — 루트는 `/choose-location`으로 리다이렉트 |
| `us.musinsa.com` | DNS 없음 (curl exit 6) |
| `www.musinsa.com/global` | 404 |

- **지역 접두(region-prefixed) 구조다.** URL은 `/{region}/...` 꼴이다.
  루트 `/`는 `/choose-location`으로 튀고, 지역을 고르면 `/{region}/main/fashion`으로 간다.
- 확인된 region 13종(choose-location 페이지 실측): `jp` `hk` `id` `my` `ph` `sg` `tw`
  `th` `vn`(아시아) · `ca` `us`(북미) · `au` `nz`(오세아니아). **국내(KR)는 여기 없다 —
  국내는 `www.musinsa.com`(`platform-musinsa`)이다.**
- **region마다 카탈로그·가격·통화가 다르다.** 같은 카테고리라도 총계가 다르다
  (US 데님 `003002` totalCount 14,858 vs JP 14,886, 2026-08-03 실측). **`meta`에 region을
  반드시 남긴다.** region을 안 밝히면 어느 시장 데이터인지 알 수 없다.

## 1. 접근 정책 — robots.txt (2026-08-03 실측, 국내와 다르다)

`https://global.musinsa.com/robots.txt`:

- **Claude 봇 명시 허용, 단 `/api/`만 제외.** `ClaudeBot`·`Claude-Web`·`Claude-User`·
  `Claude-SearchBot`·`anthropic-ai`가 다수 AI/검색 봇과 한 블록에 묶여
  **`Disallow: /api/` + `Allow: /`** 를 받는다.
- `User-agent: *` → **`Disallow: /`** (일반 봇 전면 차단 — 국내와 같다).
- **국내와의 차이**: 국내 `www.musinsa.com`은 `Claude-User`/`Claude-SearchBot`에
  **조건 없는 `Allow: /`** 를 준다(`/api/` 차단 없음). **글로벌은 Claude 봇에도 `/api/`를
  막는다.** 그래서 수집 경로가 국내와 갈린다(아래 §2).
- `Baiduspider` → `Disallow: /`. `EM-TrendBot`은 `/jp/*` 일부만 허용.

**수집 신분: 정직한 Claude 신원(`Claude-User`)으로 접근한다.** UA 위장 금지.
`/api/`는 Claude 봇에도 금지이므로 **XHR/백엔드 API를 부르지 않는다 — 서버 렌더 HTML만
쓴다(§2).** 403/429/캡차가 나오면 즉시 멈춘다.

### Cloudflare (2026-08-03 실측)

- 페이지에 Cloudflare 챌린지 스크립트(`__CF$cv$params`, `/cdn-cgi/challenge-platform/`)가
  삽입돼 있다. 다만 **정직한 Claude-User UA 요청은 전건 200**이었다(choose-location·main·
  category·category?page=2·PDP·sitemap 등 ~18요청, 챌린지·403·429·캡차 0건).
- **CF는 라이브 리스크로 남긴다** — 무인 반복·속도 초과 시 챌린지가 걸릴 수 있다.
  403/429/JS 챌린지가 뜨면 **즉시 중단하고 보고**한다. 우회(IP 로테이션·TLS 지문 위조·
  챌린지 풀이)는 범위 밖이다(SPEC-INTEL D30 선).

### 수집 방식 — 건별 수집만 (D30 기본선 적용, 2026-08-03)

- robots 구조가 W컨셉·EQL·SSF와 **동일**하다(`*: Disallow: /` + Claude 봇만 허용).
  D30은 이 구조를 **ⓐ 사용자 지시 건별 수집만** 대상으로 본다 — **크론 무인 축적은
  하지 않는다.** 랭킹 축적은 국내 무신사·29CM에서 한다.
- **"기본 플랫폼 스킬"이라는 것이 크론 축적 허가는 아니다** — EQL·SSF도 기본 플랫폼
  목록에 있으면서 건별-only다. 글로벌 크론 축적은 **사용자 명시 승인 전까지 보류**다
  (근거: robots `*: Disallow: /`). 이 판단의 기각 대안·근거는 SPEC-INTEL 결정 기록 참조.
- UA 기반 기술 차단은 관측되지 않았다(전 요청 200) — 그러나 차단이 없다는 것이 축적
  허가가 아니다. 선은 robots와 D30이 긋는다.

## 2. 렌더 구조 — 상품은 서버 렌더 HTML에 임베드 (2026-08-03 실측)

- 무신사 글로벌은 **Next.js(App Router, RSC) 앱**이다. **상품 데이터가 서버 렌더 HTML
  안에 JSON으로 임베드돼 온다**(`self.__next_f` 스트림). 국내처럼 별도 JSON API(`api.musinsa.com`)를
  부를 필요가 없고, **`/api/`는 robots가 막으므로 부르지도 않는다.**
- **경로 A = 카테고리/브랜드 HTML 페이지를 GET 해 임베드 JSON을 파싱**한다.
  경로 B(브라우저 렌더)는 백업 — 임베드 스킴이 바뀌었을 때만.
- choose-location·CSR 껍데기 페이지(PDP 등)는 일부 값이 얇게만 온다(§5).

## 3. 엔드포인트 (2026-08-03 실측)

| 용도 | 요청 | 실측 | 비고 |
|---|---|---|---|
| **카테고리 PLP** | `GET https://global.musinsa.com/{region}/category/{code}` | 확인 | 서버 렌더 HTML에 상품 배열 + `totalCount` 임베드. 페이지네이션 `?page=N` |
| 정렬 | 위 URL에 `?sortCode={code}` | 확인(코드 목록) | `RECOMMEND`(기본·Most popular)·`RANK`(Top rated in Korea)·`NEW`·`LOW_PRICE`·`HIGH_PRICE` |
| 카테고리 카탈로그 | `GET /sitemap/category/sitemap-1.xml.gz` | 확인 | `/{region}/category/{code}` URL 목록(gz). 코드 스킴은 **국내와 동일**(3자리 대분류 + 6자리 소분류) |
| 브랜드 | `GET /{region}/brand/{slug}` | URL만 확인 | slug = 상품객체 `brandId`. 상품 임베드·총계 구조 「미검증」 |
| 상품 상세 | `GET /{region}/products/{goodsNo}` | 확인(얇음) | CSR 껍데기(~42KB). 임베드에 `status`·`price`·`category` enum. 옵션·재고 「미검증」(§5) |
| 검색 | `/{region}/search/*` | robots에만 확인 | 스킴 「미검증」 |
| **랭킹(trending)** | `GET /{region}/trending/items?category1DepthCode={대}&category2DepthCodes={중}` | **사용자 제공 스크린샷 2026-08-03** | **region마다 다른 실시간 인기 랭킹.** 카드에 순위 번호가 매겨진다. 정렬·페이지네이션·순위 필드명·갱신 주기는 실제 수집 시 확인할 「미검증」 |

### 카테고리 PLP 상세 (2026-08-03 실측)

- **URL**: `https://global.musinsa.com/us/category/003002` (US 데님팬츠). `{region}`·`{code}`만
  바꾼다. **카테고리 코드는 국내와 같은 스킴**이다 — `001` 상의 · `003` 바지 · `003002`
  데님팬츠 등(국내 어댑터 §2의 코드 목록이 그대로 통한다. 단 region별 카탈로그 차이는 있다).
- **`totalCount`가 임베드된다** — US 데님 `"totalCount":14858`. 완전성 검증은 이 값으로 한다
  (`meta.source_total`). region마다 다른 값이다.
- **페이지네이션은 `?page=N` 서버 렌더다.** page 1과 page 2가 상품 goodsNo 교집합 0으로
  완전히 다른 세트를 준다(2026-08-03 실측: p1 150개 ∩ p2 150개 = 0). **국내처럼 hmac
  서명 nextPageUrl 체인이 아니다 — 단순 `?page=N`.**
- **⚠️ 한 페이지 HTML 안의 goodsNo에 PLP 리스트 외 모듈(추천·큐레이션)이 섞일 수 있다.**
  메인 페이지(`/{region}/main/fashion`)는 상품객체가 다른 필드셋(얇은 형태, `likeCount:0`)으로
  온다. **PLP 리스트 카드만 담아라.** 페이지당 정확한 PLP 카드 수(150이 순수 리스트인지
  추천 혼입인지)는 「미검증」 — 종료 판정은 카드 수가 아니라 **누적 distinct goodsNo가
  totalCount에 도달**하는지로 한다.
- **빈 페이지 종료 판정**: 국내는 소진 후에도 nextPageUrl이 계속 왔다. 글로벌 `?page=N`의
  소진 후 거동(빈 배열 vs 마지막 페이지 반복)은 「미검증」 — **빈/무증가 페이지가 나오면
  끝낸다**로 보수적으로 순회한다.

## 4. 필드 매핑 — 카테고리 PLP 임베드 객체 → 데이터 계약 (2026-08-03 실측)

PLP 임베드 상품 객체 원문(US 데님 실측):
```json
{"goodsNo":3791988,"goodsName":"Damage Washed Denim Pants - Medium Blue",
 "image":"//image.msscdn.net/.../3791988_..._big.jpg","imageUrl":"//image.msscdn.net/...",
 "brandId":"filluminate","brandName":"FILLUMINATE","brandLandingUrl":"/us/brand/filluminate",
 "status":"SALE","statusName":"Sale","labelList":[],"badgeList":null,"globalYn":"Y",
 "normalPrice":38,"price":26,"currencySymbol":"$","currencyCode":"USD","saleRate":31,
 "campaign":null,"likeCount":231815,"isMemberLike":false,
 "estimateCount":16720,"estimateAverage":4.8,"landingUrl":"/us/products/3791988"}
```

| 계약 | 글로벌 필드 | 국내와 비교 |
|---|---|---|
| `product_id` | `goodsNo` | **국내와 같은 goodsNo 체계** — 교차 매칭 가능(§6) |
| `name` | `goodsName` | 필드명 동일 |
| `url` | `landingUrl`(`/{region}/products/{no}`) — **상대경로, `https://global.musinsa.com` 접두** | 국내는 `goodsLinkUrl`(절대 URL, `www.musinsa.com`) |
| `image_url` | `imageUrl`(또는 `image`, 값 동일) | 국내 PLP는 `thumbnail` |
| `brand` | `brandName` | 동일. **slug는 `brandId`**(국내는 `brand`) |
| `price_original` | `normalPrice` — **외화 정수** | 필드명 동일, **통화 다름**(§4-1) |
| `price_sale` | `price` — **외화 정수, 표시 최종가** | **국내는 `finalPrice`(쿠폰적용가). 글로벌엔 쿠폰가(`finalPrice`) 개념이 없다** — `price`가 곧 최종 표시가다(§4-2) |
| `discount_rate` | `saleRate` | **국내는 `finalDiscount` 권장. 글로벌엔 `finalDiscount`가 없다** — `saleRate`가 최종 |
| `review_count` | `estimateCount` | 국내는 `reviewCount` |
| `rating` | `estimateAverage` — **이미 0~5 스케일** | **국내 PLP `reviewScore`는 0~100(÷20). 글로벌은 변환 불요** — ÷20 하면 안 된다 |
| `like_count` | `likeCount` — **PLP에 정수로 직접 노출** | **국내는 하트 배치 API가 필요했는데 글로벌은 PLP에 바로 온다.** 단 메인 페이지 모듈에선 0으로 옴 — PLP 리스트 값을 써라 |
| `sold_out` | `status`(`"SALE"`=판매중) | **국내는 `isSoldOut` 불리언 + `&isSoldOut=true` 파라미터**(§4-3) |
| `category` | PLP 페이지 문맥(요청한 category code). PDP엔 `category` enum | 국내는 상세 `baseCategoryFullPath`(§5) |
| `raw_extras` | `labelList`·`badgeList`·`campaign`·`globalYn` 원문 | 해석 말고 원문 보존(db-contract D19) |

### 4-1. ⚠️ 통화 — 외화·region별 (2026-08-03 실측) — db-contract 문제 제기

- **글로벌 가격은 KRW가 아니다.** `currencyCode`/`currencySymbol` 필드가 상품마다 온다.
  - US: `currencyCode:"USD"`, `$`, 정수 달러(`normalPrice:38, price:26`)
  - JP: `currencyCode:"JPY"`, `¥`(`¥`), 정수 엔(`normalPrice:12824, price:4990`)
- **⚠️ db-contract 문제 제기(계약 개정은 하지 않는다 — 문제만 남긴다):**
  현행 데이터 계약은 `price_original`/`price_sale`을 **정수 KRW**로 전제한다(db-contract §1).
  글로벌은 **정수 외화이고 region마다 통화가 다르다.** 정수 칸에 넣어도 형식은 맞지만
  **의미가 KRW가 아니다** — 국내 데이터와 같은 칸에 섞으면 조용히 오염된다.
  1. **`currencyCode`를 반드시 보존해야 한다.** 현행 계약엔 통화 필드가 없다.
     당장은 `meta`에 `"currency": "USD"`(region 단일 통화)를 남기고, 필요하면 item마다
     `raw_extras.currencyCode`로 병기한다.
  2. **KRW 기준 리포트·국내 교차비교는 환율 없이는 불가**다. 환율 변환은 별도 판단(SPEC 소관).
  3. SG·AU 등 **소수점 통화 여부는 「미검증」** — 정수 전제(`int`)가 깨질 수 있다.
  → 통화 필드의 계약 편입은 SPEC-INTEL 소관이다. 여기서는 문제 제기까지만 한다.

### 4-1b. ★ region이 랭킹의 feature 축이다 (사용자 지시 2026-08-03)

**같은 카테고리라도 국가마다 랭킹이 다르다** — 이건 함정이 아니라 **쓸 수 있는 축**이다.
사용자 제공 스크린샷(2026-08-03)에서 확정: `/hk/trending/items?category1DepthCode=003&
category2DepthCodes=003002`가 홍콩 인기 랭킹이고, 카드에 순위가 매겨진다. 같은 상품이
어느 나라에서 뜨는지가 곧 **해외 시장별 반응**이다(실측 예: 홍콩 데님 랭킹 상단에
자사 2000ARCHIVES 노출 — **UI 카테고리는 "여성"으로 표기됨**, 사용자 스크린샷에서 실측).

> 용어: 이 프로젝트에서 **자사**는 프로젝트를 진행하는 기업 2000ARCHIVES를 가리키고,
> **자사몰**은 플랫폼이 아니라 그 자체 사이트(`2000archives.com`, Cafe24)를 뜻한다.
> 무신사 글로벌 랭킹에서 자사 상품을 본다는 것은 "해외몰에서 우리 브랜드가 어디에 뜨나"다.

> ⚠️ **UI 성별 표기 ↔ PDF category enum 모순(2026-08-03 관측).** 이 랭킹에 노출된
> `goodsNo=3791988`의 PDP category enum은 `CLOTHING_BOTTOM_MEN`(**남성**, §5)인데
> UI 카테고리는 **여성**으로 표기됐다. 둘이 어긋난다 — 이 불일치는 미검증 목록에 있다.
> **`meta.target`의 성별은 UI 표기 기준으로 쓴다**(사용자가 화면에서 보는 그대로).

그래서 랭킹 스냅샷은 **region을 나눠 축적한다:**

- `meta.target`에 region을 넣는다 — `무신사글로벌 여성데님(HK)` 형식(성별은 **UI 표기 기준**,
  위 각주). 이러면 context가 `ranking:무신사글로벌 여성데님(HK)`로 국가마다 갈려 **시계열이
  안 섞인다.** HK 랭킹과 JP 랭킹을 한 축적으로 뭉개면 둘 다 못 읽는다(범위 다른 스냅샷 비교 금지, 스토리 C)
- `meta.region`·`meta.currency`도 함께 남긴다(통화가 region마다 다르다 — §4-1)
- 국가 비교("어느 나라에서 이 상품이 제일 잘 나가나")는 **여러 region context를 나란히**
  분석 단에서 본다 — 각 스냅샷은 한 나라의 한 시점이다
- **crontab 무인 축적은 하지 않는다**(D30). 사용자가 "HK 여성데님 랭킹 봐줘"라고 할 때
  그 자리에서 스냅샷을 찍는다. 자동 주기 축적은 국내 무신사·29CM 몫이다

미검증(실제 수집 때 확인): `trending/items`의 정렬 파라미터·페이지네이션·순위 필드명·
전 region 카테고리 코드 일치 여부·랭킹 갱신 주기. category1/2DepthCode 스킴은 국내
카테고리 코드(§2)와 같은 3자리/6자리로 **보이나** trending 경로에서의 정확한 대응은 「미검증」.

### 4-2. 쿠폰가 없음 — 국내 함정5는 글로벌에 해당 없음 (2026-08-03 실측)

- 국내는 `price`/`saleRate`가 쿠폰 전 값이고 `finalPrice`/`finalDiscount`(쿠폰적용가)를
  담아야 했다(국내 어댑터 함정5). **글로벌 임베드/PDP엔 `finalPrice`·`couponPrice`·
  `finalDiscount`가 없다.** `price`·`saleRate`가 화면 표시 최종가다.
  - PDP 실측(3791988): `price:26, normalPrice:38, saleRate:31`, `status:"SALE"`, `currencyCode:"USD"`.
    쿠폰 계열 필드 부재.
- **따라서 글로벌은 `price_sale=price`, `discount_rate=saleRate`가 맞다.** 국내 습관대로
  `finalPrice`를 찾지 마라(없다). 단 글로벌에 별도 쿠폰/프로모션 층이 생기는지는
  「미검증」 — 값이 화면과 어긋나면 재확인.

### 4-3. sold_out — status가 신호, 세부는 「미검증」 (2026-08-03 실측)

- PLP 임베드의 `status`가 재고 신호다. 관측값은 **`"SALE"`(판매중) 하나뿐**이었다
  (US 데님 1페이지 전건 SALE). PDP도 `"SALE"`.
- **품절 상품의 `status` 값(예상 `SOLDOUT`/`OUT_OF_STOCK`)은 실물을 못 봐 「미검증」.**
  또한 **글로벌 카테고리 PLP가 품절을 목록에 싣는지도 「미검증」** — 국내는 기본 품절
  제외였고 `&isSoldOut=true`로 포함시켰다. 글로벌의 대응 파라미터 존재 여부는 미확인.
- **판정 규칙**: `status=="SALE"` → `sold_out: false`. 그 외 값이 관측되면 그 값을 실물로
  확인해 매핑한다. **품절 포함 여부가 미확인이므로, PLP 단독 수집이면 "품절이 목록에서
  빠졌을 수 있다"를 `meta.notes`에 남긴다**(W컨셉의 `sold_out: null` 사례와 같은 정신).

## 5. 상품 상세(PDP) (2026-08-03 실측)

- `GET /{region}/products/{goodsNo}` — **CSR 껍데기(~42KB)**로 임베드가 얇다. 확인된 값:
  `status:"SALE"`, `price`/`normalPrice`/`saleRate`, `currencyCode`, **`category`**
  (예: `"CLOTHING_BOTTOM_MEN"` — 국내 `baseCategoryFullPath`와 다른 **coarse enum**).
- **옵션(컬러·사이즈)·옵션별 재고(variants)는 「미검증」.** 국내는 `goods-detail.musinsa.com`
  API로 옵션·재고를 얻었지만 그건 `/api/` 계열 호스트다 — 글로벌 robots·경로에선 미확인.
  옵션 수집이 필요하면 실측 전까지 하지 않는다.
- 리뷰 본문은 국내와 같이 수집하지 않는다(db-contract). `estimateCount`/`estimateAverage`로 충분.

## 6. 상품 ID 교차 매칭 (2026-08-03 실측)

- **글로벌 `goodsNo`는 국내 `goodsNo`와 같은 체계다.** `/us/products/3791988`는 국내
  `/products/3791988`과 같은 상품을 가리킨다(글로벌 카탈로그는 `globalYn:"Y"`인 국내 상품의
  부분집합). **국내 수집분과 `product_id`로 직접 교차 매칭이 가능하다** — 국내·29CM
  교차매칭이 가격 완전일치를 요구하는 것과 달리, 여기선 ID가 곧 확증이다.
- 단 **가격은 통화가 달라 매칭 키로 쓰면 안 된다**(USD vs KRW). 매칭은 `product_id`로,
  가격 비교는 통화 환산 후에만.

## 미검증 목록 (다음 실측의 할 일)

- [ ] **품절**: 품절 상품의 `status` 값, 글로벌 PLP의 품절 포함 여부·포함 파라미터
      (국내 `isSoldOut=true` 대응)
- [ ] **브랜드 모드**: `/{region}/brand/{slug}` 상품 임베드 구조·`totalCount`·품절 처리
- [ ] **검색** 엔드포인트 스킴
- [ ] **랭킹(trending)**: 경로는 `/{region}/trending/items?category1DepthCode=…`로 확정(사용자 스크린샷 2026-08-03). 남은 미검증 — 정렬 파라미터·페이지네이션·순위 필드명·갱신 주기·전 region 카테고리 코드 일치 여부. `sortCode=RANK`("Top rated in Korea")는 trending과 별개인 정렬(국내 랭킹 참조로 보임)
      (frontmatter refresh-cycle이 unverified인 이유)
- [ ] **trending UI 성별 표기 ↔ PDP category enum 관계**: 여성으로 표기된 HK 랭킹에
      노출된 `goodsNo=3791988`의 PDP enum이 `CLOTHING_BOTTOM_MEN`(남성)이라 서로 모순된다
      (§4-1b·§5). 첫 실수집 때 확인할 것 — (a) 여성 랭킹에 남성 enum 상품이 노출될 수
      있는 건지, (b) enum이 국내 기준이라 글로벌 UI와 다른 건지, (c) URL 파라미터
      어디에 성별 축이 있는지(`category1DepthCode` 값에 성별이 인코딩되는지)
- [ ] **`category1DepthCode`(단수) / `category2DepthCodes`(복수) 비대칭**: 스크린샷
      실측 그대로다. 무신사 오탈자일 가능성이 있으니 첫 수집 시 재확인 — 단수/복수가
      실제로 다른 파라미터인지, 복수가 여러 중분류를 받는지
- [ ] **옵션·재고(variants)** — 글로벌 goods-detail 계열 경로
- [ ] 페이지당 정확한 PLP 카드 수 및 추천/큐레이션 모듈 분리 규칙
- [ ] `?page=N` 소진 후 거동(빈 배열 vs 마지막 페이지 반복) — 종료 판정 정밀화
- [ ] SG·AU 등 **소수점 통화** 여부 (정수 전제가 깨지는지)
- [ ] `like_count` 갱신 특성(라이브 카운터인지 배치인지)
- [ ] 쿠키/세션 필요 여부 — 현재 불필요로 관측(전 요청 200)
- [x] 크론 축적 사용자 승인 — **받았다**(D41, 2026-08-04). region별로 나눠 축적한다
- [ ] region 전체에서 카테고리 코드가 완전히 국내와 일치하는지(US·JP만 확인)

## §R. 랭킹 — 2종이고, 갱신 시각은 브랜드 랭킹에만 있다 (2026-08-04 실측)

### 랭킹 경로 2종

| 경로 | 순위 필드 | `updatedAt` |
|---|---|---|
| `GET /{region}/trending/items?category1DepthCode=…&category2DepthCodes=…` | **없다 — 배열 순서가 순위다** | **항상 `null`** |
| `GET /{region}/trending/brands` | **`rank` 1~100 명시** | **실값 있다** |

- items: 서버 렌더 HTML의 `var goodsListJsonString` → `{error, totalCount, goodsInfoList[150], updatedAt}`.
  HK `003/003002` → `totalCount 14,926`. 카테고리 파라미터를 빼면 488,836으로 바뀐다
  (**파라미터가 서버에 실제로 반영된다는 양성 대조**)
- brands: `var brandRankingJsonString` → `{updatedAt, brandList[100]}`.
  각 항목에 `rank`·브랜드 대표상품 배열이 함께 온다
- **items에는 순위 필드가 없으므로 순위는 배열 인덱스로 매긴다.** 필드셋은 카테고리 PLP와 동일

### 갱신 시각 — brands만 사이트가 밝힌다

```
var brandRankingJsonString = "{\"updatedAt\":\"2026-08-04T00:48:44.444\", \"brandList\":[…]}"
```

- **items 랭킹의 `updatedAt`은 전 케이스 `null`이다** — 국내 무신사의
  `information.updatedAt`(30분 주기 판정 근거)에 해당하는 값을 글로벌 items에서는 못 얻는다
- **HK와 JP의 `updatedAt`이 완전히 동일**(둘 다 `2026-08-04T00:48:44.444`)한데 랭킹
  내용은 다르다(HK 2위 ITZAVIBE / JP 2위 MUCENT) → **전 region을 한 배치가 동시에
  갱신하는 것으로 보인다.** 다만 **1회 관측이라 단정하지 않는다**
- 수신(09:43 KST) 기준 약 9시간 전 값이라 **새벽 1회 배치처럼 보이나 주기는 「미검증」**이다.
  확정하려면 **다음날 같은 시각에 한 번 더** 받아 하루 단위로 움직이는지 본다
- 화면 문구에 주기 안내가 없다(번들 번역 문자열에 "Updated at" 류 없음)

### 기간별 랭킹: **없다** — 파라미터는 선언돼 있으나 서버가 무시한다

- 랭킹 번들의 요청 기본값에 **`period: null`이 실제로 선언**돼 있고 URL 직렬화 맵에도
  들어 있다. **그런데 값을 붙여도 결과가 안 바뀐다** — `period=DAILY`·`period=WEEKLY`·
  무파라미터 3종이 **응답 789,975B로 동일**, 상위 8개 goodsNo 순서까지 같다
- 번들 어디에도 `REALTIME/DAILY/WEEKLY/MONTHLY` 같은 **period 값 어휘가 없다**
- 필터 축 전체(`filterCondition`)에 기간 항목이 없고, **`sortTypeList`는 값이 하나뿐**이다
  — `{"code":"RANK","text":"Top rated in Korea"}`
- **판정: 기간별 랭킹은 축적으로만 얻는다.** `period` 파라미터를 믿지 마라

### 이 사이트의 랭킹 축은 기간이 아니라 **국가**다

번들에 `ranking.location.{kor,hkg,jpn,usa,can,aus,nzl,sgp,twn,tha,phl,mys,idn}` 번역과
`"Change country"` 토글(`title: i ? "global" : "korea"`)이 있다. UI가 주는 전환은
**"내 나라 랭킹 ↔ 한국 랭킹"**이지 기간이 아니다 — §4-1b의 region 축 판단과 일치한다.

### 부수 실측

- brands의 `category1DepthCode`는 **서버 응답을 바꾸지 않는다**(붙인 것과 안 붙인 것이
  647,647B 바이트 동일) → 브랜드 랭킹의 카테고리 필터는 **클라이언트 처리**이고 서버는
  항상 전체 Top 100을 준다
- 클라이언트 region 상수에 **`KR:"kr"`이 포함**돼 있다(확인된 13종 + kr). `/kr/` 실동작 「미검증」
- items 랭킹은 2분 간격·10분 간격 재요청에서 goodsNo 150개가 완전 동일
