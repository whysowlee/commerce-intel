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
# HTML 산출 경로는 D27로 폐기됐고 2026-08-03에 **실제로 삭제**했다(생성기 4종·템플릿
# 2종·report-spec.md). 회귀가 그 출력을 고정하고 있던 82건은 데이터 규칙 13건으로
# 이식했다(tests/test_intel_db.py [13]) — 나머지는 HTML 레이아웃 검증이라 함께 폐기.
# 이제 제외할 것이 없다. **assets/는 제외하지 않는다** — own-brand.json(자사 lifecycle
# 목록)이 현역이고, 통째 제외하던 탓에 배포 zip에서 빠져 tag-lifecycle이 팀원 환경에서
# 실패할 참이었다(2026-08-03 발견).

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
    -x '*.DS_Store' '*__pycache__*' '*.pyc'


echo
echo "완료: dist/commerce-intel-skills.zip ($(du -h dist/commerce-intel-skills.zip | cut -f1))"
zipinfo -1 dist/commerce-intel-skills.zip | sed 's/^/  /'
