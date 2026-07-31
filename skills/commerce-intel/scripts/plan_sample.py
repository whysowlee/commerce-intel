#!/usr/bin/env python3
"""로그-랭크 층화 표본 계획 — SPEC-INTEL D21 구현.

    plan_sample.py plan --population 24673 --per-stratum 32 --out data/sample-plan.json
    plan_sample.py expand data/sample-plan.json --out data/sample-plan-2x.json
    plan_sample.py weights data/sample-plan.json

설계(사용자 확정 2026-07-31):
  - 층 경계는 로그 간격(1, 32, 100, 316, 1000, 3162, 10000, …) — 인기 롱테일에서
    상위는 촘촘히, 하위는 성기게 보되 층별 표본 수는 자유롭게 조절한다
  - 층내 추출은 균등 간격 + 오프셋 0 — 무작위 시드 없이 결정적이라 재현된다
  - expand는 오프셋을 간격의 절반으로 두고 뽑는다 → 기존 표본과 겹침 0으로 밀도 2배.
    "더 조사"가 기존 레코드를 재조사·중복 삽입하지 않는 근거가 이 중첩 성질이다
  - weights: 층별 (모집단 크기 / 표본 수) — 가중 추정용. 리포트는 '표본 추정' 라벨 필수

주의: 이 스크립트는 순위 인덱스만 계산한다. 순위→상품 대응은 수집 시점의 목록이
정하므로, 확장 수집 때는 이미 DB에 있는 product_id를 건너뛰고(재조사 금지) 순위
재배열로 생긴 중복·이동을 meta.notes에 기록하는 것은 스킬 절차의 몫이다.
"""
import argparse
import json
import math
import sys
from pathlib import Path

BASE_BOUNDS = [1, 32, 100, 316, 1000, 3162, 10000, 31623, 100000]


def strata_for(population):
    bounds = [b for b in BASE_BOUNDS if b <= population] + [population + 1]
    out = []
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        hi = min(hi - 1, population)
        if lo <= hi:
            out.append((lo, hi))
    return out


def pick(lo, hi, step, offset):
    return [i for i in range(lo + offset, hi + 1, step)]


def make_plan(population, per_stratum):
    strata = []
    for lo, hi in strata_for(population):
        size = hi - lo + 1
        step = max(1, size // per_stratum)
        idx = pick(lo, hi, step, 0)[:per_stratum] if step > 1 else list(range(lo, hi + 1))
        strata.append({"lo": lo, "hi": hi, "size": size, "step": step,
                       "offsets": [0], "indices": idx})
    return {"population": population, "per_stratum": per_stratum,
            "strata": strata, "planned": sum(len(s["indices"]) for s in strata)}


def expand_plan(plan):
    for s in plan["strata"]:
        if s["step"] <= 1:      # 이미 층 전수 — 확장할 여지가 없다
            continue
        new_off = max(1, s["step"] // 2)
        while new_off in s["offsets"]:
            # 다음 정밀화 단계: 기존 오프셋들과 절반 간격으로 엇갈리는 지점을 찾는다
            new_off = new_off // 2
            if new_off == 0:
                break
        if new_off == 0 or new_off in s["offsets"]:
            continue
        existing = set(s["indices"])
        added = [i for i in pick(s["lo"], s["hi"], s["step"], new_off)
                 if i not in existing]
        s["indices"] = sorted(existing | set(added))
        s["offsets"].append(new_off)
    plan["planned"] = sum(len(s["indices"]) for s in plan["strata"])
    return plan


def weights(plan):
    rows = []
    for s in plan["strata"]:
        n = len(s["indices"])
        rows.append({"stratum": f"{s['lo']}–{s['hi']}", "population": s["size"],
                     "n": n, "weight": round(s["size"] / n, 3) if n else None})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("plan")
    sp.add_argument("--population", type=int, required=True)
    sp.add_argument("--per-stratum", type=int, default=32)
    sp.add_argument("--out", required=True)
    sp = sub.add_parser("expand")
    sp.add_argument("plan")
    sp.add_argument("--out", required=True)
    sp = sub.add_parser("weights")
    sp.add_argument("plan")
    args = ap.parse_args()

    if args.cmd == "plan":
        plan = make_plan(args.population, args.per_stratum)
        Path(args.out).write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print(f"표본 계획: 모집단 {plan['population']:,} → 층 {len(plan['strata'])}개, "
              f"표본 {plan['planned']}개 → {args.out}")
    elif args.cmd == "expand":
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        before = plan["planned"]
        before_idx = {i for s in plan["strata"] for i in s["indices"]}
        plan = expand_plan(plan)
        after_idx = {i for s in plan["strata"] for i in s["indices"]}
        if not before_idx <= after_idx:
            sys.exit("확장이 기존 표본을 포함하지 않는다 — 버그")
        Path(args.out).write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print(f"확장: {before} → {plan['planned']}개 (기존 표본 전부 유지, 신규 "
              f"{plan['planned'] - before}개만 수집하면 된다)")
    elif args.cmd == "weights":
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        print(json.dumps(weights(plan), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
