"""檢索與推理 — 分級加權 + 貝氏收縮 + conformal 區間。

簡化但可運作的實作;標 TODO(prod) 處待換生產級(MAPIE/CQR、層級貝氏)。
鐵則:
  - 機制硬過濾在 store.search 已做。
  - A 級定方向;C 級只壓量級(GRADE_WEIGHT)。
  - 鄰居不足 → 退回群組/物理先驗 + 寬區間(防空庫幻覺,設計 §8/§12.4)。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional

GRADE_WEIGHT = {"A": 1.0, "B": 0.4, "C": 0.1, "prediction": 0.0}

MIN_NEIGHBORS = 3          # 少於此 → 收縮力道強 / 退回先驗
MIN_A_WEIGHT_RATIO = 0.30  # top 結果 A 級權重佔比下限,否則降信心(防 C 級洗票)
SHRINK_PRIOR_STRENGTH = 3.0  # 群組先驗的等效樣本數(貝氏收縮)


@dataclass
class Estimate:
    """單一目標(參數軸或風味軸)的估計 + conformal 區間。"""
    value: Optional[float]
    lower: Optional[float]
    upper: Optional[float]
    n_effective: float
    source: str  # "neighbors" | "shrunk" | "prior"


@dataclass
class RetrievalResult:
    estimates: Dict[str, Estimate] = field(default_factory=dict)
    neighbors: List[dict] = field(default_factory=list)
    a_weight_ratio: float = 0.0
    confidence_flag: str = "low"  # low | medium | high
    warnings: List[str] = field(default_factory=list)


def _weight(hit: dict) -> float:
    p = hit["payload"]
    g = GRADE_WEIGHT.get(p.get("grade", "C"), 0.1)
    conf = p.get("confidence", 0.5) or 0.5
    sim = max(hit.get("score", 0.0), 0.0)
    return g * conf * sim


def weighted_estimate(
    hits: List[dict],
    field_key: str,
    prior_value: Optional[float] = None,
) -> Estimate:
    """對某 payload 數值欄做分級加權估計 + 貝氏收縮 + 經驗分位區間。"""
    pairs = [(_weight(h), h["payload"].get(field_key)) for h in hits]
    pairs = [(w, v) for w, v in pairs if v is not None and w > 0]

    if not pairs:
        # 無鄰居 → 純先驗,寬區間
        if prior_value is None:
            return Estimate(None, None, None, 0.0, "prior")
        return Estimate(prior_value, None, None, 0.0, "prior")

    wsum = sum(w for w, _ in pairs)
    mean = sum(w * v for w, v in pairs) / wsum
    n_eff = wsum  # 有效樣本(加權)

    # 層級貝氏收縮:鄰居少時往群組先驗拉(James-Stein 精神)
    # TODO(prod): 以階層常態模型估 shrinkage factor;此處用 n/(n+k) 近似。
    if prior_value is not None:
        k = SHRINK_PRIOR_STRENGTH
        lam = n_eff / (n_eff + k)
        value = lam * mean + (1 - lam) * prior_value
        source = "neighbors" if lam > 0.6 else "shrunk"
    else:
        value = mean
        source = "neighbors"

    # conformal 風格區間:用加權鄰居殘差的經驗分位(簡化)
    # TODO(prod): split-conformal / CQR + SSBC 小樣本修正。
    vals = [v for _, v in pairs]
    if len(vals) >= 2:
        spread = statistics.pstdev(vals) or 0.5
    else:
        spread = 1.0
    # 樣本越少區間越寬(放大係數)
    widen = 1.0 + max(0, MIN_NEIGHBORS - len(vals)) * 0.5
    margin = 1.64 * spread * widen  # ~90% 名目
    return Estimate(round(value, 2), round(value - margin, 2), round(value + margin, 2),
                    round(n_eff, 2), source)


def assess(hits: List[dict]) -> tuple[float, str, List[str]]:
    """評估鄰居品質:A 級權重佔比 → 信心旗標。"""
    warnings: List[str] = []
    if not hits:
        return 0.0, "low", ["無相符鄰居:退回物理先驗,量保守、區間寬。"]
    total = sum(_weight(h) for h in hits) or 1e-9
    a_total = sum(_weight(h) for h in hits if h["payload"].get("grade") == "A")
    ratio = a_total / total

    if len(hits) < MIN_NEIGHBORS:
        warnings.append(f"鄰居過少({len(hits)}<{MIN_NEIGHBORS}):估計向先驗收縮。")
    if ratio < MIN_A_WEIGHT_RATIO:
        warnings.append(f"A 級權重佔比低({ratio:.0%}<{MIN_A_WEIGHT_RATIO:.0%}):方向可信度降低。")

    if len(hits) >= MIN_NEIGHBORS and ratio >= MIN_A_WEIGHT_RATIO:
        flag = "high"
    elif len(hits) >= 2:
        flag = "medium"
    else:
        flag = "low"
    return round(ratio, 3), flag, warnings
