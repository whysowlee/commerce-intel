#!/usr/bin/env python3
"""프록시 자동 생성 실행기 — 카드 묶음을 받아 **rule은 즉석 판정, vision은 배치를 뽑는다** (D43).

## 두 경로를 다 돌린다

규칙으로 되는 것만 하면 **이미지에서만 나오는 축이 통째로 빠진다** — 배경·로고 노출·
모델 수·색상 수·패턴·촬영 각도 등. 어느 축을 만들지는 **AI가 요청과 상품군을 보고
정한다**(D19·D23) — 이 파일에 목록을 박지 않는다. 배경은 SPEC-INTEL D43.

    method=rule    → 이 스크립트가 즉석 판정. 비용 0, 몇 초
    method=vision  → 이 스크립트가 **배치 파일을 뽑는다**. 캐시에 있는 건 빼고,
                     같은 이미지를 쓰는 상품은 한 번만 넣는다(색상 변형이 같은 썸네일을
                     공유한다). 그 배치를 proxy-extractor 서브 에이전트 여러 개에
                     나눠 던지고, 돌아온 판정을 proxy-load로 적재한다

## 하드코딩이 아니다

D19의 "프록시는 제네릭 — 하드코딩 없이 AI가 정의"는 그대로다. 이 파일에 **어떤 프록시가
있어야 하는지는 적혀 있지 않다.** 여기 있는 건 카드를 실행하는 엔진이고, 카드는 AI가
쓴다(기본 묶음 `references/proxy-cards-default.json`은 출발점이지 정본이 아니다).

## 카드 형식 — 선언적. `eval`을 쓰지 않는다

    # rule — 위에서부터 먼저 맞는 것이 값이다(순서가 우선순위)
    {"proxy_name": "material_word", "material": "name", "method": "rule",
     "value_space": ["데님", "가죽", "소재 미표기", "unknown"],
     "rules": [{"value": "데님", "any": ["데님", "denim"]},
               {"value": "가죽", "any": ["가죽", "leather"]},
               {"value": "소재 미표기", "any": ["."]}]}

    # vision — 판정은 서브 에이전트가 한다. 카드는 질문과 값 공간만 준다
    {"proxy_name": "thumb_bg", "material": "image", "method": "vision",
     "value_space": ["단색 배경", "실내 배경", "야외 배경", "unknown"]}

수치 프록시는 `"numeric": {"kind": "char_len"|"word_count"|"count", "pattern": ...}`.

**unknown은 적재하지 않는다** — 판정 실패를 저장하면 TTL 안에 재시도가 막힌다(D35 규칙).

## 쓰는 법

    # ① rule 즉석 판정 + vision 배치 뽑기
    python3 proxy_auto.py --db data/intel.db --context "ranking:스커트" \
        --cards references/proxy-cards-default.json \
        --out data/px-rule.json --batch-dir data/px-batches --batch-size 30

    # ② rule 결과 적재 (바로 된다)
    python3 intel_db.py proxy-load data/px-rule.json

    # ③ vision 배치를 proxy-extractor 서브 에이전트들에 나눠 던지고,
    #    돌아온 JSON을 같은 방식으로 적재
    python3 intel_db.py proxy-load data/px-vision-01.json
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime


def _material(row, which):
    """카드가 지목한 재료. `image`는 컬럼 이름이 `image_url`이라 따로 짚는다."""
    key = "image_url" if which == "image" else which
    return row[key] if key in row.keys() else None


# 카드의 정규식은 **AI가 즉석에서 쓴다** — 검수된 입력이 아니다. 중첩 수량자의
# 파국적 백트래킹을 파이썬 `re`로는 완전히 못 막으니(타임아웃 없음) 입력을 자른다.
# 폭발은 입력 길이에 지수적이고 상품명은 짧아서 잘라도 잃는 게 없다.
MATCH_MAX_CHARS = 400


def _matches(pats, text, mode):
    """`any`는 하나라도, `all`은 전부. 대소문자는 무시한다 — 상품명 표기가 제멋대로다."""
    text = text[:MATCH_MAX_CHARS]
    hits = [bool(re.search(p, text, re.I)) for p in pats]
    return any(hits) if mode == "any" else (all(hits) if hits else False)


def validate_cards(cards):
    """정규식이 컴파일되는 카드만 남긴다. 버린 카드는 사유와 함께 돌려준다.

    **판정을 시작하기 전에** 걸러야 한다 — 중간에 터지면 그때까지 판정한 것이 함께
    날아가고, 무엇이 문제였는지도 스택 트레이스로만 남는다.
    """
    ok, bad = [], []
    for c in cards:
        problem = None
        for rule in c.get("rules", []):
            for p in rule.get("any", []) + rule.get("all", []):
                try:
                    re.compile(p)
                except re.error as e:
                    problem = "정규식 오류 `%s` — %s" % (p, e)
                    break
            if problem:
                break
        num = c.get("numeric") or {}
        if not problem and num.get("kind") == "count" and not num.get("pattern"):
            # kind=count인데 pattern이 없으면 판정 시점에 죽는다 — 여기서 잡는다
            problem = "numeric.kind=count 인데 pattern이 없다"
        (bad.append((c.get("proxy_name", "?"), problem)) if problem else ok.append(c))
    return ok, bad


def judge_row(card, row):
    """rule 카드 하나 × 상품 하나 → (값, 근거) 또는 None(판정 불가).

    근거를 함께 돌려준다 — "왜 이 값이지"에 답할 수 있어야 하고, 그게 없으면
    규칙 판정과 AI 판정을 구분할 수 없다.
    """
    text = _material(row, card.get("material", "name"))
    if not text:
        return None
    text = str(text)
    num = card.get("numeric")
    if num:
        kind = num.get("kind")
        if kind == "char_len":
            return float(len(text)), "글자 수"
        if kind == "word_count":
            return float(len([w for w in re.split(r"\s+", text.strip()) if w])), "단어 수"
        if kind == "count":
            # pattern이 없는 카드는 validate_cards가 먼저 걸러내지만, judge_row를
            # 직접 부르는 쪽(테스트·다른 스크립트)도 죽지 않게 여기서도 막는다
            pat = num.get("pattern")
            if not pat:
                return None
            return float(len(re.findall(pat, text[:MATCH_MAX_CHARS], re.I))), "패턴 출현 수"
        return None
    for rule in card.get("rules", []):
        mode = "all" if "all" in rule else "any"
        if _matches(rule.get(mode, []), text, mode):
            return rule["value"], "규칙(%s): %s" % (card.get("material", "name"), mode)
    return None


def _load_rows(conn, contexts):
    where, params = "", []
    if contexts:
        where = ("WHERE EXISTS (SELECT 1 FROM observations o WHERE o.site=p.site "
                 "AND o.product_id=p.product_id AND o.context IN (%s))"
                 % ",".join("?" * len(contexts)))
        params = list(contexts)
    return conn.execute(
        "SELECT site, product_id, name, brand, category, url, image_url "
        "FROM products p %s" % where, params).fetchall()


def _cached(conn, proxy_name):
    """이미 판정된 (site, product_id, fingerprint). 비전 재판정을 막는 핵심이다."""
    try:
        return {(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT site, product_id, fingerprint FROM proxy_cache WHERE proxy_name=?",
            (proxy_name,))}
    except sqlite3.OperationalError:
        return set()


def _card_proxy(card):
    """카드 → proxy-load가 먹는 정의 dict. `label`(D56)과 **rule 본문**(D65-8)을
    함께 넘긴다 — 본문이 defs에 저장돼야 분석이 캐시 miss를 즉석(lazy) 판정한다."""
    proxy = {k: card[k] for k in
             ("proxy_name", "question", "material", "method", "label",
              "rules", "numeric")
             if k in card}
    proxy["value_space"] = "numeric" if card.get("numeric") else card.get("value_space")
    return proxy


def run_rules(rows, cards):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for card in cards:
        judged = []
        for r in rows:
            res = judge_row(card, r)
            if res is None:              # 판정 불가 — 저장하지 않는다(재시도 여지를 남긴다)
                continue
            value, basis = res
            judged.append({"site": r["site"], "product_id": r["product_id"],
                           "value": value, "basis": basis,
                           "fingerprint": str(_material(r, card.get("material", "name"))),
                           "judged_at": now})
        out.append({"proxy": _card_proxy(card), "judgments": judged,
                    "_coverage": len(judged) / len(rows) if rows else 0})
    return out


def plan_vision(conn, rows, cards, batch_size):
    """비전 카드 → 서브 에이전트가 먹을 배치들.

    **재료가 같은 카드는 한 배치에 묶는다.** 카드마다 배치를 따로 만들면 같은 이미지를
    축 수만큼 다시 보게 된다 — 스커트 실측에서 카드 9장 × 이미지 1,911장 = 배치 432개가
    나왔다(proxy-extraction.md가 경고한 낭비다). 이미지 하나에 질문 9개를 함께 던지면
    48개로 끝난다. 사람이 사진 한 장을 보고 여러 가지를 동시에 읽는 것과 같다.

    **캐시와 중복 이미지도 걷어낸다.** 색상 변형이 대표컷을 공유하므로 지문이 같으면
    판정도 같다 — 이미지 하나당 한 번만 묻고, 결과를 그 이미지를 쓰는 모든 상품에 편다.
    카드마다 캐시 상태가 다를 수 있어서 **한 카드라도 미판정이면 그 이미지를 넣되,
    어느 카드가 남았는지 적어 준다**(다 된 것을 다시 묻지 않게).
    """
    by_material = {}
    for c in cards:
        by_material.setdefault(c.get("material", "image"), []).append(c)

    plans = []
    for material, group in by_material.items():
        done = {c["proxy_name"]: _cached(conn, c["proxy_name"]) for c in group}
        by_fp, no_material = {}, 0
        for r in rows:
            fp = _material(r, material)
            if not fp:
                no_material += 1
                continue
            fp = str(fp)
            need = [c["proxy_name"] for c in group
                    if (r["site"], r["product_id"], fp) not in done[c["proxy_name"]]]
            if not need:
                continue
            slot = by_fp.setdefault(fp, {"targets": [], "need": set()})
            slot["targets"].append({"site": r["site"], "product_id": r["product_id"]})
            slot["need"].update(need)
        items = [{"fingerprint": fp, "targets": v["targets"], "ask": sorted(v["need"])}
                 for fp, v in by_fp.items()]
        batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
        cards_out = [{k: c[k] for k in
                      ("proxy_name", "question", "material", "method",
                       "value_space", "label")
                      if k in c} for c in group]
        plans.append({"material": material, "cards": cards_out, "batches": batches,
                      "unique_materials": len(items), "no_material": no_material,
                      "products": sum(len(i["targets"]) for i in items)})
    return plans


def main():
    ap = argparse.ArgumentParser(
        description="프록시 카드 실행 — rule은 즉석 판정, vision은 배치 파일로 뽑는다")
    ap.add_argument("--db", default="data/intel.db")
    ap.add_argument("--context", action="append", default=[])
    ap.add_argument("--cards", required=True, help="카드 JSON (배열 또는 {cards:[...]})")
    ap.add_argument("--out", required=True, help="rule 판정 결과 — proxy-load가 먹는다")
    ap.add_argument("--batch-dir", help="vision 배치를 쓸 디렉터리 (생략하면 vision 건너뜀)")
    ap.add_argument("--batch-size", type=int, default=30,
                    help="배치 하나에 담을 재료 수 (기본 30)")
    ap.add_argument("--min-coverage", type=float, default=0.10,
                    help="이 비율 미만으로만 판정된 rule 카드는 버린다 (기본 0.10, --eager 전용)")
    ap.add_argument("--eager", action="store_true",
                    help="rule 카드를 지금 전량 판정한다 (D65-8 이전의 기본 동작). "
                         "없으면 정의만 내보내고 판정은 분석 시점에 lazy로 된다")
    a = ap.parse_args()

    raw = json.load(open(a.cards, encoding="utf-8"))
    cards = raw["cards"] if isinstance(raw, dict) else raw
    cards, bad_cards = validate_cards(cards)
    for name, why in bad_cards:
        print("  카드 버림 %-16s %s" % (name, why))
    from schema_v3 import open_db, proxy_connect, proxy_db_path
    conn = open_db(a.db)             # 로컬 경로·libsql:// URL 둘 다 (D67)
    # 캐시는 별도 proxy.db에 있다 (D65-8) — 비전 배치 계획이 이걸 본다
    pconn = proxy_connect(proxy_db_path(a.db))
    rows = _load_rows(conn, a.context)
    print("상품 %s건 · 카드 %d장" % ("{:,}".format(len(rows)), len(cards)))

    # ── rule ────────────────────────────────────────────────────────────
    # 기본은 **lazy**다 (D65-8): 정의(카드 본문 포함)만 내보내고, 판정은 분석
    # (intel_data.collect)이 캐시 miss를 만난 시점에 즉석으로 한다. --eager는
    # 지금 전량 판정하는 옛 동작 — 커버리지·값 다양성 필터는 판정이 있어야
    # 성립하므로 eager 쪽에만 있다.
    rule_cards = [c for c in cards if c.get("method", "rule") == "rule"]
    kept, dropped = [], []
    if a.eager:
        for r in run_rules(rows, rule_cards):
            name = r["proxy"]["proxy_name"]
            # 커버리지가 바닥이면 축으로 못 쓴다 — 그룹 비교는 값마다 n이 필요하다.
            # **버렸다는 사실을 찍는다.** 조용히 빠지면 "신호가 없었다"로 읽힌다.
            if r["_coverage"] < a.min_coverage:
                dropped.append((name, "커버리지 %.0f%%" % (100 * r["_coverage"])))
                continue
            if r["proxy"].get("value_space") != "numeric" \
                    and len({j["value"] for j in r["judgments"]}) < 2:
                dropped.append((name, "값이 1종뿐 — 비교 상대가 없다"))
                continue
            kept.append({"proxy": r["proxy"], "judgments": r["judgments"]})
    else:
        kept = [{"proxy": _card_proxy(c), "judgments": []} for c in rule_cards]

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    json.dump(kept, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if a.eager:
        print("\n[rule] 즉석 판정 (--eager)")
        for r in kept:
            vals = {}
            for j in r["judgments"]:
                vals[j["value"]] = vals.get(j["value"], 0) + 1
            top = ", ".join("%s %s" % (k, v) for k, v in
                            sorted(vals.items(), key=lambda kv: -kv[1])[:4])
            print("  채택 %-18s %5d건 (%3.0f%%) — %s"
                  % (r["proxy"]["proxy_name"], len(r["judgments"]),
                     100 * len(r["judgments"]) / (len(rows) or 1), top))
        for name, why in dropped:
            print("  버림 %-18s %s" % (name, why))
    else:
        print("\n[rule] 정의 %d장 내보냄 — 판정은 분석 시점에 lazy (지금 하려면 --eager)"
              % len(kept))
    print("  → intel_db.py proxy-load %s" % a.out)

    # ── vision ──────────────────────────────────────────────────────────
    vision_cards = [c for c in cards if c.get("method") in ("vision", "llm")]
    if vision_cards and not a.batch_dir:
        # lazy 기본 흐름(D66)에서 정의 등록 단계는 배치를 안 뽑는 게 정상이다.
        # 판정이 필요해지면(분석의 unjudged) --batch-dir을 주고 다시 부른다.
        print("\n[vision] 카드 %d장 — 정의만 등록 대상. 판정 배치는 분석이 요구할 때 "
              "--batch-dir로 뽑는다(D66). **그때까지 이 축은 리포트에 미판정 결측으로 "
              "보인다**" % len(vision_cards))
    elif vision_cards:
        os.makedirs(a.batch_dir, exist_ok=True)
        print("\n[vision] 서브 에이전트에 던질 배치 — 재료 하나에 질문 여러 개")
        for plan in plan_vision(pconn, rows, vision_cards, a.batch_size):
            mat = plan["material"]
            for i, batch in enumerate(plan["batches"], 1):
                path = os.path.join(a.batch_dir, "%s-%03d.json" % (mat, i))
                json.dump({"cards": plan["cards"], "items": batch},
                          open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            print("  재료 %-6s 카드 %d장 × 고유 재료 %s종(중복 접음) → 배치 %d개"
                  % (mat, len(plan["cards"]),
                     "{:,}".format(plan["unique_materials"]), len(plan["batches"])))
            print("       카드: %s" % ", ".join(c["proxy_name"] for c in plan["cards"]))
            # 재료가 없어 아예 못 묻는 상품 — 세어만 두고 안 찍으면 "전부 판정했다"로
            # 읽힌다(2026-08-04 리뷰). 이 PR이 rule 쪽에서 지키는 원칙과 같아야 한다
            if plan["no_material"]:
                print("       ※ %s건은 %s가 없어 판정 대상에서 빠졌다 — 결측이지 0이 아니다"
                      % ("{:,}".format(plan["no_material"]), mat))
        print("  → 배치를 proxy-extractor 에이전트들에 나눠 주고, 결과를 proxy-load")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
