# 판별 실패·미지원 엔진 — 인계 절차

엔진 판별식(`engine-detect.md`)에 안 걸리거나 신호가 충돌하면 여기로 온다.
**거절하지 않는다** (SPEC-INTEL D9) — 절차는 셋 중 하나다.

## 1. platform-generic으로 1회성 수집

`../../platform-generic/SKILL.md`의 정찰 → 수집 절차를 그대로 쓴다. 자사몰이므로
`platform-ownmall/SKILL.md` §3의 **엔진 무관 원칙**(robots 확인 · 카테고리 전수 +
중복 제거 · 총계 없으면 대체 근거 · 반응 지표 없으면 null · 우회 금지)을 함께 지킨다.
`meta.site`는 도메인, `meta.notes`에 "엔진 미상"과 정찰 요약을 남긴다.

## 2. 반복 대상이면 스킬로 굳힌다

같은 엔진의 스토어를 또 만날 것 같으면 — 1의 실측 기록을 입력으로
`platform-skill-maker`를 돌려 `engine-<이름>.md`를 만들고 `engine-detect.md`에
판별 신호를 추가한다(실측 일자 필수). 초안 상태임을 보고한다.

## 3. 정찰조차 안 되면 정직하게 멈춘다

로그인 벽·전면 차단·robots가 상품 목록 자체를 막는 경우 — 우회하지 않고,
무엇이 막혔는지와 거기까지 확인한 사실을 보고한다. 부분적으로 모은 것이 있으면
부분 리포트를 만든다(`meta.incomplete: true`).
