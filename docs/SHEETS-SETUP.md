# 구글 시트 미러 — 최초 1회 설정

`sync_sheets.py`가 시트에 쓰려면 **서비스 계정 키**가 필요하다. 한 번만 하면 된다. 약 10분.

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
