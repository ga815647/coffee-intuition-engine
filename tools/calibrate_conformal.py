"""按機制 split-conformal 校準:從留出殘差反推每個 (機制, 風味軸) 的半寬係數 q̂。

取代 weighted_estimate 區間的魔術數 1.64(假設殘差高斯,但實測覆蓋 0.96 ≫ 名目 0.90 →
殘差非高斯、係統性過寬)。正解:用**留出殘差的經驗 conformal 分位**校準每個 (機制, 軸) 的
歸一化半寬係數,寫入 cie/conformal_q.json,由 retrieval.conformal_z 注入。

鐵則:
  §1 機制硬隔離 — q̂ 按 (機制, 軸) 分開算,**絕不跨機制併**。
  §4 誠實不確定、寧過勿欠 — 用**保守有限樣本 conformal 分位**(ceil((n+1)(1-α)) 名次;
      名次 > n 即資料不足保證 → 不寫該條目 → retrieval 退回 1.64)。覆蓋只增不減的
      MIN_FLAVOR_MARGIN 地板 + [0,10] 夾域在 q̂ 之後照舊套用。
  §15.2 無洩漏 — 殘差來自 run_cv_eval 的 **out-of-fold** 留出預測(holdout 不在自己那折的
      召回庫),拿來校準 q̂ 不含折內洩漏。

歸一化分數(load-bearing):
  weighted_estimate 的區間半寬 = z × spread × widen,z 預設 1.64。對一筆留出預測,
  歸一化 conformity 分數 s = |true - pred| / (spread × widen) = 1.64 × |true - pred| / h,
  其中 h = (upper - lower)/2 是**未經地板/夾域**的原始半寬。故只取:
    - source ∈ {neighbors, shrunk}(走 z 路徑;prior / 物理粗略路徑不用 z,排除)。
    - 對稱(upper - pred ≈ pred - lower):非 [0,10] 夾域點(夾域破壞對稱、h ≠ 原始半寬)。
    - h > MIN_FLAVOR_MARGIN(+eps):非地板點(地板把 h 釘在 0.5,原始半寬不可復原;
      地板點真實 s 更大,以 h=0.5 算會低估 → 排除是保守的,不會灌出偏小 q̂)。
  排除的點在生產端由地板/夾域**過覆蓋**,故 q̂ 只校準仍走 z 的點 = Mondrian 正確條件化。

**強制 1.64 基線**:校準前把 retrieval._Q_TABLE 清空,確保 CV 用的是 z=1.64 原始區間
(否則若 conformal_q.json 已存在,會在已套 q̂ 的區間上再校準 → s 公式失真)。

唯讀:run_cv_eval 用獨立記憶體 store,不碰 D1 / 不寫 canonical / 不動正式索引。
**硬 gate**:啟動即 assert 嵌入器是 workers_ai(缺金鑰靜默退雜湊版會得假 q̂)。

跑法:
  python -m tools.calibrate_conformal              # 算並寫 cie/conformal_q.json
  python -m tools.calibrate_conformal --dry-run    # 只印、不寫
  python -m tools.calibrate_conformal --alpha 0.10 # 覆蓋名目(預設取 eval NOMINAL_COVERAGE)

寫出後須在**新行程**跑 `python -m eval.run`(或驗證腳本)重跑 CV,確認每機制 + 每軸
覆蓋 ≥ 名目(§4 終極把關;_Q_TABLE 於 import 時載入,故須新行程才吃得到新表)。

⚠ **--verify 是 in-sample 健全檢查,非獨立 out-of-sample 驗證**:CV 折是決定性的(無 RNG),
q̂ 擬合的歸一化分數集與 --verify 量覆蓋的留出集**同源**;加上保守 conformal 分位 + 地板/夾域
只增不減,「每 (機制×軸) 覆蓋 ≥ 名目」由**算術**保證、近乎恆真,**不證對新豆的泛化**。薄格
(n≈22–53)僅以 0.02–0.04 餘裕過關,線上抽樣變異仍可能下探。它能抓的是:q̂ 寫對了、地板/夾域
有套上、MAE 未動(點估未挪)、無 §1 跨機制洩漏——回歸護欄,非泛化證明。真實泛化待盲測新豆。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

# ── .env 必須在 import cie.config 之前載入(否則靜默走 LOCAL,見 cie-cli-needs-env-loaded)──
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

for _stream in (sys.stdout, sys.stderr):  # Windows 終端 UTF-8
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass

import cie.retrieval as retrieval  # noqa: E402
from cie.config import CONFIG  # noqa: E402
from cie.embedding import get_embedder  # noqa: E402
from cie.portability import read_jsonl  # noqa: E402
from cie.schema import FLAVOR_AXES  # noqa: E402
from eval.run import CORPUS_PATH, NOMINAL_COVERAGE, run_cv_eval  # noqa: E402

Q_TABLE_PATH = Path(retrieval.__file__).resolve().parent / "conformal_q.json"
# 地板點偵測容差:Estimate 把 lo/hi round 到 2 位,夾域點恰為 0.0 / 10.0。
_FLOOR_EPS = 0.02   # h > MIN_FLAVOR_MARGIN + eps 才算「非地板」(避開恰好頂地板的點)
_SYM_EPS = 0.02     # |（upper-pred) - (pred-lower)| ≤ eps 才算「未夾域」(對稱)


def _corpus_fingerprint(corpus_path: Path) -> Dict:
    """語料指紋:筆數 + 內容 md5(校準 provenance,語料漂移後可辨識 q̂ 是否過期)。"""
    raw = corpus_path.read_bytes()
    return {"path": str(corpus_path.name), "n_records": len(read_jsonl(corpus_path)),
            "md5": hashlib.md5(raw).hexdigest()}


def _conformal_quantile(scores: List[float], alpha: float) -> Optional[float]:
    """保守有限樣本 split-conformal 分位:第 ceil((n+1)(1-α)) 小(1-indexed)。

    名次 > n → None(資料不足以保證 1-α 覆蓋 → 呼叫端不寫條目 → retrieval 退 1.64)。
    """
    n = len(scores)
    if n == 0:
        return None
    rank = math.ceil((n + 1) * (1 - alpha))
    if rank > n:
        return None  # 資料不足:無法給有限樣本保證
    return sorted(scores)[rank - 1]


def collect_scores(per_record: List[Dict]) -> Dict[str, Dict[str, List[float]]]:
    """從 out-of-fold per_record 收集每 (機制, 軸) 的歸一化 conformity 分數 s。

    只取 source∈{neighbors,shrunk}、對稱(未夾域)、h>地板 的點(見模組 docstring)。
    回傳 {mech: {axis: [s, ...]}}。同時把排除統計掛在回傳物件的 .stats 上。
    """
    scores: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
    stats: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int)))
    z = retrieval.CONFORMAL_Z_FALLBACK  # 1.64:校準時 CV 區間用的基線係數
    floor = retrieval.MIN_FLAVOR_MARGIN
    for rec in per_record:
        mech = rec["mechanism"]
        for axis, e in rec["axes"].items():
            st = stats[mech][axis]
            st["seen"] += 1
            pred, true_v = e.get("pred"), e.get("true")
            lo, hi = e.get("lower"), e.get("upper")
            if pred is None or true_v is None or lo is None or hi is None:
                st["skip_missing"] += 1
                continue
            if e.get("source") not in ("neighbors", "shrunk"):
                st["skip_not_z_path"] += 1   # prior / 物理粗略路徑不用 z
                continue
            # 夾域偵測:夾到 [0,10] 會破壞對稱;未夾域時 (hi-pred)==(pred-lo)==margin。
            if abs((hi - pred) - (pred - lo)) > _SYM_EPS:
                st["skip_clamped"] += 1
                continue
            h = (hi - lo) / 2.0
            if h <= floor + _FLOOR_EPS:
                st["skip_floored"] += 1      # 地板釘住 h,原始半寬不可復原(排除=保守)
                continue
            # s = |true-pred| / (spread×widen) = z × |true-pred| / h
            s = z * abs(true_v - pred) / h
            scores[mech][axis].append(s)
            st["used"] += 1
    # 把 stats 附在回傳(plain dict 化,方便序列化)
    scores_out = {m: {a: list(v) for a, v in ax.items()} for m, ax in scores.items()}
    stats_out = {m: {a: dict(d) for a, d in ax.items()} for m, ax in stats.items()}
    return scores_out, stats_out


def build_table(scores: Dict[str, Dict[str, List[float]]], alpha: float,
                min_n: int = 1) -> Dict[str, Dict[str, float]]:
    """每 (機制, 軸):q̂ = 保守 conformal 分位;資料不足 → 略過該條目(退 1.64)。"""
    table: Dict[str, Dict[str, float]] = {}
    for mech, axes in scores.items():
        for axis, ss in axes.items():
            if len(ss) < min_n:
                continue
            q = _conformal_quantile(ss, alpha)
            if q is None or not (q > 0):
                continue
            # **向上**取到 4 位(鐵則 §4):round() 可能把 conformal 分位捨到真實次序統計量之下、
            # 漏掉點 → 覆蓋跌破名目。ceil 保證存出的 q̂ ≥ 真實分位 → 覆蓋只增不減。
            table.setdefault(mech, {})[axis] = math.ceil(float(q) * 10000) / 10000
    return table


def _coverage_at(scores: List[float], q: float) -> float:
    """校準集上、若用此 q̂(忽略地板/夾域)的 in-sample 覆蓋(透明度用,非保證)。"""
    if not scores:
        return float("nan")
    return sum(1 for s in scores if s <= q) / len(scores)


def _cell_coverage(per_record: List[Dict]) -> Dict[str, Dict[str, Dict]]:
    """每 (機制, 軸) 的端到端覆蓋(所有有區間的 holdout 點,不分 source)。

    這是 q̂ 真正作用的精細單位(§1 機制 × 軸)。回 {mech: {axis: {n, covered, coverage}}}。
    """
    agg: Dict[str, Dict[str, List[bool]]] = defaultdict(lambda: defaultdict(list))
    for rec in per_record:
        mech = rec["mechanism"]
        for axis, e in rec["axes"].items():
            if "covered" in e:
                agg[mech][axis].append(bool(e["covered"]))
    out: Dict[str, Dict[str, Dict]] = {}
    for mech, axes in agg.items():
        out[mech] = {}
        for axis, covs in axes.items():
            n = len(covs)
            out[mech][axis] = {"n": n, "covered": sum(covs),
                               "coverage": (sum(covs) / n) if n else None}
    return out


VERIFY_ARTIFACT_PATH = Path(__file__).resolve().parent.parent / "eval" / "conformal_verify.json"
_VERIFY_INTERPRETATION = (
    "IN-SAMPLE sanity check over deterministic CV folds — NOT independent out-of-sample "
    "validation. q̂ is fit on the same z-path score set this coverage is measured against; "
    "with the conservative conformal quantile + MIN_FLAVOR_MARGIN floor + [0,10] clamp, "
    "per-(mechanism×axis) coverage ≥ nominal is largely guaranteed by ARITHMETIC, not "
    "generalization. Thin cells (n≈22–53) clear nominal by only ~0.02–0.04, so deployment "
    "sampling on new beans can dip below. What this DOES prove: q̂ written correctly, "
    "floor/clamp applied, MAE unchanged (point estimates not moved), no §1 cross-mechanism "
    "leak. True generalization awaits blind-test on held-out beans."
)


def _run_verify(k: int, nominal: float) -> int:
    """§4 把關 + 可審產物:載入**已寫入**的 q̂ 表(本行程 import 時載入)重跑 CV,
    檢查每 (機制×軸)、每機制、每軸覆蓋 ≥ 名目;確認 MAE 不動(q̂ 只移區間、不動點估);
    並把結果寫入 eval/conformal_verify.json(入庫、附 embedder + 語料 md5,讓覆蓋宣稱可審)。

    ⚠ **這是 in-sample 健全檢查,非獨立 out-of-sample 驗證**(見模組 docstring):CV 折決定性、
    q̂ 與此處覆蓋同源,「每格 ≥ 名目」近乎算術恆真,不證對新豆泛化。它把關的是 q̂ 寫對 / 地板夾域
    有套 / MAE 未動 / 無 §1 洩漏這些回歸面向。

    回傳 process exit code(0=全過,1=有覆蓋跌破名目)。
    """
    loaded = retrieval._Q_TABLE
    n_loaded = sum(len(a) for a in loaded.values())
    print(f"--verify:已載入 q̂ 表 {n_loaded} 條目(來源 {retrieval._Q_TABLE_PATH.name})。")
    print(f"⚠ in-sample 健全檢查(非獨立 out-of-sample 驗證):{_VERIFY_INTERPRETATION}")
    if n_loaded == 0:
        print("⚠ q̂ 表為空(無 conformal_q.json 或全 fallback)→ 行為等同 z=1.64,無可驗。")
    report = run_cv_eval(k=k, include_per_record=True)
    per_record = report["per_record"]
    print(f"CV 完成:holdout={report['n_holdout']}  embedder={report['embedder']}  "
          f"整體 MAE={report['overall']['mae']}  整體覆蓋={report['overall']['coverage']}")

    eps = 1e-9
    violations: List[str] = []

    # 1) 每 (機制 × 軸) — q̂ 作用的精細單位
    cells = _cell_coverage(per_record)
    print("-" * 72)
    print(f"每 (機制×軸) 覆蓋(名目 {nominal:.0%};★=有 q̂ 條目):")
    print(f"{'機制':<12}{'軸':<12}{'n':>5}{'覆蓋':>8}{'q̂':>9}  判定")
    for mech in sorted(cells):
        for axis in sorted(cells[mech]):
            c = cells[mech][axis]
            cov = c["coverage"]
            q = loaded.get(mech, {}).get(axis)
            star = f"{q:.3f}" if q is not None else "1.64"
            ok = cov is not None and cov >= nominal - eps
            tag = "OK" if ok else "✗ 跌破名目"
            if not ok and cov is not None:
                violations.append(f"{mech}×{axis} 覆蓋 {cov:.3f} < {nominal}")
            covs = "—" if cov is None else f"{cov:.3f}"
            print(f"{mech:<12}{axis:<12}{c['n']:>5}{covs:>8}{star:>9}  {tag}")

    # 2) 每機制 / 每軸 aggregate(對齊既有報告)
    print("-" * 72)
    print("每機制覆蓋:", {m: v["coverage"] for m, v in report["by_mechanism"].items()})
    print("每軸覆蓋  :", {a: report["axes"][a]["coverage"] for a in FLAVOR_AXES})
    for m, v in report["by_mechanism"].items():
        if v["coverage"] is not None and v["coverage"] < nominal - eps:
            violations.append(f"機制 {m} 覆蓋 {v['coverage']:.3f} < {nominal}")
    for a in FLAVOR_AXES:
        cov = report["axes"][a]["coverage"]
        if cov is not None and cov < nominal - eps:
            violations.append(f"軸 {a} 覆蓋 {cov:.3f} < {nominal}")

    print("-" * 72)
    print("分機制方向 acc:", {m: v["direction_acc"] for m, v in report["by_mechanism"].items()})
    print("洩漏檢查:", report["leakage_checks"])

    # 可審產物(入庫 eval/conformal_verify.json):附 embedder + 語料 md5,讓「覆蓋 ≥ 名目」
    # 宣稱可從 repo 核對(對照 conformal_q.json 的 provenance.embedder / corpus.md5)。
    artifact = {
        "header": {
            "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "embedder": report["embedder"],
            "corpus": _corpus_fingerprint(CORPUS_PATH),
            "nominal_coverage": nominal,
            "k_folds": k,
            "n_holdout": report["n_holdout"],
            "q_entries_loaded": n_loaded,
            "interpretation": _VERIFY_INTERPRETATION,
        },
        "q_table_loaded": loaded,
        "overall": report["overall"],
        "by_cell": cells,
        "by_mechanism": {m: v["coverage"] for m, v in report["by_mechanism"].items()},
        "by_axis": {a: report["axes"][a]["coverage"] for a in FLAVOR_AXES},
        "violations": violations,
    }
    VERIFY_ARTIFACT_PATH.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ 已寫入可審產物 {VERIFY_ARTIFACT_PATH}(入庫;附 embedder + 語料 md5)。")

    if violations:
        print(f"✗ 驗證失敗:{len(violations)} 處覆蓋跌破名目(鐵則 §4):")
        for v in violations:
            print("   -", v)
        return 1
    print("✓ 驗證通過:每 (機制×軸)、每機制、每軸覆蓋皆 ≥ 名目(in-sample 健全檢查;鐵則 §4 護欄守住)。")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="按機制 split-conformal 校準 q̂")
    ap.add_argument("--alpha", type=float, default=round(1 - NOMINAL_COVERAGE, 4),
                    help=f"miscoverage(預設 {round(1 - NOMINAL_COVERAGE, 4)} = 名目 {NOMINAL_COVERAGE})")
    ap.add_argument("--k", type=int, default=5, help="CV 折數")
    ap.add_argument("--min-n", type=int, default=20,
                    help="每 (機制,軸) 至少幾個校準點才寫 q̂(否則退 1.64)")
    ap.add_argument("--dry-run", action="store_true", help="只印不寫檔")
    ap.add_argument("--verify", action="store_true",
                    help="不重算:載入既有 conformal_q.json 重跑 CV,驗每機制×軸覆蓋 ≥ 名目(§4 把關)")
    args = ap.parse_args()

    # 硬 gate:必須真嵌入,否則 q̂ 無意義。
    embedder = get_embedder(CONFIG)
    if not embedder.model_id.startswith("workers_ai:"):
        print(f"✗ 中止:需 workers_ai 真嵌入,實得 {embedder.model_id!r}。"
              f"\n  八成 .env / CF 金鑰沒載(get_embedder 靜默退回雜湊版),會得假 q̂。",
              file=sys.stderr)
        sys.exit(1)

    if args.verify:
        print(f"嵌入器:{embedder.model_id}   名目覆蓋 {1 - args.alpha:.0%}")
        sys.exit(_run_verify(args.k, nominal=round(1 - args.alpha, 4)))

    print(f"嵌入器:{embedder.model_id}   alpha={args.alpha}(名目覆蓋 {1 - args.alpha:.0%})")

    # 強制 1.64 基線:清空已載入的 q̂ 表,確保 CV 跑的是原始 z=1.64 區間(s 公式才正確)。
    retrieval._Q_TABLE = {}
    # 防禦:若日後重構讓別處也快取了表,這裡會炸,避免在已套 q̂ 的區間上再校準(s 失真)。
    assert retrieval._Q_TABLE == {}, "清空後 _Q_TABLE 非空:校準會在已套 q̂ 的區間上失真"
    print("已清空 retrieval._Q_TABLE → CV 用 z=1.64 原始區間校準。")

    report = run_cv_eval(k=args.k, include_per_record=True)
    per_record = report["per_record"]
    print(f"CV 完成:holdout={report['n_holdout']}  embedder={report['embedder']}  "
          f"庫≈{report['library_count']}/{report['corpus_size']}")

    scores, stats = collect_scores(per_record)
    table = build_table(scores, args.alpha, min_n=args.min_n)

    # 報告:每 (機制,軸) 的 n_used / q̂ / in-sample 覆蓋 / 排除明細。
    print("-" * 84)
    print(f"{'機制':<12}{'軸':<12}{'n_used':>7}{'q̂':>8}{'vs1.64':>8}{'cov@q̂':>8}   排除(missing/notz/clamp/floor)")
    for mech in sorted(scores):
        for axis in sorted(scores[mech]):
            ss = scores[mech][axis]
            st = stats[mech][axis]
            q = table.get(mech, {}).get(axis)
            qs = "—(退1.64)" if q is None else f"{q:.3f}"
            delta = "—" if q is None else f"{q - 1.64:+.3f}"
            cov = "—" if q is None else f"{_coverage_at(ss, q):.3f}"
            excl = f"{st.get('skip_missing',0)}/{st.get('skip_not_z_path',0)}/" \
                   f"{st.get('skip_clamped',0)}/{st.get('skip_floored',0)}"
            print(f"{mech:<12}{axis:<12}{len(ss):>7}{qs:>8}{delta:>8}{cov:>8}   {excl}")
    print("-" * 84)
    n_written = sum(len(a) for a in table.values())
    print(f"q̂ 條目:{n_written}(其餘退 1.64 fallback)")

    out = {
        "provenance": {
            "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "embedder": report["embedder"],
            "corpus": _corpus_fingerprint(CORPUS_PATH),
            "nominal_coverage": round(1 - args.alpha, 4),
            "alpha": args.alpha,
            "k_folds": args.k,
            "min_n": args.min_n,
            "method": "mondrian split-conformal (per mechanism×axis), conservative finite-sample "
                      "quantile rank=ceil((n+1)(1-alpha)); normalized score s=1.64*|true-pred|/halfwidth; "
                      "z-path(neighbors/shrunk) symmetric non-clamped non-floored points only",
            "exclusion_stats": stats,
        },
        "fallback": retrieval.CONFORMAL_Z_FALLBACK,
        "q": table,
    }

    if args.dry_run:
        print("--dry-run:不寫檔。")
        return
    Q_TABLE_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已寫入 {Q_TABLE_PATH}")
    print("→ 下一步(§4 把關):新行程跑 `python -m eval.run`,確認每機制 + 每軸覆蓋 ≥ 名目。")


if __name__ == "__main__":
    main()
