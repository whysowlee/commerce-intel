#!/usr/bin/env python3
"""색상만 다른 같은 상품을 묶어 속성 분류 횟수를 줄인다 (스토리2).

같은 상품의 색상 전개가 **각각 별개 상품으로** 목록에 올라온다. 색상은 핏을 바꾸지
않으므로 그룹당 한 번만 판단하면 된다. 이미지 확인은 상품 수 × 몇 초라서 이 절감이
그대로 시간이 된다.

usage:
    # ① 계획 — 읽기만 한다. 그룹과 대표 상품, 이미 아는 값을 뽑는다
    python3 group_variants.py data/raw/scan.json --attr 핏 --plan data/plan.json

    # ② 계획 파일의 value가 빈 그룹만 대표 이미지를 보고 채운다 (에이전트가 한다)

    # ③ 반영 — 그룹의 값을 구성원 전체에 전파한다
    python3 group_variants.py data/raw/scan.json --attr 핏 --apply data/plan.json

exit code:
    0  정상
    2  입력 오류

**묶는 규칙은 결정적이다.** 유사도로 묶지 않는다 — 이 프로젝트는 상품명 유사도 매칭을
실측으로 기각했다(`헨리넥 숏슬리브` vs `와플 헨리넥 롱슬리브`가 높은 유사도로 잡혔다).
같은 브랜드 안에서 **색상·품번·괄호를 떼어낸 이름이 완전히 같을 때만** 한 그룹이다.
"""

import argparse
import json
import re
import sys

# 색상 토큰. 이름 어디에 있어도 떼어낸다 — 색상은 핏을 바꾸지 않는다.
COLOR = (
    r"블랙|화이트|아이보리|베이지|그레이지|그레이|차콜|챠콜|네이비|라이트블루|스카이블루|"
    r"연청|중청|진청|흑청|인디고|블루|브라운|카멜|카키|올리브|그린|퍼플|바이올렛|핑크|레드|"
    r"와인|버건디|옐로우|오렌지|크림|에크루|멜란지|모카|샌드|스톤|더스티|"
    r"light\s?blue|sky\s?blue|dark\s?grey|dark\s?gray|"
    r"black|white|ivory|beige|charcoal|grey|gray|navy|indigo|blue|brown|camel|khaki|"
    r"olive|green|purple|violet|pink|red|wine|burgundy|yellow|orange|cream|ecru|"
    r"melange|mocha|sand|stone"
)
BRACKET = re.compile(r"\[[^\]]*\]|\([^)]*\)")
COLORCOUNT = re.compile(r"\b\d?\s?colors?\b", re.I)
SKU = re.compile(r"\b[A-Z]{2,}\d{3,}[A-Z0-9]*\b")
COLOR_TOKEN = re.compile(r"(?<![가-힣A-Za-z])(?:%s)(?![가-힣A-Za-z])" % COLOR, re.I)


def base_name(name):
    """색상·품번·괄호를 떼어낸 이름. 이게 같으면 같은 상품의 다른 색으로 본다."""
    text = name or ""
    text = BRACKET.sub(" ", text)        # [품번] · (색상) · (기장선택)
    text = COLORCOUNT.sub(" ", text)     # 2color · 3 colors
    text = SKU.sub(" ", text)            # VJ5AL370 같은 품번
    text = COLOR_TOKEN.sub(" ", text)
    text = re.sub(r"[^0-9A-Za-z가-힣]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def value_of(item, attr):
    attrs = item.get("attributes")
    if not isinstance(attrs, dict):
        return None
    value = attrs.get(attr)
    return None if value in (None, "", "unknown") else value


def build_groups(items, attr):
    groups = {}
    for idx, item in enumerate(items):
        key = (item.get("brand") or "", base_name(item.get("name")))
        groups.setdefault(key, []).append(idx)
    out = []
    for (brand, base), idxs in groups.items():
        known = sorted({value_of(items[i], attr) for i in idxs} - {None})
        rep = items[idxs[0]]
        # 대표는 이미지가 있는 것을 고른다 — 이미지로 판단해야 하기 때문이다
        for i in idxs:
            if items[i].get("image_url"):
                rep = items[i]
                break
        out.append({
            "brand": brand, "base": base,
            "size": len(idxs),
            "known": known,
            "value": known[0] if len(known) == 1 else "",
            "conflict": len(known) > 1,
            "unknown_count": len([i for i in idxs if value_of(items[i], attr) is None]),
            "representative": {
                "product_id": rep.get("product_id"),
                "name": rep.get("name"),
                "image_url": rep.get("image_url"),
            },
            "members": [items[i].get("product_id") for i in idxs],
        })
    out.sort(key=lambda g: (-g["unknown_count"], -g["size"], g["base"]))
    return out


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main():
    parser = argparse.ArgumentParser(description="색상 변형을 묶어 속성 분류 횟수를 줄인다")
    parser.add_argument("path", help="수집 JSON")
    parser.add_argument("--attr", default="핏", help="묶어서 채울 속성 이름 (기본: 핏)")
    parser.add_argument("--plan", help="계획 JSON 저장 경로 (읽기만 한다)")
    parser.add_argument("--apply", dest="apply_path", help="계획 JSON을 읽어 수집 JSON에 반영")
    args = parser.parse_args()

    if not args.plan and not args.apply_path:
        print("입력 오류 — --plan 또는 --apply 중 하나를 줄 것")
        return 2
    try:
        data = load(args.path)
    except (OSError, json.JSONDecodeError) as exc:
        print("입력 오류 — %s" % exc)
        return 2
    items = data.get("items") or []
    if not items:
        print("입력 오류 — items가 비어 있다")
        return 2

    if args.plan:
        groups = build_groups(items, args.attr)
        need = [g for g in groups if g["unknown_count"] and not g["value"]]
        unknown_total = sum(g["unknown_count"] for g in groups)
        plan = {
            "attr": args.attr,
            "source": args.path,
            "summary": {
                "items": len(items),
                "groups": len(groups),
                "unknown_items": unknown_total,
                "groups_needing_decision": len(need),
                "propagatable_items": sum(
                    g["unknown_count"] for g in groups if g["value"]),
                "conflict_groups": len([g for g in groups if g["conflict"]]),
                "saved_ratio_pct": round((1 - len(need) / unknown_total) * 100, 1)
                if unknown_total else 0.0,
            },
            "groups": groups,
        }
        with open(args.plan, "w", encoding="utf-8") as handle:
            json.dump(plan, handle, ensure_ascii=False, indent=1)
        s = plan["summary"]
        print("상품 %d개 → 그룹 %d개" % (s["items"], s["groups"]))
        print("미분류 %d개 중 형제에서 전파 가능 %d개" % (s["unknown_items"], s["propagatable_items"]))
        print("**판단이 필요한 그룹 %d개** (미분류 %d개 → %d회, %.1f%% 절감)"
              % (s["groups_needing_decision"], s["unknown_items"],
                 s["groups_needing_decision"], s["saved_ratio_pct"]))
        if s["conflict_groups"]:
            print("주의 — 한 그룹에 서로 다른 값이 섞인 그룹 %d개. value를 직접 정해야 한다"
                  % s["conflict_groups"])
        print("계획 저장: %s" % args.plan)
        print("value가 빈 그룹만 대표 이미지를 보고 채운 뒤 --apply로 반영할 것")
        return 0

    try:
        plan = load(args.apply_path)
    except (OSError, json.JSONDecodeError) as exc:
        print("입력 오류 — 계획 JSON을 읽지 못했다: %s" % exc)
        return 2
    attr = plan.get("attr") or args.attr
    by_id = {}
    for item in items:
        by_id.setdefault(str(item.get("product_id")), []).append(item)

    filled = 0
    groups_used = 0
    skipped = 0
    for group in plan.get("groups") or []:
        value = (group.get("value") or "").strip()
        if not value:
            skipped += 1
            continue
        groups_used += 1
        for pid in group.get("members") or []:
            for item in by_id.get(str(pid)) or []:
                attrs = item.setdefault("attributes", {})
                if attrs.get(attr) in (None, "", "unknown"):
                    attrs[attr] = value
                    # 근거는 그룹 전파다 — 이미지를 직접 본 것과 구분한다
                    item["attributes_basis"] = (
                        "name" if group.get("known") else "group")
                    filled += 1
    total = len(items)
    known = len([i for i in items if value_of(i, attr)])
    data.setdefault("meta", {}).setdefault("notes", []).append(
        "색상 변형 그룹 전파(group_variants.py): 그룹 %d개에서 %d건을 채웠다. "
        "값이 빈 그룹 %d개는 그대로 뒀다. 최종 %s 분류율 %.1f%%(%d/%d)"
        % (groups_used, filled, skipped, attr, known / total * 100 if total else 0, known, total))
    with open(args.path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    print("그룹 %d개 → %d건 채움 (빈 그룹 %d개는 유지)" % (groups_used, filled, skipped))
    print("%s 분류율 %.1f%% (%d/%d)" % (attr, known / total * 100 if total else 0, known, total))
    print("저장: %s" % args.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
