---
name: platform-eql
description: EQL(eqlstore.com, 한섬 편집숍)에서 상품 데이터를 수집해 commerce-intel 데이터
  계약 JSON을 만든다. 요청에 EQL/이큐엘/eqlstore가 명시되고 수집·정리 태스크가 함께 있을 때
  쓴다. 예: "EQL에서 이 브랜드 상품 정리해줘", "EQL 우먼 아우터 카테고리 전수조사",
  "eqlstore 신상품 훑어줘", "EQL 랭킹 계속 모아줘". **랭킹 크론 축적이 허용된다(D41,
  2026-08-04 사용자 승인 — 구 D30의 건별 제한을 개정)**. 다른 플랫폼 요청이나 상품 구매
  대행에는 쓰지 않는다.
compatibility: 웹 요청 도구로 수집이 된다(쿠키 유지 필요 — adapter.md 참조). 브라우저
  제어 도구는 백업 경로용.
metadata:
  version: 0.1.0
  status: draft            # draft → ready 승격은 사용자가 한다
  refresh-cycle: "unverified · 크론 잠정 60분"   # 사이트가 밝힌 값 없음. 기간별 랭킹은 sort로 직접 취득(§R)
  measured-at: "2026-08-04"   # 가장 최근 실측일
---

# platform-eql

EQL(한섬 편집숍)에서 **화면에 노출된 값만** 모아 데이터 계약 JSON을 만든다.
DB 적재·분석·리포트와 공통 규칙(속도·차단 중단·추정 금지·순회 상한)은
`commerce-intel` 오케스트레이터가 갖는다. `meta.site` 값은 `eql`이다.

> ⚠️ **이 스킬은 초안(draft)이다.** `「미검증」` 표기 항목은 사실이 아니라 확인 과제다.
> 미검증 항목에 의존하는 수집을 하게 되면 그 사실을 사용자에게 먼저 알린다.

## 수집 신분 — 랭킹 크론 축적 허용 (D41, 2026-08-04 사용자 승인)

- EQL robots.txt는 `User-agent: *`에 **`Disallow: /`** 다(루트 `/$`·favicon만 허용).
  대신 **생성형 AI 크롤러 블록이 따로 있어 `Claude-SearchBot`·`Claude-User` 등에
  `Allow: /`** 를 준다(`/secured/`·`/public/member/`만 제외). (2026-08-03 실측)
- **랭킹 주기 축적을 등록해도 된다**(D41). 구 D30은 이 사이트를 "사용자 지시 건별
  수집만, 크론은 사용자 명시 승인 전까지 보류"로 뒀는데 **그 승인이 나왔다**(2026-08-04).
  robots는 그대로이고 바뀐 것은 우리 판단이다 — 이력은 SPEC-INTEL D30·D41에 있다.
- **주기는 frontmatter `refresh-cycle`을 따른다.** EQL은 사이트가 갱신 주기를 밝히지
  않아 「미검증」이고 크론은 잠정 1시간이다(§R). 촘촘한 쪽이 안전하다 — 관측 시각이
  키라서 중복 저장될 뿐 데이터가 상하지 않는다.
- **기간별 랭킹(일간·주간·월간)은 축적 없이 바로 받을 수 있다**(§R의 `sort` 파라미터).
  과거 기간이 필요하면 축적을 기다리지 말고 그것을 쓴다.
- UA 기반 기술 차단은 없다(전 요청 200, 2026-08-03 실측). 차단이 없다는 것이 축적
  허가는 아니었고, 허가는 D41이 준 것이다.

## 수집 절차

1. **`references/adapter.md`를 먼저 읽는다** — 검증된 요청 스킴·필드 매핑·함정이
   실측 일자와 함께 있다. 어댑터에 없는 스킴을 사실처럼 쓰지 않는다.
2. **쿠키 발급** — 목록 페이지를 1회 GET 해 `WMONID` 쿠키를 받는다. 로그인·CSRF는
   불필요하다. **쿠키 없이 목록 XHR을 부르면 에러가 아니라 200 + 빈 본문이 온다**
   (함정 — adapter.md §4-4).
3. **대상 확인** — 카테고리 코드는 고정폭 계층(`EQL`+`A01`+`A01`+`A01`)이고, **목록
   HTML 1건의 서버 렌더 GNB에서 카테고리 링크 ~140개를 한 번에 수확**할 수 있다.
   **코드 무차별 순회 금지** — 중분류 코드가 연속이 아니다. 브랜드 단위 수집은
   「미검증」이다(adapter.md 미검증 목록).
4. **목록 순회** — `POST /category/v2/godListHtml` (응답은 JSON이 아니라 HTML 조각이다).
   페이지당 40개 고정, 종료 판정은 **totalRow 대비 누적 도달**이다.
5. **총계 확보** — `source_total`은 godListHtml 말미 인라인 스크립트의 **`totalRow`**
   하나다(화면 표기·페이지네이션 실측과 정확 일치). `/filter/v2/count`의 totalCount는
   값이 흔들린다 — 완전성 근거 금지(함정 — adapter.md §4-3). 총계를 읽은 시점의
   필터 상태(품절 포함 여부)를 `meta.notes`에 병기한다(db-contract 규칙).
6. **품절 처리** — **기본이 품절 포함**이다(`excludeSoldoutGodYn=N`, 실측 8,427 vs
   제외 8,034). 기본값 그대로 수집하고 notes에 남긴다.
7. **이퀄 수 수집** — `POST /sync/v2/equalCount`로 like_count를 얹는다.
   **미반환 상품 = 0**이다(40건 중 19건만 반환 실측).
8. **검증 대조** — 수집 건수를 totalRow와 나란히 보고한다. 다르면 그 차이를 밝힌다.

## 이 플랫폼의 노출 지표

| 계약 필드 | 노출 여부 | 출처 (adapter.md 참조) |
|---|---|---|
| review_count / rating | **수집하지 않는다(항상 null)** | 리뷰가 EQL 밖 제3자 위젯(cre.ma)에 있다 — 별도 도메인의 robots·약관 미확인이라 수집하지 않는다 |
| like_count | 노출 — EQL "이퀄" 수 | `equalCount` API. 미반환 = 0 |
| view_count | 미노출(항상 null) | — |
| purchase_count | 미노출(항상 null) | — |
| viewers_now / buyers_now | 미노출(항상 null) | — |

미노출 필드는 항상 `null`이다 — 다른 출처에서 끌어와 채우지 않는다.

## EQL 고유 함정 (상세는 adapter.md §4)

- **`lastSalePrc` 속성을 믿지 마라** — 같은 응답 안에서 정가/판매가 의미가 섞인다
  (실측 상반 사례 2건). 가격은 `<del class="normal">`/`<span class="current">`/
  `<span class="discount">` 삼종만 쓴다. `<del>`이 없으면 무할인이다(40건 중 26건).
- **상세 JSON-LD `offers.price`도 판매가가 아니다**(정가 238,000 vs 실판매가 166,600).
  `availability`(InStock/OutOfStock)는 쓸 만하다.
- **쿠키 없는 POST가 200 + 빈 본문** — 에러 코드가 없다. `totalRow`가 공백이면
  요청 실패로 판정한다.
- **모바일 리다이렉트 JS가 전 페이지에 있다** — UA에 Android|iPhone|iPad가 들어가면
  튄다. 데스크톱 UA를 유지한다.
- **품절 상품은 정렬 무관 목록 끝으로 밀린다** — 앞쪽 페이지만 보고 "품절 없음"으로
  판단하지 마라.

## 참고

- `references/adapter.md` — 엔드포인트·필드 매핑·함정 전체 (수집 시작 전 필독)
- 스킴이 바뀐 것을 감지하면(400/404·필드 소실·결측 급증): 브라우저(경로 B)로 폴백해
  완주하고 `meta.notes`에 기록한 뒤 사용자에게 보고한다. 어댑터 개정은 보고 후의 일이다.

## 미검증 목록 (다음 실측의 할 일)

- [ ] **브랜드 모드**(`mallGubun=BRAND`) 목록 — brand-linesheet 스토리에 필요하다
- [ ] 검색 엔드포인트
- [ ] 옵션별 재고 — 재입고 모달 `restockGodOptionSelect`에 option_id·사이즈는 보이나
      전체 옵션인지 품절분만인지 미확인
- [ ] 랭킹(`bestYn=Y`) 갱신 주기 (frontmatter refresh-cycle이 unverified인 이유)
- [ ] `POST /category/v2/lnb` 응답 구조
- [ ] cre.ma(리뷰 위젯) robots·약관 — 확인되면 review_count/rating 수집 가능 여부 재판정
- [ ] 레이트리밋 임계 — 2초 간격 17요청 무사고까지만 확인
- [ ] `equalCount` 요청 본문 형식(응답 형식·의미는 실측됨)
