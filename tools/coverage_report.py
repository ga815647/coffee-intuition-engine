"""Tier-1 常見豆「分層詳盡覆蓋」報告(§4.1)。

量化 corpus 對 Tier-1 常見產地 × 處理法(× 機制)格的覆蓋:每格依**來源分級**(A/B/C)
計數、標出空格(gap),並回報**單元錨點覆蓋率**(該 origin×process×mechanism 是否有
`variety=""` 的單元錨點——沒有 → 該格的任何同豆查詢都會硬湊物理先驗,§16.4)。

Tier-1 的定義(產地集 / 各產地處理法 / 機制 / pressure 例外)**直接取自 `tools.seed_tier1`**,
單一真相、不漂移:覆蓋報告衡量的正是 seed_tier1 要鋪平的同一張網。

用法:
    python -m tools.coverage_report                 # 人類可讀網格 + 摘要
    python -m tools.coverage_report --json          # 機器可讀(before/after diff 用)
    python -m tools.coverage_report --corpus <path> # 指定語料(預設 corpus/global.jsonl)

報告分層(**誠實分級:seeded ≠ covered**):
  1. origin × process 網格:每格 A/B/C 計數,並標兩層狀態 —
     · **seeded** = ≥1 筆同豆(任一級);0 筆 = EMPTY 空格(★ 標 Kenya×natural 等優先缺口)。
     · **covered** = 共識品質門檻(≥1 A/B 同豆 **或** ≥3 C 同豆)。單一 derived 泛用 C 單元錨點
       只算 seeded、不算 covered(`~weak`)——一筆社群原型是 seed,不是 consensus 覆蓋(§4.1)。
  2. 單元錨點覆蓋:Tier-1 的 (origin,process,mechanism) 有 variety="" 錨點的比例(硬湊率結構上界)。
  3. 摘要:seeded% 與 covered% **各自**百分比 + 缺格清單(空格 / seeded·未達共識)。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

from tools.seed_tier1 import (
    CORPUS,
    DEFAULT_MECHS,
    ORIGIN_PROCS,
    PRESSURE_CELLS,
    _load_corpus,
    anchored_cells,
    origin_token,
)

GRADES = ("A", "B", "C")
PRIORITY_GAPS = {("kenya", "natural")}  # ★ 任務指定最高優先缺口


def _is_covered(counts: Dict[str, int]) -> bool:
    """**共識品質**門檻(§4.1):≥1 A 或 ≥1 B 同豆,**或** ≥3 C 同豆。

    誠實分級鐵則:**單一 derived 泛用 C 單元錨點(variety="")是 seed、不是 consensus 覆蓋**。
    一筆社群原型只把該格從「硬湊物理」抬到「有同豆鄰居」(seeded),離「共識品質覆蓋」(covered)
    仍差——需有閉環/文獻級 A/B,或夠多(≥3)獨立 C 才算共識。故 seeded 永不等同 covered。
    """
    return counts.get("A", 0) >= 1 or counts.get("B", 0) >= 1 or counts.get("C", 0) >= 3


def _tier1_cells() -> List[Tuple[str, str, List[str]]]:
    """Tier-1 全格:(origin_display, process, [mechanisms])。"""
    cells: List[Tuple[str, str, List[str]]] = []
    for origin, procs in ORIGIN_PROCS.items():
        ot = origin_token(origin)
        for process in procs:
            mechs = list(DEFAULT_MECHS)
            if (ot, process) in PRESSURE_CELLS:
                mechs.append("pressure")
            cells.append((origin, process, mechs))
    return cells


def _grade_grid(corpus_rows: List[dict]) -> Dict[Tuple[str, str], Dict[str, int]]:
    """(origin_token, process) → {grade: count}(只計 Tier-1 產地的記錄)。"""
    grid: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in corpus_rows:
        b = r.get("bean", {})
        ot = origin_token(b.get("origin", ""))
        if not ot:
            continue
        process = (b.get("process") or "")
        grade = (r.get("grade") or "?")
        grid[(ot, process)][grade] += 1
    return grid


def build_report(corpus_rows: List[dict]) -> dict:
    grid = _grade_grid(corpus_rows)
    anchored = anchored_cells(corpus_rows)

    cells = _tier1_cells()
    grid_rows: List[dict] = []
    op_total = op_seeded = op_covered = 0   # origin×process 層(兩層誠實分級)
    mech_total = mech_anchored = 0          # origin×process×mechanism 層
    empty_cells: List[str] = []             # 0 筆:連 seed 都沒有
    weak_cells: List[str] = []              # ≥1 筆但未達共識門檻(seeded 但非 covered)
    priority_status: List[dict] = []

    for origin, process, mechs in cells:
        ot = origin_token(origin)
        counts = {g: grid.get((ot, process), {}).get(g, 0) for g in GRADES}
        total = sum(grid.get((ot, process), {}).values())
        seeded = total > 0                  # ≥1 筆同豆(任一級)
        covered = _is_covered(counts)       # ≥1 A/B 或 ≥3 C(共識品質)
        op_total += 1
        if seeded:
            op_seeded += 1
        if covered:
            op_covered += 1
        if not seeded:
            empty_cells.append(f"{origin}×{process}")
        elif not covered:
            weak_cells.append(f"{origin}×{process}")

        anchor_have = sum(1 for m in mechs if (ot, process, m) in anchored)
        mech_total += len(mechs)
        mech_anchored += anchor_have

        is_priority = (ot, process) in PRIORITY_GAPS
        row = {
            "origin": origin, "process": process, "origin_token": ot,
            "counts": counts, "total": total,
            "seeded": seeded, "covered": covered,
            "mechanisms": mechs, "anchored_mechs": anchor_have,
            "missing_anchor_mechs": [m for m in mechs if (ot, process, m) not in anchored],
            "priority": is_priority,
        }
        grid_rows.append(row)
        if is_priority:
            priority_status.append({
                "cell": f"{origin}×{process}", "total": total, "counts": counts,
                "seeded": seeded, "covered": covered,
                "anchored_mechs": anchor_have, "of_mechs": len(mechs),
            })

    covered_gaps = empty_cells + weak_cells  # 未達共識門檻(空格 + seeded 弱格)
    return {
        "n_records": len(corpus_rows),
        "origin_process": {
            "total": op_total,
            "seeded": op_seeded, "covered": op_covered,
            "seeded_gaps": op_total - op_seeded,    # 空格數
            "covered_gaps": op_total - op_covered,  # 未達共識數(⊇ 空格)
        },
        "mechanism_anchor": {"total": mech_total, "anchored": mech_anchored,
                             "missing": mech_total - mech_anchored},
        "gaps": empty_cells,            # 0 筆(back-compat:= seeded 空格)
        "weak_cells": weak_cells,       # seeded 但未達共識(單一 C 錨點等)
        "covered_gaps": covered_gaps,   # 未達 covered 門檻(empty + weak)
        "priority_gaps": priority_status,
        "grid": grid_rows,
    }


def format_report(rep: dict) -> str:
    lines: List[str] = []
    lines.append(f"=== Tier-1 覆蓋報告(語料 {rep['n_records']} 筆)===")
    op = rep["origin_process"]
    ma = rep["mechanism_anchor"]
    seeded_pct = 100.0 * op["seeded"] / op["total"] if op["total"] else 0.0
    covered_pct = 100.0 * op["covered"] / op["total"] if op["total"] else 0.0
    ma_pct = 100.0 * ma["anchored"] / ma["total"] if ma["total"] else 0.0
    # 兩層誠實分級:seeded(≥1 筆同豆)≠ covered(共識品質)。別把 seeded 叫 covered。
    lines.append(
        f"origin×process — seeded(≥1 筆同豆,任一級):"
        f"{op['seeded']}/{op['total']}({seeded_pct:.0f}%),空格 {op['seeded_gaps']}"
    )
    lines.append(
        f"origin×process — covered(共識品質:≥1 A/B 同豆 或 ≥3 C 同豆):"
        f"{op['covered']}/{op['total']}({covered_pct:.0f}%),未達共識 {op['covered_gaps']}"
    )
    lines.append("  ↳ 註:單一 derived 泛用 C 單元錨點只算 seeded,不算 covered(誠實分級,§4.1)。")
    lines.append(
        f"單元錨點(origin×process×機制 有 variety=\"\" 錨點):"
        f"{ma['anchored']}/{ma['total']}({ma_pct:.0f}%),缺錨點 {ma['missing']} 格"
        f"  ← 硬湊率結構上界 {100.0 - ma_pct:.0f}%"
    )
    lines.append("")
    lines.append(f"{'產地':12} {'處理法':10} {'A':>3} {'B':>3} {'C':>3} {'總':>4}  錨點  缺錨機制  狀態")
    lines.append("-" * 84)
    for row in rep["grid"]:
        c = row["counts"]
        flag = ""
        if not row["seeded"]:
            flag = " ★EMPTY" if row["priority"] else " ·EMPTY"   # 0 筆
        elif not row["covered"]:
            flag = " ~weak"                                       # seeded 但未達共識
        miss = ",".join(m[:4] for m in row["missing_anchor_mechs"]) or "—"
        lines.append(
            f"{row['origin']:12} {row['process']:10} "
            f"{c['A']:>3} {c['B']:>3} {c['C']:>3} {row['total']:>4}  "
            f"{row['anchored_mechs']}/{len(row['mechanisms'])}  {miss:14}{flag}"
        )
    lines.append("")
    if rep["priority_gaps"]:
        lines.append("★ 優先缺口狀態:")
        for p in rep["priority_gaps"]:
            if not p["seeded"]:
                tag = "EMPTY(未補)"
            elif not p["covered"]:
                tag = "seeded·未達共識覆蓋"      # 有同豆錨點但仍非共識品質
            else:
                tag = "covered(共識品質)"
            lines.append(
                f"  {p['cell']}: 總 {p['total']} 筆 {p['counts']}, "
                f"錨點 {p['anchored_mechs']}/{p['of_mechs']} 機制 [{tag}]"
            )
    if rep["gaps"]:
        lines.append("")
        lines.append("空格(origin×process 0 筆):" + ", ".join(rep["gaps"]))
    if rep["weak_cells"]:
        lines.append("")
        lines.append(
            f"seeded·未達共識({len(rep['weak_cells'])} 格,有同豆錨點但 <共識門檻):"
            + ", ".join(rep["weak_cells"])
        )
    return "\n".join(lines)


def main() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Tier-1 覆蓋報告(§4.1)")
    ap.add_argument("--corpus", type=Path, default=CORPUS, help="語料檔(預設 corpus/global.jsonl)")
    ap.add_argument("--json", action="store_true", help="輸出機器可讀 JSON(before/after diff)")
    args = ap.parse_args()

    rep = build_report(_load_corpus(args.corpus))
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(format_report(rep))


if __name__ == "__main__":
    main()
