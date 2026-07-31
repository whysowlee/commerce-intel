# 엔진 판별식 카탈로그

도메인이 확정된 뒤, 어느 엔진인지 판별한다. **페이지 HTML(원본)을 받아 아래 신호를
순서대로 검사한다.** 하나가 걸리면 거기서 멈추고 해당 엔진으로 분기한다.
모두 안 걸리면 `engine-unknown.md`.

| 순서 | 엔진 | 신호 | 상태 |
|---|---|---|---|
| 1 | **Cafe24** | HTML에 `/cafe24\|ecimg\.cafe24\|cfa-js\.cafe24\|app4you\.cafe24/i` | **실측 2026-07-30** — 인사일런스(`cfa-js.cafe24.com`)·2000아카이브스(`cfa-js` + `app4you`) |
| 2 | **Shopify** | HTML에 `cdn.shopify.com` 또는 `Shopify.theme`, `/products.json`이 **본문까지 진짜 JSON** | **실측 2026-07-31** — 표준형: LEWKIN(`cdn.shopify.com` ×89 · `Shopify.theme` · products.json JSON · robots에 `/{스토어ID}/checkouts`). **헤드리스형은 이 신호가 HTML에 없다** — 아래 함정 참조 |
| 3 | **고도몰(NHN커머스)** | HTML에 `godomall` / `nhn-commerce` 계열 리소스 | 「미검증」 |
| 4 | **임웹** | HTML에 `imweb` 계열 리소스(`cdn.imweb.me` 등) | 「미검증」 |
| 5 | **메이크샵** | HTML에 `makeshop` 계열 리소스 | 「미검증」 |

- 「미검증」 신호로 판별했으면 **판별 결과도 미검증이다** — `meta.notes`에 "엔진 추정:
  X (신호: Y, 미검증 판별식)"으로 남기고, 수집 중 실측으로 확증되면 이 표를 갱신할지
  사용자에게 제안한다.
- 신호가 **여러 엔진에 걸리면**(위젯·마이그레이션 흔적) 판별 실패로 취급하고
  `engine-unknown.md`로 간다 — 찍지 않는다.
- 판별식 검사는 **원본 HTML 문자열**로 한다 — 마크다운 변환기를 거치면 스크립트
  태그가 사라져 신호를 놓친다.

## 함정 2건 (실측 2026-07-31)

- **`/products.json` 200은 본문이 JSON인지까지 확인해야 한다.** SPA 자사몰은 **아무
  경로나 200 + index.html 셸**을 돌려준다 — intl.thisisneverthat.com의 `/products.json`이
  HTTP 200이지만 본문은 HTML이었다. 상태 코드만 보면 오판·오탐이 둘 다 난다.
- **헤드리스 Shopify는 HTML 신호가 없다.** 프런트가 Vite/React SPA(2KB 셸)면
  `cdn.shopify.com`도 `Shopify.theme`도 안 나온다. 번들 JS에서 `*.myshopify.com`
  참조를 찾아 판별한다(실측: intl.thisisneverthat.com → `thisisneverthat-intl.myshopify.com`
  발견, 그 백킹 도메인의 `/products.json`은 진짜 JSON). 판별식 5종이 전부 미검출인데
  사이트가 멀쩡히 돌아가면 **자체 구축**일 수도 있다(실측: adererror.com — 자체
  `/_api/goods/*` API·아임포트/토스 직접 연동) — `engine-unknown.md` 분기에서 실측으로
  특정한다.
