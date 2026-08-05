#!/usr/bin/env python3
"""skills/*/SKILL.md 프론트매터가 **스킬 로더에서 실제로 파싱되는지** 검사한다.

    python3 tools/validate_skills.py          # 전부 검사, 실패면 exit 1

왜 있나 — 2026-08-04 실측(D57): platform-* 7종이 팀원 환경에 **조용히 설치되지 않았다**.
원인은 description 안의 `예: "..."`였다. 인용부호 없는 YAML 평문 스칼라에 `: `가 들어가면
파서가 매핑으로 오인해 프론트매터 전체가 깨지고, 로더는 그 스킬을 그냥 건너뛴다.
에러도 안 보이고 zip에는 파일이 멀쩡히 들어 있어서 **배포한 쪽에서는 알 방법이 없었다.**

그래서 package.sh가 압축 전에 이걸 돌린다. 실패하면 zip을 만들지 않는다.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DESC_MAX = 1024          # 스킬 로더 상한
NAME_MAX = 64

try:
    import yaml           # 있으면 진짜 파서로 검사한다 (권장)
except ImportError:
    yaml = None


def frontmatter(text: str) -> str | None:
    m = re.match(r"^---\n(.*?\n)---\n", text, re.S)
    return m.group(1) if m else None


KEY_RE = re.compile(r"^(\s*)([\w.-]+):(?:[ ]+(.*))?$")
BLOCK = {">", ">-", ">+", "|", "|-", "|+"}


def lint_plain_scalars(fm: str) -> list[str]:
    """pyyaml이 없을 때의 최소 그물 — **평문 스칼라 속** `: `만 잡는다.

    인용부호로 감싼 값과 `>-`/`|` 블록 본문은 `: `가 있어도 정상이므로 건드리지 않는다.
    """
    problems: list[str] = []
    key = None
    mode = "plain"        # plain | quoted | block | map
    block_indent = 0
    for i, line in enumerate(fm.splitlines(), start=2):
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if mode == "block" and indent > block_indent:
            continue                              # 블록 스칼라 본문 — 무엇이 있어도 안전하다
        m = KEY_RE.match(line)
        if m:                                     # 키가 시작된 줄
            key = m.group(2)
            value = (m.group(3) or "").strip()
            block_indent = len(m.group(1))
            if not value:
                mode = "map"
            elif value in BLOCK:
                mode = "block"
            elif value[:1] in "\"'":
                mode = "quoted"
            else:
                mode = "plain"
            rest = value
        else:                                     # 앞 값에서 이어지는 줄
            rest = line
        if mode == "plain" and re.search(r"\S: ", rest):
            problems.append(
                f"{i}행 `{key}` 값에 인용부호 없는 `: ` — `{key}: >-` 블록으로 바꿔라: {line.strip()[:60]}"
            )
    return problems


def check(skill_dir: Path) -> list[str]:
    errs: list[str] = []
    path = skill_dir / "SKILL.md"
    if not path.exists():
        return ["SKILL.md가 없다"]
    text = path.read_text(encoding="utf-8")
    fm = frontmatter(text)
    if fm is None:
        return ["프론트매터가 없다 — 파일이 `---` 줄로 시작해야 한다"]

    data = None
    if yaml is not None:
        try:
            data = yaml.safe_load(fm)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f"{mark.line + 2}행" if mark else "위치 불명"
            line = fm.splitlines()[mark.line].strip()[:70] if mark else ""
            errs.append(f"YAML 파싱 실패({where}) — 스킬이 조용히 무시된다: {line}")
            errs += lint_plain_scalars(fm)
            return errs
        if not isinstance(data, dict):
            return ["프론트매터가 키-값 매핑이 아니다"]
    else:
        errs += lint_plain_scalars(fm)

    if data is None:                             # pyyaml 없음 — 여기까지가 한계
        return errs

    name = data.get("name")
    if not isinstance(name, str) or not name:
        errs.append("`name`이 없다")
    else:
        if len(name) > NAME_MAX:
            errs.append(f"`name`이 {len(name)}자 — {NAME_MAX}자 상한 초과")
        if not NAME_RE.match(name):
            errs.append(f"`name`이 소문자·숫자·하이픈 형식이 아니다: {name}")
        if name != skill_dir.name:
            errs.append(f"`name`({name})이 디렉터리명({skill_dir.name})과 다르다")

    desc = data.get("description")
    if not isinstance(desc, str) or not desc.strip():
        errs.append("`description`이 없다 — 없으면 트리거링이 죽는다")
    elif len(desc) > DESC_MAX:
        errs.append(f"`description`이 {len(desc)}자 — {DESC_MAX}자 상한 초과")
    return errs


def main() -> int:
    dirs = sorted(p for p in (ROOT / "skills").iterdir() if p.is_dir())
    failed = 0
    for d in dirs:
        errs = check(d)
        if errs:
            failed += 1
            print(f"  ✗ {d.name}")
            for e in errs:
                print(f"      {e}")
        else:
            print(f"  ✓ {d.name}")
    if yaml is None:
        print("  ! pyyaml이 없어 형식 린트만 돌렸다 — pip install pyyaml 하면 실제 파서로 검사한다")
    if failed:
        print(f"\n{failed}개 스킬의 프론트매터가 깨져 있다. 이대로 배포하면 팀원 환경에 설치되지 않는다.")
        return 1
    print(f"\n{len(dirs)}개 스킬 프론트매터 정상")
    return 0


if __name__ == "__main__":
    sys.exit(main())
