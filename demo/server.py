#!/usr/bin/env python3
"""commerce-intel 4단계 데모 서버.

  ① 수집 요청(자연어 → claude -p 해석·실행 — 재사용 판정이 먼저, 필요할 때만 실수집)
  ② DB 업데이트(intel_db.py import-snapshots — 멱등)
  ③ 분석 요청(자연어 → claude -p 해석·소견)
  ④ 레포트 생성(build_analysis_report.py → HTML 대시보드)

의존성 없음(표준 라이브러리만). 실행:
  python3 demo/server.py            # http://127.0.0.1:8765
  python3 demo/server.py --port N
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

REPO = Path(__file__).resolve().parents[1]
# INTEL_DEMO_DB로 다른 DB(예: 리허설용 복사본)를 지정할 수 있다
DB = Path(os.environ.get("INTEL_DEMO_DB") or (REPO / "data" / "intel.db"))
SNAPSHOTS = REPO / "data" / "snapshots"
RAW = REPO / "data" / "raw"
OUTPUT = REPO / "output"
DEMO_LOGS = REPO / "data" / "demo-logs" / "jobs.jsonl"  # git 미추적(data/*)
INTEL_DB = REPO / "skills" / "commerce-intel" / "scripts" / "intel_db.py"
INSIGHT = REPO / "skills" / "commerce-intel" / "scripts" / "insight.py"
SYNC_SHEETS = REPO / "skills" / "commerce-intel" / "scripts" / "sync_sheets.py"
VALIDATE = REPO / "skills" / "commerce-intel" / "scripts" / "validate_data.py"

CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
CLAUDE_MODEL = "claude-opus-5"  # --model 플래그로 변경 가능
# 데모 헤드리스 세션이 권한 프롬프트에 멈추지 않게 하는 허용 목록.
# 로컬 데모 전제 — 원격 노출 시 재검토할 것.
CLAUDE_ALLOWED_TOOLS = ["Bash", "Read", "Glob", "Grep", "Write", "Edit",
                        "Agent", "ToolSearch", "WebSearch", "WebFetch"]

JOBS = {}          # id -> {status, display_log[], full_log[], result, proc}
JOBS_LOCK = threading.Lock()
# 단계 간 전달값은 **탭(세션)마다** 따로 둔다 — 전역 하나면 두 탭이 동시에 ①을 돌릴 때
# 나중에 끝난 쪽이 덮어써서 ②·③이 남의 수집 결과를 물고 간다.
SESSIONS = {}
SESS_LOCK = threading.Lock()
DB_LOCK = threading.Lock()   # ②는 DB 쓰기·시트 미러라 동시에 돌면 안 된다 (SQLite 잠금)


def sess(sid):
    """탭별 상태 슬롯. sid가 없으면 공용 슬롯 — 구버전 페이지도 그대로 동작한다."""
    sid = re.sub(r"[^\w-]", "", str(sid or ""))[:64] or "shared"
    with SESS_LOCK:
        return SESSIONS.setdefault(
            sid, {"sid": sid, "collected_files": [], "contexts": [], "step1": None})

PROMPT_COLLECT_LIVE = """\
commerce-intel 저장소에서 실행 중인 데모 수집 세션이다. 아래 사용자 수집 요청을 intel 절차대로 처리하라.

사용자 요청: {text}

절차:
1. 요청을 스토리로 해석한다 — 브랜드 라인시트 / 카테고리 전수조사 / 랭킹 스냅샷 중 하나다.
2. **재사용 판정이 먼저다** (SPEC-INTEL §2-2). \
`python3 skills/commerce-intel/scripts/intel_db.py --db "{db}" stats` 로 기존 관측 문맥을 확인하고, \
해당 문맥이 있으면 관측이 있는 사이트마다 \
`python3 skills/commerce-intel/scripts/intel_db.py --db "{db}" check --site <site> --context "<문맥>" --cycle-minutes <주기>` \
로 시변 스킵 창을 판정한다. 주기: 랭킹은 무신사 30·29CM 60, 라인시트·전수조사는 옵션 생략(기본 24시간). \
단, 사용자가 "지금"·"다시"·"새로" 등 명시적 재수집을 요구하면 판정을 건너뛰고 수집한다.
3. skip=true면 수집하지 않는다 — 어떤 문맥을 언제 수집한 몇 건짜리 관측으로 재사용하는지 summary에 보고한다.
4. 수집이 필요하면 — **아래 공통 규칙이 스토리와 무관하게 먼저 걸린다.**
   - **㉠ 미완 작업을 남기고 세션을 끝내지 마라.** 이 세션이 끝나는 순간의 결과만 남는다. \
안 끝난 작업이 있으면 이 턴에서 끝까지 처리하고, **정말 못 기다리겠으면 어디까지 됐고 \
무엇을 어디서 기다리는지 summary에 정확히 적어라** — 데모가 같은 세션을 `--resume`으로 \
이어서 물어본다(①은 대화형이다). 애매하게 "진행 중입니다"로 끝내면 그 작업은 버려진다.
     - 부연: **백그라운드(`&`·백그라운드 실행)로 띄우고 기다리는 방식은 쓰지 마라.** \
헤드리스 세션에는 "완료 알림을 받을 다음 턴"이 자기 안에 없다 — 대화형과 다른 지점이고, \
후속 턴은 **사용자가 열어줄 때만** 생긴다. 실측 2건: 프랜틱서비스 자사몰 상세 197건은 \
백그라운드에서 정상 완료됐는데 세션이 40초 먼저 끝나 계약 JSON을 못 만들었고, \
여성 니트 전수조사 8.6만 건은 백그라운드 프로세스가 세션과 함께 죽어 통째로 사라졌다.
   - **㉡ 길면 나눠 돌리고 구간마다 저장한다** (SKILL.md 1단계: "수집하면서 페이지 단위로 \
`data/raw/`에 저장한다 — 중단 시 이어서"). 한 번에 다 모아 마지막에 저장하면 중간에 끊길 때 \
**0건**이다. 사이트별·페이지 구간별로 끊어 돌리고 **구간이 끝날 때마다 파일로 저장**해 \
그 지점까지는 디스크에 남게 하라. 한 명령이 지나치게 길면 실행 하네스가 그것을 \
백그라운드로 넘겨버리므로, ㉠을 지키려면 배치가 짧아야 한다.
   - **㉢ 저장한 파일은 무조건 `files`에 담는다**(6번 참조) — 미완이어도 빼지 마라.

   스토리별 절차:
   - **랭킹 스냅샷**: `python3 data/.tools/snap_ranking_any.py --site {{musinsa|29cm}} --target <카테고리이름>` \
(타겟이 불확실하면 --list). 사이트 미지정이면 카탈로그에 있는 쪽, 둘 다 있으면 둘 다 수집한다.
     - **성별 지정이 있으면**(여자·여성·남자·남성) 무신사는 `--gender F`(여성)·`--gender M`(남성)을 \
붙인다 — 랭킹 API `gf` 필터이고 실제로 다른 랭킹이 온다. 29CM은 성별이 카테고리에 있으므로 \
`--target "여성의류>상의>반소매 티셔츠"`처럼 경로형으로 지정한다. \
**반영하지 못했으면 반영한 척하지 마라** — 전체(성별 무관) 랭킹이라는 사실을 summary에 적는다.
     - **기간·지속 수집 요청이면**("오후 11시까지", "내일까지", "계속", "매시간") 단발 수집이 아니다. \
㉠ 먼저 1회 수집해 즉시 결과를 만들고, ㉡ 같은 인자에 `--cron --until '<YYYY-MM-DD HH:MM>'`을 붙여 \
주기 등록한다. **`--cron`은 등록용 셸 명령을 출력만 한다** — 그 출력 줄을 실제로 실행해야 등록된다. \
주기는 스크립트가 사이트 실측값으로 정한다(무신사 30분·29CM 60분). 마감이 지나면 크론 줄이 \
**자기를 스스로 지우므로** 해제 작업은 필요 없다. 등록 후 `crontab -l | grep commerce-research-snap`으로 \
확인하고, summary에 **주기·마감·앞으로 몇 회 예정인지**를 적는다. \
데모 잡은 여기서 완료되는 것이 정상이다 — 축적은 크론이 이어가고 화면의 「진행 중인 모니터링」 패널에 뜬다.
   - **브랜드 라인시트**: 실제로 수집한다. 절차는 저장소에 있다 — 반드시 먼저 읽어라:
     `skills/commerce-intel/references/story-catalog.md` §A(라인시트 절차·완전성 판정),
     `skills/commerce-intel/SKILL.md`(데이터 계약 JSON 스펙),
     그리고 대상 플랫폼의 `skills/platform-{{musinsa|29cm|ownmall|generic}}/references/adapter.md`(실측 API).
     - 플랫폼이 지정 안 됐으면 `channel-scout` 서브 에이전트를 Agent 도구로 스폰해 입점처를 찾는다.
       **스폰 프롬프트에 사이트를 한정해 묻지 마라.** "무신사·29CM에 입점했는지 확인하라"처럼 물으면 \
스카우트가 나머지 채널을 「범위 외」로 라벨링해 돌려주고, 실제로 찾은 입점처가 버려진다. \
"이 브랜드가 실제로 판매되는 채널을 전부 찾아라"로 묻는다.
     - **수집 대상은 무신사·29CM에 한정되지 않는다.** SPEC-INTEL §9-4: "처음 보는 플랫폼 이름이 \
나오면 channel-scout 정찰 → generic 절차로 1회성 수집, 또는 skill-maker로 스킬 초안 — 어느 쪽이든 \
**하드코딩된 입점처 목록에 없다는 이유로 거절하지 않는다.**" (D9: 자사몰은 엔진 무관.) \
확인된 입점처를 수집 경로에 매핑한다 — 무신사→`platform-musinsa` · 29CM→`platform-29cm` · \
브랜드 공식몰(자사몰)→`platform-ownmall`(engine-detect로 엔진 판별) · \
그 밖의 편집숍·플랫폼→`platform-generic`(recon-checklist로 목록 경로 정찰 후 수집). \
**전용 스킬이 없다는 것은 수집 불가 사유가 아니다.**
     - 확인된 입점처(신뢰도 높음)만 수집 대상이고, 미확정 후보는 목록으로만 보고한다. \
확인된 입점처가 **하나도 없을 때만** 멈춘다 — 무신사·29CM에 없다는 이유로는 멈추지 않는다. \
차단(403/캡차)으로 실패한 채널은 그 사실을 적고 나머지는 끝낸다.
     - **브랜드가 그 사이트에 실재하는지 먼저 확증한다.** 무신사는 브랜드 검색 \
(`api2/dp/v1/search/brand?keyword=`)이 유사어를 섞어 주므로 `brandName` 완전일치를 확인하고, \
`caller=BRAND` totalCount=0은 상품 없음의 근거가 아니다(`caller=SEARCH` 대조). \
어느 사이트에도 없으면 그 사실을 보고하고 멈춘다 — 비슷한 브랜드로 대체하지 마라.
     - 품절 포함이 기본이고, 완전성은 독립 총계 대조로 판정한다. 한 사이트가 실패해도 나머지는 끝낸다.
     - 산출물은 사이트마다 따로 `data/raw/{{site}}-brand-linesheet-{{브랜드}}-{{YYYYMMDD-HHMM}}.json`에 \
데이터 계약 JSON으로 저장한다(`meta.story`는 `brand-linesheet`). 기존 파일 \
`data/raw/29cm-brand-linesheet-로우클래식-*.json`이 형식 참고용이다.
     - 저장 후 `python3 skills/commerce-intel/scripts/validate_data.py <파일>` 로 검증한다.
   - **전수조사**: 절차는 `skills/commerce-intel/references/story-catalog.md` §B다 — 먼저 읽어라.
     - **§B-0이 첫 단계다. 플랫폼이 지정 안 됐으면 임의로 고르지 마라.** \
`channel-scout`를 **카테고리 모드**로 스폰해(누적 카탈로그 `intel_db.py export --table platforms` 를 \
프롬프트에 넘긴다) 플랫폼별 해당 상품군 규모를 확인하고, **규모와 함께 제시해 사용자가 고르게 한다.** \
**무신사·29CM으로 좁히지 마라** — 카탈로그에 있는 특화몰·편집숍·자사몰이 전부 후보다(실측: \
`market:데님팬츠(남성)`에 bymono.com 389건이 이 경로로 들어와 있다). 데모는 되묻기가 어려우므로, \
사용자 요청에 플랫폼이 없으면 **수집하지 말고 후보 목록·규모·권장안을 summary에 담아 보고하고 멈춘다.** \
그 다음 요청에서 사용자가 고르면 수집한다.
     - **선택된 플랫폼별로 수집 경로가 갈린다.** 무신사·29CM은 \
`python3 data/.tools/scan_market_any.py --site {{musinsa|29cm}} --target <카테고리> …`(성별은 무신사 \
`--gender F|M`, 29CM은 경로형 타겟). **그 밖의 플랫폼은 `platform-generic`으로 정찰 후 수집한다** — \
전용 스킬도 카탈로그 코드도 없다는 것은 수집 불가 사유가 아니다.
     - **규모부터 본 뒤**(무신사·29CM은 `--count-only`, 그 외는 목록 첫 페이지 총계) \
**예상 소요를 보고하고 진행한다**(규모 상한은 없다 — D22로 폐지됐다). 0건이면 중단 보고. \
사이트에 해당 카테고리가 없으면 `--keyword` ∩ 상위 카테고리로 범위를 잡는다(가드 G1~G3).
     - **임의 샘플링은 금지다** — 묻지 않고 상위 N개만 모으는 것이 최악의 실패다(D21). \
사용자가 "표본으로"·"샘플링해서"라고 **명시했을 때만** §B-표본 절차를 쓴다: \
`python3 skills/commerce-intel/scripts/plan_sample.py plan --population <총계> --per-stratum <n>`으로 \
로그-랭크 층화 표본을 계획하고, `meta.sampling`·`meta.target`("…(표본 190/24,673)") 규격을 지킨다. \
`source_total`은 모집단 총계 그대로 둔다(표본 수를 넣지 마라).
5. DB 적재는 하지 마라 — 데모의 다음 단계(②)가 수행한다. 시트 미러·리포트도 하지 마라.
6. 마지막 출력 줄에 JSON 한 줄만 출력하라(다른 텍스트 금지):
{{"files": ["<신규 수집 파일 경로>", ...], "summary": "<한두 문장 — 재사용이면 문맥·수집 시각·건수>"}}
files에는 **이번 실행에서 실제로 저장한 파일을 전부** 담는다(랭킹은 `data/snapshots/…`, \
라인시트는 `data/raw/…`). **일부 사이트만 끝냈든 중간에 멈췄든, 이미 저장한 파일은 반드시 넣어라** — \
여기 빠지면 ②가 적재하지 않아 수집분이 통째로 버려진다. 미완이면 files는 그대로 채우고 \
summary에 "무엇까지 됐고 무엇이 남았는지"를 쓴다. \
저장한 파일이 하나도 없을 때만(재사용 포함) 빈 배열이다.
"""

PROMPT_ANALYZE = """\
commerce-intel 저장소에서 실행 중인 데모 분석 세션이다. 아래 사용자 분석 요청을 해석하라. \
정본 데이터는 `data/intel.db`(SQLite)다.

사용자 요청: {text}

{prev}

절차:
0. 분석 요청이 대상(브랜드·카테고리 등)을 생략했거나 모호하면 **직전 수집 요청의 주제를 분석 대상으로
삼는다** — 4단계 데모는 하나의 흐름이다. 분석 요청이 명시적으로 다른 대상을 지정한 경우에만 그쪽을 따른다.
1. `python3 skills/commerce-intel/scripts/intel_db.py --db "{db}" stats` 로 관측 문맥(context) 목록을 확인하고, \
요청에 맞는 문맥을 고른다(예: "ranking:모자", "market:데님팬츠(남성)").
2. `python3 skills/commerce-intel/scripts/build_analysis_report.py --db "{db}" \
--context "<문맥>" --emit-json --out {emit}` 으로 데이터를 뽑아 살펴본다. \
**출력 경로는 이 잡 전용이다 — 바꾸지 마라**(다른 탭의 분석과 섞인다).
3. 핵심 소견 3~5개를 불릿으로 요약한다. 정직성 규칙을 지켜라: 상관≠인과, n 표시, \
구간 표기 값(view_count 등)은 정량 근거로 쓰지 않는다.
4. DB 수정·수집·시트 미러는 금지다. 읽기만 한다.
5. 마지막 출력 줄에 JSON 한 줄만 출력하라(다른 텍스트 금지):
{{"contexts": ["<선택한 문맥>", ...], "findings": "<불릿 소견 전문>"}}
"""


PROMPT_FOLLOWUP_COLLECT = """\
{text}

(commerce-intel 데모 ①단계의 **후속 메시지**다. 앞 턴에서 읽은 절차·규칙·수집 결과는
그대로 유효하니 다시 읽지 말고 이어서 수행하라. 사용자가 플랫폼·범위를 고른 것이면
그 선택대로 진행한다.)

마지막 출력 줄에 JSON 한 줄만 출력하라(다른 텍스트 금지):
{{"files": ["<이번 턴에 저장한 파일 경로>", ...], "summary": "<한두 문장>"}}
files에는 **이번 턴에 새로 저장한 파일만** 담는다(앞 턴에서 이미 보고한 것은 뺀다).
저장한 파일이 없으면 빈 배열이다.
"""

PROMPT_FOLLOWUP_ANALYZE = """\
{text}

(commerce-intel 데모 ③단계의 **후속 메시지**다. 앞 턴에서 확인한 데이터·문맥은 그대로
유효하니 이어서 답하라. DB 수정·수집·시트 미러는 여전히 금지고 읽기만 한다.)

마지막 출력 줄에 JSON 한 줄만 출력하라(다른 텍스트 금지):
{{"contexts": ["<이번 답변의 대상 문맥>", ...], "findings": "<불릿 소견 전문>"}}
"""


def log(job, line, display=True):
    """화면 표시 여부를 제어. display=False는 로그 파일에만 남고 UI에 안 뜬다."""
    with JOBS_LOCK:
        if "display_log" not in job:
            job["display_log"] = []
        if "full_log" not in job:
            job["full_log"] = []
        job["full_log"].append(line)
        if display:
            job["display_log"].append(line)


def strip_tail_json(text):
    """어시스턴트 텍스트에서 말미 JSON 줄을 로그 표시용으로 걷어낸다."""
    lines = text.splitlines()
    kept = [l for l in lines
            if not (l.strip().startswith('{"') and l.strip().endswith("}"))]
    return "\n".join(kept).strip()


def tail_json(text):
    """출력 마지막 부분에서 JSON 오브젝트 한 줄을 찾는다."""
    for line in reversed([l.strip() for l in text.splitlines() if l.strip()]):
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def run_claude(job, prompt, resume=None):
    """claude -p 스트리밍 실행. 최종 result 텍스트를 반환한다.

    resume에 세션 id를 주면 `--resume`으로 **같은 대화를 이어간다** — 앞 턴의 문맥·
    수집 결과를 그대로 기억한다. 이게 「이어서 말하기」의 근간이다.
    """
    cmd = [CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--model", CLAUDE_MODEL]
    if resume:
        cmd += ["--resume", resume]
    cmd += ["--allowedTools"] + CLAUDE_ALLOWED_TOOLS
    proc = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    with JOBS_LOCK:
        job["proc"] = proc
    result_text = ""
    for raw in proc.stdout:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if ev.get("session_id") and not job.get("claude_session_id"):
            with JOBS_LOCK:
                job["claude_session_id"] = ev["session_id"]
        if t == "system" and ev.get("subtype") == "init":
            log(job, f"· 세션 시작 (model={ev.get('model', '?')})")
        elif t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    shown = strip_tail_json(block.get("text", ""))
                    if shown:
                        log(job, shown)
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {})
                    brief = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                    brief = str(brief).replace("\n", " ")[:120]
                    log(job, f"🔧 {name}: {brief}", display=False)
        elif t == "user":
            for block in ev.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("is_error"):
                    content = str(block.get("content", ""))[:200]
                    log(job, f"⚠️ 도구 오류: {content}", display=False)
        elif t == "result":
            result_text = ev.get("result") or ""
    proc.wait()
    stderr = proc.stderr.read().strip()
    if proc.returncode != 0 and stderr:
        log(job, f"⚠️ claude 종료 코드 {proc.returncode}: {stderr[:300]}", display=False)
    return result_text


def run_script(job, cmd):
    """스크립트 실행 — 출력을 로그로 흘리고 (returncode, 전체출력)을 반환."""
    log(job, f"$ {' '.join(str(c) for c in cmd)}", display=False)
    proc = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    with JOBS_LOCK:
        job["proc"] = proc
    lines = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            lines.append(line)
            log(job, line, display=False)
    proc.wait()
    return proc.returncode, "\n".join(lines)


def persist_job(job):
    """완료된 잡을 JSONL로 남긴다 — 사후 트러블슈팅용."""
    DEMO_LOGS.parent.mkdir(parents=True, exist_ok=True)
    with JOBS_LOCK:
        rec = {"id": job["id"], "step": job["step"], "input": job["meta"],
               "status": job["status"], "started_at": job["started_at"],
               "ended_at": datetime.now().isoformat(timespec="seconds"),
               "claude_session_id": job.get("claude_session_id"),
               "result": job["result"], "log": job.get("full_log", job.get("log", []))}
    with open(DEMO_LOGS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def job_wrapper(job, fn):
    try:
        fn(job)
        with JOBS_LOCK:
            if job["status"] == "running":
                job["status"] = "done"
    except Exception as e:  # 데모 서버 — 어떤 실패든 화면에 보이게 한다
        log(job, f"❌ {type(e).__name__}: {e}")
        with JOBS_LOCK:
            job["status"] = "error"
    finally:
        with JOBS_LOCK:
            job["proc"] = None
        try:
            persist_job(job)
        except OSError:
            pass


def new_job(fn, step, meta):
    job = {"id": uuid.uuid4().hex[:8], "status": "running",
           "display_log": [], "full_log": [],
           "result": None, "proc": None, "step": step, "meta": meta,
           "started_at": datetime.now().isoformat(timespec="seconds")}
    with JOBS_LOCK:
        JOBS[job["id"]] = job
    threading.Thread(target=job_wrapper, args=(job, fn), daemon=True).start()
    return job["id"]


# ── 4단계 구현 ──────────────────────────────────────────────

def step1(text, sid, resume=False):
    """①. resume=True면 이 탭의 직전 수집 세션을 이어간다(「이어서 말하기」)."""
    s = sess(sid)
    prev = s.get("session1") if resume else None

    def run(job):
        if prev:
            log(job, f"▶ 직전 수집 대화를 이어간다 (세션 {prev[:8]})")
            prompt = PROMPT_FOLLOWUP_COLLECT.format(text=text)
        else:
            log(job, "▶ 수집 요청 해석 시작 (재사용 판정 → 필요하면 수집)")
            prompt = PROMPT_COLLECT_LIVE.format(text=text, db=DB)
        final = run_claude(job, prompt, resume=prev)
        parsed = tail_json(final) or {}
        files = parsed.get("files") or []
        summary = parsed.get("summary") or (final.strip()[:300] if final else "(응답 없음)")
        # 이어말하기는 앞 턴의 수집물을 덮지 않는다 — ②가 전부 적재해야 한다
        if prev:
            merged = list(s.get("collected_files") or [])
            merged += [f for f in files if f not in merged]
            s["collected_files"] = merged
        else:
            s["collected_files"] = files
        s["session1"] = job.get("claude_session_id") or prev
        s["step1"] = {"text": text, "summary": summary}
        with JOBS_LOCK:
            job["result"] = {"files": files, "summary": summary,
                             "can_continue": bool(s.get("session1"))}
        log(job, f"✅ 파일 {len(files)}개 — {summary}")
    return new_job(run, 1, {"text": text, "sid": s["sid"], "resumed": bool(prev)})


def unloaded_raw_files():
    """`data/raw/`의 라인시트 계약 JSON 중 DB에 아직 없는 것.

    안전망이다 — ①이 파일을 만들고도 files 보고를 빠뜨리면(실측 2026-07-31, 프랜틱서비스)
    수집분이 통째로 버려진다. runs.raw_file과 파일명으로 대조해 누락분을 찾는다.
    전수조사(market-scan)는 파일이 커서 자동으로 줍지 않는다 — 알리기만 한다.
    """
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        loaded = {os.path.basename(r[0] or "") for r in
                  conn.execute("SELECT raw_file FROM runs WHERE raw_file IS NOT NULL")}
        conn.close()
    except sqlite3.Error:
        return [], []
    pick, skip = [], []
    for p in sorted(RAW.glob("*.json")):
        if p.name in loaded:
            continue
        try:
            meta = (json.loads(p.read_text(encoding="utf-8")) or {}).get("meta") or {}
        except (OSError, ValueError):
            continue
        row = (p, meta)
        (pick if meta.get("story") == "brand-linesheet" else skip).append(row)
    return pick, skip


def step2(sid):
    s = sess(sid)

    def run(job):
        if DB_LOCK.locked():   # 다른 탭의 ②가 DB를 쓰는 중이면 끝날 때까지 기다린다
            log(job, "· 다른 탭의 DB 업데이트가 끝나기를 기다리는 중…")
        with DB_LOCK:
            _step2_body(job, s)
        s["db_updated"] = True      # 이 탭에서 시트 업로드 버튼이 열리는 조건
    return new_job(run, 2, {"sid": s["sid"], "kind": "db"})


def step2_sheets(sid):
    """②-2 구글 시트 업로드. 정본 적재와 분리된 버튼이다 — 정본이 먼저다."""
    s = sess(sid)

    def run(job):
        if DB_LOCK.locked():
            log(job, "· 다른 탭의 DB 작업이 끝나기를 기다리는 중…")
        with DB_LOCK:
            # 단방향 미러 (SPEC-INTEL §3). 실패해도 정본은 유효하므로 잡을 죽이지 않고
            # 사실만 보고한다 — §9 성공 기준 2("다음 성공 때 따라잡는다").
            log(job, "▶ 구글 시트 미러 시작 (단방향 — 정본은 DB다)")
            rc, out = run_script(job, ["python3", str(SYNC_SHEETS), "--db", str(DB)])
            if rc == 0:
                mirror = "성공"
            elif rc == 3:
                mirror = "건너뜀 — 인증·설정 없음"
                log(job, "⚠️ 인증·설정이 없다 (docs/SHEETS-SETUP.md). 정본은 그대로다")
            else:
                mirror = f"실패 (코드 {rc})"
                log(job, "⚠️ 미러 실패 — 정본은 유효하고 다음 업로드 때 따라잡는다")
            with JOBS_LOCK:
                job["result"] = {"sheets": mirror, "output": out[-800:] if out else ""}
            log(job, f"✅ 구글 시트 업로드 {mirror}")
    return new_job(run, 2, {"sid": s["sid"], "kind": "sheets"})


def _step2_body(job, s):
        log(job, "▶ DB 적재 시작 (중복 관측은 자동 스킵 — 멱등)")
        rc, _ = run_script(job, ["python3", str(INTEL_DB), "--db", str(DB),
                                 "import-snapshots", str(SNAPSHOTS)])
        if rc != 0:
            raise RuntimeError(f"import-snapshots 실패 (코드 {rc})")
        # ①이 data/snapshots 밖에 만든 수집물(라인시트 등)은 파일 단위로 적재한다
        extra = [f for f in s.get("collected_files") or []
                 if not f.startswith("data/snapshots/")]
        for rel in extra:
            path = REPO / rel
            if not path.exists():
                log(job, f"⚠️ 건너뜀 — 파일 없음: {rel}")
                continue
            rc, _ = run_script(job, ["python3", str(INTEL_DB), "--db", str(DB),
                                     "load", str(path)])
            if rc != 0:
                raise RuntimeError(f"load 실패: {rel} (코드 {rc})")
        # 안전망 — ①이 보고하지 않았지만 디스크에 있는 라인시트를 줍는다
        pick, skip = unloaded_raw_files()
        rescued = 0
        for path, meta in pick:
            log(job, f"⚠️ ①이 보고하지 않은 수집 파일 발견: {path.name} "
                     f"({meta.get('site')} · {meta.get('target')} · {meta.get('item_count')}건)")
            # 검증을 통과한 것만 적재한다 — 파이프라인 순서(수집→검증→적재)를 안전망도 지킨다
            rc, _ = run_script(job, ["python3", str(VALIDATE), str(path)])
            if rc != 0:
                log(job, f"⚠️ 검증 FAIL — 적재하지 않는다: {path.name} "
                         "(수집한 세션에서 원인을 확인해야 한다)")
                continue
            rc, _ = run_script(job, ["python3", str(INTEL_DB), "--db", str(DB),
                                     "load", str(path)])
            if rc == 0:
                rescued += 1
            else:                       # 안전망이 잡을 죽이면 안 된다 — 알리고 넘어간다
                log(job, f"⚠️ 적재 실패 — 건너뛴다: {path.name} (코드 {rc})")
        for path, meta in skip:
            log(job, f"· 미적재 파일 있음(자동 적재 대상 아님): {path.name} "
                     f"— story={meta.get('story')}")
        _, stats = run_script(job, ["python3", str(INTEL_DB), "--db", str(DB), "stats"])
        with JOBS_LOCK:
            job["result"] = {"stats": stats, "rescued": rescued}
        log(job, "✅ 정본 DB 업데이트 완료"
                 + (f" · 누락 수집분 {rescued}건 구제" if rescued else "")
                 + " — 구글 시트 업로드 버튼이 열렸다")


def step3(text, sid, resume=False):
    """③. resume=True면 이 탭의 직전 분석 세션을 이어간다(「이어서 말하기」)."""
    s = sess(sid)
    prior = s.get("session3") if resume else None

    def run(job):
        if prior:
            log(job, f"▶ 직전 분석 대화를 이어간다 (세션 {prior[:8]})")
            prompt = PROMPT_FOLLOWUP_ANALYZE.format(text=text)
        else:
            p1 = s.get("step1")
            prev = (f'직전 수집 요청(①단계): "{p1["text"]}"\n'
                    f'직전 수집 결과 요약: {p1["summary"]}') if p1 else "(직전 수집 요청 없음)"
            log(job, "▶ 분석 요청 해석 시작"
                     + (f" — 직전 수집 문맥 승계: {p1['text']}" if p1 else ""))
            # 추출 파일은 잡마다 따로 — 고정 경로면 동시에 도는 ③끼리 서로 덮어쓴다
            emit = f"output/demo-analysis-emit-{job['id']}.json"
            prompt = PROMPT_ANALYZE.format(text=text, db=DB, prev=prev, emit=emit)
        final = run_claude(job, prompt, resume=prior)
        parsed = tail_json(final) or {}
        contexts = parsed.get("contexts") or []
        findings = parsed.get("findings") or (final.strip()[:1000] if final else "(응답 없음)")
        if contexts or not prior:      # 후속 답이 문맥을 안 주면 앞 턴 선정을 유지한다
            s["contexts"] = contexts
        s["session3"] = job.get("claude_session_id") or prior
        with JOBS_LOCK:
            job["result"] = {"contexts": s.get("contexts") or [], "findings": findings,
                             "can_continue": bool(s.get("session3"))}
        log(job, f"✅ 문맥 {len(s.get('contexts') or [])}개 선정: "
                 + (', '.join(s.get('contexts') or []) or '(없음 — 전체 DB)'))
    return new_job(run, 3, {"text": text, "sid": s["sid"], "resumed": bool(prior)})


def _slug(s):
    return re.sub(r"[\s_]+", "-", s.strip().lower())


def linesheet_inputs(brand):
    """brand 문맥에 맞는 라인시트 raw JSON을 사이트별 최신 1개씩 고른다.

    파일명 규약: {site}-brand-linesheet-{브랜드}-{YYYYMMDD-HHMM}.json
    문맥의 브랜드명과 파일명 슬러그는 대소문자·공백이 다를 수 있어 정규화해 맞춘다.
    """
    want = _slug(brand)
    latest = {}  # site -> (stamp, path)
    for p in sorted(RAW.glob("*-brand-linesheet-*.json")):
        m = re.fullmatch(r"(.+)-brand-linesheet-(.+)-(\d{8}-\d{4})\.json", p.name)
        if not m or _slug(m.group(2)) != want:
            continue
        site, stamp = m.group(1), m.group(3)
        if site not in latest or stamp > latest[site][0]:
            latest[site] = (stamp, p)
    return [latest[site][1] for site in sorted(latest)]


def latest_context():
    """가장 최근에 수집된 관측 문맥 하나. ④에 문맥이 안 넘어왔을 때 쓴다.

    ③을 건너뛰면 문맥이 비는데, 그대로 두면 문맥 13개가 한 판에 섞인 대시보드가
    나온다 — 카테고리·사이트·수집 목적이 뒤섞이면 변인통제가 성립하지 않는다.
    그래서 직전 수집 문맥으로 좁힌다(①의 결과를 이어받는 셈이다).
    """
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        row = conn.execute("SELECT context FROM observations WHERE context IS NOT NULL "
                           "ORDER BY observed_at DESC, rowid DESC LIMIT 1").fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def step4(contexts, sid):
    s = sess(sid)
    auto = None
    if not contexts:
        auto = latest_context()
        if auto:
            contexts = [auto]

    def run(job):
        if auto:
            log(job, f"· 문맥 미지정 — 가장 최근 수집 문맥으로 좁힌다: {auto}")
        elif not contexts:
            log(job, "· 문맥 미지정 · DB에 관측이 없다 — 전체 DB로 만든다")
        # 파일명에 잡 id를 붙인다 — 초 단위 타임스탬프만으로는 동시 생성 시 덮어쓴다
        ts = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + job["id"]
        OUTPUT.mkdir(exist_ok=True)
        reports = []
        # 인사이트 PDF 2층 (insight.py — DB가 입력). HTML 리포트는 D27로 폐기됐다.
        # 문맥마다 따로 낸다 — 대상이 다르면 리포트도 다르다.
        for c in contexts:
            target = c.split(":", 1)[1] if ":" in c else c
            log(job, f"▶ 인사이트 생성 — {target} (EDA → 방법론 결정 → 5관문 → PDF 2층)")
            rc, _ = run_script(job, ["python3", str(INSIGHT), "--db", str(DB),
                                     "--context", c, "--target", target,
                                     "--out", str(OUTPUT)])
            if rc != 0:
                raise RuntimeError(f"인사이트 생성 실패: {target} (코드 {rc})")
            # insight.py는 파일명에 자기 타임스탬프를 쓴다 — 방금 만든 것을 집어낸다
            safe = target.replace("/", "-").replace(":", "-")
            for kind, label in (("insight", "인사이트"), ("detail", "상세 근거")):
                hits = sorted(OUTPUT.glob(f"{kind}-{safe}-*.pdf"),
                              key=lambda f: f.stat().st_mtime)
                if hits:
                    reports.append({"url": f"/output/{hits[-1].name}",
                                    "label": f"{target} {label}"})
        with JOBS_LOCK:
            job["result"] = {"reports": reports}
        log(job, f"✅ 생성 완료 {len(reports)}개 — "
                 + ", ".join(r["url"].rsplit("/", 1)[-1] for r in reports))
    return new_job(run, 4, {"contexts": contexts, "auto_context": auto, "sid": s["sid"]})


# ── 진행 중인 모니터링 (crontab 등록분) ──────────────────────

CRON_TAG = "commerce-research-snap-"
GENDER_LABEL = {"A": "전체", "M": "남성", "F": "여성"}


def _cron_opt(line, name):
    m = re.search(r"--%s\s+'([^']*)'|--%s\s+(\S+)" % (name, name), line)
    return (m.group(1) or m.group(2)) if m else None


def crontab_lines():
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except OSError:
        return []
    return r.stdout.splitlines() if r.returncode == 0 else []


def write_crontab(lines):
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)


def monitor_stop(tag):
    """태그가 정확히 일치하는 줄만 지운다 — 부분 일치로 다른 잡을 지우면 안 된다."""
    lines = crontab_lines()
    kept = [l for l in lines if not l.rstrip().endswith("# " + tag)]
    if len(kept) == len(lines):
        return False
    write_crontab(kept)
    return True


def monitor_set_until(tag, until):
    """마감을 바꾼다. until이 None이면 무기한으로 되돌린다."""
    lines, hit = crontab_lines(), False
    out = []
    for l in lines:
        if l.rstrip().endswith("# " + tag):
            hit = True
            l = re.sub(r"\s--until\s+'[^']*'", "", l)
            if until:
                l = l.replace(" >>", " --until '%s' >>" % until, 1)
        out.append(l)
    if hit:
        write_crontab(out)
    return hit


def monitors():
    """crontab의 랭킹 모니터링 잡 + 지금까지의 축적 현황을 읽는다.

    ①이 기간 요청을 cron으로 등록하므로(잡은 즉시 완료된다) 축적은 여기서 보인다.
    마감이 지난 잡은 목록에서 빼고 보여준다 — crontab 줄은 다음 실행에 스스로 사라진다.
    """
    out = []
    for line in crontab_lines():
        if CRON_TAG not in line or line.lstrip().startswith("#"):
            continue
        site, base = _cron_opt(line, "site"), _cron_opt(line, "target")
        if not (site and base):
            continue
        gender = _cron_opt(line, "gender") or "A"
        until = _cron_opt(line, "until")
        target = base if gender == "A" else f"{base}({GENDER_LABEL.get(gender, gender)})"
        minute = line.split()[0]
        files = sorted(SNAPSHOTS.glob(f"{site}-ranking-{target.replace('/', '·')}-*.json"))
        last = None
        if files:
            stamp = files[-1].stem.rsplit("-", 2)[-2:]        # YYYYMMDD, HHMM
            last = f"{stamp[0][4:6]}-{stamp[0][6:]} {stamp[1][:2]}:{stamp[1][2:]}"
        try:
            expired = bool(until and datetime.now() >= datetime.strptime(until, "%Y-%m-%d %H:%M"))
        except ValueError:                    # 손으로 고친 --until 형식이 깨진 경우
            expired = False
        if expired:                           # 마감분은 목록에서 뺀다 (줄은 스스로 사라진다)
            continue
        tag = line.rsplit("# ", 1)[-1].strip() if "# " in line else None
        out.append({"site": site, "target": target, "cycle": f"매시 {minute}분",
                    "until": until, "tag": tag, "count": len(files), "last": last})
    return out


# ── HTTP ────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._file(Path(__file__).parent / "index.html", "text/html; charset=utf-8")
        elif self.path.startswith("/api/job/"):
            job = JOBS.get(self.path.rsplit("/", 1)[-1])
            if not job:
                self._json({"error": "no such job"}, 404)
                return
            with JOBS_LOCK:
                self._json({"status": job["status"],
                            "log": job.get("display_log", job.get("log", [])),
                            "result": job["result"]})
        elif self.path.split("?")[0] == "/api/state":
            q = parse_qs(urlparse(self.path).query)
            s = sess((q.get("sid") or [""])[0])
            self._json({"collected_files": s["collected_files"],
                        "contexts": s["contexts"], "sid": s["sid"], "db": str(DB)})
        elif self.path == "/api/monitors":
            self._json({"monitors": monitors()})
        elif self.path.startswith("/output/"):
            # 브라우저가 한글 파일명을 퍼센트 인코딩해 보내므로 먼저 푼다
            name = os.path.basename(unquote(self.path))
            if not re.fullmatch(r"[\w.\-가-힣 ]+\.(html|pdf)", name):
                self.send_error(400)
                return
            # PDF를 text/html로 내보내면 브라우저가 원문을 텍스트로 뿌린다
            ctype = ("application/pdf" if name.lower().endswith(".pdf")
                     else "text/html; charset=utf-8")
            self._file(OUTPUT / name, ctype)
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            body = self._body()
        except (json.JSONDecodeError, ValueError):
            self._json({"error": "bad json"}, 400)
            return
        sid = body.get("sid")
        if self.path in ("/api/step1", "/api/step1/continue"):
            text = (body.get("text") or "").strip()
            if not text:
                self._json({"error": "요청 문장을 입력하세요"}, 400)
                return
            resume = self.path.endswith("/continue")
            if resume and not sess(sid).get("session1"):
                self._json({"error": "이어갈 수집 대화가 없습니다 — 먼저 요청을 실행하세요"}, 400)
                return
            self._json({"job": step1(text, sid, resume=resume)})
        elif self.path == "/api/step2":
            self._json({"job": step2(sid)})
        elif self.path == "/api/step2-sheets":
            # 화면에서도 막지만 서버에서도 막는다 — 정본이 먼저다
            if not sess(sid).get("db_updated"):
                self._json({"error": "정본 DB 업데이트를 먼저 실행하세요"}, 400)
                return
            self._json({"job": step2_sheets(sid)})
        elif self.path in ("/api/step3", "/api/step3/continue"):
            text = (body.get("text") or "").strip()
            if not text:
                self._json({"error": "요청 문장을 입력하세요"}, 400)
                return
            resume = self.path.endswith("/continue")
            if resume and not sess(sid).get("session3"):
                self._json({"error": "이어갈 분석 대화가 없습니다 — 먼저 요청을 실행하세요"}, 400)
                return
            self._json({"job": step3(text, sid, resume=resume)})
        elif self.path == "/api/step4":
            contexts = [c.strip() for c in (body.get("contexts") or []) if c.strip()]
            self._json({"job": step4(contexts, sid)})
        elif self.path in ("/api/monitor/stop", "/api/monitor/until"):
            tag = (body.get("tag") or "").strip()
            if not tag.startswith(CRON_TAG):   # 태그 밖 crontab 줄은 건드리지 않는다
                self._json({"error": "모니터링 태그가 아니다"}, 400)
                return
            if self.path.endswith("/stop"):
                self._json({"ok": monitor_stop(tag)})
                return
            until = (body.get("until") or "").strip() or None
            if until:
                try:
                    datetime.strptime(until, "%Y-%m-%d %H:%M")
                except ValueError:
                    self._json({"error": "마감 형식은 'YYYY-MM-DD HH:MM'이다"}, 400)
                    return
            self._json({"ok": monitor_set_until(tag, until)})
        elif self.path.startswith("/api/job/") and self.path.endswith("/cancel"):
            job = JOBS.get(self.path.split("/")[3])
            if job and job.get("proc"):
                job["proc"].terminate()
                log(job, "⏹ 사용자 취소")
                with JOBS_LOCK:
                    job["status"] = "error"
            self._json({"ok": True})
        else:
            self.send_error(404)

    def log_message(self, *args):  # 액세스 로그 침묵
        pass


def main():
    global CLAUDE_MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--model", default="claude-opus-5",
                    choices=["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"],
                    help="헤드리스 세션 모델")
    args = ap.parse_args()
    CLAUDE_MODEL = args.model
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"commerce-intel 데모: http://127.0.0.1:{args.port}  (Ctrl-C로 종료)")
    print(f"DB: {DB}")
    print(f"헤드리스 모델: {CLAUDE_MODEL}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
