# SSF샵 어댑터

- 사이트: https://www.ssfshop.com (삼성물산 패션부문) · `meta.site` 값: `ssfshop`
- 최초 실측: 2026-08-03 · 최근 확인: 2026-08-03
  (16요청 · 2초 간격 · 전부 200 · 차단 없음)

**규칙: 여기 적힌 것만 검증된 스킴이다.** 항목마다 실측 일자를 붙이고, 확인 안 된 것은
`「미검증」`으로 표기한다. 미검증 항목을 지우지 말 것 — 할 일 목록이다.

## 0. 접근 정책 — robots.txt (2026-08-03 실측) + D30

- `User-agent: *` → `Disallow: /` (일반 봇 전면 차단)
- **명시 허용 19종에 Claude 3종 포함**: `ClaudeBot`·`Claude-User`·`Claude-SearchBot`
  (Googlebot 4종·NaverBot·Yeti·Bingbot·GPTBot 등과 함께). 전부
  `Disallow: /secured/`, `/raffle/` + `Allow: /`
- **`/secured/`·`/raffle/`은 건드리지 않는다**
- UA 기반 기술 차단 없음 — 봇 UA에도 전부 200 (2026-08-03 실측).
  정직한 신원으로 접근한다. **UA를 위장하지 마라.** 403/429가 나오면 멈춘다
- **D41 (사용자 승인, 2026-08-04): 랭킹 크론 축적 허용.** 구 D30(2026-08-03)의
  "건별 수집만, 크론 금지"를 개정한 것이다 — robots는 그대로이고 바뀐 것은 우리 판단이다.
  **주기는 1시간**(사이트가 `/ranking`에 명시, §R)

## 1. 엔드포인트 (경로 A)

경로 A가 **HTML 프래그먼트 엔드포인트**다(JSON 아님). 화면 밖 직접 조립으로 200 —
쿠키·referer·서명 전부 불필요 (2026-08-03 실측, 무신사식 hmac 함정 없음).

| 용도 | 요청 | 실측 일자 | 비고 |
|---|---|---|---|
| 목록 순회 | `GET https://www.ssfshop.com/selectProductList?dspCtgryNo={카테고리코드}&currentPage={n}&sortColumn=SALE_QTY_SEQ&serviceType=DSP&ctgrySectCd=GNRL_CTGRY&fitPsbYn=N` | 2026-08-03 | 200, `text/html;charset=UTF-8`, 상품 60개/페이지 |
| 상세 | `GET https://www.ssfshop.com/{브랜드슬러그}/{godNo}/good` | 2026-08-03 | JSON-LD 포함. **슬러그는 장식** — `/x/{godNo}/good`도 200 |
| 카테고리 카탈로그 | `GET https://www.ssfshop.com/sitemap_category.xml` | 2026-08-03 | 카테고리 URL 1,353개 전부 나열 |

- 페이지네이션: `currentPage` 직접 조립 — 서명 불필요 (2026-08-03 실측).
  **페이지당 60개 고정** — `pageSize` 파라미터는 조용히 무시된다.
  **깊은 페이지 상한 없음** — 여성 전체 162,131개(2,702페이지)에서 p2700 정상 응답,
  중복 0 (2026-08-03 실측)
- 총계: 목록 응답 안의 hidden input —
  `<input type="hidden" value="4233" id="ctgryGodsListTotalRow" />` (독립 총계).
  **총계는 필터의 함수다** — §4 함정 2
- 카테고리 화면(`/Denim/list?dspCtgryNo=…`)은 필터 껍데기다 — 그리드는
  `selectProductList`가 채운다 (2026-08-03 실측)
- 검색 경로: `/search/result`는 CSR 껍데기, 상품 API 미추적 — 「미검증」.
  카테고리 경로만으로 수집이 완결되므로 차단 요인은 아니다
- 브랜드 단위 목록 경로: 「미검증」

### 정렬 (`sortColumn`, 2026-08-03 실측)

`NEW_GOD_SEQ`(신상) · **`SALE_QTY_SEQ`(판매량=인기)** · `LWET_PRC_SEQ`(저가) ·
`BEST_PRC_SEQ` · `BEST_DC_SEQ`(할인율) · `PCH_PS_SEQ`(구매후기) · `MD_RECOMMEND_SEQ`

- **랭킹 전용 페이지·엔드포인트가 있다** (2026-08-04 실측 — 2026-08-03의 "별도 BEST
  없음" 기록을 정정한다): `/ranking` + `/rankingGodList`(§R). **갱신 주기는 1시간**으로
  사이트가 명시한다. 카테고리 정렬 `SALE_QTY_SEQ`도 계속 유효하지만 **둘이 같은 집계인지는
  확인하지 않았다** — §R의 「관계 재확인 필요」 참조
- **연령대별 인기순**: `preferAge=1020AGE / 30AGE / 40AGE / 50AGE_OVER` —
  다른 플랫폼에 없는 축. 쓰면 `raw_extras`에 담는다 (해석 금지)

### 필터

- `benefit=EXC_SLDOUT` — 품절 제외 (2026-08-03 실측: 4,233 → 4,091).
  **기본은 품절 포함이다**(제외가 opt-in) — 29CM과 같고 무신사와 반대다

## 2. 필드 매핑 (계약 필드 ← 목록 li, 2026-08-03 실측 — 필수 10필드 결측 0%)

| 계약 필드 | 출처 | 실측 일자 | 주의 |
|---|---|---|---|
| product_id | `<li data-prdno="GM0026061727209">` | 2026-08-03 | godNo |
| name | `<span class="name">` | 2026-08-03 | |
| url | `/{브랜드슬러그}/{godNo}/good` | 2026-08-03 | 슬러그는 장식(`/x/…`도 200) — godNo만으로 조립 가능 |
| image_url | `img.ssfshop.com/cmd/LB_500x660/src/…` | 2026-08-03 | 리사이즈 프리픽스(`LB_500x660`) 조절 가능 |
| brand | `<span class="brand">` | 2026-08-03 | |
| category | 요청 `dspCtgryNo` + 상세 JSON-LD 경로(`여성 » 팬츠 » 와이드`) | 2026-08-03 | 목록 카드에는 없다 |
| price_original | `<del>458,000</del>` | 2026-08-03 | 무할인 상품은 `<del>` 없이 단일가 (실측 35/60 할인) |
| price_sale | price 블록 마지막 금액 | 2026-08-03 | 상세 JSON-LD price와 일치 확인은 1개 상품만 — 개인화 가격 함정 「미검증」(§4 함정 4) |
| discount_rate | `<em class='sale'>5%</em>` | 2026-08-03 | 없으면 0 |
| sold_out | `class="god-item soldout"` | 2026-08-03 | **판매량순에서 꼬리로 밀린다** — §4 함정 1 |
| rating | 목록 노출 | 2026-08-03 | **5점 만점 그대로**(변환 불요). 결측 28.3% = 리뷰 없는 상품 → `null` 정확 |
| review_count | 목록 노출 | 2026-08-03 | 결측 28.3%(rating과 동일 상품) |
| like_count | 목록 노출(하트) | 2026-08-03 | 결측 0% |
| view_count / viewers_now / buyers_now / purchase_count | **미노출** | 2026-08-03 | 항상 `null` |

- 덤: 목록 카드에 **색상 변형 목록**이 노출된다 (2026-08-03 실측) — `raw_extras` 후보
- 상세 JSON-LD: `availability` `InStock`/`OutOfStock` 실측 확인 (2026-08-03)

## 3. 카테고리 체계 (2026-08-03 실측)

- `sitemap_category.xml`에 **1,353개 카테고리 URL 전부 나열**
- 코드는 접두사+3자 세그먼트 계층, 5단계까지 존재:
  `SFMA41`(여성) → `SFMA41A04`(팬츠) → `SFMA41A04A07`(데님, 실측일 4,233개)
- 대분류 10종: WOMEN `SFMA41` · MEN `SFMA42` · KIDS `SFMA43` · OUTLET `SFMA44` ·
  BEAUTY `SFMA45` · BAG-SHOES `SFMA46` · LIFE `SFMB84` · LXRY `SFME34` ·
  GLF `SFME35` · SPORTS `SFME37`

## 4. 함정 (실측된 것만)

1. **판매량순 정렬에서 품절품이 꼬리로 밀린다** (2026-08-03 실측) — 1페이지만 보면
   "품절 미노출"로 오판한다. 실측: p1 품절 0건, p70에서 60/60 전부 품절.
   교차검증: `benefit=EXC_SLDOUT` 필터 시 품절 0건. `sold_out` 노출 여부 판정은
   반드시 꼬리 페이지까지 보고 한다.
2. **총계는 필터의 함수다** (2026-08-03 실측) — 기본(무필터) 4,233 / `EXC_SLDOUT`
   4,091. **기본 목록은 품절 포함**(제외가 opt-in). `source_total`에 담을 때 읽은
   시점의 필터 상태를 `meta.notes`에 병기한다.
3. **`pageSize` 파라미터가 조용히 무시된다** (2026-08-03 실측) — 에러 없이 60개가 온다.
   페이지 수 계산은 총계÷60으로 한다.
4. **개인화 가격 「미검증」** — 상세 JSON-LD price 435,100이 목록 판매가와 일치했지만
   (비로그인 익명 = 누구나 받는 가격) **1개 상품만 대조했다** (2026-08-03).
   29CM의 `totalDiscountedItemPrice` 같은 개인화 가격 필드가 숨어 있을 가능성을
   배제하지 못했다 — 수집 시 목록가와 상세가를 두어 개 교차 확인해라.
5. 공통 함정 후보는 `../../platform-generic/references/common-traps.md` 참조 —
   이 플랫폼에서 재현 확인된 것만 여기로 옮겨 적는다.

## 5. 갱신 주기

- 「미검증」 — 판매량순(랭킹 프록시) 갱신 주기는 같은 요청을 시차를 두고 두 번 받아
  대조해야 한다. 확정 전 스킵 창은 24시간 기본. (D30 제약상 무인 주기 실측은 못 한다 —
  사용자 지시 건별 수집 중 시차 대조 기회를 잡는다)

## 6. 경로 B (브라우저 백업)

- 「미검증」 — 경로 A(HTML 프래그먼트)가 쿠키·서명 없이 완결되어 실측 범위에서
  브라우저가 필요한 장면이 없었다. 폴백이 필요해지면 `platform-generic` 절차를 따른다.
  카테고리 화면(`/Denim/list?…`)은 필터 껍데기라는 것까지만 확인 (2026-08-03).

## 미검증 목록

- [ ] 검색 경로 — `/search/result` CSR 껍데기 뒤의 상품 API 추적 (네트워크 캡처)
- [ ] 브랜드 단위 목록 경로 — 브랜드 전 상품 수집 스킴
- [ ] 옵션/사이즈별 재고(`variants[]`) — 상세 HTML에 없고 AJAX, 엔드포인트 미특정
- [ ] 랭킹(`SALE_QTY_SEQ`) 갱신 주기 — 시차 2회 대조
- [ ] 개인화 가격 — 목록가↔상세 JSON-LD price 대조 표본 확대 (현재 1개 상품)
- [ ] 레이트리밋 임계 — 2초 간격 16요청 무사고까지만 확인
- [ ] 이미지 리사이즈 프리픽스의 유효 값 범위 (`LB_500x660` 외)

## §R. 랭킹 — 갱신 주기 1시간 (사이트 명시, 2026-08-04 실측)

### 갱신 주기: **매 1시간** — 사이트가 직접 밝힌다

`/ranking` 페이지의 「랭킹 노출 기준」 안내 원문:

> **"순위는 매 시간마다 갱신됩니다."** NEW: 50위권 신규 진입한 상품

시차 관측이 아니라 **사이트 표기**라 확정이다. 크론 주기는 이 값에 맞춘다.
(교차 확인: 10분 간격 두 스냅샷에서 상위 20위 변동 0 — 모순 없음)

### 랭킹 경로

```
GET https://www.ssfshop.com/rankingGodList?{폼 전체}
Referer: https://www.ssfshop.com/ranking
X-Requested-With: XMLHttpRequest
Cookie: /ranking 을 먼저 GET 해서 받은 세션
```

**⚠️ 함정 — 폼 파라미터를 하나라도 빠뜨리면 200 + 빈 응답(0B)이 온다.** 에러를 주지
않으므로 코드로 실패를 감지하려면 **본문 길이 0**을 실패로 잡아야 한다. 전체:

```
ctgryFlterCd=CTGRY_ALL&brndShopId=&brandShopNo=&otltYn=&dspCtgryNo=&endIndex=0
&cnncCtgryNo=&rankSect={종류}&preferAgeCd=ALL&lagSpcltyYn=&goodListPageSize=&utag=&currentPage=1
```

Referer·세션 쿠키도 필수다(둘 중 하나만 빠져도 빈 응답).

### 랭킹 종류 `rankSect` — 2종 동작 (실측)

| 값 | 결과 | 비고 |
|---|---|---|
| `CLICK_RANK` | 60개 · 1위 `GM0026070235540` | 클릭랭킹(페이지 기본) |
| `SALE_RANK` | 60개 · 1위 `GM0026070235545` | 판매랭킹 — **1위가 다르다** |
| `SEARCH_RANK` | 빈 응답 | 페이지 JS에 값은 있으나 미동작 |

`preferAgeCd`로 연령축을 나눌 수 있다(ALL·1020AGE·30AGE·40AGE·50AGE_OVER).

### ⚠️ `SALE_QTY_SEQ`와 `rankingGodList`의 관계는 「미검증」

둘 다 "판매 기준 인기"를 표방하지만 **같은 집계인지 확인하지 않았다.**

| | 경로 | 성격 |
|---|---|---|
| `sortColumn=SALE_QTY_SEQ` | `/selectProductList` (카테고리 목록) | 카테고리 안에서의 판매량 정렬 |
| `rankSect=SALE_RANK` | `/rankingGodList` (랭킹 전용) | 사이트 전체 판매 랭킹 |

- 파라미터·경로·응답 형태가 모두 다르다. 카테고리 목록은 카테고리 코드를 받고,
  랭킹은 `ctgryFlterCd`로 거른다
- **집계 창(오늘/최근 N일)이 같은지, 순위가 일치하는지 대조하지 않았다.**
  같은 카테고리로 두 경로를 나란히 받아 상위 N을 비교하면 갈린다 — **다음 실측 과제**
- 그때까지는 **랭킹 축적에 `/rankingGodList`를 쓴다**(사이트가 "랭킹"이라 부르고 갱신
  주기를 밝힌 쪽이다). `SALE_QTY_SEQ`는 카테고리 전수조사의 정렬 옵션으로 남긴다

### 기간별 랭킹: **없다** (실측)

`periodCd=DAILY` · `termCd=WEEKLY` · `stdDt=20260803`을 붙여도 **1위가 그대로**다
(`GM0026070235540`). 무신사(`period=`)·29CM(`periodFacetInput`)·EQL(`sort=*_SALE_SEQ`)
같은 기간 축이 SSF에는 없다 — **일간/주간/월간 랭킹은 축적으로만 얻는다.**
