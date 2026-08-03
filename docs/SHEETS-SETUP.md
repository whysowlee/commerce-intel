# 구글 시트 미러 — 최초 1회 설정

`sync_sheets.py`가 시트에 쓰려면 **서비스 계정 키**가 필요하다. 한 번만 하면 된다. 약 10분.

> **팀 배포라면 §5를 먼저 읽는다.** 아래 1~4는 **미러를 쓰는 사람(1명)** 절차다.
> 팀원은 다른 절차를 따르고, **편집자 권한을 주면 안 된다**(§5에 이유).

## 1. GCP에서 서비스 계정 만들기

1. https://console.cloud.google.com → 프로젝트 선택(없으면 새로 만들기, 예: `commerce-intel`)
2. **API 및 서비스 → 라이브러리** → `Google Sheets API` 검색 → **사용 설정**
3. **IAM 및 관리자 → 서비스 계정 → 서비스 계정 만들기**
   - 이름: `intel-sheets-mirror` (역할은 부여하지 않아도 된다 — 시트 공유로 권한을 준다)
4. 만든 계정 클릭 → **키 → 키 추가 → 새 키 만들기 → JSON** → 다운로드

## 2. 키 파일 두기

```bash
mkdir -p ~/.config/intel
mv ~/Downloads/<다운로드된-키>.json ~/.config/intel/service-account.json
chmod 600 ~/.config/intel/service-account.json
```

다른 경로를 쓰려면 `export INTEL_SHEETS_CREDENTIALS=/path/to/key.json`.
**키 파일을 레포 안에 두지 않는다** (git에 올라가면 즉시 폐기·재발급).

## 3. 스프레드시트 만들고 공유하기

1. https://sheets.new 로 빈 스프레드시트 생성 (이름 예: `commerce-intel mirror`)
2. URL에서 ID를 복사한다 — `https://docs.google.com/spreadsheets/d/`**`<이 부분>`**`/edit`
3. **공유** → 키 JSON 안의 `client_email` 값(`...@....iam.gserviceaccount.com`)을
   **편집자**로 추가
4. 레포의 `data/sheets_config.json`에 기록:

```json
{"spreadsheet_id": "여기에-ID"}
```

## 4. 확인

```bash
pip3 install gspread          # 최초 1회
python3 skills/commerce-intel/scripts/sync_sheets.py
```

`안내` 탭과 테이블 탭들이 생기면 성공이다. 이후에는 수집 파이프라인이 적재 후 자동으로
호출한다 — 설정 전까지는 "미러가 밀렸다"는 보고만 나오고 수집·분석은 정상 동작한다.

## 5. 팀원 접근 (전원 풀 배포 — D31·D32)

### 먼저: 접근이 두 종류다

| 무엇 | 누가 쓰나 | 필요한 것 |
|---|---|---|
| **눈으로 본다** — 인사이트·뷰 탭 열람 | 사람 | 구글 계정을 시트에 **뷰어**로 공유 |
| **스킬이 읽는다** — `check --team`이 `runs` 조회 | `intel_db.py` | **서비스 계정 키**(뷰어 권한) |
| **스킬이 쓴다** — `sync_sheets.py` 미러 | `sync_sheets.py` | 서비스 계정 키(편집자) — **1명만** |

팀원이 수집을 하려면 두 번째가 반드시 필요하다. 눈으로 보는 것만으로는 중복 수집
경고(D32)가 동작하지 않는다 — 그건 사람이 아니라 스크립트가 시트를 읽는 일이다.

### 팀원에게 편집자를 주면 안 되는 이유

`sync_sheets.py`는 `products`·`variants`·`platforms`·`runs`·`proxy_defs` 탭을
**전체 다시 쓰기**한다. 팀원이 편집자 권한으로 이걸 돌리면:

- `ws.clear()` 후 **자기 로컬 DB 내용으로 덮어쓴다** — 팀 전체 축적이 그 팀원 것만 남는다
- 자기 DB에 없는 테이블은 `del_worksheet()`로 **탭 자체를 지운다**
- `observations`는 진행점(`sync_state`)이 로컬이라 자기 rowid 1번부터 전량 재append한다

즉 편집자를 나눠주는 순간, 이 구조가 피하려던 덮어쓰기가 그대로 일어난다.
**미러를 쓰는 사람은 1명으로 고정한다.**

### 팀원 데이터를 합치는 경로

시트가 아니라 DB 파일이다. 팀원이 자기 `data/intel.db`를 보내면(드라이브·슬랙 등
아무 수단이나) 미러 담당자가 합치고, 합친 결과를 미러한다.

```bash
python3 skills/commerce-intel/scripts/intel_db.py merge <팀원이-보낸>.db
python3 skills/commerce-intel/scripts/sync_sheets.py
```

시트 셀로 펼쳤다 되접으면 타입이 깎이고 셀 한도(1,000만)를 팀원 수만큼 곱하게 된다.

### 절차 — 소유자가 할 일 (약 10분)

GCP 프로젝트(`commerce-intel`)와 Sheets API는 **이미 설정돼 있다**(쓰기 계정
`intel-sheets-mirror@commerce-intel.iam.gserviceaccount.com`이 그 증거다).
따라서 §1의 1~2는 건너뛰고, **읽기 전용 계정 하나만 더 만들면 된다.**

**① 읽기 전용 서비스 계정 만들기**

1. https://console.cloud.google.com/iam-admin/serviceaccounts?project=commerce-intel 접속
2. 위쪽 **`+ 서비스 계정 만들기`**
3. 이름에 `intel-team-read` 입력 → **`만들고 계속하기`**
4. "역할 부여"는 **건너뛴다**(권한은 시트 공유로 준다) → **`완료`**

**② 키 파일 받기**

1. 방금 만든 `intel-team-read`를 목록에서 클릭
2. **`키`** 탭 → **`키 추가`** → **`새 키 만들기`** → **JSON** 선택 → **`만들기`**
3. JSON 파일이 자동으로 다운로드된다. **이 파일을 팀원들에게 나눠준다**

**③ 시트에 뷰어로 추가** — 두 종류를 각각 추가한다

키 파일 안의 이메일을 확인한다(파일을 열어 `client_email`을 봐도 된다):

```bash
python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['client_email'])" \
        ~/Downloads/<받은-키>.json
```

스프레드시트 우상단 **`공유`**를 누르고,

- 위에서 확인한 `intel-team-read@commerce-intel.iam.gserviceaccount.com` → **뷰어**
- **팀원 각자의 구글 계정** → **뷰어** (사람이 눈으로 볼 창구)

⚠️ 서비스 계정을 추가할 때는 **`알림 보내기` 체크를 해제**한다 — 사람이 아니라
메일이 반송된다. 그리고 권한 드롭다운이 기본 `편집자`인 경우가 있으니 **반드시
`뷰어`로 바꾼다**(이유는 위 "편집자를 주면 안 되는 이유").

**④ 팀원에게 두 가지 전달** — ②의 키 파일, 그리고 스프레드시트 ID
(시트 URL의 `/d/` 와 `/edit` 사이 문자열)

### 계정을 하나만 쓸까, 팀원마다 나눌까

위 절차는 **읽기 전용 계정 하나를 팀이 공유**하는 방식이다. 소규모·안정적인 팀이면
이걸로 충분하다 — 유출돼도 상대가 얻는 건 이 시트 **읽기** 권한뿐이고, 회수는 GCP에서
키 하나 지우고 새로 나눠주면 끝난다.

**팀원마다 따로 만드는 편이 나은 경우**: 인원이 늘거나(대략 5명 이상), 이동이 잦거나,
누가 얼마나 호출하는지 봐야 할 때. 그때는 ①~②를 사람 수만큼 반복하고 이름을
`intel-read-<이름>`으로 구분해 짓는다. 그러면 나간 사람 것만 지우면 되고 호출 로그가
갈린다. **나중에 바꿔도 되는 결정이다** — 지금 하나로 시작해도 막히지 않는다.

### 절차 — 팀원이 할 일

```bash
mkdir -p ~/.config/intel
mv ~/Downloads/<받은-키>.json ~/.config/intel/service-account.json
chmod 600 ~/.config/intel/service-account.json
pip3 install gspread

# 레포 안에 data/sheets_config.json 생성 (ID는 소유자에게 받는다)
echo '{"spreadsheet_id": "<받은-ID>"}' > data/sheets_config.json

# 확인 — 팀 커버리지가 조회되면 성공이다
python3 skills/commerce-intel/scripts/intel_db.py check \
        --site 29cm --context "ranking:여성슈즈" --cycle-minutes 60 --team
```

`"consulted": true`가 나오면 된 것이다. `false`면 `error`에 사유가 있다
(키 경로·시트 공유 누락·`gspread` 미설치). **`consulted: false`여도 수집은 막히지
않는다** — 로컬 판정으로 진행되니 급하면 그대로 써도 된다.

팀원은 `sync_sheets.py`를 돌리지 않는다. 돌려도 뷰어 권한이라 exit 1로 실패한다 —
권한 설정이 곧 안전장치다.

### 키 전달 주의

- 키 JSON은 **비밀번호와 같다.** 레포·슬랙 공개 채널·이메일 본문에 붙여넣지 않는다
- `.gitignore` 여부와 무관하게 **레포 안에 두지 않는다.** git에 올라갔다면 즉시
  GCP에서 해당 키를 삭제하고 재발급한다(파일을 지우는 것만으로는 이력에 남는다)
- 팀원이 나가면 **그 사람 서비스 계정만 삭제**한다. 시트 공유도 함께 해제한다
