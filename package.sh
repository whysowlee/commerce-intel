#!/usr/bin/env bash
# skills/ 아래 intel 스킬들을 배포용 zip으로 묶는다.
#
#   ./package.sh          →  dist/commerce-intel-skills.zip
#
# 묶기 전에 회귀 테스트를 돌리고, 실패하면 만들지 않는다.
set -euo pipefail

cd "$(dirname "$0")"

echo "[1/3] 회귀 테스트"
python3 tests/run_tests.py > /dev/null || {
    echo "테스트 실패 — 패키징을 중단한다. python3 tests/run_tests.py 로 확인할 것." >&2
    exit 1
}
echo "      통과"

echo "[2/3] 템플릿 최신화 확인"
python3 skills/commerce-intel/scripts/build_report.py --emit-template \
    --out skills/commerce-intel/assets/report-template.html > /dev/null

echo "[3/3] 압축"
rm -rf dist
mkdir -p dist
find skills -name '.DS_Store' -delete
zip -rq dist/commerce-intel-skills.zip skills \
    -x '*.DS_Store' '*__pycache__*' '*.pyc'

echo
echo "완료: dist/commerce-intel-skills.zip ($(du -h dist/commerce-intel-skills.zip | cut -f1))"
zipinfo -1 dist/commerce-intel-skills.zip | sed 's/^/  /'
