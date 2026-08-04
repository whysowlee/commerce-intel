# W컨셉 어댑터

- 사이트: **국내** https://www.wconcept.co.kr (브랜드관 https://display.wconcept.co.kr ·
  PLP API https://api-display.wconcept.co.kr) · **글로벌** https://www.wconcept.com —
  `meta.site` 값: `wconcept` / `wconcept-global`
- 최초 실측: 2026-07-31 · 최근 확인: **2026-08-03 (국내 PLP API 실측 — 주 경로)**

**규칙: 여기 적힌 것만 검증된 스킴이다.** 항목마다 실측 일자를 붙이고, 확인 안 된 것은
`「미검증」`으로 표기한다. 미검증 항목을 지우지 말 것 — 할 일 목록이다.

> ✅ **건별 수집 상태다 (2026-08-03 D30).** 2026-07-31 "수집 포기" 결정은 **국내 robots
> 오독**에 근거했고, 재실측으로 뒤집혔다. 국내 robots는 `ClaudeBot`·`Claude-User`를 명시
> 허용하고, 브라우저 UA 헤더 3종으로 PLP API 200을 받는다. **주 경로는 국내 PLP(§A)**,
> 글로벌(§B)은 보조다. `sold_out`은 두 경로 모두 구조적으로 `null`이다(§A는 PLP가 품절
> 제외, §B는 재고 필드 부재). **제약: 건별 수집만·크론 축적 금지**(D30). 상세는 SKILL.md
> 결정절과 `docs/SPEC-INTEL.md` D30.

---

## §A. 국내 PLP API — 주 경로 (확인, 2026-08-03)

**국내가 이제 주 경로다.** `api-display.wconcept.co.kr`의 카테고리 상품 API가 필수 9필드와
총계·딥 페이지네이션을 응답 안에서 준다. `sold_out`만 구조적으로 불가하다.

### 접근 (2026-08-03 실측)

- 국내 robots(`www.wconcept.co.kr`·`display.wconcept.co.kr`)는 `User-agent: *`에
  `Disallow: /`지만 **§4에서 `ClaudeBot`·`Claude-User`를 `Allow: /`로 명시 허용**한다
  (2026-07-31에 이 화이트리스트 섹션을 못 보고 "전면 차단"으로 오독). 단
  `display.wconcept.co.kr`의 `/api/`·`/rn/api/`는 **모든 그룹에 Disallow**.
- 정직한 신원으로 상세를 요청하면 **403**(선언 정책과 서버 집행 불일치). **브라우저 UA
  헤더 3종**(Chrome UA + `Accept` + `Accept-Language: ko-KR`)으로 **200** — 재시도·백오프
  불필요. 선은 UA·Accept 헤더까지다(캡차 우회·IP 로테이션·TLS 지문 위조는 범위 밖 — D30).
- PLP 호스트 `api-display.wconcept.co.kr`는 **별도 호스트로 robots.txt를 서빙하지 않는다**
  (키 없는 요청에 401 JSON). robots 부재 호스트를 어떻게 취급할지는 **프로젝트 정책 판단으로
  남긴다** — 여기에는 사실만 기록한다.

### 목록 API (핵심, 2026-08-03 실측)

```
POST https://api-display.wconcept.co.kr/display/api/v2/category/products/{mediumCd}/{largeCategory}
Headers:
  display-api-key: <DISPLAY_API_KEY>   ← 비로그인 방문자 전원에게 내려가는
                                          __NEXT_DATA__.runtimeConfig.DISPLAY_API_KEY 공개 상수.
                                          탈취 토큰이 아니다
  Content-Type: application/json; charset=UTF-8
  devicetype: PC
Body: {"custNo":"","gender":"All","sort":"WCK","pageNo":1,"pageSize":60,"bcds":[],"colors":[],
       "benefits":[],"discounts":[],"status":["01"],"shopCds":[],"domainType":"pc"}
```

- `custNo:""`(익명)으로 정상 응답.
- **`pageSize`는 서버가 60으로 강제**(200 요청해도 60 반환). `pageNo`를 올려 순회한다.
  **딥 페이지네이션 상한 없음** — 최종면 `pageNo=7936`도 60건 정상.
- **카테고리 SSR HTML(`__NEXT_DATA__`)에는 상품 목록이 없고 `productTotal`만 있다** —
  목록은 이 POST API로만 얻는다.
- 카테고리 트리: `GET /display/api/v2/category/list?categoryType=women&gender=women&category=001&shopcd=`
  (PC용은 `/list/pc`).
- 정렬(`sort`): `NEW`(신상)·`WCK`(추천)·`SALE`(판매순)·`DISCOUNT`·`LOWPRICE`·`HIGHPRICE`·`REVIEW`.

### 필드 매핑 (계약 필드 ← PLP 응답, itemCd 305914779 실측, 2026-08-03)

| 계약 필드 | 응답 필드 | 주의 |
|---|---|---|
| product_id | `itemCd` | |
| name | `itemName` | |
| url | `www.wconcept.co.kr` + `webViewUrl` | 상대경로 조립 |
| image_url | `imageUrlMobile` | |
| brand | `brandNameKr` / `brandNameEn` / `brandCd` | |
| category | `categoryDepthName1/2/3` + `mediumName` | 3뎁스, 목록에 나온다 |
| price_original | `customerPrice` | KRW |
| price_sale | `finalPrice` | **전 회원 공통 쿠폰적용가로 검증됨.** W컨셉이 상세 화면에서 "쿠폰적용가 [할인내역] 69,025원 29%"로 **문구 직접 라벨링**(29CM과 달리 추론 아님). 개인화 가격(신규회원가·카드사 할인)은 별도 칸으로 분리 |
| discount_rate | `finalDiscountRate` | |
| review_count | `reviewCnt` | |
| rating | `reviewScore` | 4.921 관측 — **5점 만점 그대로** |
| like_count | `heartCnt` | |
| sold_out | **PLP로 불가 → `null`** | `status`를 `["01"]`/`[]`/`["01","04"]`/`["04"]`로 바꿔도 **전부 무시**(totalElements 476,130 불변, 반환 60건 전부 `statusCd=01`). **PLP는 품절 상품을 애초에 목록에 싣지 않는다.** notes에 "PLP가 품절 제외" 명기. **`false`로 채우면 오염**(결측이 아니라 오염). 상세로만 가능(아래 함정) |
| view_count · viewers_now · buyers_now · purchase_count | **PLP 미노출 → `null`** | 상세 노출 여부 「미검증」 |

### 총계 (2026-08-03 실측)

- PLP `data.productList.totalElements` / SSR `__NEXT_DATA__.initialData.productTotal`
  (여성/의류 476,163). **품절 제외 기준** — `source_total`에 담을 때 notes에 "품절 제외
  총계" 명기.
- 30초 3회 관측 476,117→476,130→476,163 흔들림 → **완전성 검증 시 허용 오차**를 둔다.

### 카테고리 체계 (2026-08-03 실측)

- 2축: `mediumCd`(대분류 문자코드, `M33439436`=여성/의류·`M39593862`=뷰티) +
  `largeCategory`/`middleCategory`/`smallCategory`(3자리 숫자, `000`=전체).
- URL: `display.wconcept.co.kr/category/{women|men|beauty}/{largeCategory}`,
  API 경로는 `/{mediumCd}/{largeCategory}`.

### 함정 (국내 PLP, 2026-08-03 실측)

1. **HTML 문자열 "SOLD OUT"은 판매중 페이지에도 존재**(템플릿). 품절은 상세 서버 렌더
   인라인 **`var statusCd`**(`'04'`=품절/`'01'`=판매중)를 파싱해야 한다. 단 상품당 상세
   1회라 47만 건 전수는 비현실적 — **라인시트(수백~수천)나 랭킹 상위 N에서만.**
2. **`totalQty`는 잔여 재고가 아니다**(품절 상품이 984). 의미 미확인, **쓰지 말 것**.
3. **`01`/`04` 외 statusCd 의미 「미검증」**.
4. **옵션(사이즈)별 재고**: 상세 `<select>`는 품절 옵션도 나열, 2뎁스는
   `SetNextOption_Sku1()` AJAX — **엔드포인트 「미검증」**.

---

## §B. 글로벌 경로 A — 보조 (확인, 2026-07-31)

**글로벌은 보조 경로다** — 국내에 없는 글로벌 전용 진열이 필요할 때만. 마뗑킴 886/886으로
완주되지만 KRW 미노출·`sold_out` 미노출 한계가 있다.

## §B.0 국내와 글로벌은 사실상 다른 플랫폼이다 (2026-07-31 실측 · 2026-08-03 국내 정정)

**데이터 경로가 완전히 다르다.** 국내 수집은 §A(주 경로)를 쓴다 — 아래 표의 국내 열은
2026-08-03 재실측으로 정정됐다(2026-07-31의 "불가"는 robots 오독).

| | 국내 (§A) | 글로벌 (`www.wconcept.com`) |
|---|---|---|
| robots | `*: Disallow: /`지만 **Claude 봇은 `Allow: /` 명시**(§A 접근) | **`Allow: /`** (전면 허용 + sitemap 공개) |
| 도구 기본 UA | 정직한 신원은 **403**, **브라우저 UA 헤더 3종으로 200** | 정상 200 |
| 수집 가능 | **가능 — PLP API(§A), 주 경로** | 가능 — 경로 A 확보(§B.1), 보조 |
| 브랜드 ID | 로우클래식 100216 · 인사일런스 남성 103603/우먼 108005 · 마뗑킴 109612 | **다른 체계** — 마뗑킴 **3233**(확증) |
| 통화 | KRW | **KRW 없음** — USD 등 9종(§B.3 함정) |

정책은 바뀔 수 있으니 수집 전 양쪽 robots를 런타임에 다시 읽는다.

## §B.1 엔드포인트 (경로 A) — 글로벌 (확인, 2026-07-31)

| 용도 | 요청 | 실측 일자 | 비고 |
|---|---|---|---|
| 브랜드관 화면 | `GET https://www.wconcept.com/brand/{slug}/{brandId}.html` | 2026-07-31 | 예: `/brand/matinkim/3233.html` → 타이틀 `Matin Kim | W Concept`. **slug와 `.html`이 둘 다 있어야 한다**(아래 함정) |
| **목록 (주 경로)** | `GET https://api.yesplz.ai/api/v1/retailer/wconcept/textsearch/` | 2026-07-31 | 아래 파라미터. **총계·페이지네이션 모두 응답 안에 있다** |
| 카테고리 facet | `GET https://api.yesplz.ai/api/v1/retailer/wconcept/filters/categories/?brands={브랜드명}&lang=eng&currency=USD` | 2026-07-31 | 범위 협의용 |

```
GET https://api.yesplz.ai/api/v1/retailer/wconcept/textsearch/
    ?brands[]=Matin+Kim        ← 브랜드 "표시명"이다. brandId(3233)가 아니다
    &category=all&categories=
    &limit=80&offset=0
    &sort=newest&lang=eng&currency=USD&preview=false&format=json
```

- **W컨셉 글로벌은 검색을 서드파티(`api.yesplz.ai`)에 위임한다.** 화면이 실제로 부르는
  요청이고 비로그인 200이다. 이 호스트에는 **robots.txt가 없다**(JSON fallback 응답) —
  차단 표명이 없다는 뜻이고, 원 사이트(`wconcept.com`) robots는 전면 허용이다.
- **총계: `counts.total`** (마뗑킴 **886**, `relation: "eq"` = 정확값). `source_total`은 이 값이다.
- **페이지네이션: 응답의 `next` URL을 그대로 따라간다**(offset 기반). 종료는 `next: null`.
- **`limit`은 200까지 동작 확인**(200건 정상 수신). **`limit=500`은 응답 형식이 깨진다**
  (`counts` 없음) — 200 이하로 쓴다.
- `filter_meta`에 좁힐 축 13종이 온다(카테고리·가격·할인·색·사이즈·소재·패턴 등) —
  범위 협의에 쓴다.

### 함정 — 브랜드 URL은 slug + `.html`이 필수다 (2026-07-31 실측)

`brandId`만으로 만든 URL은 **전부 404**다: `/brand/3233` · `/brands/3233` · `/Brand/3233`.
게다가 **404인데 본문은 140KB 앱 셸이 온다** — 상태 코드나 본문 크기로 "페이지 있음"을
판정하면 오판한다(engine-detect의 SPA 함정과 같은 성질).

**brand slug·ID 얻는 법**: 홈(`https://www.wconcept.com/main.html`)이나 `/brands.html`의
브랜드 링크에서 `/brand/{slug}/{id}.html` 형태로 나온다. 응답 상품의
`custom_fields.brandOptionId`로 ID를 교차 확인할 수 있다(마뗑킴 3233 확인).

## §B.2 필드 매핑 (계약 필드 ← `textsearch` 응답) — 글로벌 (확인, 2026-07-31)

응답 경로는 `results[].product`다.

| 계약 필드 | 응답 필드 | 실측 일자 | 주의 |
|---|---|---|---|
| product_id | `product_id` | 2026-07-31 | `custom_fields.productSku`와 같은 값 |
| name | `product_name` (영문은 `product_name_en`) | 2026-07-31 | 원문 그대로 담는다 |
| url | `product_url` | 2026-07-31 | 완전 URL로 온다(조립 불요) |
| image_url | `front_img_src` | 2026-07-31 | `second_img_src`는 `null`인 경우가 있다 |
| brand | `brand` | 2026-07-31 | 요청의 `brands[]`와 같은 표시명 |
| category | `retailer_category[]` 최심부 또는 `custom_fields.standardCategoryNm` | 2026-07-31 | 예: `WOMEN > CLOTHING > TOPS > T-SHIRTS`. **목록에 나온다 — 상세를 열 필요가 없다** |
| price_original | `original_price` | 2026-07-31 | 요청 `currency` 기준 |
| price_sale | `sale_price` | 2026-07-31 | 통화 함정 참조 |
| discount_rate | `discount_rate` | 2026-07-31 | 실측 범위 0~8.4 |
| sold_out | **미노출** | 2026-07-31 | 재고 필드도 품절 필터도 없다. `sticker1`에 `"Low Stock"`만 온다(80건 중 21건) — **품절과 다르다.** `null`로 둔다 |
| review_count | `custom_fields.numberOfReviews` | 2026-07-31 | **채워지는 상품이 있다** — 마뗑킴 886건 중 37건(1~11건). 나머지는 `0`인데 '실제 0'과 '미노출 0 인코딩'을 구분할 수 없다 → **0은 `null`로 담는다** |
| rating | `custom_fields.reviewAverageRating` | 2026-07-31 | 〃 37건 관측. **관측값이 전부 정확히 `5.0`이라 5점 스케일로 보이지만 5 초과를 못 봤을 뿐이다** — 100점 스케일 가능성을 배제할 근거는 아직 없다(「미검증」 유지) |
| like_count · view_count · purchase_count · viewers_now · buyers_now | **미노출** | 2026-07-31 | 항상 `null` |

부가 재료(프록시용 `raw_extras` 후보): `colors[]` · `variants[]`(옵션별 이미지) ·
`custom_fields.exclusive` · `sticker1`/`sticker2` · `custom_fields.standardCategoryNo`.

### ⚠️ 함정 — 글로벌은 KRW를 주지 않는다 (2026-07-31 실측)

`currency` 객체에 **AUD·CAD·EUR·GBP·JPY·SGD·THB·USD·VND 9종이 오는데 KRW가 없다.**

- **국내 플랫폼(무신사·29CM·자사몰)과 가격을 직접 비교하지 마라** — 통화가 다르고,
  글로벌가는 관세·배송 정책이 반영된 별도 가격일 수 있다(「미검증」).
- 라인시트에 섞어 쓸 때는 `meta.site`를 `wconcept-global`로 구분하고, 가격 비교 축에서
  빼거나 통화를 리포트에 명시한다. 환산해서 같은 축에 올리는 것은 **추정**이다.

## §B.3 갱신 주기

- 「미검증」 — 스킵 창은 24시간 기본.

## §B.4 함정 (실측된 것만)

- **§B.1 brand URL 함정** — slug + `.html` 없으면 404 + SPA 셸(2026-07-31).
- **§B.2 통화 함정** — 글로벌에 KRW 없음(2026-07-31).
- **국내 브랜드관 화면(display.…)이 열린다고 robots 허용으로 단정하지 마라**(2026-07-31) —
  브랜드관 HTML 1회 수신이 정찰 중 robots 확인 이전에 이뤄진 적이 있다. 판단 기준은 robots
  문서다(2026-08-03 정정: 국내 robots는 Claude 봇을 명시 허용하지만 `display.…/api/`·
  `/rn/api/`는 전 그룹 Disallow다 — 대상별로 다르니 경로를 봐야 한다).
- **국내 브랜드관 상품 목록은 서버 HTML에 없다**(2026-07-31) — Next.js RSC 스트리밍
  (`self.__next_f.push`)으로 CSR 렌더. 원본 HTML만 파싱하면 "상품 0개"로 조용히 틀린다.
  (국내 카테고리 목록은 §A의 PLP POST API로 얻는다 — SSR HTML에는 `productTotal`만 있다.)
- **브랜드에 따라 남성/우먼 브랜드관이 분리돼 있다**(국내 실측: 인사일런스 103603/108005).
  하나만 잡으면 절반을 놓친다. 글로벌도 그런지는 「미검증」.
- 공통 함정 후보는 `../platform-generic/references/common-traps.md` 참조.

## §B.5 경로 B (브라우저 백업)

- 글로벌 브랜드관 화면은 브라우저로 정상 렌더된다(2026-07-31). 경로 A가 막히면
  화면 순회로 폴백하되, 렌더 방식·카드 경계·스크롤 종료 판정은 「미검증」이다.
- 국내 백업은 §A의 상세 페이지(서버 렌더 `var statusCd`)를 쓴다 — 품절이 필요한 경우.

## 미검증 목록

**국내 PLP(§A)·글로벌 경로 A(§B)는 확보됐다. 남은 것은 아래.**

국내 (§A):
- [x] 상세에서 품절 획득 — 인라인 `var statusCd`(2026-08-03). 전수는 비현실적, 라인시트·랭킹 상위 N에서만
- [ ] 옵션(사이즈)별 재고 — 2뎁스 `SetNextOption_Sku1()` AJAX 엔드포인트 「미검증」
- [ ] `totalQty` 의미 「미검증」(잔여 재고 아님 — 품절 상품이 984, 쓰지 말 것)
- [ ] `01`/`04` 외 statusCd 의미 「미검증」
- [ ] view_count/viewers_now/buyers_now/purchase_count가 상세에는 나오는지 「미검증」
- [ ] 갱신 주기
- [ ] `api-display.wconcept.co.kr`(robots 부재 호스트) 취급 — 프로젝트 정책 판단 대기

글로벌 (§B):
- [x] `review_count`·`rating`이 채워지는 상품 존재 — 886건 중 37건 확인(2026-07-31)
- [ ] rating 스케일 확정 — 관측값이 전부 5.0이라 5점인지 100점인지 못 가린다
- [ ] 품절 상품이 목록에 아예 안 오는가, 아니면 표시 없이 섞여 오는가
      (`sticker1: "Low Stock"` 외에 재고 신호 없음)
- [ ] `limit` 정확한 상한(200 동작 / 500 실패 — 그 사이 미확인)
- [ ] 상세 페이지에 재고·품절이 있는가

교차 (국내 ↔ 글로벌):
- [ ] 카탈로그 차이 (같은 브랜드라도 진열이 다를 수 있다)
- [ ] 브랜드 ID 대응 관계 (마뗑킴 3233 ↔ 109612)

## §R. 랭킹 — 별도 API가 없다. `sort=SALE`이 랭킹이다 (2026-08-04 실측)

### 랭킹 경로: 카테고리 PLP API의 판매순 정렬

**상품 베스트 전용 API가 없다.** 베스트 페이지(`display.wconcept.co.kr/rn/best` —
`/best`·`/ranking`은 307로 여기 또는 모바일로 튄다)의 JS 번들 39개(3.6MB)를 훑어도
display API 경로에 상품 랭킹이 없다. 있는 것은 **스타일클립(콘텐츠) 랭킹**뿐이다
(`/display/api/styleclip/v1/styling/ranking/weekly/{content,product}`).

→ **랭킹은 §A의 카테고리 PLP API에 `"sort":"SALE"`(판매순)로 얻는다.**

### 갱신 주기: **사이트가 밝힌 값 없음** 「미검증」

- 베스트 페이지·카테고리 페이지 HTML에 "실시간/집계/업데이트/갱신" 안내 문구가 없다
- PLP 응답에도 `updatedAt`·기준일 필드가 없다
- JS 번들 3.6MB에도 주기 문구가 없다
- 10분 간격 두 스냅샷에서 상위 20위 변동 0 → **10분보다는 길다**는 것만 확정.
  정확한 주기는 날짜를 넘겨 재관측해야 한다

**크론 주기는 잠정 1시간으로 둔다** — 실제 주기보다 촘촘하면 같은 랭킹을 중복 저장할
뿐이라 데이터가 상하지 않는다(관측 시각이 키에 들어가므로 덮어쓰지도 않는다).
주기가 확정되면 조정한다.

### 기간별 랭킹: **없다** (실측)

요청 body에 `"period":"DAILY"` · `"periodType":"WEEKLY"` · `"rankPeriod":"MONTHLY"`를
넣어도 **1위가 그대로**다(`305914779`). 무신사·29CM·EQL 같은 기간 축이 없다 —
**일간/주간/월간 랭킹은 축적으로만 얻는다.**
