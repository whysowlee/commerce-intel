---
name: intel-store
description: 파이프라인 **적재·공유 단**. commerce-intel 오케스트레이터가
  적재·미러·재사용 판정이 필요할 때 위임하거나, 그 작업만 할 사용자가 이름을 직접
  지정해 쓴다. 계약 JSON을 정본 DB(SQLite)에 넣고, 구글 시트로 미러하고, 팀원 DB를
  합치고(merge), 재사용 판정(TTL·갱신 주기·팀 커버리지)에 답한다. 수집 방법과 분석
  방법을 모른다. 사용자 요청의 진입점은 commerce-intel이다.
compatibility: 코드 실행이 필요하다. 시트 미러는 `pip install gspread`와 서비스 계정
  키가 있어야 하고, 없으면 적재만 하고 미러는 건너뛴다(그 사실을 보고한다).
metadata:
  version: 1.0.0
---

# intel-store — 적재·공유 단

파이프라인 2단이다. **앞 단의 산출물 형식(계약 JSON)만 알고, 뒷 단(분석)을 모른다.**

```
intel-collect → [intel-store] → intel-explore → intel-analyze → intel-insight
   계약 JSON       intel.db·시트
```

## 1. 적재

```bash
python3 ../commerce-intel/scripts/validate_data.py data/raw/<파일>.json \
    --json data/validation.json          # 먼저 검증한다
python3 ../commerce-intel/scripts/intel_db.py load data/raw/<파일>.json
```

- **검증이 `2`(실패)인 파일은 적재하지 않는다.** 결측 30% 초과는 사이트 구조 변경
  신호이고, 그대로 넣으면 오염된 관측이 영구히 남는다
- 관측은 **append only**다 — 같은 `(site, product_id, observed_at, context)`는 중복으로
  스킵된다. 같은 파일을 두 번 적재해도 안전하다(멱등)
- 정적 속성은 **비어 있지 않은 값만** 덮는다. 이번 수집이 모르는 필드를 `null`로 밀어
  기존 값을 지우지 않는다

`context`는 `brand:로우클래식` · `market:데님팬츠(남성)` · `ranking:스커트` 형식이다.
**랭킹에만 나오는 실시간 지표를 다른 문맥에 섞지 않는다** — 관측의 출처 화면을 보존한다.

## 2. 재사용 판정 — 수집 **전에** 답하는 것도 이 단의 일이다

```bash
# 시변 값: 신선한 관측이 있으면 수집 자체를 생략할 수 있다 (exit 0 = 스킵 가능)
python3 ../commerce-intel/scripts/intel_db.py check \
    --site musinsa --context "ranking:바지" --cycle-minutes 30 --team

# 정적 속성: 비싼 판단(핏 분류 등)을 DB에서 재사용
python3 ../commerce-intel/scripts/intel_db.py reuse-attrs data/raw/<수집>.json
```

- 정적 속성 TTL **90일**, 시변 값 스킵 창 = **사이트 갱신 주기**(미상이면 24시간)
- **`--team`을 항상 붙인다**(D32) — 각자 로컬 DB를 갖는 배포라 내 DB에 없다고 아무도
  안 한 게 아니다. 시트 `runs` 탭을 봐서 팀원이 이미 수집했는지 확인한다
- **스킵했으면 반드시 보고한다** — "마지막 관측이 N분 전이라 재수집을 생략했다"고
  말한다. 옛 데이터를 새것처럼 조용히 내지 않는다

## 3. 시트 미러 — 팀의 유일한 공유 지점

```bash
python3 ../commerce-intel/scripts/sync_sheets.py
```

**정본은 로컬 `intel.db`이고 시트는 교환소다**(D1). 값이 어긋나면 DB가 맞다 —
시트에서 손으로 고친 값은 다음 미러에서 덮인다.

그래도 시트가 없으면 팀이 굴러가지 않는다. 하는 일이 둘이다:

| | 무엇 | 누가 읽나 |
|---|---|---|
| 수집분 | 관측·상품·`runs` 이력 | 팀 커버리지 판정(D32) |
| **인사이트** | `insights` 테이블 — 강한 주장·약한 단서 | **전원** |

- 방향은 **로컬 → 시트 단방향**이다. 읽기는 딱 한 곳, 팀 커버리지 판정뿐이다(D32)
- **인증이 없거나 실패해도 수집·적재는 유효하다.** 미러만 밀린 것이고 다음 성공 때
  `sync_state`부터 따라잡는다. 실패 사실은 사용자에게 보고한다
- 최초 설정(서비스 계정 발급)은 `docs/SHEETS-SETUP.md` — 사용자가 1회 수행한다

⚠️ **셀 상한 1,000만 개**가 실제 제약이다. 관측이 5만 행을 넘으면 원시 관측 전부가
아니라 요약·뷰 위주로 올린다.

## 4. 팀원 DB 합치기

```bash
python3 ../commerce-intel/scripts/intel_db.py merge <팀원의 intel.db>
```

각자 로컬 DB를 갖는 배포(D31)라 축적이 사람 수만큼 갈라진다. 합칠 길이 없으면 영영
못 만난다. 충돌 규칙은 `load`와 같다 — **관측은 append only**(같은 시각·같은 문맥이면
같은 관측이라 덮을 것이 없다), **정적 계열은 비어 있지 않은 값만** 덮는다.

### 4-a. 스킬과 함께 온 seed 데이터 (D58)

배포본에는 **정본의 일부**가 `assets/seed-intel.db`로 함께 온다. 처음 설치한 사람은
빈 DB로 시작하지 않도록 이걸 한 번 합친다 — 같은 `merge` 명령이다.

```bash
python3 ../commerce-intel/scripts/intel_db.py merge \
    ~/.claude/skills/commerce-intel/assets/seed-intel.db
```

들어 있는 범위는 **2000아카이브스 전 제품**(무신사·29CM·자사몰)과 **여성 데님팬츠
브랜드랭킹 상위30 전수조사**, 그리고 그 상품들에 걸린 파생 판정(핏·lifecycle·프록시)과
관련 인사이트 이력이다. **그 밖의 정본 데이터는 배포하지 않는다**(2026-08-04 지시).

- 멱등이다 — 두 번 합쳐도 관측이 부풀지 않는다
- 이미 자기 수집이 쌓인 DB에 합쳐도 안전하다(빈 값으로 덮지 않는다)
- 여성 데님팬츠 **인사이트는 있는데 근거 전수(11,989건)는 범위 밖**이다. 주장을 읽을
  수는 있어도 수치를 되짚을 수는 없다 — 되짚어야 하면 그 런까지 배포 범위에 넣어야 한다

## 5. 축적 스냅샷 소급 적재

```bash
python3 ../commerce-intel/scripts/intel_db.py import-snapshots data/snapshots
```

멱등이라 여러 번 돌려도 안전하다. 랭킹 파일이 쌓여 있는데 DB에 없으면 시계열 분석이
그만큼 짧아진다 — 분석 전에 한 번 확인한다.

## 6. 지금 뭐가 쌓였는지

```bash
python3 ../commerce-intel/scripts/intel_db.py stats
python3 ../commerce-intel/scripts/intel_db.py export --table products --format csv
```

`stats`는 테이블별 행 수와 **문맥별 관측 수·기간**을 보여준다. 분석이 가능한 문맥인지
(시점이 2개 이상인지) 여기서 먼저 확인한다.
