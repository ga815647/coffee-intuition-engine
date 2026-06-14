"""檢索與推理 — 分級加權 + 貝氏收縮 + conformal 區間。

簡化但可運作的實作;標 TODO(prod) 處待換生產級(MAPIE/CQR、層級貝氏)。
鐵則:
  - 機制硬過濾在 store.search 已做。
  - A 級定方向;C 級只壓量級(GRADE_WEIGHT)。
  - 鄰居不足 → 退回群組/物理先驗 + 寬區間(防空庫幻覺,設計 §8/§12.4)。
"""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .schema import FLAVOR_AXES

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


# ─────────────────── 同豆閘 + 社群傾向(召回範圍依特異度分流;§3.2 / §3.3 / §16.4) ───────────────────
# 鐵則:風味「這隻豆的特色」只信同豆(bean_match=True);跨豆(含 A/B)與 C 級的風味永不寫進
# predicted_flavor 特色,只降級進 social_tendency(reputed、低信心、不流入客觀估計、不呼叫
# weighted_estimate)。沖煮「大方向」(起手參數 / 過酸苦診斷)則可借廣鄰居(物理可遷移)。

# origin 主產地 token 的通用詞(去除後取第一個有意義 token:kenya / yirgacheffe / panama)。
_ORIGIN_STOPWORDS = frozenset({"blend", "single", "origin", "coffee", "beans", "bean"})

# social_tendency 各軸傾向帶(§3.3:low<4 / med 4–6.5 / high>6.5)。
_BAND_LOW, _BAND_HIGH = 4.0, 6.5


def _norm(s: Optional[str]) -> str:
    """小寫、去前後空白、壓多空白。"""
    return " ".join((s or "").lower().split())


def origin_main_token(origin: Optional[str]) -> str:
    """主產地 token:小寫去通用詞後第一個有意義 token(`Ethiopia Yirgacheffe`→`ethiopia`)。"""
    for t in _norm(origin).replace(",", " ").split():
        if t not in _ORIGIN_STOPWORDS:
            return t
    return ""


def proc_norm(process: Optional[str]) -> str:
    """處理法正規化;空字串 / `other` 視為未指定(放行)。"""
    p = _norm(process)
    return "" if p in ("", "other") else p


def _band(v: float) -> str:
    if v < _BAND_LOW:
        return "low"
    if v <= _BAND_HIGH:
        return "med"
    return "high"


def bean_match(
    q_origin: Optional[str], q_variety: Optional[str], q_process: Optional[str],
    payload: Optional[dict],
) -> Tuple[bool, str]:
    """同豆閘(§3.2):origin 主產地 token **且** variety **且** process 皆符才算同豆。

    **origin = 身分錨點**:雙方都要有主產地 token 且相等才可能同豆——缺 origin 的「泛用沖煮知識」
    料(無特定豆)不是「這支豆」,只能進 recommend 大方向(全鄰居),永不定義某豆的風味特色
    (對齊決策 1;否則 61 筆 blank-origin 料會變成所有豆的萬用風味捐贈者=破鐵則)。
    **variety / process = 子屬性**:沿用「任一方未指定 = 該欄放行」的寬鬆(specificity 降 'low'),
    讓只給部分資訊的查詢/料仍能在同產地內對上;雙方皆具體且符 = 'high'。回傳 (是否同豆, specificity)。
    """
    p = payload or {}
    qo, ho = origin_main_token(q_origin), origin_main_token(p.get("origin"))
    if not qo or not ho or qo != ho:   # origin 無法正向確認相同 → 非同豆
        return False, "low"
    specificity = "high"
    for q, h in ((_norm(q_variety), _norm(p.get("variety"))),
                 (proc_norm(q_process), proc_norm(p.get("process")))):
        if not q or not h:             # 子屬性任一方未指定 → 放行,特異度降 low
            specificity = "low"
            continue
        if q != h:                     # 兩方都具體且不同 → 非同豆
            return False, specificity
    return True, specificity


def _bean_fields(query_bean) -> Tuple[str, str, str]:
    """從 BeanRoast(或鴨子型別)取出 (origin, variety, process 字串)。"""
    origin = getattr(query_bean, "origin", "") or ""
    variety = getattr(query_bean, "variety", "") or ""
    proc = getattr(query_bean, "process", "")
    proc = getattr(proc, "value", proc) or ""  # Process enum 或純字串皆可
    return origin, variety, proc


def social_tendency(hits: List[dict], query_bean, top_notes: int = 6) -> Optional[dict]:
    """跨豆 / 社群風味傾向(§3.3 / §16.4)——**additive、reputed、低信心**,與客觀估計並列。

    取「被 §3.2 同豆閘排除於 flavor 主估計」的 hits:`bean_match==False`(任一級)**或** grade==C。
    無可參考 → None。**永不呼叫 weighted_estimate、不流進 predicted_flavor 特色。**
    """
    q_origin, q_variety, q_process = _bean_fields(query_bean)

    bean_match_any = False
    pool: List[dict] = []
    for h in hits:
        p = h.get("payload") or {}
        bm, _ = bean_match(q_origin, q_variety, q_process, p)
        if bm:
            bean_match_any = True
        if (not bm) or p.get("grade") == "C":   # 跨豆(任一級)或 C → 降級進社群傾向池
            pool.append(h)
    if not pool:
        return None

    notes_counter: Counter = Counter()
    grades_counter: Counter = Counter()
    origins: List[str] = []
    varieties: List[str] = []
    axis_vals: Dict[str, List[float]] = {a: [] for a in FLAVOR_AXES}
    for h in pool:
        p = h.get("payload") or {}
        for n in (p.get("flavor_notes") or []):
            if n:
                notes_counter[n] += 1
        g = p.get("grade")
        if g:
            grades_counter[g] += 1
        o = p.get("origin")
        if o and o not in origins:
            origins.append(o)
        v = p.get("variety")
        if v and v not in varieties:
            varieties.append(v)
        for a in FLAVOR_AXES:
            val = p.get(f"flavor_{a}")
            if val is not None:
                axis_vals[a].append(val)

    axis_tendency: Dict[str, dict] = {}
    for a, vals in axis_vals.items():
        if vals:
            m = sum(vals) / len(vals)
            axis_tendency[a] = {"band": _band(m), "mean": round(m, 2)}  # reputed 均值,非主估計

    return {
        "reputed": True,
        "confidence": "low",
        "based_on_n": len(pool),
        "grades": dict(grades_counter),
        "origins": origins,
        "varieties": varieties,
        "bean_match_any": bean_match_any,
        "flavor_notes": [n for n, _ in notes_counter.most_common(top_notes)],
        "axis_tendency": axis_tendency,
        "note": "跨豆/社群參考、非本豆實測;社群(C)項另帶發表偏差(難喝的沒人貼)。",
    }
