"""物理先驗 — 按萃取機制三軌分立(設計 §12.1)。

每軸記錄各物理變數對「萃取率 E」的敏感度方向與強度,以及 TDS/EY→風味的相關。
這些是『方向可信、量保守』的先驗骨架;真值由校準資料收斂。

來源:UC Davis/Nature 2021(浸泡平衡)、Coffee ad Astra / Hoffmann(滴濾流動)、
Matter 2020(義式力學,研磨→E 非單調)。詳見 design §2.2 / §12.1。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

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


# 無同豆校準時的『物理粗略』軸量級(§3.2 / §3.4:generic 大方向風味,非精確真值)。
# 焙度帶/萃取的『確立方向』給保守 0-10 錨點;呼叫端標 source='prior',並附 warning
# 「物理粗略、非實測」。特色(具體風味詞)永遠交給 social_tendency,不在此。
_COARSE_BASE = {"acidity": 5.0, "sweetness": 5.5, "bitterness": 4.0, "body": 5.0,
                "aftertaste": 5.0, "balance": 5.5, "clarity": 5.5}

# 冷啟動 0-10 軸的保守半寬(物理先驗導出的『誠實寬區間』,鐵則 §4 不給假精確 / §6 退先驗要寬)。
# 焙度帶與萃取皆未知時資訊近零 → 再放寬一級。刻意寬:寧可區間誠實過寬,不可假精確。
COARSE_MARGIN = 2.5
COARSE_MARGIN_NO_INFO = 3.0


def coarse_flavor_axes(bean, params: BrewParams,
                       group_prior=None) -> Dict[str, Tuple[float, float, float]]:
    """無同豆鄰居時的風味軸『量級 + 保守寬區間』(0-10、coarse、非真值)。

    軸中心優先用 `group_prior`(機制分軌的經驗群組均值,§1 不跨機制;治『硬編 ~5 中點系統性偏低』)
    ——某軸無群組資料才退回物理常數 `_COARSE_BASE` + 焙度方向。焙度方向**只**對「退物理常數」的軸施加
    (有群組均值者已含焙度帶分層,不重複計入);EY 萃取方向跨機制通用、群組均值未條件於 EY,故一律可加。

    回傳 `{axis: (value, lower, upper)}`,`[lower, upper]` 為由先驗導出的**保守寬區間**(半寬
    `COARSE_MARGIN`;完全退物理且焙度帶與 EY 皆未知 → 資訊近零,加寬到 `COARSE_MARGIN_NO_INFO`),
    clamp 在 0-10。呼叫端標 source='prior'。鐵則:冷啟動不給假精確點值(§4),退先驗一律附寬區間(§6)。
    """
    band = bean.roast_band() if hasattr(bean, "roast_band") else "unknown"
    ey = params.ey_pct
    proc = bean.process.value if getattr(bean, "process", None) else ""
    emp = (group_prior.axis_priors(params.brew_mechanism, band, proc)
           if group_prior is not None else {})

    axes: Dict[str, float] = {}
    have_emp: Dict[str, bool] = {}
    for a, base in _COARSE_BASE.items():
        if a in emp:
            axes[a] = emp[a]; have_emp[a] = True       # 經驗群組均值(機制分軌)
        else:
            axes[a] = base; have_emp[a] = False         # 退物理常數中點

    def _roast(a: str, delta: float) -> None:
        if not have_emp.get(a):                          # 只對退物理常數的軸施加焙度方向
            axes[a] += delta
    if band == "light":
        _roast("acidity", 1.5); _roast("clarity", 1.0)
        _roast("bitterness", -1.0); _roast("body", -1.0)
    elif band == "dark":
        _roast("acidity", -1.5); _roast("clarity", -1.0)
        _roast("bitterness", 1.5); _roast("body", 1.5)
    if ey is not None:                                   # 萃取方向:群組均值未含 EY,故一律可加
        if ey < 18:
            axes["acidity"] += 1.0; axes["sweetness"] -= 0.5; axes["balance"] -= 0.5
        elif ey > 22:
            axes["bitterness"] += 1.0; axes["clarity"] -= 0.5; axes["balance"] -= 0.5
    # 帶寬:有任一經驗群組均值(站得較穩)用 COARSE_MARGIN;完全退物理且焙度+EY 皆未知 → 最寬。
    any_emp = any(have_emp.values())
    margin = (COARSE_MARGIN_NO_INFO if (not any_emp and band == "unknown" and ey is None)
              else COARSE_MARGIN)
    out: Dict[str, Tuple[float, float, float]] = {}
    for a, v in axes.items():
        v = round(min(10.0, max(0.0, v)), 1)
        lo = round(max(0.0, v - margin), 1)
        hi = round(min(10.0, v + margin), 1)
        out[a] = (v, lo, hi)
    return out


# ────────────────────────────── 偏酸 fix 方向:已知爭議 ──────────────────────────────
# 鐵則:這裡**不選邊**。傳統「酸=萃取不足→增萃(磨細/升溫/延長)」是 working prior
# (跨來源 convergent + 三軌物理先驗);UC Davis Coffee Center 感官研究(Frost / Batali /
# Cotter / Ristenpart / Guinard)提出的「第二訊號」其實要**把濃度軸與萃取軸拆開看**——
# 這是本議題最常被混為一談、也最關鍵的一刀:
#   • 穩健那一半 = **濃度(TDS)軸**:知覺酸度主要由濃度驅動;可滴定酸度(sour 的化學基礎)
#     與 TDS **線性正相關**、與 EY(萃取率/PE)**幾乎無關**;知覺 sour 追隨可滴定酸度而非 pH。
#     ⇒ 加水/降 TDS 會**真的降低知覺酸度**(全因子 RSM 中 sour 是所有描述項裡擺幅最大的,
#     高 TDS/低 EY 角最酸、低 TDS/高 EY 角最不酸,~20 分/百分制)。
#   • 被誤掛、較弱那一半 = 「**多萃(↑EY)就降酸**」:在 drip,提高 EY 其實讓 sour **微降**
#     (與傳統同向但弱),但這常被誤講成反向。真正的陷阱是:percolation 裡「磨細/升溫/延長」
#     會**同時拉高 EY 與 TDS**——EY↑ 弱降酸、TDS↑ 升酸,兩者方向相反、淨效不定,故「一味增萃」
#     不保證降酸,甚至可能因濃度上升而更酸。且 EY→酸度的**符號隨機制翻面**(drip:EY↑ 降 sour;
#     immersion:延長萃取反而升酸)——正是機制三軌硬隔離為何必要。
# 證據量級:UC Davis 跨 4+ 篇同行評審、內部高度一致、附開放資料(Dryad),遠強過社群口耳(C);
# 但仍是**單一實驗室/單一受訓 panel(~12 人)/drip 限定/特定豆焙與區間/無第三方獨立複現**,
# 且不可外推到 immersion/pressure → **B 級第二訊號**,非可外推的 A 級定律,**不覆蓋** working
# prior;grade 只影響第二訊號權重,不改「爭議 + 待 A/B」的結論。唯一 A 級裁決 = 使用者自己的
# 閉環 A/B(具 protocol 的閉環真值,如 SCA_cupping)。對齊前車之鑑:convergent 共識不等於對
# (系統打臉過「鎂=明亮」),但單一來源也不得翻 CIE——故維持 open。詳見 DESIGN §3 / §12.2。

# 第二訊號的來源標記(與 Phase 3 寫入 D1 的 global 知識條目共用,確保 code 與 data 一致)。
CONTESTED_ACIDITY_GRADE = "B"
CONTESTED_ACIDITY_PROTOCOL = "study:UCDavis_CoffeeCenter_TDS_vs_EY_drip_sensory"
CONTESTED_ACIDITY_SOURCE = (
    "UC Davis Coffee Center / Coffee Science Foundation drip 感官研究(Frost / Batali / "
    "Cotter / Ristenpart / Guinard)。主證:Batali et al. 2021,『Titratable Acidity, "
    "Perceived Sourness, and Liking of Acidity in Drip Brewed Coffee』,ACS Food Sci. "
    "Technol. 1(4):559-569,doi:10.1021/acsfoodscitech.0c00078(知覺酸度隨 TDS、可滴定酸度與 "
    "TDS 線性而與 EY 無關);反應曲面:Batali et al. 2020,Sci. Reports 10:16450,"
    "doi:10.1038/s41598-020-73341-4(sour 擺幅最大,高 TDS/低 EY 最酸);消費者資料集:"
    "Dryad doi:10.25338/B8993H。限定:單一實驗室、drip、無獨立複現 → B 級,不可跨機制外推。"
)

# 偏酸缺陷關鍵詞(只對『酸』類觸發爭議旗標;水感/薄不在此議題)。
SOUR_DEFECT_KEYS = ("酸", "sour", "acid", "尖")


def _sour_ab_test(mechanism: BrewMechanism) -> str:
    """機制相應的閉環 A/B:同豆兩杯,一杯往增萃、一杯往降濃度,盲喝比酸度,舌頭裁決。"""
    if mechanism == BrewMechanism.IMMERSION:
        return ("閉環 A/B(同豆同批):A 杯延長浸泡 / 提高粉水比(增萃方向);"
                "B 杯沖完加水稀釋一成(降濃度方向)。盲喝比酸度,以你的舌頭裁決哪邊降酸。")
    if mechanism == BrewMechanism.PRESSURE:
        return ("閉環 A/B(同豆):A 杯磨細 / 拉長萃取(增萃方向,留意通道效應);"
                "B 杯萃取後加水稀釋成 Americano(降濃度方向)。盲喝比酸度。")
    return ("閉環 A/B(同豆同批):A 杯磨細一階(增萃方向);"
            "B 杯維持研磨、沖完加水稀釋一成(降濃度方向)。盲喝比酸度,以你的舌頭裁決。")


def contested_diagnosis(mechanism: BrewMechanism, defect: str) -> dict | None:
    """偏酸 → 回傳『兩方向並陳 + 寬區間 + 低信心 + 閉環 A/B 旗標』的爭議結構;非偏酸回 None。

    刻意**不選邊**:working prior(增萃)與 second signal(降濃度,Cotter B 級)並列,
    結論維持 open(待使用者閉環 A/B)。回傳 JSON-ready dict,供 engine.diagnose 直接掛上、
    經 MCP 原樣輸出。
    """
    d = (defect or "").lower()
    if not any(k in d for k in SOUR_DEFECT_KEYS):
        return None
    working_adjustments = diagnose_prior(mechanism, defect)  # 既有增萃方向 = working prior
    return {
        "topic": "偏酸的 fix 方向",
        "question": ("偏酸該往『增萃(磨細/升溫/延長)』還是『降濃度(加水/降 TDS)』?"
                     "兩個先驗分歧、未定論,系統不替你選邊。"),
        "directions": [
            {
                "stance": "working_prior",
                "direction": "增萃降酸:把酸視為萃取不足,往增萃方向走(磨細/升溫/延長接觸/提高粉水比)。",
                "adjustments": working_adjustments,
                "grade": "working_prior",
                "basis": ("跨來源 convergent(SCA 沖煮控制表、主流 barista 教學、風味矩陣、"
                          "舊版 Aiden、一般常識)+ 三軌物理先驗;作為起手 working prior。"),
            },
            {
                "stance": "second_signal",
                "direction": ("拆開濃度軸與萃取軸看(本議題關鍵):①【穩健】降濃度降酸——加水 / 降"
                              " TDS 會真的降低知覺酸度(知覺 sour 追隨可滴定酸度,後者與 TDS 線性、"
                              "與 EY 幾乎無關)。②【較弱/常被誤掛】『多萃就降酸』不可靠——drip 提高"
                              " EY 只讓酸**微降**;且 percolation 裡『磨細/升溫/延長』會同時拉高 EY"
                              "(弱降酸)與 TDS(升酸),兩者反向、淨效不定,一味增萃不保證降酸、"
                              "甚至可能因濃度上升而更酸。EY→酸度符號還隨機制翻面(immersion 相反)。"),
                "adjustments": ["先判斷是『濃度(TDS)』還是『萃取(EY)』在主導酸感,再決定方向",
                                "想降酸最穩的一手:沖完加水 / 降低沖煮濃度(↓TDS)",
                                "別預設『一味增萃就會降酸』——增萃常同時升 TDS,可能反而更酸"],
                "grade": CONTESTED_ACIDITY_GRADE,  # B:第二訊號,不覆蓋 working prior
                "basis": ("UC Davis Coffee Center drip 感官研究——把濃度(TDS)軸與萃取(EY)軸分離:"
                          "知覺酸度主要由 TDS 驅動、與 EY 關係弱(且隨機制變號)。單一實驗室、drip 限定、"
                          "特定豆/焙/族群/區間的描述性感官迴歸、無獨立複現 → B 級第二訊號,非可外推的"
                          " A 級定律;不覆蓋 working prior,亦不可跨機制外推。"),
                "protocol": CONTESTED_ACIDITY_PROTOCOL,
                "source": CONTESTED_ACIDITY_SOURCE,
            },
        ],
        "confidence": "low",
        "interval_note": "此議題證據分歧:視為寬區間 / 低信心 open question,不給單一有把握方向。",
        "needs_ab_test": _sour_ab_test(mechanism),
        "resolution": ("唯一 A 級裁決 = 你自己的閉環 A/B 結果(先進 self;跨人成立才晉升 global)。"
                       "在你給出閉環真值前,系統維持『兩方向並陳 + 寬區間 + 低信心』的 open 狀態。"),
        "note": ("鐵則:單一來源(含 Cotter)不得翻 CIE;convergent 傳統共識也不等於對"
                 "(參『鎂=明亮』前車之鑑)。故維持 open,等你的舌頭裁決。"),
    }
