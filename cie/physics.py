"""物理先驗 — 按萃取機制三軌分立(設計 §12.1)。

每軸記錄各物理變數對「萃取率 E」的敏感度方向與強度,以及 TDS/EY→風味的相關。
這些是『方向可信、量保守』的先驗骨架;真值由校準資料收斂。

來源:UC Davis/Nature 2021(浸泡平衡)、Coffee ad Astra / Hoffmann(滴濾流動)、
Matter 2020(義式力學,研磨→E 非單調)。詳見 design §2.2 / §12.1。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from .schema import BrewMechanism, BrewParams, FlavorProfile


# 敏感度:某變數每提高一單位(正規化後),對萃取率 E 的方向性影響。
# 0 = 不影響終點;正 = 增萃;"peak" = 非單調(有最適點)。
@dataclass(frozen=True)
class MechanismPrior:
    name: BrewMechanism
    note: str
    # 對萃取率 E 的敏感度(終點意義)
    grind_to_ey: str          # "low" | "high" | "peak"  (注意:細→快,故變細通常增萃)
    temp_to_ey: str           # "low" | "high"
    time_to_ey: str           # "low" | "high"
    agitation_to_ey: str      # "low" | "high"
    # TDS 與粉水比關係
    tds_vs_ratio: str         # "inverse"(濃度~1/粉水比)
    ey_vs_ratio: str          # "independent" | "coupled"
    # 黃金杯目標(粗略區間)
    target_ey: tuple = (18.0, 22.0)
    target_tds: tuple = (1.15, 1.45)


PRIORS: Dict[BrewMechanism, MechanismPrior] = {
    BrewMechanism.IMMERSION: MechanismPrior(
        name=BrewMechanism.IMMERSION,
        note="趨熱力學平衡;E 對研磨/溫度/攪拌不敏感(只改達平衡速度)。TDS~1/粉水比,EY 與粉水比獨立。",
        grind_to_ey="low", temp_to_ey="low", time_to_ey="low", agitation_to_ey="low",
        tds_vs_ratio="inverse", ey_vs_ratio="independent",
    ),
    BrewMechanism.PERCOLATION: MechanismPrior(
        name=BrewMechanism.PERCOLATION,
        note="非平衡流動;新鮮水維持高梯度,E 由流體傳輸主控,對研磨與流速/時間極敏感。",
        grind_to_ey="high", temp_to_ey="high", time_to_ey="high", agitation_to_ey="high",
        tds_vs_ratio="inverse", ey_vs_ratio="coupled",
    ),
    BrewMechanism.PRESSURE: MechanismPrior(
        name=BrewMechanism.PRESSURE,
        note="加壓滲濾;研磨→E 非單調(過細誘發通道效應反降 E、破壞重現性)。",
        grind_to_ey="peak", temp_to_ey="high", time_to_ey="high", agitation_to_ey="low",
        tds_vs_ratio="inverse", ey_vs_ratio="coupled",
        target_ey=(18.0, 22.0), target_tds=(8.0, 12.0),
    ),
}


# TDS/EY → 風味的物理相關(跨機制通用,來自萃取研究)。
# 用於正向預測骨架與反向診斷。
def flavor_prior_from_extraction(tds_pct: float | None, ey_pct: float | None) -> Dict[str, str]:
    """回傳由 TDS/EY 推得的風味傾向(定性方向,非數值真值)。"""
    out: Dict[str, str] = {}
    if ey_pct is not None:
        if ey_pct < 18:
            out["under_extraction"] = "尖酸、鹹、收尾水(萃取不足)"
        elif ey_pct > 22:
            out["over_extraction"] = "苦、澀、乾(過萃)"
        else:
            out["extraction"] = "落在黃金杯區間"
    if tds_pct is not None:
        if tds_pct < 1.15:
            out["strength"] = "低 TDS → 甜感、茶感、花香傾向"
        elif tds_pct > 1.45:
            out["strength"] = "高 TDS → 苦、煙燻、烘烤調傾向"
    # 交互
    if tds_pct is not None and ey_pct is not None and tds_pct > 1.45 and ey_pct < 18:
        out["interaction"] = "高 TDS + 低 EY → 尖酸、柑橘酸突出"
    return out


# 診斷:風味偏差 → 該動哪個物理軸(反向映射先驗)。機制相關。
def diagnose_prior(mechanism: BrewMechanism, defect: str) -> list[str]:
    """給定機制與風味偏差,回傳排序的調整建議(物理先驗層)。"""
    p = PRIORS[mechanism]
    d = defect.lower()
    sour = any(k in d for k in ["酸", "sour", "acid", "尖"])
    bitter = any(k in d for k in ["苦", "澀", "bitter", "astringent", "乾"])
    watery = any(k in d for k in ["水", "薄", "weak", "watery", "hollow"])

    tips: list[str] = []
    if sour or watery:  # 多為萃取不足
        if p.grind_to_ey in ("high", "peak"):
            tips.append("研磨調細(增萃)")
        tips.append("提高水溫")
        tips.append("延長接觸時間")
        if mechanism == BrewMechanism.IMMERSION:
            tips = ["延長浸泡時間至接近平衡", "提高粉水比(降稀釋)"]  # 浸泡:研磨/溫度無效
    if bitter:  # 多為過萃
        if p.grind_to_ey in ("high", "peak"):
            tips.append("研磨調粗(降萃)")
        tips.append("降低水溫")
        tips.append("縮短接觸時間")
        if mechanism == BrewMechanism.PRESSURE:
            tips.insert(0, "檢查通道效應 / 重新整粉布粉")
    if not tips:
        tips = ["資訊不足,建議記錄 TDS/EY 後再診斷"]
    return tips


def golden_cup_target(mechanism: BrewMechanism) -> dict:
    p = PRIORS[mechanism]
    return {"target_ey": p.target_ey, "target_tds": p.target_tds, "note": p.note}
