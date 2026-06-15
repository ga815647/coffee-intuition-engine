"""檢索與推理 — 分級加權 + 貝氏收縮 + conformal 區間。

簡化但可運作的實作;標 TODO(prod) 處待換生產級(MAPIE/CQR、層級貝氏)。
鐵則:
  - 機制硬過濾在 store.search 已做。
  - A 級定方向;C 級只壓量級(GRADE_WEIGHT)。
  - 鄰居不足 → 退回群組/物理先驗 + 寬區間(防空庫幻覺,設計 §8/§12.4)。
"""
from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .schema import FLAVOR_AXES

GRADE_WEIGHT = {"A": 1.0, "B": 0.4, "C": 0.1, "prediction": 0.0}

# 0-10 風味軸的 payload 欄名(weighted_estimate 套絕對 margin 地板的對象;見下)。
FLAVOR_FIELD_KEYS = frozenset(f"flavor_{a}" for a in FLAVOR_AXES)
# 0-10 風味軸的 conformal 半寬絕對下限(鐵則 §4:誠實不確定)。近重複鄰居會讓經驗 spread→0,
# 算出假精確的窄區間;對 0-10 軸設地板,保證區間不窄於 ±0.5。參數軸(溫度/比例/研磨,尺度迥異)
# 不套此地板。TODO(ingest S0):有 provenance 後可用「獨立來源數」進一步收緊 / 放寬,此處只先補地板。
MIN_FLAVOR_MARGIN = 0.5
# 有群組先驗但該風味軸無同豆值時的半寬:用先驗點 + 誠實寬區間(冷啟動級,鐵則 §4/§6)。
PRIOR_ONLY_FLAVOR_MARGIN = 2.5

MIN_NEIGHBORS = 3          # 少於此 → 收縮力道強 / 退回先驗
MIN_A_WEIGHT_RATIO = 0.30  # top 結果 A 級權重佔比下限,否則降信心(防 C 級洗票)
MIN_EFFECTIVE_WEIGHT = 1.0  # 聚合有效權重 Σ(grade×conf×sim) 下限;低於此即便鄰居「數量」夠也強制 low
SHRINK_PRIOR_STRENGTH = 3.0  # 群組先驗的等效樣本數(貝氏收縮)
# 機制根層的群組均值有效權重下限:低於此即該機制資料太薄、其均值不可信,GroupPrior.mean 回 None,
# 呼叫端退回物理常數(不讓 1-2 筆退化資料的均值假裝成可信先驗;真語料各機制皆遠超此值)。
MIN_GROUP_WEIGHT = 3.0
# 近常數軸誠實標的「可排序性」門檻 = 機制內分級加權標準差下限(鐵則 §3 方向>絕對 / §4 誠實不確定)。
# **這是離散度 proxy,不是直接量方向準確率**:某軸在該機制內的加權離散度低於此 → 跨樣本差異小於
# 引擎有效解析(與風味軸 conformal 半寬地板 MIN_FLAVOR_MARGIN=0.5 同數量級)→ 任何排序/方向落在
# 雜訊內、**不可靠** → 標 rankable=False、只報量級、不宣稱方向。
# **刻意保守(寧過度抑制,不過度宣稱)**:此 proxy 會連帶標掉「離散低但仍有些微方向訊號」的軸——例如
# sweetness 機制內 wstd 0.56–0.69 被標,但 CV pairwise 方向其實到 0.60–0.67;在 §3/§4 下,對弱訊號軸
# 少宣稱方向(over-suppress)是安全的錯誤方向,勝過對近常數軸假裝有序。真正零訊號的 balance/aftertaste
# (CV 方向 0.50/0.56)才是本旗標核心目標。要更嚴謹可改用直接量的 per-(機制,軸) pairwise 方向準確率
# (eval.run 已分機制/分軸報方向、GroupPrior.axis_stdev 給 wstd,可據此重校門檻);wstd 是其堪用近似。
# 門檻訂 0.75(≈1.5×MIN_FLAVOR_MARGIN,經驗值)在當前語料乾淨切開:標 balance/aftertaste/sweetness +
# immersion/percolation 的 bitterness(機制內 wstd 0.44–0.72),不誤標 acidity/body/clarity 與 pressure 的
# bitterness(0.83–1.42)。⚠ 邊際偏薄(aftertaste/immersion 0.72、body/percolation 0.83 距門檻僅
# 0.03/0.08),語料漂移可能翻轉個別格 → 動語料後須重跑 CV 重校(對應 corpus 測試也須更新)。
# bitterness 跨機制不同判定正是 §1「變異只在機制內算」鐵證。
RANKABLE_STD_MIN = 0.75


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


# ─────────────────── 經驗群組均值先驗(機制分軌;冷啟動/薄證據取代硬編中點) ───────────────────

@dataclass
class _Acc:
    """分級加權累加器:Σw、Σwv、Σwv² → 加權均值與加權標準差。"""
    w: float = 0.0
    wv: float = 0.0
    wvv: float = 0.0   # Σw·v²(第二動差,供加權變異/標準差;見 stdev)

    def add(self, weight: float, value: float) -> None:
        self.w += weight
        self.wv += weight * value
        self.wvv += weight * value * value

    @property
    def mean(self) -> Optional[float]:
        return self.wv / self.w if self.w > 0 else None

    @property
    def stdev(self) -> Optional[float]:
        """分級加權標準差(母體式):√(Σwv²/Σw − 均值²)。w≤0 → None。

        用計算公式 E[v²]−E[v]²;近常數輸入會讓浮點抵消殘留極小正/負值(±~1e-13),
        變異 < 1e-9 一律視為 0(0-10 軸的真實離散遠大於此,絕不誤殺)→ 常數軸吐乾淨 0.0。
        與 mean 同源權重,故不改任何既有均值/估計行為(additive 第二動差)。
        """
        if self.w <= 0:
            return None
        var = self.wvv / self.w - (self.wv / self.w) ** 2
        return math.sqrt(var) if var > 1e-9 else 0.0


class GroupPrior:
    """機制分軌的經驗群組均值先驗(冷啟動/薄證據時取代硬編 ~5 中點,治『先驗錯置中心』)。

    分級加權(GRADE_WEIGHT×confidence,排除 prediction)的軸均值。鐵則:
      - §1 **永不跨機制平均**:層級頂端 = 機制均值,只在同機制內往下分(焙度帶 / 處理法)。
        某機制完全無資料 → 該軸回 None,呼叫端退回物理常數(不借別機制的量級)。
      - §3/§5 只做**量級**聚合(C 級允許壓量級),不碰方向投票;prediction 級不入(權重 0)。
    層級:機制 → 機制×焙度帶 → 機制×焙度帶×處理法。薄群組往父層收縮(James-Stein,
    lam=w/(w+k),k=SHRINK_PRIOR_STRENGTH),樣本足才信具體層、稀疏就靠機制大盤。
    """

    def __init__(self) -> None:
        # key: ("m",mech) / ("mb",mech,band) / ("mbp",mech,band,proc) → {axis: _Acc}
        self._acc: Dict[tuple, Dict[str, _Acc]] = {}

    def _bucket(self, key: tuple) -> Dict[str, _Acc]:
        return self._acc.setdefault(key, {a: _Acc() for a in FLAVOR_AXES})

    @classmethod
    def from_records(cls, records) -> "GroupPrior":
        gp = cls()
        for r in records:
            grade = getattr(r.grade, "value", r.grade)
            w0 = GRADE_WEIGHT.get(grade, 0.0)
            if w0 <= 0:                       # prediction / 未知 → 不入先驗(§5)
                continue
            conf = getattr(r, "confidence", 0.5)
            w = w0 * (conf if conf is not None else 0.5)
            if w <= 0:
                continue
            mech = r.params.brew_mechanism.value
            band = r.bean.roast_band()
            proc = proc_norm(r.bean.process.value if r.bean.process else "")
            keys = [("m", mech), ("mb", mech, band)]
            if proc:
                keys.append(("mbp", mech, band, proc))
            for a in FLAVOR_AXES:
                v = getattr(r.flavor, a)
                if v is None:
                    continue
                for k in keys:
                    gp._bucket(k)[a].add(w, v)
        return gp

    def mean(self, axis: str, mechanism, roast_band: str, process) -> Optional[float]:
        """機制內層級收縮均值;該機制該軸無資料 → None(呼叫端退物理常數)。"""
        mech = getattr(mechanism, "value", mechanism)
        proc = proc_norm(getattr(process, "value", process) or "")
        root = self._acc.get(("m", mech))
        # §1:不跨機制借量級;且機制根層太薄(< MIN_GROUP_WEIGHT)→ 均值不可信,退物理常數。
        if not root or root[axis].w < MIN_GROUP_WEIGHT:
            return None
        est = root[axis].mean
        k = SHRINK_PRIOR_STRENGTH
        mb = self._acc.get(("mb", mech, roast_band))
        if mb and mb[axis].mean is not None:
            acc = mb[axis]
            lam = acc.w / (acc.w + k)
            est = lam * acc.mean + (1 - lam) * est
        if proc:
            mbp = self._acc.get(("mbp", mech, roast_band, proc))
            if mbp and mbp[axis].mean is not None:
                acc = mbp[axis]
                lam = acc.w / (acc.w + k)
                est = lam * acc.mean + (1 - lam) * est
        return est

    def axis_priors(self, mechanism, roast_band: str, process) -> Dict[str, float]:
        """該機制/焙度/處理法有經驗均值的所有風味軸 → {axis: prior}。"""
        out: Dict[str, float] = {}
        for a in FLAVOR_AXES:
            m = self.mean(a, mechanism, roast_band, process)
            if m is not None:
                out[a] = m
        return out

    def axis_stdev(self, axis: str, mechanism) -> Optional[float]:
        """機制根層(§1 **只在機制內**,不跨機制混離散度)該軸的分級加權標準差。

        資料太薄(機制根層該軸 Σw < MIN_GROUP_WEIGHT,與 `mean` 同閘)→ None=無從判定離散度,
        呼叫端不標旗(維持現行為,不亂標)。注意:**刻意只用機制根層 ("m",mech) 桶**,不往焙度/
        處理法子層收縮——可排序性問的是「整個機制下這軸到底有沒有跨樣本的變化」,該用最大樣本的根層
        母體離散度;子層分群會人為縮小組內變異、造成假性『不可排序』。
        """
        mech = getattr(mechanism, "value", mechanism)
        root = self._acc.get(("m", mech))
        if not root or root[axis].w < MIN_GROUP_WEIGHT:
            return None
        return root[axis].stdev

    def rankable(self, axis: str, mechanism) -> Optional[bool]:
        """該軸在此機制內是否離散到足以可靠排序(鐵則 §3 方向>絕對 / §4 誠實不確定)。

        None = 資料太薄、無從判定(不標旗);否則 機制內加權標準差 ≥ RANKABLE_STD_MIN。
        低於門檻 = 近常數軸:點估/區間仍可報量級水平,但**方向/排序落在雜訊內、不可宣稱**。
        """
        s = self.axis_stdev(axis, mechanism)
        return None if s is None else s >= RANKABLE_STD_MIN


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
        # 有群組先驗:風味軸給先驗點 + 誠實寬區間(clamp 0-10);參數軸尺度迥異,不套此地板。
        if field_key in FLAVOR_FIELD_KEYS:
            m = PRIOR_ONLY_FLAVOR_MARGIN
            lo = round(max(0.0, prior_value - m), 2)
            hi = round(min(10.0, prior_value + m), 2)
            return Estimate(round(prior_value, 2), lo, hi, 0.0, "prior")
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
    lo = value - margin
    hi = value + margin
    # 0-10 風味軸的絕對 margin 地板:近重複鄰居 spread→0 不得造出假精確窄區間(鐵則 §4)。
    if field_key in FLAVOR_FIELD_KEYS:
        margin = max(margin, MIN_FLAVOR_MARGIN)
        lo = value - margin
        hi = value + margin
        # 夾回 [0,10] 軸定義域:真值必落此域,夾掉外側多餘區間=純送資訊量、絕不漏掉任何域內真值
        # → 覆蓋率單調不降(鐵則 §4)。prior-only 與 physics 粗略路徑已夾,此為主鄰居路徑原本的漏夾。
        # **僅限風味軸**——參數軸(溫度/比例/研磨/接觸時間)尺度迥異、不在 FLAVOR_FIELD_KEYS,絕不夾。
        lo = max(0.0, lo)
        hi = min(10.0, hi)
    return Estimate(round(value, 2), round(lo, 2), round(hi, 2),
                    round(n_eff, 2), source)


def assess(hits: List[dict]) -> tuple[float, str, List[str]]:
    """評估鄰居品質 → 信心旗標。兩道誠實閘:

    1. **A 級權重佔比** ≥ `MIN_A_WEIGHT_RATIO`(30%)才可能 high(防 C 級量大洗票方向)。
    2. **聚合有效權重** Σ(grade×conf×sim) ≥ `MIN_EFFECTIVE_WEIGHT`(1.0),否則即便鄰居
       「數量」湊夠也**強制 low**——數量夠但有效樣本趨零的假 medium 不算有把握(誠實不確定,
       鐵則 §4;不動 GRADE_WEIGHT,只在 count 旗標上加有效樣本地板)。
    """
    warnings: List[str] = []
    if not hits:
        return 0.0, "low", ["無相符鄰居:退回物理先驗,量保守、區間寬。"]
    eff = sum(_weight(h) for h in hits)        # 聚合有效權重(非鄰居計數)
    total = eff or 1e-9
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
    # 有效樣本過小(n_eff<1):聚合有效權重趨零 → 強制 low(覆蓋 count 給的 medium)。
    # high 門檻(A 級佔比 ≥30%)本身不變;這是其上的有效樣本地板,殺「數量湊夠但訊號趨零」的假信心。
    if eff < MIN_EFFECTIVE_WEIGHT:
        if flag != "low":
            warnings.append(
                f"有效樣本過小(n_eff<1,Σ權重={eff:.2f}<{MIN_EFFECTIVE_WEIGHT:.1f}):退回低信心。"
            )
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
    payload: Optional[dict], *, strict_variety: bool = False,
) -> Tuple[bool, str]:
    """同豆閘(§3.2):origin 主產地 token **且** variety **且** process 皆符才算同豆。

    **origin = 身分錨點**:雙方都要有主產地 token 且相等才可能同豆——缺 origin 的「泛用沖煮知識」
    料(無特定豆)不是「這支豆」,只能進 recommend 大方向(全鄰居),永不定義某豆的風味特色
    (對齊決策 1;否則 61 筆 blank-origin 料會變成所有豆的萬用風味捐贈者=破鐵則)。
    **variety / process = 子屬性**:預設沿用「任一方未指定 = 該欄放行」的寬鬆(specificity 降 'low'),
    讓只給部分資訊的查詢/料仍能在同產地內對上;雙方皆具體且符 = 'high'。

    **`strict_variety`(風味同豆閘專用,§3.2)**:風味「這隻豆的特色」越特異越不可借鄰居——查詢
    **指名了 variety**(如耶加藝妓)時,**鄰居 variety 空白 → 不算同豆風味**(空白單元錨點是該產地
    泛用基準,不是某特定品種的真值;藝妓≠一般耶加)。只收緊 variety 這條特異度軸;process 維持
    寬鬆。查詢未指名 variety 時不受影響(空白錨點仍是合法同產地基準)。回傳 (是否同豆, specificity)。
    """
    p = payload or {}
    qo, ho = origin_main_token(q_origin), origin_main_token(p.get("origin"))
    if not qo or not ho or qo != ho:   # origin 無法正向確認相同 → 非同豆
        return False, "low"
    qv, hv = _norm(q_variety), _norm(p.get("variety"))
    # 風味嚴格化:查詢指名品種而鄰居空白 → 非同豆(泛用錨點不得當特異品種風味真值)。
    if strict_variety and qv and not hv:
        return False, "low"
    specificity = "high"
    for q, h in ((qv, hv), (proc_norm(q_process), proc_norm(p.get("process")))):
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

    一致性(§3.2):此處與風味同豆閘(`_same_bean`)**共用同一述詞**(`strict_variety=True`)——
    被嚴格化踢出風味的空白單元錨點(查詢指名品種、鄰居 variety 空白)會落進此池當同產地 reputed
    參考,不致兩邊都漏接而消失(現況錨點皆 C、本就經 grade==C 進池;共用述詞以防日後 B 錨點漏接)。
    """
    q_origin, q_variety, q_process = _bean_fields(query_bean)

    bean_match_any = False
    pool: List[dict] = []
    for h in hits:
        p = h.get("payload") or {}
        bm, _ = bean_match(q_origin, q_variety, q_process, p, strict_variety=True)
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
