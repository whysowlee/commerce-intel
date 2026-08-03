---
name: intel-collect
description: 파이프라인 **수집 단**. commerce-intel 오케스트레이터가
  수집이 필요할 때 위임하거나, 수집만 하고 끝낼 사용자가 이름을 직접 지정해 쓴다.
  플랫폼 스킬(platform-musinsa/29cm/ownmall/wconcept/generic)의 절차를 따라 상품·랭킹을
  모아 **데이터 계약 JSON 하나**를 만들고 검증까지 한다. DB 적재·분석·리포트는 하지
  않는다. 수집 요청을 사용자에게서 직접 받는 진입점은 commerce-intel이다.
compatibility: 웹 요청(JSON API)을 보낼 수 있는 도구가 필요하다. 브라우저 제어 도구는
  백업 경로용. 코드 실행 없이도 수집·JSON 작성은 가능하다.
metadata:
  version: 1.0.0
---

# intel-collect — 수집 단

파이프라인 1단이다. **앞이 없고, 뒤(적재·분석)를 모른다.**

```
[intel-collect] → 계약 JSON → intel-store → intel-explore → intel-insight
```

이 단의 책임은 하나다: **화면에 노출된 값만 모아 데이터 계약 JSON을 만든다.**

## 왜 분리돼 있나

팀원마다 필요한 단이 다르다(D29). 수집만 필요한 사람에게 DB·통계·PDF를 통과시킬 이유가
없다. 반대로 이 스킬은 수집 결과가 어디에 쓰이는지 몰라도 된다 — 계약만 지키면 된다.

## 1. 수집 방법은 플랫폼 스킬이 갖는다

이 스킬은 **어느 사이트를 어떻게 긁는지 모른다.**

| 사이트 | 스킬 |
|---|---|
| 무신사 | `platform-musinsa` |
| 29CM | `platform-29cm` |
| 자사몰(Cafe24·Shopify 등) | `platform-ownmall` |
| W컨셉 | `platform-wconcept` |
| 처음 보는 곳 | `platform-generic` (정찰부터) |

작업할 사이트의 플랫폼 스킬을 **먼저 읽고** 그 절차를 따른다. 어댑터에 적힌 것은 전부
실측이다 — 추측으로 대체하지 않는다.

## 2. 무엇을 확정하고 시작하나

| 항목 | 규칙 |
|---|---|
| 무엇을 | 브랜드명 / 카테고리명 |
| 사이트 | **명시됐으면 그것만.** 친절하게 더 붙이지 않는다 |
| 범위 | 규모 상한 없음(D22). 대신 **예상 소요를 먼저 보고**하고 진행 |
| 품절 | **포함이 기본.** 목록 기본 필터가 사이트마다 다르므로 반드시 맞춘다 |

**임의 샘플링은 최악의 실패다.** 묻지 않고 상위 N개만 모으지 않는다. 표본은 사용자가
"표본으로"라고 명시했을 때만이고, 그때는 `plan_sample.py`로 계획한다(D21).

## 3. 산출물 — 데이터 계약 JSON

정본 정의는 `../commerce-intel/references/db-contract.md` §1이다. 핵심만:

- 필수: `product_id` `name` `url` `image_url` `brand` `category` `price_original`
  `price_sale` `discount_rate` `sold_out` — 결측 5% 경고 / **30% 초과면 수집 실패**
- 노출될 때만: `review_count` `rating` `view_count` `purchase_count` `like_count`
  `viewers_now` `buyers_now` — **안 보이면 `null`.** 0과 다르다
- `price_sale`은 **전 회원 공통 쿠폰적용가**. 개인화 가격(「나의 구매 가능 가격」)은 담지 않는다
- 축약(`1.2천`)은 정수 파싱, **구간(`300회 이상`)은 `null` + `*_display`만**
- `source_total`에 수집 건수를 넣지 않는다 — 자기 자신과 비교하는 순환 검증이 된다

저장 위치는 `data/raw/<site>-<story>-<대상>-<YYYYMMDD-HHmm>.json`이고,
**페이지 단위로 저장한다**(중단되면 이어서).

## 4. 완전성은 독립 총계로만 판정한다

사이트가 주는 `totalCount`나 화면 총계와 대조한다. **총계를 읽은 시점의 필터 상태
(품절 포함 여부)를 `meta.notes`에 함께 적는다** — 안 적으면 나중에 왜 안 맞는지 모른다.

총계가 없는 사이트는 그 플랫폼 스킬이 정한 대체 근거(카테고리 소진 등)를 쓰고
리포트에 명시한다.

## 5. 검증까지가 이 단의 일이다

```bash
python3 ../commerce-intel/scripts/validate_data.py data/raw/<파일>.json \
    --json data/validation.json
```

`0` 통과 · `1` 경고를 사용자에게 전달하고 진행 · `2` **적재 금지, 원인부터.**
결측 30% 초과는 사이트 구조 변경 신호다 — 플랫폼 스킬의 어댑터 매핑을 확인한다.

## 6. 지켜야 할 규칙

- **요청 속도** — 페이지 사이 0.5~1.5초. 병렬로 몰아치지 않는다
- **차단** — 403/429/캡차는 **즉시 중단**. 우회하지 않고 `meta.incomplete: true`로 남긴다
- **재시도** — 2회(간격 증가), 3연속 실패면 건너뛰고 `meta.notes`에 기록
- **로그인** — 비로그인으로 보이는 것만. 필요해지면 묻는다
- **추정 금지** — 미노출 값은 `null`이다. 리뷰 수로 판매량을 역산하는 식의 추정은
  **사용자가 요청해도 하지 않는다**
- **EQL·SSF는 건별 수집만**(D30) — 두 사이트는 robots가 명시 봇만 허용한다.
  크론 무인 축적은 하지 않는다. 랭킹 축적은 무신사·29CM에서 한다
- **W컨셉은 `sold_out`을 못 가져온다** — PLP가 품절 상품을 목록에 싣지 않는다.
  `false`로 채우지 말고 `null` + notes 명기

## 7. 다 모으면

수집만 필요했으면 여기서 끝이다. JSON 경로와 건수·총계 대조 결과를 보고한다.

이어서 쌓으려면 `intel-store`로 넘긴다.
