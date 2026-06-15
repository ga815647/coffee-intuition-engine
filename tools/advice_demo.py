"""前向建議 demo(headline 目標:不管什麼豆都給建議)。

對兩支豆各跑 recommend / predict / diagnose,並排呈現:
  1. well-covered:衣索比亞耶加雪菲 水洗(語料密集)——應有同豆鄰居、較窄區間。
  2. cold-start:刻意冷門(尼泊爾 Bourbon Pointu 日曬)——應退物理先驗 + 寬區間 + 警告。

重點看冷啟動豆的 predict:有沒有守鐵則 §4/§6(誠實寬區間、source=prior、
「保守寬區間、非實測」警告),而不是硬給假精確點值。

唯讀:獨立記憶體 store,不碰 D1 / 不寫 canonical / 不動正式索引。
**硬 gate**:啟動即 assert 嵌入器是 workers_ai;缺金鑰靜默退回雜湊版會得假結果。

跑法:python -m tools.advice_demo   (需 .env 含 CF 金鑰)
"""
from __future__ import annotations

import sys

# ── .env 必須在 import cie.config 之前載入(否則靜默走 LOCAL,見 cie-cli-needs-env-loaded)──
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

# Windows 終端預設 cp950,印 CJK 會 UnicodeEncodeError;強制 UTF-8 輸出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass

from cie.config import CONFIG  # noqa: E402
from cie.embedding import get_embedder  # noqa: E402
from cie.engine import Engine  # noqa: E402
from cie.portability import read_jsonl  # noqa: E402
from cie.schema import BeanRoast, BrewMechanism, BrewParams, Process  # noqa: E402
from eval.run import CORPUS_PATH, _isolated_memory_store  # noqa: E402


# ────────────────────────────── 測試豆 ──────────────────────────────

WELL_COVERED = BeanRoast(
    origin="Ethiopia", variety="Heirloom", process=Process.WASHED, roast_agtron=72,
)
COLD_START = BeanRoast(
    origin="Nepal", variety="Bourbon Pointu", process=Process.NATURAL, roast_agtron=68,
)

# predict 用的沖煮參數(percolation / V60 起手)
PREDICT_PARAMS = BrewParams(
    brew_mechanism=BrewMechanism.PERCOLATION, method="V60",
    water_temp_c=92, brew_ratio=16, grind_um=700, contact_time_s=150,
)


# ────────────────────────────── 格式化 ──────────────────────────────

def _fmt_est(d: dict) -> str:
    """把 Estimate.__dict__ / {value,range,source} 印成 value [lo, hi] (source, n=...)。"""
    val = d.get("value")
    lo = d.get("lower", (d.get("range") or [None, None])[0])
    hi = d.get("upper", (d.get("range") or [None, None])[1])
    src = d.get("source", "?")
    n = d.get("n_effective")
    val_s = f"{val:.1f}" if isinstance(val, (int, float)) else str(val)
    iv = (f"[{lo:.1f}, {hi:.1f}]" if isinstance(lo, (int, float))
          and isinstance(hi, (int, float)) else "[—]")
    nbit = f", n_eff={n:.1f}" if isinstance(n, (int, float)) else ""
    return f"{val_s:>6} {iv:>16}  ({src}{nbit})"


def _print_recommend(eng: Engine, bean: BeanRoast, mech: BrewMechanism) -> None:
    r = eng.recommend(bean, mech)
    print(f"  · recommend [{r['mechanism']}]  信心={r['confidence_flag']}  "
          f"A權重佔比={r['a_weight_ratio']:.2f}  證據={len(r['evidence'])}筆")
    for k, est in r["suggested_params"].items():
        print(f"      {k:<16} {_fmt_est(est)}")
    for w in r["warnings"]:
        print(f"      ⚠ {w}")


def _print_predict(eng: Engine, bean: BeanRoast, params: BrewParams) -> None:
    r = eng.predict(bean, params)
    print(f"  · predict   [{r['mechanism']}]  信心={r['confidence_flag']}  "
          f"A權重佔比={r['a_weight_ratio']:.2f}  同豆證據={len(r['evidence'])}筆")
    for axis, est in r["predicted_flavor"].items():
        print(f"      {axis:<16} {_fmt_est(est)}")
    st = r.get("social_tendency") or {}
    origins = st.get("origins") or st.get("top_origins")
    if origins:
        print(f"      social_tendency.origins = {origins}")
    for w in r["warnings"]:
        print(f"      ⚠ {w}")


def _print_diagnose(eng: Engine, mech: BrewMechanism, defect: str) -> None:
    r = eng.diagnose(mech, defect, bean=None)
    print(f"  · diagnose  [{r['mechanism']}]  缺陷=「{defect}」  爭議={r.get('contested')}")
    for t in r.get("suggested_adjustments", []):
        print(f"      → {t}")
    for w in r["warnings"]:
        print(f"      ⚠ {w}")


# ────────────────────────────── 主程式 ──────────────────────────────

def main() -> int:
    embedder = get_embedder(CONFIG)
    if not embedder.model_id.startswith("workers_ai:"):
        print(f"✗ 中止:需 workers_ai 真嵌入,實得 {embedder.model_id!r}。"
              f"\n  八成 .env / CF 金鑰沒載(get_embedder 靜默退回雜湊版),會得假結果。",
              file=sys.stderr)
        return 2
    print(f"嵌入器:{embedder.model_id}")

    store = _isolated_memory_store(CONFIG, embedder=embedder)
    corpus = read_jsonl(CORPUS_PATH)
    loaded = store.upsert_many(corpus)
    print(f"召回庫:{loaded}/{len(corpus)} 筆(corpus/global.jsonl,獨立記憶體)\n")
    eng = Engine(store=store, canonical=None)

    mech = BrewMechanism.PERCOLATION
    for tag, bean in [("WELL-COVERED", WELL_COVERED), ("COLD-START", COLD_START)]:
        label = f"{bean.origin}/{bean.variety} [{bean.process.value}] agtron={bean.roast_agtron}"
        print("=" * 72)
        print(f"{tag}:{label}")
        print("=" * 72)
        _print_recommend(eng, bean, mech)
        print()
        _print_predict(eng, bean, PREDICT_PARAMS)
        print()
        _print_diagnose(eng, mech, "偏酸")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
