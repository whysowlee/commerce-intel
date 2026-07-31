# W컨셉 어댑터

- 사이트: **국내** https://www.wconcept.co.kr (브랜드관 https://display.wconcept.co.kr) ·
  **글로벌** https://www.wconcept.com — `meta.site` 값: `wconcept` / `wconcept-global`
- 최초 실측: 2026-07-31 · 최근 확인: 2026-07-31 (글로벌 경로 A 실측 완료)

**규칙: 여기 적힌 것만 검증된 스킴이다.** 항목마다 실측 일자를 붙이고, 확인 안 된 것은
`「미검증」`으로 표기한다. 미검증 항목을 지우지 말 것 — 할 일 목록이다.

> 🛑 **수집 포기 상태다 (2026-07-31 사용자 확정).** 아래 글로벌 스킴은 실제로 완주되지만
> (마뗑킴 886/886), `sold_out` 구조적 미노출로 데이터 계약을 만족하지 못해
> `validate_data.py`가 exit 2를 낸다. **결정이 바뀔 때를 위해 실측을 보존해 둔 문서**이고,
> 지금은 이 스킴으로 수집하지 않는다. 상세는 SKILL.md 🛑절.

## §0. 국내와 글로벌은 사실상 다른 플랫폼이다 (2026-07-31 실측)

**robots 정책이 정반대이고, 데이터 경로도 완전히 다르다.** 국내가 막혔다고 W컨셉 전체를
포기하면 안 된다 — **글로벌은 열려 있고 총계까지 준다.**

| | 국내 (`wconcept.co.kr` / `display.…`) | 글로벌 (`www.wconcept.com`) |
|---|---|---|
| robots `User-agent: *` | **화이트리스트 전용 — `Disallow: /`** | **`Allow: /`** (전면 허용 + sitemap 공개) |
| 도구 기본 UA | 즉시 **403** (집행 확인) | 정상 200 |
| 수집 가능 | **불가** | **가능 — 경로 A 확보(§1)** |
| 브랜드 ID | 로우클래식 100216 · 인사일런스 남성 103603/우먼 108005 · 마뗑킴 109612 | **다른 체계** — 마뗑킴 **3233**(확증) |
| 통화 | KRW | **KRW 없음** — USD 등 9종(§3 함정) |

정책은 바뀔 수 있으니 수집 전 양쪽 robots를 런타임에 다시 읽는다.

## 1. 엔드포인트 (경로 A) — 글로벌 (확인, 2026-07-31)

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

## 2. 필드 매핑 (계약 필드 ← `textsearch` 응답) — 글로벌 (확인, 2026-07-31)

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

## 3. 갱신 주기

- 「미검증」 — 스킵 창은 24시간 기본.

## 4. 함정 (실측된 것만)

- **§1 brand URL 함정** — slug + `.html` 없으면 404 + SPA 셸(2026-07-31).
- **§2 통화 함정** — 글로벌에 KRW 없음(2026-07-31).
- **국내 robots 차단인데 페이지 일부는 열린다고 착각하기 쉽다**(2026-07-31) — 브랜드관
  HTML 1회 수신이 정찰 중 robots 확인 이전에 이뤄진 적이 있다. 열렸다는 사실이 허용의
  근거가 아니다. 판단 기준은 robots 문서다.
- **국내 브랜드관 상품 목록은 서버 HTML에 없다**(2026-07-31) — Next.js RSC 스트리밍
  (`self.__next_f.push`)으로 CSR 렌더. 원본 HTML만 파싱하면 "상품 0개"로 조용히 틀린다.
- **브랜드에 따라 남성/우먼 브랜드관이 분리돼 있다**(국내 실측: 인사일런스 103603/108005).
  하나만 잡으면 절반을 놓친다. 글로벌도 그런지는 「미검증」.
- 공통 함정 후보는 `../platform-generic/references/common-traps.md` 참조.

## 5. 경로 B (브라우저 백업)

- 글로벌 브랜드관 화면은 브라우저로 정상 렌더된다(2026-07-31). 경로 A가 막히면
  화면 순회로 폴백하되, 렌더 방식·카드 경계·스크롤 종료 판정은 「미검증」이다.
- 국내는 robots 차단이라 경로 B도 자동 수집이면 같은 제약을 받는다 — 실측하지 않는다.

## 미검증 목록

**글로벌 경로 A는 확보됐다. 남은 것은 아래.**

- [x] `review_count`·`rating`이 채워지는 상품 존재 — 886건 중 37건 확인(2026-07-31)
- [ ] rating 스케일 확정 — 관측값이 전부 5.0이라 5점인지 100점인지 못 가린다
- [ ] 품절 상품이 목록에 아예 안 오는가, 아니면 표시 없이 섞여 오는가
      (`sticker1: "Low Stock"` 외에 재고 신호 없음)
- [ ] `limit` 정확한 상한(200 동작 / 500 실패 — 그 사이 미확인)
- [ ] 상세 페이지에 재고·품절이 있는가 — **있으면 수집 포기 결정을 재검토할 근거가 된다**
- [ ] 글로벌 카탈로그 ↔ 국내 카탈로그 차이 (같은 브랜드라도 진열이 다를 수 있다)
- [ ] 글로벌 브랜드 ID ↔ 국내 브랜드 ID 대응 관계 (마뗑킴 3233 ↔ 109612)
- [ ] 갱신 주기
- [ ] 상세 페이지에서 추가로 나오는 값(옵션·재고·후기)
- [ ] (국내) robots 완화 시에만: 국내 경로 전부
