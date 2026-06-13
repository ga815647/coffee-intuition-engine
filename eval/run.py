"""盲測評測:對『庫裡沒有的豆』先預測、再比對人工真值。

    python -m eval.run

量化三件事(對應鐵則「方向 > 絕對值」與 §12.3 區間):
  (a) L3 各軸 MAE / RMSE;
  (b) conformal 區間**覆蓋率**(真值落在預測 [下界,上界] 的比例 vs 名目);
  (c) **方向 / 排序**:同機制配對裡,預測的高低排序是否與真值一致(pairwise accuracy)。

防洩漏鐵則(§15 / design §12.6,務必):
  1. 留出豆**絕不進召回庫**:此處用獨立記憶體 store,只灌 seeds;執行期驗證 id 與庫互斥,
     且任何一筆證據都不得是留出豆。
  2. 嚴禁「事後感官子項」回推總分(R²≈0.82 陷阱):predict() 只吃 bean + params,
     結構上完全不碰任何真值風味軸——這是設計層保證,非靠自律。
  3. 評測產生的預測**一律不寫回**當校準(不呼叫 log_calibration;store 筆數前後不變,
     報告中明列三項檢查)。

注意:離線雜湊嵌入本就不準,本 harness **不對 MAE 下硬門檻**;它證明的是
『評測協定可跑、留出豆確被排除、覆蓋率與方向指標算得出』。真實準度數字待接
workers_ai 嵌入 + 真實資料後再看(屆時同一 harness 直接複用)。
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

from cie.config import CONFIG, Config
from cie.engine import Engine
from cie.portability import read_jsonl
from cie.schema import FLAVOR_AXES
from cie.seed import seed
from cie.store import StoreBackend, VectorStore

DATASET_PATH = Path(__file__).resolve().parent / "dataset.jsonl"
REPORT_PATH = Path(__file__).resolve().parent / "report.json"
NOMINAL_COVERAGE = 0.90  # weighted_estimate 用 ~90% 名目區間


# ────────────────────────────── 小工具 ──────────────────────────────

def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _rmse(errs: List[float]) -> float:
    return math.sqrt(_mean([e * e for e in errs])) if errs else 0.0


# ────────────────────────────── 召回庫(防洩漏) ──────────────────────────────

def build_library_store(holdout_ids: set, config: Config = CONFIG) -> VectorStore:
    """建一個**獨立記憶體** store,只灌 A 級 seeds;確保留出豆不在其中。

    沿用設定的嵌入器(local / workers_ai 等),但強制記憶體模式以隔離正式索引,
    且絕不寫入留出豆。執行期驗證 id 互斥(雙重保險,不只靠『沒插入』)。
    """
    iso_cfg = replace(config, qdrant_url="", qdrant_api_key="", store_backend_override="memory")
    store = VectorStore(iso_cfg)
    seed(store)  # 只灌 seeds;canonical 不掛(零副作用)
    leaked = holdout_ids & {r.id for r in store.iter_records()}
    if leaked:  # pragma: no cover - 防禦:seeds 與 holdout id 命名互斥,不應發生
        raise RuntimeError(f"洩漏:留出豆出現在召回庫 → {leaked}")
    return store


# ────────────────────────────── 評測主體 ──────────────────────────────

def run_eval(dataset_path: Path = DATASET_PATH, store: Optional[StoreBackend] = None,
             nominal_coverage: float = NOMINAL_COVERAGE, config: Config = CONFIG) -> Dict:
    """跑盲測,回傳結構化報告 dict(不寫檔;寫檔由 main() 負責)。"""
    holdouts = read_jsonl(dataset_path)
    holdout_ids = {r.id for r in holdouts}

    if store is None:
        store = build_library_store(holdout_ids, config)

    # 防洩漏 1:留出豆不得在召回庫
    store_ids = {r.id for r in store.iter_records()} if hasattr(store, "iter_records") else set()
    holdout_ids_excluded = holdout_ids.isdisjoint(store_ids)

    engine = Engine(store=store, canonical=None)  # 不掛 canonical sink:評測零副作用
    count_before = store.count()

    axis_abs: Dict[str, List[float]] = {a: [] for a in FLAVOR_AXES}
    axis_cov: Dict[str, List[bool]] = {a: [] for a in FLAVOR_AXES}
    axis_points: Dict[str, List[tuple]] = {a: [] for a in FLAVOR_AXES}  # (mechanism, true, pred)
    no_holdout_in_evidence = True
    per_record: List[Dict] = []

    for h in holdouts:
        pred = engine.predict(h.bean, h.params)  # 只吃 bean+params:結構上無風味洩漏
        # 防洩漏 2(縱深防禦,downstream 於 holdout_ids_excluded):證據取自召回鄰居,
        # 而鄰居只能來自 store;故 store 已排除留出豆時此檢查恆 True。它捕捉的是
        # 「庫被污染」的殘餘情境(見 test_eval 主動洩漏測試證明可翻 False)。
        ev_ids = {e.get("id") for e in pred.get("evidence", [])}
        if ev_ids & holdout_ids:
            no_holdout_in_evidence = False
        pf = pred["predicted_flavor"]
        rec_out = {"id": h.id, "mechanism": h.params.brew_mechanism.value, "axes": {}}
        for a in FLAVOR_AXES:
            true_v = getattr(h.flavor, a)
            est = pf.get(a)
            pv = est["value"] if est else None
            lo = est["lower"] if est else None
            hi = est["upper"] if est else None
            entry: Dict = {"true": true_v, "pred": pv, "lower": lo, "upper": hi}
            if true_v is not None and pv is not None:
                err = abs(pv - true_v)
                axis_abs[a].append(err)
                axis_points[a].append((h.params.brew_mechanism.value, true_v, pv))
                entry["abs_err"] = round(err, 3)
                if lo is not None and hi is not None:
                    covered = lo <= true_v <= hi
                    axis_cov[a].append(covered)
                    entry["covered"] = covered
            rec_out["axes"][a] = entry
        per_record.append(rec_out)

    # 彙總 (a) MAE/RMSE (b) 覆蓋率
    axes_report: Dict[str, Dict] = {}
    all_errs: List[float] = []
    all_cov: List[bool] = []
    for a in FLAVOR_AXES:
        errs, covs = axis_abs[a], axis_cov[a]
        all_errs += errs
        all_cov += covs
        axes_report[a] = {
            "n": len(errs),
            "mae": round(_mean(errs), 4) if errs else None,
            "rmse": round(_rmse(errs), 4) if errs else None,
            "n_with_interval": len(covs),
            "coverage": round(_mean([1.0 if c else 0.0 for c in covs]), 4) if covs else None,
        }
    overall = {
        "n": len(all_errs),
        "mae": round(_mean(all_errs), 4) if all_errs else None,
        "rmse": round(_rmse(all_errs), 4) if all_errs else None,
        "coverage": round(_mean([1.0 if c else 0.0 for c in all_cov]), 4) if all_cov else None,
    }

    # (c) 方向:**同機制**配對排序準確率(跨機制不可比,鐵則 §12.1)
    direction = _direction_metric(axis_points)

    return {
        "n_holdout": len(holdouts),
        "embedder": store.model_id,
        "store_backend": type(store).__name__,
        "library_count": count_before,
        "nominal_coverage": nominal_coverage,
        "axes": axes_report,
        "overall": overall,
        "direction": direction,
        "leakage_checks": {
            "holdout_ids_excluded": holdout_ids_excluded,
            "no_holdout_in_evidence": no_holdout_in_evidence,
            "predictions_not_written_back": store.count() == count_before,
        },
        "per_record": per_record,
        "note": "離線雜湊嵌入數值僅示意;看方向與覆蓋,不看絕對 MAE。",
    }


def _direction_metric(axis_points: Dict[str, List[tuple]]) -> Dict[str, Dict]:
    """各軸:在同機制群組內，預測高低排序與真值一致的配對比例。"""
    out: Dict[str, Dict] = {}
    for a, pts in axis_points.items():
        n_pairs = concordant = 0
        for mech in {m for m, _, _ in pts}:
            grp = [(t, p) for m, t, p in pts if m == mech]
            for i in range(len(grp)):
                for j in range(i + 1, len(grp)):
                    t_i, p_i = grp[i]
                    t_j, p_j = grp[j]
                    if t_i == t_j:
                        continue  # 真值平手 → 無排序可比
                    n_pairs += 1
                    if p_i != p_j and (t_i > t_j) == (p_i > p_j):
                        concordant += 1
        out[a] = {
            "n_pairs": n_pairs,
            "concordant": concordant,
            "pairwise_accuracy": round(concordant / n_pairs, 4) if n_pairs else None,
        }
    return out


# ────────────────────────────── 輸出 ──────────────────────────────

def _fmt(v, nd=3) -> str:
    return "  —  " if v is None else f"{v:.{nd}f}"


def format_report(r: Dict) -> str:
    lines: List[str] = []
    lines.append("=" * 64)
    lines.append(f"盲測評測報告  留出豆={r['n_holdout']}  嵌入器={r['embedder']}  "
                 f"庫={r['library_count']}  後端={r['store_backend']}")
    lines.append(f"名目覆蓋={r['nominal_coverage']:.0%}   ⚠ {r['note']}")
    lines.append("-" * 64)
    lines.append(f"{'軸':<12}{'n':>3} {'MAE':>7} {'RMSE':>7} {'覆蓋率':>8}{'  方向acc(配對)':>16}")
    for a in FLAVOR_AXES:
        ax = r["axes"][a]
        d = r["direction"][a]
        dacc = "—" if d["pairwise_accuracy"] is None else f"{d['pairwise_accuracy']:.2f}"
        pair_str = "{0}({1})".format(dacc, d["n_pairs"])
        cov_str = _fmt(ax["coverage"], 2)
        lines.append(f"{a:<12}{ax['n']:>3} {_fmt(ax['mae']):>7} {_fmt(ax['rmse']):>7} "
                     f"{cov_str:>8}{pair_str:>16}")
    ov = r["overall"]
    lines.append("-" * 64)
    lines.append(f"{'overall':<12}{ov['n']:>3} {_fmt(ov['mae']):>7} {_fmt(ov['rmse']):>7} "
                 f"{_fmt(ov['coverage'],2):>8}")
    lines.append("-" * 64)
    lc = r["leakage_checks"]
    lines.append("防洩漏檢查:")
    lines.append(f"  留出豆排除於召回庫 : {lc['holdout_ids_excluded']}")
    lines.append(f"  證據未含留出豆     : {lc['no_holdout_in_evidence']}")
    lines.append(f"  預測未寫回(無收斂污染): {lc['predictions_not_written_back']}")
    lines.append("=" * 64)
    return "\n".join(lines)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows 主控台 UTF-8
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    report = run_eval()
    print(format_report(report))
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON 報告已寫入 {REPORT_PATH}")


if __name__ == "__main__":
    main()
