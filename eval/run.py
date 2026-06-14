"""盲測評測:對『庫裡沒有的豆』先預測、再比對人工真值。

    python -m eval.run     # 預設:對 corpus/global.jsonl 的 A/B 級記錄做
                           # 「按機制分層的 k-fold」交叉驗證(統計上撐得起結論)

量化(對應鐵則「方向 > 絕對值」與 §12.3 區間),並**分機制**報告:
  (a) L3 各軸 MAE / RMSE;
  (b) conformal 區間覆蓋率(真值落在 [下界,上界] 的比例 vs 名目);
  (c) **同機制**配對的方向 / 排序準確率(pairwise accuracy)。

留出集設計(§15.2):
  - holdout 來自 corpus/global.jsonl 的 **A/B 級**記錄,**按機制分層**抽樣
    (k-fold:每筆 A/B 記錄輪流當一次 holdout,統計效力最大;取代撐不起結論的
    5 筆合成 holdout)。
  - **C 級永不當 holdout 真值**(開環、標籤不一致;只能留在召回庫壓量級,鐵則 §3)。
  - 合成 `dataset.jsonl` 留作**洩漏偵測器回歸**用(`run_eval` 路徑,證明守衛非虛設)。

防洩漏鐵則(§15 / design §12.6,三道,務必):
  1. 留出豆**絕不進召回庫**:獨立記憶體 store,灌入語料並**按內容指紋扣除 holdout**
     (語料不帶穩定 id,故按內容比對);執行期再驗證 id 與庫互斥(縱深防禦)。
  2. 嚴禁「事後感官子項」回推總分(R²≈0.82 陷阱):predict() 只吃 bean + params,
     結構上完全不碰任何真值風味軸——設計層保證,非靠自律。
  3. 評測產生的預測**一律不寫回**(不呼叫 log_calibration;store 筆數前後不變)。

注意:離線雜湊嵌入本就不準,harness **不對 MAE 下硬門檻**;它證明的是
『協定可跑、留出豆確被排除、覆蓋/方向算得出、分機制統計成立』。真實準度數字待接
workers_ai 嵌入 + 真實資料後再看(屆時同一 harness 直接複用)。
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cie.config import CONFIG, Config
from cie.embedding import CachingEmbedder, get_embedder
from cie.engine import Engine
from cie.portability import read_jsonl
from cie.schema import FLAVOR_AXES, Record
from cie.store import StoreBackend, VectorStore

DATASET_PATH = Path(__file__).resolve().parent / "dataset.jsonl"
REPORT_PATH = Path(__file__).resolve().parent / "report.json"
# 召回庫來源:策展語料(446 筆真相),不是 6 筆 seeds/anchors.jsonl。
CORPUS_PATH = Path(__file__).resolve().parent.parent / "corpus" / "global.jsonl"
NOMINAL_COVERAGE = 0.90       # weighted_estimate 用 ~90% 名目區間
HOLDOUT_GRADES = ("A", "B")   # C 級永不當 holdout 真值(鐵則 §3:開環只壓量級)
DEFAULT_K = 5                 # 分層 k-fold 的 k


# ────────────────────────────── 小工具 ──────────────────────────────

def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _rmse(errs: List[float]) -> float:
    return math.sqrt(_mean([e * e for e in errs])) if errs else 0.0


# ────────────────────────────── 召回庫(防洩漏) ──────────────────────────────

def _holdout_signature(r: Record) -> tuple:
    """內容指紋:同一支豆 + 同機制 + 同泡法 + 核心參數 → 視為同一資料點。

    用於從召回庫『扣除 holdout』。**不能靠 id**:`corpus/global.jsonl` 不帶 id
    (schema 預設 uuid4,每次載入都不同),故按內容比對才可靠。浮點四捨五入避免噪音。
    """
    b, p = r.bean, r.params

    def _rnd(x):
        return None if x is None else round(float(x), 2)

    return (
        (b.origin or "").strip().lower(),
        (b.variety or "").strip().lower(),
        b.process.value,
        _rnd(b.roast_agtron),
        p.brew_mechanism.value,
        (p.method or "").strip().lower(),
        _rnd(p.water_temp_c), _rnd(p.brew_ratio), _rnd(p.grind_um),
        _rnd(p.contact_time_s), _rnd(p.pressure_bar),
    )


def _isolated_memory_store(config: Config, embedder=None) -> VectorStore:
    """強制記憶體模式的獨立 store,隔離正式索引(評測零副作用)。

    可注入共用嵌入器(CachingEmbedder),讓 k-fold 跨折共用同一嵌入快取。
    """
    iso = replace(config, qdrant_url="", qdrant_api_key="", store_backend_override="memory")
    return VectorStore(iso, embedder=embedder)


def build_library_store(holdout_ids: set, config: Config = CONFIG,
                        corpus_path: Path = CORPUS_PATH,
                        holdout_records: Optional[List[Record]] = None) -> VectorStore:
    """(dataset 路徑)建獨立記憶體 store,灌入 `corpus/global.jsonl`,**扣除 holdout**。

    扣除 holdout 走**內容指紋**(id 由系統隨機生成不可靠);另保留 id 互斥檢查作縱深防禦。
    沿用設定的嵌入器(local / workers_ai),強制記憶體模式以隔離正式索引、絕不寫入留出豆。
    """
    store = _isolated_memory_store(config)
    corpus = read_jsonl(corpus_path)
    holdout_sigs = {_holdout_signature(h) for h in (holdout_records or [])}
    kept = [r for r in corpus
            if r.id not in holdout_ids and _holdout_signature(r) not in holdout_sigs]
    store.upsert_many(kept)  # canonical 不掛(零副作用)
    leaked = holdout_ids & {r.id for r in store.iter_records()}
    if leaked:  # pragma: no cover - 防禦:holdout id 與語料(uuid)命名互斥,不應發生
        raise RuntimeError(f"洩漏:留出豆出現在召回庫 → {leaked}")
    return store


def _stratified_folds(records: List[Record], k: int) -> List[List[Record]]:
    """把記錄**按機制分層**round-robin 切成 k 折(確定性、可重現)。

    每機制各自排序(依內容指紋,**不依隨機 uuid**)後輪流分配 → 各折的機制比例一致。
    這保證:跨折的機制分布均衡、且同一份語料每次切法相同(回歸測試可重現)。
    """
    by_mech: Dict[str, List[Record]] = defaultdict(list)
    for r in records:
        by_mech[r.params.brew_mechanism.value].append(r)
    folds: List[List[Record]] = [[] for _ in range(k)]
    for mech in sorted(by_mech):
        grp = sorted(by_mech[mech], key=lambda r: (repr(_holdout_signature(r)), r.source or ""))
        for i, r in enumerate(grp):
            folds[i % k].append(r)
    return folds


# ────────────────────────────── 評分(單組 holdout) ──────────────────────────────

def _score_holdouts(engine: Engine, holdouts: List[Record],
                    holdout_ids: set) -> Tuple[List[Dict], bool]:
    """對一組 holdout 逐筆 predict 並記錄各軸 (true,pred,區間)。

    回傳 (per_record, no_holdout_in_evidence)。predict() 只吃 bean+params:結構上無風味洩漏。
    """
    per_record: List[Dict] = []
    no_holdout_in_evidence = True
    for h in holdouts:
        pred = engine.predict(h.bean, h.params)
        ev_ids = {e.get("id") for e in pred.get("evidence", [])}
        if ev_ids & holdout_ids:  # 縱深防禦:鄰居只能來自 store;store 已排除 holdout 時恆 True
            no_holdout_in_evidence = False
        pf = pred["predicted_flavor"]
        # 硬湊(hard-stretch):無同豆鄰居 → predicted_flavor 退回物理粗略(§16.4)。
        # 物理退回時所有軸 source=="prior";有同豆時各軸為 neighbors/shrunk(value=None 的軸被略過,
        # 永不以 source=="prior" 入列)。故「全軸 prior」⟺ 該筆是硬湊出來的、非同豆實測。
        hard_stretch = bool(pf) and all(e.get("source") == "prior" for e in pf.values())
        rec_out: Dict = {"id": h.id, "mechanism": h.params.brew_mechanism.value,
                         "grade": h.grade.value, "hard_stretch": hard_stretch, "axes": {}}
        for a in FLAVOR_AXES:
            true_v = getattr(h.flavor, a)
            est = pf.get(a)
            pv = est["value"] if est else None
            lo = est["lower"] if est else None
            hi = est["upper"] if est else None
            entry: Dict = {"true": true_v, "pred": pv, "lower": lo, "upper": hi}
            if true_v is not None and pv is not None:
                entry["abs_err"] = round(abs(pv - true_v), 4)
                if lo is not None and hi is not None:
                    entry["covered"] = lo <= true_v <= hi
            rec_out["axes"][a] = entry
        per_record.append(rec_out)
    return per_record, no_holdout_in_evidence


# ────────────────────────────── 彙總 ──────────────────────────────

def _direction_metric(axis_points: Dict[str, List[tuple]]) -> Dict[str, Dict]:
    """各軸:在**同機制**群組內,預測高低排序與真值一致的配對比例(跨機制不配對)。"""
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


def _collect_axis_data(per_record: List[Dict]):
    """從 per_record 還原各軸的 errs / coverage / (mech,true,pred) 點。"""
    axis_abs: Dict[str, List[float]] = {a: [] for a in FLAVOR_AXES}
    axis_cov: Dict[str, List[bool]] = {a: [] for a in FLAVOR_AXES}
    axis_points: Dict[str, List[tuple]] = {a: [] for a in FLAVOR_AXES}
    for rec in per_record:
        mech = rec["mechanism"]
        for a in FLAVOR_AXES:
            e = rec["axes"][a]
            if "abs_err" in e:
                axis_abs[a].append(e["abs_err"])
                axis_points[a].append((mech, e["true"], e["pred"]))
                if "covered" in e:
                    axis_cov[a].append(e["covered"])
    return axis_abs, axis_cov, axis_points


def _aggregate(per_record: List[Dict]):
    """→ (axes_report, overall, direction):各軸與 overall 的 MAE/RMSE/覆蓋 + 同機制方向。"""
    axis_abs, axis_cov, axis_points = _collect_axis_data(per_record)
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
    # `overall` 是對「**已按機制隔離**做出的」每筆預測誤差做的描述性彙總,
    # **不是**跨機制推論/檢索/估計/方向投票(那些才受鐵則 §1「永不跨機制平均」約束,
    # 且已由 store 機制硬過濾 + _direction_metric 同機制配對落實)。分機制細節見 _by_mechanism。
    overall = {
        "n": len(all_errs),
        "mae": round(_mean(all_errs), 4) if all_errs else None,
        "rmse": round(_rmse(all_errs), 4) if all_errs else None,
        "coverage": round(_mean([1.0 if c else 0.0 for c in all_cov]), 4) if all_cov else None,
    }
    return axes_report, overall, _direction_metric(axis_points)


def _by_mechanism(per_record: List[Dict]) -> Dict[str, Dict]:
    """**分機制**彙總:n(holdout 筆數)、MAE/RMSE、覆蓋率、方向(同機制配對,跨軸彙總)。"""
    out: Dict[str, Dict] = {}
    for mech in sorted({r["mechanism"] for r in per_record}):
        recs = [r for r in per_record if r["mechanism"] == mech]
        errs, covs = [], []
        pts: Dict[str, List[tuple]] = {a: [] for a in FLAVOR_AXES}
        for rec in recs:
            for a in FLAVOR_AXES:
                e = rec["axes"][a]
                if "abs_err" in e:
                    errs.append(e["abs_err"])
                    pts[a].append((mech, e["true"], e["pred"]))
                    if "covered" in e:
                        covs.append(e["covered"])
        dm = _direction_metric(pts)
        d_pairs = sum(dm[a]["n_pairs"] for a in FLAVOR_AXES)
        d_conc = sum(dm[a]["concordant"] for a in FLAVOR_AXES)
        n_hs = sum(1 for r in recs if r.get("hard_stretch"))
        out[mech] = {
            "n_holdout": len(recs),
            "n_axis_points": len(errs),
            "mae": round(_mean(errs), 4) if errs else None,
            "rmse": round(_rmse(errs), 4) if errs else None,
            "coverage": round(_mean([1.0 if c else 0.0 for c in covs]), 4) if covs else None,
            "direction_pairs": d_pairs,
            "direction_acc": round(d_conc / d_pairs, 4) if d_pairs else None,
            "hard_stretch_rate": round(n_hs / len(recs), 4) if recs else None,
        }
    return out


def _hard_stretch(per_record: List[Dict]) -> Dict:
    """硬湊率:holdout 中『無同豆鄰居、退回物理粗略』的比例(整體 + 分機制)。

    衡量召回庫對「這支豆的特色」的同豆覆蓋有多薄——率越高 = 越多豆只能硬湊物理先驗、
    答不出個別風味特色(§16.4)。**只統計 A/B holdout**(C 永不當 holdout)。
    補 Tier-1 同豆料(§4)應讓此率下降。
    """
    n = len(per_record)
    n_hs = sum(1 for r in per_record if r.get("hard_stretch"))
    by_mech: Dict[str, Dict] = {}
    for mech in sorted({r["mechanism"] for r in per_record}):
        recs = [r for r in per_record if r["mechanism"] == mech]
        h = sum(1 for r in recs if r.get("hard_stretch"))
        by_mech[mech] = {"n": len(recs), "n_hard_stretch": h,
                         "rate": round(h / len(recs), 4) if recs else None}
    return {
        "n": n, "n_hard_stretch": n_hs,
        "rate": round(n_hs / n, 4) if n else None,
        "by_mechanism": by_mech,
        "note": "無同豆鄰居→物理粗略退回的 holdout 占比;越低=同豆覆蓋越扎實。",
    }


# ────────────────────────────── 評測主體:分層 k-fold CV(預設) ──────────────────────────────

def run_cv_eval(k: int = DEFAULT_K, config: Config = CONFIG,
                nominal_coverage: float = NOMINAL_COVERAGE,
                corpus_path: Path = CORPUS_PATH) -> Dict:
    """對語料 A/B 級記錄做**按機制分層的 k-fold** 盲測 CV。回傳結構化報告 dict。

    每折:holdout = 該折 A/B 記錄;召回庫 = 全語料(含 C)**按內容指紋扣除本折 holdout**。
    每筆 A/B 記錄正好被評測一次(out-of-fold),最後彙總分機制 n/MAE/覆蓋/方向。
    C 級永遠留在召回庫、**永不當 holdout 真值**。
    """
    corpus = read_jsonl(corpus_path)
    eligible = [r for r in corpus if r.grade.value in HOLDOUT_GRADES]  # C 不可當 holdout
    folds = _stratified_folds(eligible, k)

    # 跨折共用一個快取嵌入器:同一筆語料在 k-1 折的召回庫重複出現,快取把
    # 雲端(workers_ai)嵌入呼叫降到 ~1/k。鍵含 model_id(鐵則 §14.5,不跨模型混用)。
    shared_embedder = CachingEmbedder(get_embedder(config))

    all_per_record: List[Dict] = []
    ids_excluded = no_ev_leak = not_written = True
    embedder: Optional[str] = None
    fold_summaries: List[Dict] = []
    lib_counts: List[int] = []

    for fi, fold in enumerate(folds):
        if not fold:
            continue
        holdout_sigs = {_holdout_signature(h) for h in fold}
        holdout_ids = {h.id for h in fold}
        library = [r for r in corpus if _holdout_signature(r) not in holdout_sigs]
        store = _isolated_memory_store(config, embedder=shared_embedder)
        store.upsert_many(library)
        embedder = store.model_id
        before = store.count()
        lib_counts.append(before)
        # 防洩漏 1(本路徑非虛設:holdout 即語料記錄、被主動以指紋排除,uuid 一致可對撞)
        if not holdout_ids.isdisjoint({r.id for r in store.iter_records()}):
            ids_excluded = False
        engine = Engine(store=store, canonical=None)  # 不掛 canonical sink
        per_record, no_ev = _score_holdouts(engine, fold, holdout_ids)
        if not no_ev:
            no_ev_leak = False
        if store.count() != before:  # 防洩漏 3:預測不寫回
            not_written = False
        all_per_record += per_record
        fold_summaries.append({"fold": fi, "n_holdout": len(fold), "library_count": before})

    axes_report, overall, direction = _aggregate(all_per_record)
    return {
        "mode": "cv_stratified",
        "k_folds": k,
        "n_holdout": len(all_per_record),
        "corpus_size": len(corpus),
        "eligible_ab": len(eligible),
        "embedder": embedder,
        "store_backend": "VectorStore(memory)",
        "library_count": round(_mean(lib_counts)) if lib_counts else 0,
        "nominal_coverage": nominal_coverage,
        "axes": axes_report,
        "overall": overall,
        "direction": direction,
        "by_mechanism": _by_mechanism(all_per_record),
        "hard_stretch": _hard_stretch(all_per_record),
        "leakage_checks": {
            "holdout_ids_excluded": ids_excluded,
            "no_holdout_in_evidence": no_ev_leak,
            "predictions_not_written_back": not_written,
            "c_grade_never_holdout": all(r["grade"] in HOLDOUT_GRADES for r in all_per_record),
        },
        "fold_summaries": fold_summaries,
        "embed_cache": shared_embedder.cache_info(),  # size/hits/misses:驗證跨折去重生效
        "note": "離線雜湊嵌入數值僅示意;看分機制方向與覆蓋,不看絕對 MAE。",
    }


# ────────────────────────────── 評測主體:dataset 路徑(洩漏偵測器回歸) ──────────────────────────────

def run_eval(dataset_path: Path = DATASET_PATH, store: Optional[StoreBackend] = None,
             nominal_coverage: float = NOMINAL_COVERAGE, config: Config = CONFIG) -> Dict:
    """對顯式 `dataset.jsonl` 跑盲測(留作洩漏偵測器回歸)。回傳結構化報告 dict。"""
    holdouts = read_jsonl(dataset_path)
    holdout_ids = {r.id for r in holdouts}

    if store is None:
        store = build_library_store(holdout_ids, config, holdout_records=holdouts)

    store_ids = {r.id for r in store.iter_records()} if hasattr(store, "iter_records") else set()
    holdout_ids_excluded = holdout_ids.isdisjoint(store_ids)

    engine = Engine(store=store, canonical=None)  # 不掛 canonical sink:評測零副作用
    count_before = store.count()
    per_record, no_holdout_in_evidence = _score_holdouts(engine, holdouts, holdout_ids)
    axes_report, overall, direction = _aggregate(per_record)

    return {
        "mode": "dataset",
        "n_holdout": len(holdouts),
        "embedder": store.model_id,
        "store_backend": type(store).__name__,
        "library_count": count_before,
        "nominal_coverage": nominal_coverage,
        "axes": axes_report,
        "overall": overall,
        "direction": direction,
        "by_mechanism": _by_mechanism(per_record),
        "hard_stretch": _hard_stretch(per_record),
        "leakage_checks": {
            "holdout_ids_excluded": holdout_ids_excluded,
            "no_holdout_in_evidence": no_holdout_in_evidence,
            "predictions_not_written_back": store.count() == count_before,
            "c_grade_never_holdout": all(r["grade"] in HOLDOUT_GRADES for r in per_record),
        },
        "per_record": per_record,
        "note": "離線雜湊嵌入數值僅示意;看方向與覆蓋,不看絕對 MAE。",
    }


# ────────────────────────────── 輸出 ──────────────────────────────

def _fmt(v, nd=3) -> str:
    return "  —  " if v is None else f"{v:.{nd}f}"


def _pair_str(acc, pairs) -> str:
    dacc = "—" if acc is None else f"{acc:.2f}"
    return f"{dacc}({pairs})"


def format_report(r: Dict) -> str:
    lines: List[str] = []
    lines.append("=" * 72)
    if r.get("mode") == "cv_stratified":
        lines.append(f"盲測評測(按機制分層 {r['k_folds']}-fold CV)  "
                     f"holdout={r['n_holdout']}/{r['eligible_ab']} A/B(C 不入 holdout)  "
                     f"嵌入器={r['embedder']}  庫≈{r['library_count']}/{r['corpus_size']}")
    else:
        lines.append(f"盲測評測(dataset)  留出豆={r['n_holdout']}  嵌入器={r['embedder']}  "
                     f"庫={r['library_count']}  後端={r['store_backend']}")
    lines.append(f"名目覆蓋={r['nominal_coverage']:.0%}   ⚠ {r['note']}")

    # 分機制(本次升級重點)
    lines.append("-" * 72)
    lines.append(f"{'機制':<12}{'n':>5} {'MAE':>7} {'RMSE':>7} {'覆蓋率':>8}{'  方向acc(配對)':>18}")
    for mech, m in r["by_mechanism"].items():
        lines.append(f"{mech:<12}{m['n_holdout']:>5} {_fmt(m['mae']):>7} {_fmt(m['rmse']):>7} "
                     f"{_fmt(m['coverage'], 2):>8}{_pair_str(m['direction_acc'], m['direction_pairs']):>18}")

    # 各軸(overall,跨全部 out-of-fold 預測)
    lines.append("-" * 72)
    lines.append(f"{'軸':<12}{'n':>5} {'MAE':>7} {'RMSE':>7} {'覆蓋率':>8}{'  方向acc(配對)':>18}")
    for a in FLAVOR_AXES:
        ax = r["axes"][a]
        d = r["direction"][a]
        lines.append(f"{a:<12}{ax['n']:>5} {_fmt(ax['mae']):>7} {_fmt(ax['rmse']):>7} "
                     f"{_fmt(ax['coverage'], 2):>8}{_pair_str(d['pairwise_accuracy'], d['n_pairs']):>18}")
    ov = r["overall"]
    lines.append("-" * 72)
    lines.append(f"{'overall':<12}{ov['n']:>5} {_fmt(ov['mae']):>7} {_fmt(ov['rmse']):>7} "
                 f"{_fmt(ov['coverage'], 2):>8}")

    # 硬湊率(無同豆鄰居→物理粗略退回的占比;§16.4 / §4)
    hs = r.get("hard_stretch")
    if hs:
        lines.append("-" * 72)
        per = "  ".join(f"{m}={mm['rate']:.0%}({mm['n_hard_stretch']}/{mm['n']})"
                        for m, mm in hs["by_mechanism"].items())
        rate = "—" if hs["rate"] is None else f"{hs['rate']:.1%}"
        lines.append(f"硬湊率(無同豆→物理退回): {rate}  ({hs['n_hard_stretch']}/{hs['n']})   {per}")

    lines.append("-" * 72)
    lc = r["leakage_checks"]
    lines.append("防洩漏檢查:")
    lines.append(f"  留出豆排除於召回庫    : {lc['holdout_ids_excluded']}")
    lines.append(f"  證據未含留出豆        : {lc['no_holdout_in_evidence']}")
    lines.append(f"  預測未寫回(無收斂污染) : {lc['predictions_not_written_back']}")
    lines.append(f"  C 級未當 holdout 真值  : {lc['c_grade_never_holdout']}")
    lines.append("=" * 72)
    return "\n".join(lines)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows 主控台 UTF-8
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    report = run_cv_eval()  # 預設:分層 k-fold CV(取代撐不起結論的 5 筆合成 holdout)
    print(format_report(report))
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nJSON 報告已寫入 {REPORT_PATH}")


if __name__ == "__main__":
    main()
