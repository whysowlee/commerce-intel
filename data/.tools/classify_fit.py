#!/usr/bin/env python3
"""상품명으로 핏을 분류한다 (스토리 B 속성 분류 1단계, 근거 = name).

스킬 §B의 값 집합만 쓴다: 부츠컷 · 와이드 · 스트레이트 · 슬림 · 테이퍼드 · 쇼츠 · unknown.
이름에 단서가 없으면 **찍지 않고** unknown으로 둔다(2단계는 색상변형 그룹 전파,
3단계는 대표 이미지 확인 — 이 파일은 1단계만 한다).

usage: classify_fit.py data/raw/<수집>.json [--dry-run]

우선순위(겹칠 때 앞이 이긴다): 쇼츠 > 부츠컷 > 와이드 > 테이퍼드 > 스트레이트 > 슬림
  쇼츠가 먼저인 이유는 기장 축이라 레그 실루엣 값과 섞이면 안 되기 때문이다
  (스킬 §B: "반바지는 쇼츠다").
"""
import argparse
import json
import re
import sys

# 함정 1: `shorts_light blue`처럼 밑줄이 붙는 이름이 많다 — 단어 경계(\b)로 찾으면 놓친다.
#         그래서 전부 부분 문자열로 찾는다.
# 함정 2: `하프 밴딩`·`Half-band`는 기장이 아니라 허리 밴딩이다 → 긴바지로 둔다.
RULES = [
    ("쇼츠", ["쇼츠", "숏츠", "숏팬츠", "숏 팬츠", "반바지", "버뮤다", "bermuda", "burmuda",
             "shorts", "short pants", "2부", "3부", "4부", "5부"]),
    ("부츠컷", ["부츠컷", "부츠 컷", "부츠커트", "bootcut", "boots cut", "boot cut",
              "bootscut", "플레어", "flare"]),
    ("와이드", ["와이드", "wide"]),
    ("테이퍼드", ["테이퍼드", "테이퍼", "tapered", "taper"]),
    ("스트레이트", ["스트레이트", "straight"]),
    ("슬림", ["슬림", "slim"]),
]
HALF = re.compile(r"하프(?!\s*(밴딩|밴드))|half(?!\s*[- ]?band)", re.I)


def classify(name):
    low = (name or "").lower()
    for value, keys in RULES:
        if any(k.lower() in low for k in keys):
            return value
    if HALF.search(low):        # '하프' 단독은 기장 신호로 본다
        return "쇼츠"
    return None


def main():
    p = argparse.ArgumentParser(description="상품명으로 핏 분류")
    p.add_argument("path")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    with open(args.path, encoding="utf-8") as f:
        doc = json.load(f)

    counts = {}
    filled = 0
    for it in doc["items"]:
        attrs = it.setdefault("attributes", {})
        if attrs.get("핏"):
            continue
        v = classify(it.get("name"))
        if v:
            attrs["핏"] = v
            it["attributes_basis"] = "name"
            filled += 1
        counts[v or "unknown"] = counts.get(v or "unknown", 0) + 1

    total = len(doc["items"])
    print("상품명 분류: %d/%d (%.1f%%)" % (filled, total, 100 * filled / total))
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print("  %-8s %d" % (k, v))
    if args.dry_run:
        return 0
    doc["meta"].setdefault("notes", []).append(
        "핏 분류 1단계(상품명): %d/%d건(%.1f%%). 우선순위 쇼츠>부츠컷>와이드>테이퍼드>"
        "스트레이트>슬림, 단서 없으면 찍지 않고 unknown" % (filled, total, 100 * filled / total))
    with open(args.path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("반영 완료:", args.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
