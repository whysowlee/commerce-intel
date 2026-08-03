#!/usr/bin/env bash
# skills/ 아래 intel 스킬들을 배포용 zip으로 묶는다.
#
#   ./package.sh          →  dist/commerce-intel-skills.zip
#
# 묶기 전에 회귀 테스트와 PDF 스모크를 돌리고, 실패하면 만들지 않는다.
set -euo pipefail

cd "$(dirname "$0")"

# ── 배포에서 빼는 것 (D27 — HTML 산출 폐기) ────────────────────────────────
#
# 산출물은 PDF뿐이므로 **팀원 손에 HTML 생성기가 가면 안 된다.** 다만 저장소에서는
# 아직 지우지 않는다 — 회귀 테스트 111건 중 82건(B·D 계열)이 이 생성기들의 HTML
# 출력으로 diff 규칙·매칭·급등락 판정을 검증하고 있고, 그 테스트들은 실제 버그를
# 잡아 만들어진 것들이다(순위권 재진입 상품의 할인 감지 등).
#
# 테스트를 PDF·데이터 경로로 이식한 뒤 실제로 삭제한다. 그때까지는 여기서 막는다.
EXCLUDE_HTML=(
    'skills/commerce-intel/scripts/build_report.py'
    'skills/commerce-intel/scripts/build_analysis_report.py'
    'skills/commerce-intel/scripts/report_ui.py'
    'skills/commerce-intel/scripts/lint_analysis_html.py'
    'skills/commerce-intel/assets/*'
    'skills/commerce-intel/references/report-spec.md'
)

echo "[1/4] 권한 파일 최신 확인"
python3 tools/gen_permissions.py --check || {
    echo ".claude/settings.json이 정본 목록과 다르다 — python3 tools/gen_permissions.py 로 재생성하라." >&2
    exit 1
}

echo "[2/4] 회귀 테스트"
python3 tests/run_tests.py > /dev/null || {
    echo "테스트 실패 — 패키징을 중단한다. python3 tests/run_tests.py 로 확인할 것." >&2
    exit 1
}
echo "      통과"

echo "[3/4] PDF 경로 스모크"
python3 -c "import reportlab" 2>/dev/null || {
    echo "reportlab이 없다 — PDF 리포트를 만들 수 없다." >&2
    echo "    python3 -m pip install reportlab" >&2
    exit 1
}
python3 skills/commerce-intel/scripts/pdf_doc.py > /dev/null
echo "      통과 (한글 렌더 포함)"

echo "[4/4] 압축"
rm -rf dist
mkdir -p dist
find skills -name '.DS_Store' -delete
zip -rq dist/commerce-intel-skills.zip skills \
    -x '*.DS_Store' '*__pycache__*' '*.pyc' "${EXCLUDE_HTML[@]}"

# 뺐어야 할 것이 새어 나갔는지 확인한다 — 조용히 새면 팀원이 HTML을 만들게 된다
if zipinfo -1 dist/commerce-intel-skills.zip \
   | grep -qE '\.html$|build_report\.py|report_ui\.py|build_analysis_report\.py'; then
    echo "HTML 생성기가 패키지에 남아 있다 — EXCLUDE_HTML을 확인할 것." >&2
    exit 1
fi

echo
echo "완료: dist/commerce-intel-skills.zip ($(du -h dist/commerce-intel-skills.zip | cut -f1))"
zipinfo -1 dist/commerce-intel-skills.zip | sed 's/^/  /'
