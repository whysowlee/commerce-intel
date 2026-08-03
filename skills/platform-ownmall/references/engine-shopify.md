# Shopify 어댑터 — 1차 실측 (2026-07-31)

실측 스토어는 **thisisneverthat INTL**(`intl.thisisneverthat.com`, **헤드리스형**) 1곳이고,
**LEWKIN**(`lewkin.com`, 표준 테마형)은 정찰까지만 했다(robots가 AI 봇 차단 — 아래).
"확인"은 그날 실제 응답으로 검증한 것이다. **표준 테마형의 본수집은 아직 없다** —
표준형에서 이 문서와 다른 동작을 보면 실측 일자와 함께 갱신을 제안하라.

## 실측 기록 (스텁의 검증 순서 1~7 대응)

| 항목 | 결과 | 실측 일자 | 스토어 |
|---|---|---|---|
| robots.txt | 스토어마다 다르다 — **반드시 먼저 읽는다.** thisisneverthat INTL은 `User-agent: * Allow: /` 전면 허용. **LEWKIN은 ClaudeBot·GPTBot 등 AI 봇을 `Disallow: /` 전면 차단**(WebFetch 403 실측) → 그 스토어는 수집하지 않는다 | 2026-07-31 | 양쪽 |
| `/products.json` | **동작 확인** — `?limit=250&page=N`, 빈 페이지가 종료 신호(950건 = 250×3+200). **단 200 상태 코드만 믿으면 안 된다** — 본문이 JSON인지 확인(헤드리스 프런트는 아무 경로나 200+HTML 셸) | 2026-07-31 | thisisneverthat INTL |
| 컬렉션 구조 | `/collections/all/products.json` 동작 확인. **`/products.json` 전량과 컬렉션 전수의 일치 여부는 미검증** — 총계가 없어 단정할 수 없다. Cafe24 `ALL` 23% 누락 같은 함정이 있는지는 남은 실측 과제 | 2026-07-31 | thisisneverthat INTL |
| 총계 | **어디에도 없다** — `source_total`은 `null`, 완전성 근거는 "products.json 빈 페이지 도달"로 세우고 `meta.notes`에 남긴다. product sitemap으로 근사만 가능(LEWKIN ≈7,000) | 2026-07-31 | 양쪽 |
| 필드 매핑 | 아래 표. variant에 **`available` 필드 존재 확인** | 2026-07-31 | thisisneverthat INTL |
| 반응 지표 | 노출 없음 — 전부 `null`. 후기 앱(Judge.me 등) 붙은 스토어는 「미검증」 — 위젯 API를 뜯지 않는 정책은 Cafe24와 동일 | 2026-07-31 | thisisneverthat INTL |
| 갱신 주기 | 미상 — 스킵 창 24시간 기본 유지 | — | — |

## 헤드리스 변형 (실측, 2026-07-31)

프런트가 SPA(예: Vite/React 셸)면 화면 도메인에는 Shopify 신호도 `/products.json`도 없다.

- **백킹 스토어 도메인(`{스토어}.myshopify.com`)을 번들 JS에서 찾는다** —
  `thisisneverthat-intl.myshopify.com`이 이렇게 나왔고, 그쪽 `/products.json`은 공개다.
- 수집은 백킹 도메인으로, `meta.site`는 사용자 대면 도메인으로 담고 수집 경로를
  `meta.notes`에 남긴다.
- 상품 `url`은 프런트 `/products/{handle}`로 조립한다(Shopify 표준 라우팅 — 캐노니컬
  리다이렉트는 미검증, notes에 남긴다).
- 백킹 robots에 **Shopify 에이전트 커머스(UCP/MCP) 엔드포인트**가 노출된다
  (`/api/ucp/mcp`, `agents.md`) — 카탈로그·카트 API가 공식 제공되는 신기능. 후속 실측
  가치가 있다(미검증).

## 필드 매핑 (`/products.json` 기준, 확인)

| 계약 | Shopify | 주의 |
|---|---|---|
| `product_id` | `id` | |
| `name` | `title` | |
| `url` | 프런트 `/products/{handle}` | 헤드리스는 위 절 참조 |
| `image_url` | `images[0].src` | |
| `brand` | `vendor` | |
| `category` | `product_type` | 빈 값이 있다(실측 4.4%) — 그때 `null` |
| `price_sale` | `variants[].price` 최솟값 | 문자열로 온다 — 숫자 변환 |
| `price_original` | `variants[].compare_at_price` 최댓값 | 없으면 판매가와 같다 |
| `sold_out` | `variants[].available` 전부 false | `available` 없는 응답 변형을 만나면 추정하지 말고 `null` |
| 반응 지표 전부 | 노출 없음 → `null` | |

**통화 함정**: 글로벌몰은 가격이 KRW가 아닐 수 있다(thisisneverthat INTL 실측 —
`.js` 프로브로도 통화 필드 확정 실패). **국내 플랫폼과 가격을 비교하기 전에 통화를
확인하고, 확정 못 하면 비교 축에서 빼고 각주로 밝힌다.**

## 남은 미검증 과제

1. 표준 테마형 스토어의 본수집 (LEWKIN은 robots 차단으로 부적합 — 다른 표준형 필요)
2. `/products.json` 전량 ↔ 컬렉션 전수 일치 여부 (숨은 상품 존재 가능성)
3. 통화 필드의 확정 경로 · 후기 앱이 붙은 스토어의 화면 노출
4. UCP/MCP 에이전트 엔드포인트의 실사용성
