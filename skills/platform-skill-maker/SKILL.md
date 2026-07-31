---
name: platform-skill-maker
description: 새 커머스 플랫폼의 수집 스킬 초안을 만든다. channel-scout의 정찰 보고서나
  platform-generic으로 수행한 1회성 수집의 실측 기록이 있을 때, 그것을 재사용 가능한
  platform-* 스킬로 굳히는 데 쓴다. "이 플랫폼 스킬로 만들어줘", "어댑터 만들어",
  "다음에도 여기서 수집할 수 있게 해줘" 같은 요청이나, 같은 플랫폼 수집이 반복될 조짐이
  보일 때 오케스트레이터가 호출한다. 스킬을 처음부터 발명하는 도구가 아니다 — 실측
  없이는 초안을 만들지 않는다.
compatibility: 파일 읽기·쓰기만 필요하다.
metadata:
  version: 1.0.0
---

# platform-skill-maker

정찰·실측 기록을 **platform-\* 스킬 초안**으로 바꾼다. 초안까지가 이 스킬의 일이고
(SPEC-INTEL D6), 최종 다듬기·배포·`skill_status: ready` 승격은 사용자가 한다.

**제1원칙: 실측 없는 항목을 사실처럼 쓰지 않는다.** 이 프로젝트 어댑터의 가치는 전부
"검증된 스킴 + 실측 일자"에서 온다. 추정으로 채운 어댑터는 없느니만 못하다 —
조용히 틀린 수집을 만든다.

## 입력 — 없으면 시작하지 않는다

다음 중 하나 이상이 필요하다. 없으면 `platform-generic`으로 정찰·1회성 수집부터
하라고 안내하고 멈춘다.

1. **channel-scout 정찰 보고서** — 목록 데이터 경로, 총계 노출, robots, 페이지네이션
2. **platform-generic 1회성 수집의 실측 기록** — 실제로 동작한 요청·파라미터·필드
3. 사용자가 준 실측 자료(네트워크 캡처, 동작 확인된 URL 등)

## 절차

### 1. 사용 예시를 확정한다

스킬은 트리거 문구로 산다. 만들기 전에 확인한다:
- 이 플랫폼에서 뭘 수집하게 되나? (브랜드 카탈로그 / 카테고리 전수 / 랭킹 — 전부는 아닐 수 있다)
- 사용자가 뭐라고 말하면 이 스킬이 떠야 하나? 예시 3개 이상.
- 갱신 주기를 아는 값이 있나? (모르면 frontmatter에 `unverified`로 두고 24시간 기본)

### 2. 템플릿을 채운다

`assets/platform-skill-template/`의 두 파일을 복사해 채운다:

```bash
mkdir -p skills/platform-<이름>/references
cp assets/platform-skill-template/SKILL.md.tmpl skills/platform-<이름>/SKILL.md
cp assets/platform-skill-template/adapter.md.tmpl skills/platform-<이름>/references/adapter.md
```

채우기 규칙:
- **모든 `[PLACEHOLDER]`를 실제 값으로 바꾼다.** 제네릭 조언보다 구체적 URL·필드명
  하나가 낫다. 채울 실측이 없는 placeholder는 지우지 말고 **`「미검증」`으로 바꾼다**
- **실측 항목마다 일자를 붙인다** — `(2026-07-31 실측)`. 일자 없는 실측 표기는 금지
- 템플릿의 `## 미검증 목록` 섹션에 미검증 항목을 모아 적는다 — 다음 실측의 할 일 목록이 된다
- 데이터 계약 필드 중 이 플랫폼이 노출하지 않는 것은 "미노출(항상 null)"로 명시한다 —
  빈칸으로 두면 나중에 "확인 안 한 것"과 구분되지 않는다

### 3. 검증 체크리스트를 돌린다

전부 통과해야 초안이다 (하나라도 실패면 고치고 다시):

- [ ] frontmatter `description`이 **3인칭 + 구체적 트리거 문구 3개 이상** —
  "…할 때 쓴다" 형식. "이 플랫폼 관련 작업에 쓴다" 같은 모호한 문구 금지
- [ ] description에 **쓰지 않는 경우**도 있다 (다른 플랫폼 요청, 구매 대행 등)
- [ ] SKILL.md 본문이 **간결하다(2,000단어 이하)** — 상세는 `references/adapter.md`로.
  같은 정보를 두 파일에 쓰지 않는다
- [ ] SKILL.md가 adapter.md를 명시적으로 가리킨다 (참조 없는 리소스는 죽은 파일이다)
- [ ] 실측 항목에 전부 일자가 있고, 미검증 항목이 `「미검증」`으로 표기돼 있다
- [ ] 산출물이 데이터 계약 JSON임이 명시돼 있고, 필드 매핑 표가 있다
- [ ] 갱신 주기(`refresh-cycle`)가 frontmatter에 있다 (미상이면 `unverified`)
- [ ] 공통 규칙(속도·차단 중단·추정 금지)을 **다시 쓰지 않고** 오케스트레이터를 가리킨다

### 4. 상태를 기록하고 보고한다

```bash
python3 ../commerce-intel/scripts/intel_db.py init  # DB 없으면
# platforms 테이블에 skill_status='draft' upsert는 오케스트레이터 세션에서 SQL로:
# INSERT INTO platforms (platform_key, name, skill_status, updated_at) VALUES (...)
#   ON CONFLICT(platform_key) DO UPDATE SET skill_status='draft', updated_at=...
```

사용자에게 보고한다: 만든 파일 경로 · 실측으로 채운 항목 수 · **미검증 항목 목록**(이게
남은 일이다) · "다듬어서 ready로 올리는 것은 사용자 몫"이라는 사실.

### 5. 반복 개선

초안 스킬을 실제 수집에 써 본 뒤가 개선 적기다: 트리거가 약하면 description에 실제
사용자 문구를 추가하고, 실측이 늘면 `「미검증」`을 실측+일자로 교체하고, 함정을 만나면
adapter.md 함정 섹션에 추가한다. 개선해도 `ready` 승격은 사용자 확인 후다.

## 참고

- 템플릿: `assets/platform-skill-template/` (SKILL.md.tmpl · adapter.md.tmpl)
- 잘 된 예: `../platform-musinsa/` — 트리거 문구·실측 일자·함정 기록의 기준선
- 방법론 출처: Anthropic skill-development(plugin-dev) 6단계 프로세스와 검증 체크리스트,
  knowledge-work-plugins data-context-extractor의 placeholder 설계를 이 프로젝트
  원칙(실측 일자·미검증 표기)과 결합했다
