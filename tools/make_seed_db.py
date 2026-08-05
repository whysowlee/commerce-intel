#!/usr/bin/env python3
"""정본 DB에서 **배포 허용 범위만** 떼어 seed DB를 만든다 (D58).

    python3 tools/make_seed_db.py                      # 기본 경로로 생성 + 검산
    python3 tools/make_seed_db.py --check              # 만들지 않고 현재 seed만 검산
    python3 tools/make_seed_db.py --out /tmp/seed.db

배포 범위 — 2026-08-04 사용자 지시:

    ① 자사 브랜드 **2000아카이브스** 전 제품 (무신사·29CM·자사몰)
    ② **여성 데님팬츠 브랜드랭킹 상위30 전수조사** (이미 수집한 그 런)
    그 밖의 정본은 배포하지 않는다.

**허용 목록 방식이다.** 범위를 넓히는 건 한 줄 고치면 되지만, 빼는 걸 잊으면
남의 데이터가 나간다 — 그래서 "이것만 넣는다"로 짰고, 만든 뒤 **범위 밖 행이
한 줄이라도 있으면 실패**시킨다(`verify`). package.sh가 압축 전에 이걸 돌린다.

팀원 쪽 사용법은 그대로다: `intel_db.py merge <seed.db>` — 관측은 append only,
정적 필드는 빈 값으로 덮지 않는다(cmd_merge 규약).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "commerce-intel" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from intel_data import brand_key            # noqa: E402  (표기 정규화 — D51)
from schema_v3 import assign_category  # noqa: E402  (v3 — D65)

SRC_DEFAULT = ROOT / "data" / "intel.db"
OUT_DEFAULT = ROOT / "skills" / "commerce-intel" / "assets" / "seed-intel.db"

# ── 배포 범위 정의 ─────────────────────────────────────────────────────────
# 브랜드는 표기 변형이 있다(`2000아카이브스` · `2000Archives` · `2000 Archives`).
# brand_key로 묶는다 — 한글과 영문은 서로 다른 키라 둘 다 적는다(음차 매칭 안 한다).
SEED_BRAND_KEYS = {"2000아카이브스", "2000archives"}
# 런은 target으로 고른다. 여성 데님팬츠 **브랜드랭킹 상위30** 전수조사만이다 —
# 같은 날 돌린 여성 데님팬츠 전수조사 전체(11,989건)는 범위 밖이다.
SEED_RUN_TARGET_LIKE = "%브랜드랭킹 상위30%"
# 인사이트는 근거 데이터가 아니라 주장이라 product 키가 없다 — context로 고른다.
SEED_INSIGHT_CONTEXTS_LIKE = ("brand:2000아카이브스%", "market:데님팬츠(여성%")

# 상품 키에 매달린 것들 — 범위 상품에 걸린 행만 간다.
# proxy_cache는 v3부터 별도 proxy.db에 살지만(D65-8) seed는 **한 파일**로 나간다 —
# copy_proxy()가 proxy.db에서 떼어 seed 안에 담고, 팀원 쪽 merge가 도로
# 자기 proxy.db로 돌려 넣는다.
PRODUCT_SCOPED = ("observations", "variants", "variant_observations",
                  "product_attributes", "proxy_cache")
# 전량 간다 — 데이터가 아니라 해석에 필요한 계약·정의다
WHOLE = ("platforms", "proxy_defs")
# 아예 안 간다 — sync_state는 우리 시트 미러 위치다(팀원 DB와 무관하고 새면 안 된다)
NEVER = ("sync_state",)


def logical_tables(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
        "AND name NOT LIKE 'sqlite_%'")}


def clone_schema(src: sqlite3.Connection, dst_path: Path) -> None:
    """정본과 **같은 스키마**로 빈 DB를 짓는다.

    intel_db.connect()로 만들지 않는 이유: 그건 빈 파일에 항상 v2를 짓는다.
    정본이 아직 v1이면 모양이 갈리고, merge 상대가 어느 쪽인지에 따라 조용히
    어긋난다. 정본의 DDL을 그대로 복제하는 게 정의상 안전하다.
    """
    if dst_path.exists():
        dst_path.unlink()
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    ddl = []
    for kind in ("table", "view", "trigger", "index"):
        for (sql,) in src.execute(
                "SELECT sql FROM sqlite_master WHERE type=? AND sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%'", (kind,)):
            ddl.append(sql)
    out = sqlite3.connect(dst_path)
    for stmt in ddl:
        out.execute(stmt)
    out.commit()
    out.close()


def build_scope(src: sqlite3.Connection) -> tuple[int, int]:
    """범위 상품을 temp.scope에 담는다. (브랜드분, 런분) 건수를 돌려준다."""
    src.execute("CREATE TEMP TABLE scope (site TEXT, product_id TEXT, "
                "PRIMARY KEY (site, product_id))")
    brands = [r for r in src.execute("SELECT DISTINCT brand FROM products WHERE brand IS NOT NULL")]
    hit = [r[0] for r in brands if brand_key(r[0]) in SEED_BRAND_KEYS]
    n_brand = 0
    if hit:
        qs = ",".join("?" * len(hit))
        n_brand = src.execute(
            f"INSERT OR IGNORE INTO scope SELECT site, product_id FROM products "
            f"WHERE brand IN ({qs})", hit).rowcount
    n_run = src.execute(
        "INSERT OR IGNORE INTO scope SELECT DISTINCT o.site, o.product_id "
        "FROM observations o WHERE o.run_id IN "
        "(SELECT run_id FROM runs WHERE target LIKE ?)",
        (SEED_RUN_TARGET_LIKE,)).rowcount
    return n_brand, n_run


def copy_rows(src: sqlite3.Connection, dst_path: Path) -> dict[str, int]:
    """범위 행만 옮긴다. 삽입은 **논리 이름**으로 한다 — v2면 뷰·트리거가 받는다."""
    counts: dict[str, int] = {}
    src.execute("ATTACH DATABASE ? AS dst", (str(dst_path),))
    tables = logical_tables(src)

    def cols(table):
        return [r[1] for r in src.execute(f"PRAGMA table_info({table})")]

    def insert(table, where, params=()):
        c = ",".join(cols(table))
        n = src.execute(
            f"INSERT INTO dst.{table} ({c}) SELECT {c} FROM main.{table} {where}",
            params).rowcount
        counts[table] = n

    # **런이 관측보다 먼저다** (D59와 같은 함정). v3 관측 뷰 트리거는 NEW.run_id를
    # `SELECT id FROM runs`로 푸는데, 런이 아직 없으면 참조가 NULL로 풀리고 예외도
    # 없다. 그래서 런 선별을 dst가 아니라 **main의 범위 관측**으로 미리 계산한다.
    insert("runs",
           "WHERE target LIKE ? OR run_id IN ("
           "  SELECT o.run_id FROM main.observations o"
           "  JOIN scope sc ON sc.site=o.site AND sc.product_id=o.product_id"
           "  UNION SELECT vo.run_id FROM main.variant_observations vo"
           "  JOIN scope sc ON sc.site=vo.site AND sc.product_id=vo.product_id)",
           (SEED_RUN_TARGET_LIKE,))
    insert("products", "WHERE (site, product_id) IN (SELECT site, product_id FROM scope)")
    for t in PRODUCT_SCOPED:
        if t in tables:
            insert(t, "WHERE (site, product_id) IN (SELECT site, product_id FROM scope)")
    for t in WHOLE:
        if t in tables:
            insert(t, "")
    if "insights" in tables:
        conds = " OR ".join("context LIKE ?" for _ in SEED_INSIGHT_CONTEXTS_LIKE)
        insert("insights", f"WHERE {conds}", SEED_INSIGHT_CONTEXTS_LIKE)
    for t in NEVER:
        counts[t] = 0
    src.commit()
    src.execute("DETACH DATABASE dst")
    return counts


def copy_categories(src: sqlite3.Connection, dst_path: Path) -> int:
    """seed 상품에 카테고리를 다시 단다 (v3 — D65-3).

    상품은 뷰 트리거로 들어갔는데 트리거는 NEW.category를 못 받는다(경로 분해 —
    schema_v3 docstring). 소스 뷰가 도로 편 경로 문자열을 assign_category()로
    계층에 넣는다 — 이걸 빼먹으면 seed의 카테고리만 조용히 빈다.
    """
    dst = sqlite3.connect(dst_path)
    cache, n = {}, 0
    for site, pid in [tuple(r) for r in dst.execute("SELECT site, product_id FROM products")]:
        row = src.execute("SELECT category FROM products WHERE site=? AND product_id=?",
                          (site, pid)).fetchone()
        if row and row[0]:
            assign_category(dst, site, pid, row[0], cache)
            n += 1
    dst.commit()
    dst.close()
    return n



# D69: copy_proxy 삭제 — 프록시가 본 DB에 통합됨 (copy_rows가 담당)


def verify(dst_path: Path, src_path: Path) -> list[str]:
    """**범위 밖 행이 한 줄이라도 있으면 잡는다.** 만든 직후와 --check가 같은 걸 본다."""
    errs: list[str] = []
    out = sqlite3.connect(dst_path)
    out.execute("ATTACH DATABASE ? AS ref", (str(src_path),))
    tables = logical_tables(out)

    brands = [r[0] for r in out.execute(
        "SELECT DISTINCT brand FROM products WHERE brand IS NOT NULL")]
    stray_brand = [b for b in brands if brand_key(b) not in SEED_BRAND_KEYS]
    # 브랜드 범위 밖 상품은 **런 범위로 들어온 것만** 정당하다
    for b in stray_brand:
        n = out.execute(
            "SELECT COUNT(*) FROM products p WHERE p.brand=? AND NOT EXISTS ("
            "  SELECT 1 FROM ref.observations o JOIN ref.runs r ON r.run_id=o.run_id"
            "  WHERE o.site=p.site AND o.product_id=p.product_id AND r.target LIKE ?)",
            (b, SEED_RUN_TARGET_LIKE)).fetchone()[0]
        if n:
            errs.append(f"범위 밖 상품 {n}건 — 브랜드 `{b}`가 허용 브랜드도, 허용 런도 아니다")

    for t in PRODUCT_SCOPED:
        if t not in tables:
            continue
        n = out.execute(
            f"SELECT COUNT(*) FROM {t} x WHERE NOT EXISTS ("
            f"  SELECT 1 FROM products p WHERE p.site=x.site AND p.product_id=x.product_id)"
        ).fetchone()[0]
        if n:
            errs.append(f"{t}에 상품 없는 고아 행 {n}건 — 범위 판정이 새고 있다")

    if "insights" in tables:
        conds = " AND ".join("context NOT LIKE ?" for _ in SEED_INSIGHT_CONTEXTS_LIKE)
        n = out.execute(f"SELECT COUNT(*) FROM insights WHERE {conds}",
                        SEED_INSIGHT_CONTEXTS_LIKE).fetchone()[0]
        if n:
            errs.append(f"범위 밖 인사이트 {n}건 — 다른 카테고리 주장이 섞였다")

    for t in NEVER:
        if t in tables and out.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]:
            errs.append(f"{t}는 배포 대상이 아닌데 행이 있다")

    out.close()
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(SRC_DEFAULT))
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--check", action="store_true", help="만들지 않고 기존 seed만 검산")
    args = ap.parse_args()
    # 상대 경로로 불릴 수 있다(package.sh가 그렇게 부른다) — 절대로 맞춰 놓는다
    src_path, out_path = Path(args.src).resolve(), Path(args.out).resolve()
    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:
        shown = out_path

    if not src_path.exists():
        print(f"정본 DB가 없다: {src_path}", file=sys.stderr)
        return 1

    if not args.check:
        src = sqlite3.connect(src_path)
        clone_schema(src, out_path)
        n_brand, n_run = build_scope(src)
        counts = copy_rows(src, out_path)
        counts["product_categories"] = copy_categories(src, out_path)   # v3 (D65-3)
    # D69: copy_proxy 삭제 — 프록시가 본 DB copy_rows에 포함
        src.close()
        print(f"  범위 상품 — 브랜드(2000아카이브스) {n_brand} · 런(브랜드랭킹 상위30) {n_run}")
        for t, n in sorted(counts.items()):
            print(f"      {t:22} {n:>7}")
        size = out_path.stat().st_size
        print(f"  seed DB {shown} — {size / 1e6:.1f}MB")

    if not out_path.exists():
        print(f"seed DB가 없다: {out_path}", file=sys.stderr)
        return 1
    errs = verify(out_path, src_path)
    if errs:
        print("\n검산 실패 — 배포 범위 밖 데이터가 들어 있다:", file=sys.stderr)
        for e in errs:
            print(f"      {e}", file=sys.stderr)
        return 1
    print("  검산 통과 — 범위 밖 행 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
