"""端到端 demo:灌種子 → recommend / predict / diagnose / method_swap。

    python -m cie.demo

預設離線(記憶體向量庫 + local 雜湊嵌入);若設了 CF / Qdrant 金鑰會自動改用對應後端。
"""
from __future__ import annotations

import json
import sys

from .engine import Engine
from .schema import BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Process
from .seed import seed
from .store import get_store


def _show(title: str, obj) -> None:
    print("\n" + "=" * 72)
    print(f"> {title}")
    print("=" * 72)
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    # 強制 UTF-8 輸出:Windows 主控台預設 cp950 無法輸出中文 / 符號。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

    store = get_store()
    print(f"後端: {type(store).__name__}  |  嵌入器: {store.model_id}")
    if store.count() <= 0:
        n = seed(store)
        print(f"已灌入 {n} 筆 A 級種子。")
    engine = Engine(store)

    bean = BeanRoast(origin="Ethiopia Yirgacheffe", variety="Heirloom",
                     process=Process.WASHED, roast_agtron=74)

    _show("recommend(percolation 起手參數)",
          engine.recommend(bean, BrewMechanism.PERCOLATION,
                           target_flavor=FlavorProfile(acidity=7.5, sweetness=7.0)))

    params = BrewParams(brew_mechanism=BrewMechanism.PERCOLATION, water_temp_c=92,
                        brew_ratio=15.5, grind_um=680, contact_time_s=160,
                        tds_pct=1.42, ey_pct=21.0)
    _show("predict(預測風味)", engine.predict(bean, params))

    _show("diagnose(尖酸、收尾水 → 歸因)",
          engine.diagnose(BrewMechanism.PERCOLATION, "尖酸、收尾水", bean))

    _show("method_swap(V60 percolation → Espresso pressure,跨機制)",
          engine.method_swap(bean, params, BrewMechanism.PRESSURE, "Espresso"))


if __name__ == "__main__":
    main()
