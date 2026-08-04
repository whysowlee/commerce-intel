# EQL 어댑터

- 사이트: https://www.eqlstore.com (한섬 편집숍) · `meta.site` 값: `eql`
- 최초 실측: **2026-08-03** · 최근 확인: 2026-08-03
- robots.txt 정책 (2026-08-03 실측): `User-agent: *`는 **`Disallow: /`** (루트 `/$`·favicon만
  허용). **생성형 AI 크롤러 블록이 별도로 있다** — `Claude-SearchBot`·`Claude-User` 등에
  `Allow: /` (`/secured/`·`/public/member/`만 제외). UA 기반 기술 차단은 없다(전 요청 200).
- **수집 신분: 랭킹 크론 축적 허용 (SPEC-INTEL D41, 2026-08-04 사용자 승인).**
  구 D30(2026-08-03)은 "건별 수집만, 크론 금지"였는데 개정됐다 — robots는 그대로이고
  바뀐 것은 우리 판단이다. 주기는 SKILL.md frontmatter `refresh-cycle` 참조(§R).

**규칙: 여기 적힌 것만 검증된 스킴이다.** 항목마다 실측 일자를 붙이고, 확인 안 된 것은
`「미검증」`으로 표기한다. 미검증 항목을 지우지 말 것 — 할 일 목록이다.

## 0. 렌더 구조 — HTML 페이지는 껍데기, 상품은 100% XHR (2026-08-03 실측)

카테고리 목록 HTML 페이지는 **완전 CSR 껍데기**다 — 카테고리를 바꿔도 크기가 사실상
같다(77,740 vs 77,741 바이트). 상품 데이터는 전부 아래 목록 XHR(§1)에서 온다.
단 **GNB(카테고리 내비게이션)는 모든 페이지에 서버 렌더**된다 — 이게 카탈로그 구축
경로다(§3).

특이점: 목록 XHR의 응답이 JSON이 아니라 **`<ul><li name="product">…` HTML 조각 +
말미 인라인 스크립트**다. 경로 A(직접 요청)와 경로 B(브라우저)가 같은 응답을 본다 —
파싱 대상은 어느 쪽이든 이 HTML 조각이다.

## 1. 엔드포인트

| 용도 | 요청 | 실측 일자 | 비고 |
|---|---|---|---|
| **목록 순회** | `POST https://www.eqlstore.com/category/v2/godListHtml` | 2026-08-03 | HTML 조각 응답. 40개/페이지 고정. 아래 상세 |
| 이퀄 수(like) | `POST https://www.eqlstore.com/sync/v2/equalCount` | 2026-08-03 | 응답 `{"result":[{"EQUAL_CNT":5,"ID":"GM00..."},...]}`. **요청 본문 형식은 「미검증」**(브라우저 캡처 재확인 필요) |
| 브랜드 패싯 총계 | `POST https://www.eqlstore.com/filter/v2/count` | 2026-08-03 | ⚠️ **완전성 근거 금지** — §4-3 |
| 상세 | `GET https://www.eqlstore.com/product/{godNo}/detail` | 2026-08-03 | JSON-LD 있음 — `category` 평문·`availability`는 유용, **`offers.price`는 함정**(§4-2) |
| sitemap | `GET https://www.eqlstore.com/sitemap.xml` → product.xml | 2026-08-03 | 상품 URL 22,380개 |
| LNB | `POST /category/v2/lnb` | — | 존재만 관측. **응답 구조 「미검증」** |
| 검색 | 「미검증」 | — | |
| 브랜드 목록 | `mallGubun=BRAND` 「미검증」 — brand-linesheet에 필요 | — | |

### 목록 요청 상세 (2026-08-03 실측)

```
POST https://www.eqlstore.com/category/v2/godListHtml
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Cookie: WMONID=...   ← 목록 페이지 1회 GET으로 발급. 로그인·CSRF 불필요

selectCtgryNo=EQLA01A01A01&mallGubun=CTGRY&mallType=&ctgryType=&dspEqlOtltYn=
&sort=NEW_GOD_SEQ&page=1&back=N&exclusiveGodYn=N&dcGodYn=N
&excludeSoldoutGodYn=N&preOrderGodYn=N&productBrand=&productSubBrand=&price=&color=
```

- **쿠키 필수** — 없으면 200 + 빈 본문(800B, `totalRow=''`). 에러 코드를 안 준다(§4-4)
- 페이지네이션: `page` 파라미터, **40개/페이지 고정**. 종료 판정 = **totalRow 대비
  누적 도달.** 실측: 우먼>의류>아우터 210페이지×40 + 27 = 8,427 = totalRow 정확 일치
- 총계: 응답 말미 인라인 스크립트 **`const totalRow = Number('8427')`** — 화면 표기·
  페이지네이션 실측과 정확 일치. **이것이 `source_total`이다**
- 품절 필터: `excludeSoldoutGodYn=N`(기본, 품절 포함) → totalRow 8,427 /
  `Y` → 8,034 (델타 393 = 품절). **기본이 품절 포함이다.** `source_total`을 담을 때
  필터 상태를 `meta.notes`에 병기한다(db-contract 규칙)
- 데스크톱 UA 유지 — 모바일 리다이렉트 JS가 모든 페이지에 있다(§4-6)
- 정렬 `NEW_GOD_SEQ`(신상품순)로 완주 실측. 다른 sort 값 어휘는 「미검증」
- 랭킹류(`bestYn=Y`) 갱신 주기 「미검증」

## 2. 필드 매핑 (계약 필드 ← 목록 `<li name="product">` 요소, 2026-08-03 실측)

| 계약 필드 | 출처 | 주의 |
|---|---|---|
| `product_id` | `godNo` 속성 (예: `GR9226072787178`) | 접두가 상품마다 다르다(GR·GQ·GM… 실측 관찰) — 전체 문자열이 ID다 |
| `name` | `godNm` 속성 | **HTML 이스케이프 상태**로 온다 — unescape 후 담는다 |
| `url` | `https://www.eqlstore.com/product/{godNo}/detail` | |
| `image_url` | `cdn.eqlstore.com/goods/...jpg?RS=389` | `RS`가 해상도 파라미터(목록 389, 상세 1300) |
| `brand` | `brndNm` 속성 + `<span class="brand">` | |
| `category` | 요청한 `selectCtgryNo`가 문맥 | 평문 이름은 상세 JSON-LD `category`("우먼 > 의류 > 아우터") 또는 GNB 링크 텍스트(§3) |
| `price_original` | `<del class="normal">238,000</del>` | **`<del>`이 없으면 무할인 → `price_sale`과 동일값으로 담는다**(40건 중 26건 무할인 실측) |
| `price_sale` | `<span class="current">166,600</span>` | ⚠️ `lastSalePrc` 속성 금지 — §4-1 |
| `discount_rate` | `<span class="discount">30%</span>` | 없으면 0 |
| `sold_out` | **3중 신호** — `isSoldout="true"` 속성 + `<li class="is_soldout">` + SOLD OUT 배지 | 기본 필터가 품절 **포함**(§1) |
| `review_count` / `rating` | **수집하지 않는다 — 항상 `null`** | 리뷰가 EQL 밖 제3자 cre.ma 위젯이다(`review3.cre.ma/api/eqlstore.com/product_score`, 5점 만점). **별도 도메인의 robots·약관 미확인** → 확인 전까지 null + 사유를 notes에 |
| `like_count` | `POST /sync/v2/equalCount` → `result[].EQUAL_CNT` | EQL "이퀄" 기능. **미반환 = 0**(40건 중 19건만 반환 실측) — null이 아니다 |
| `view_count` / `purchase_count` / `viewers_now` / `buyers_now` | 미노출 — 항상 `null` | |
| `raw_extras` | `<ul class="badge_auto_list">` → NEW·COUPON 배지 문자열, `eqlOtltYn`(아울렛 여부) | 해석하지 않고 원문 보존(D19) |

## 3. 카테고리 체계 (2026-08-03 실측)

- **고정폭 계층 코드**: `EQL` + `A01`(대) + `A01`(중) + `A01`(소).
  예: `EQLA01A01A01` = 우먼>의류>아우터 (8,427개)
- **카탈로그 구축은 GNB 수확으로 한다** — GNB가 모든 페이지에 서버 렌더되므로
  목록 HTML 1건에서 `/display/productsList?categoryNumber=...` 링크 **~140개**를
  요청 1회로 수확할 수 있다
- **코드 무차별 순회 금지** — 중분류 코드가 연속이 아니다(A02, A08, A09, A19… 실측).
  순회로 트리를 만들면 구멍이 난다
- 카테고리명 평문 소스: 상세 JSON-LD `category` + GA4 dataLayer(`ep_page_1Depth` 등)
- sitemap: `/sitemap.xml` → product.xml에 상품 URL 22,380개 (2026-08-03 시점)

## 4. 함정 (실측된 것만 — 2026-08-03)

이 섹션이 어댑터의 핵심 가치다. "조용히 틀리는" 것들이다.

1. **`lastSalePrc` 속성을 믿지 마라** — 같은 응답 안에서 정가/판매가 의미가 섞인다.
   실측: `GR9226072787178`은 정가 238,000이 들었고, `GQBE26072987589`는 판매가
   287,100이 들었다. 가격은 `<del class="normal">`(정가) / `<span class="current">`(판매가)
   / `<span class="discount">`(할인율) **삼종만** 쓴다.
2. **상세 JSON-LD `offers.price`도 판매가가 아니다** — 실측 238,000 vs 실판매가
   166,600(정가가 들어 있다). `availability`(InStock/OutOfStock)는 쓸 만하다.
3. **총계 소스가 두 개고 값이 다르다** — godListHtml 말미 `totalRow`(8,427 — 화면
   표기·페이지네이션 완주와 정확 일치, **이것이 source_total**) vs `POST /filter/v2/count`의
   totalCount(8,596 → 8,577로 **호출마다 흔들린다** — 브랜드 패싯용이다).
   count 쪽을 완전성 근거로 쓰지 마라.
4. **쿠키 없는 POST가 200 + 빈 본문(800B)** — HTTP 상태로는 실패를 감지할 수 없다.
   **`totalRow`가 공백(`''`)이면 실패**로 잡는다. WMONID는 목록 페이지 1회 GET으로 발급.
5. **품절 상품은 정렬 무관 목록 끝으로 밀린다** — `NEW_GOD_SEQ`인데 page 150에 품절
   0건, page 205·211에 몰림(실측). 앞쪽 페이지 표본으로 품절률을 추정하지 마라.
6. **모바일 리다이렉트 JS가 모든 페이지에 있다** — UA에 `Android|iPhone|iPad`가
   들어가면 모바일로 튄다. 데스크톱 UA를 유지한다.

공통 함정 후보는 `../../platform-generic/references/common-traps.md` 참조 — 이 플랫폼에서
재현 확인된 것만 여기로 옮겨 적는다.

## 5. 갱신 주기

- 「미검증」 — 랭킹(`bestYn=Y`)·목록의 원본 갱신 주기를 재지 못했다. 확인 방법:
  같은 요청을 시차를 두고 두 번 받아 대조. 확인 전 스킵 창은 24시간 기본.
  단 **D30(건별 수집만)** 때문에 주기 축적 용도 자체가 없다 — 주기는 재사용 판정
  (`check --cycle-minutes`)에만 쓰인다.

## 6. 레이트리밋

- **2초 간격 17요청 무사고까지만 확인**(2026-08-03). 임계는 「미검증」 — 이 간격보다
  촘촘히 보내지 마라. 오케스트레이터 공통 규칙(차단 시 즉시 중단)이 우선한다.

## 미검증 목록

- [ ] 브랜드 모드(`mallGubun=BRAND`) 목록 — brand-linesheet 스토리에 필요. 브라우저로
      브랜드 페이지를 열어 XHR을 캡처하면 된다
- [ ] 검색 엔드포인트
- [ ] 옵션별 재고 — 재입고 모달 `restockGodOptionSelect`에 option_id·사이즈는 보이나
      **전체 옵션인지 품절분만인지 미확인**
- [ ] 랭킹(`bestYn=Y`) 갱신 주기 — 시차 이중 수신으로 확인
- [ ] `POST /category/v2/lnb` 응답 구조
- [ ] cre.ma robots·약관 — 허용이 확인되면 review_count/rating(5점 만점) 수집 재검토
- [ ] 레이트리밋 임계 (2초×17요청 무사고까지만)
- [ ] `equalCount` 요청 본문 형식 — 응답 형식·의미(미반환=0)는 실측됨
- [ ] `sort` 유효 어휘 (`NEW_GOD_SEQ`만 완주 실측)

## §R. 랭킹(BEST) — 기간별 랭킹이 있다 (2026-08-04 실측)

### ★ 기간 축이 `sort`에 들어 있다 — 축적 없이 일간·주간·월간을 바로 받는다

무신사(`period=`)·29CM(`periodFacetInput.type`)에 대응하는 것이 EQL에는 **`sort`
파라미터**로 들어 있다. 랭킹을 쌓기 전에도 과거 기간 랭킹을 얻을 수 있다는 뜻이다.

| `sort` 값 | 유효 | 1위(우먼 BEST, 2026-08-04) | md5 |
|---|---|---|---|
| `DAILY_SALE_SEQ` | ✅ | `GQ2U25073147163` | `536c9b62` |
| `WEEKLY_SALE_SEQ` | ✅ | `GQEZ24092703139` | `5d05d271` |
| `MONTHLY_SALE_SEQ` | ✅ | `GP3924032010693` | `937fed07` |
| `MONTHS_3_SALE_SEQ` · `YEAR_SALE_SEQ` · `REALTIME_SALE_SEQ` | ❌ | fallback | — |

**3개월·6개월은 없다**(29CM의 `MONTHS_3`/`MONTHS_6` 대응 없음).

**⚠️ 미지원 값도 200 + 정상 본문을 준다.** 가짜 값 `ZZZ_BOGUS_SEQ`도 120개가 오고
md5가 또 다르다(`910f3da3`) — **"200이니까 동작한다"로 판정하면 틀린다.** 새 정렬값을
쓸 때는 반드시 **결과를 대조**해라. 위 세 값이 유효하다는 근거도 md5·1위가 서로 다르고
가짜 값과도 다르다는 대조에서 나왔다.

### 랭킹 경로

```
페이지: GET /display/productsList?categoryNumber={대분류}A05&bestYn=Y
        (우먼 EQLA01A05 · 맨 EQLA02A05 · 라이프 EQLA03A05 — GNB 서버 렌더 링크)
데이터: POST /category/v2/godListHtml
        selectCtgryNo=EQLA01        ← **대분류 코드**(URL의 A05가 아니다)
        &rendingCtgryNo=EQLA01A05
        &ctgryType=BEST             ← 일반 목록은 빈 값
        &sort={기간}&page=1&excludeSoldoutGodYn=N&mallGubun=CTGRY
```

- **BEST는 Top 100 캡**이다(`totalRow=100`). 일반 카테고리는 전체 수를 준다
- **일반 카테고리에서도 기간 sort가 먹는다** — Top 100 캡 없이 기간 랭킹을 뽑으려면
  `ctgryType=`를 비우고 카테고리 코드로 요청한다(아우터 8,396건 전체가 기간 판매순 정렬)

### ⚠️ 쿠키 — 목록 페이지에서 받아야 한다 (§1 보강)

홈(`/`)에서 받은 쿠키로는 **200 + 빈 본문(750B)**이 온다. **목록/랭킹 페이지**
(`/display/productsList?...`)를 먼저 GET 해야 유효한 WMONID가 발급된다(2026-08-04 실측).

### 갱신 주기: **사이트가 밝힌 값 없음** 「미확인」

- `godListHtml` 응답에 `updatedAt`·기준일·집계시각 필드가 **없다**(말미 스크립트는
  `totalRow`/`endIndex`뿐)
- 랭킹 페이지에도 "실시간/집계/업데이트" 안내 문구가 없다
- 같은 요청을 시차 두고 보내면 **md5가 동일**해 세션 내 관측으로는 못 잰다 —
  주기를 확정하려면 **날짜를 넘겨 재관측**해야 한다

### 정렬 어휘 정정 (`/resources/js/queJS/UI/SortUI.js` 실측)

정본 어휘: `GOD_BEST_POINT`(베스트순) · `NEW_GOD_SEQ` · `LWET_PRC_SEQ` ·
`BEST_PRC_SEQ` · `BEST_DC_SEQ` · `PCH_PS_SEQ` · `MD_RECOMMEND_SEQ`

⚠️ **`BEST_GOD`·`SALE_QTY`는 이 목록에 없다.** 이 두 값은 2026-08-04 조사 과정에서
시험해 200 + 상품 120개를 받았고 그때 "동작한다"고 봤는데, **미지원 값도 200 + 정상
본문을 준다는 것이 뒤에 밝혀졌다**(가짜 값 `ZZZ_BOGUS_SEQ` 대조). 따라서 그 판정은
근거가 없다 — **결과 대조 없이는 유효하다고 볼 수 없다** 「미확인」.
(출처 주의: 어댑터 §1에는 이 두 값이 적혀 있지 않다. SSF의 `SALE_QTY_SEQ`와 혼동한
기록일 가능성이 있어 출처 재확인이 필요하다.)
`GOD_BEST_POINT`는 BEST 페이지에서 `DAILY_SALE_SEQ`와 바이트 동일(사실상 일간 판매순).

※ 아우터 `totalRow`가 8,427(08-03) → 8,396(08-04)로 하루 만에 변동했다 —
  총계는 시점 값이라 대조할 때 같은 날 것을 써야 한다.
