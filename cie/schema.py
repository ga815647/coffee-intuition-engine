"""核心資料模型(v0.2)。

三個空間 + 條件層:
  L1 BeanRoast     豆/焙條件(移動映射)
  WaterProfile     水質(控制變數,不進風味因果)
  L2 BrewParams    沖煮物理參數(brew_mechanism 為硬分區鍵)
  L3 FlavorProfile 杯測量化風味

Record = 一筆完整校準經驗,帶來源分級。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional
from uuid import uuid4

# 數值風味軸名(供向量化 / 收縮)。模組級常數,避免 pydantic 將其誤判為欄位。
FLAVOR_AXES = ("acidity", "sweetness", "bitterness", "body", "aftertaste", "balance", "clarity")

from pydantic import BaseModel, Field, field_validator


# ────────────────────────────── 列舉 ──────────────────────────────

class BrewMechanism(str, Enum):
    """萃取機制 — 映射的硬分區鍵。三軌不可互通(設計 §12.1)。"""
    IMMERSION = "immersion"      # 全浸泡:趨平衡,E 對研磨/溫度/攪拌不敏感
    PERCOLATION = "percolation"  # 滴濾/注水:非平衡流動,E 對研磨/流速極敏感
    PRESSURE = "pressure"        # 義式加壓:E 對研磨呈峰值,通道效應主導重現性


class Process(str, Enum):
    WASHED = "washed"
    NATURAL = "natural"
    HONEY = "honey"
    ANAEROBIC = "anaerobic"
    OTHER = "other"


class Grade(str, Enum):
    """校準品質分級(不是名氣,是標籤可信度;設計 §3)。"""
    A = "A"  # 閉環、標準化協定(SCA 杯測/競賽/明確方法/自己的精確校正)→ 定方向與錨點
    B = "B"  # 有對照、具體但單人主觀 → 補充
    C = "C"  # 社群海量、標籤不一致、開環 → 只壓雜訊、估量級
    PREDICTION = "prediction"  # 引擎自身預測,禁止當校準、禁止進方向投票(防 model collapse §12)


class AcidityType(str, Enum):
    CITRIC = "citric"
    MALIC = "malic"
    ACETIC = "acetic"
    LACTIC = "lactic"
    MIXED = "mixed"
    NONE = "none"


# ────────────────────────────── L1 豆/焙 ──────────────────────────────

class BeanRoast(BaseModel):
    """起始物料:決定映射落在哪。"""
    origin: str = ""
    variety: str = ""
    process: Process = Process.OTHER
    roast_agtron: Optional[float] = Field(None, description="Agtron 數值;越低越深")
    dtr: Optional[float] = Field(None, ge=0, le=1, description="發展時間比 development time ratio")
    days_off_roast: Optional[int] = Field(None, ge=0)
    density: Optional[float] = None
    moisture: Optional[float] = None

    def roast_band(self) -> str:
        """粗分焙度帶,供群組先驗收縮用。"""
        a = self.roast_agtron
        if a is None:
            return "unknown"
        if a >= 70:
            return "light"
        if a >= 55:
            return "medium"
        return "dark"


# ────────────────────────────── 水質(控制變數) ──────────────────────────────

class WaterProfile(BaseModel):
    """水質。鐵則:只作分群/控制變數,不進風味因果(設計 §12.2)。

    通俗口訣「鎂=明亮、鈣=body」有同儕審查反證(Bratthäll et al.),
    故系統不得把水→風味因果寫死。記錄 Ca:Mg 比是因為同 GH/KH 下它仍改風味,
    但這是『需控制』而非『可推因果』。
    """
    gh: Optional[float] = Field(None, description="總硬度 mg/L CaCO3;SCA 目標 68(17-85)")
    kh: Optional[float] = Field(None, description="鹼度 mg/L CaCO3;SCA 目標 ~40")
    tds_water: Optional[float] = Field(None, description="水 TDS ppm;SCA 目標 150(75-250)")
    ph: Optional[float] = Field(None, ge=0, le=14)
    ca_mg_ratio: Optional[float] = Field(None, description="鈣:鎂 比(額外自由度)")
    recipe_name: str = Field("", description="水配方名,作為批次常數標籤")


# ────────────────────────────── L2 參數(物理軸) ──────────────────────────────

class BrewParams(BaseModel):
    """沖煮物理參數。brew_mechanism 為必填硬分區鍵。"""
    brew_mechanism: BrewMechanism
    method: str = Field("", description="具體泡法名,如 V60 / AeroPress;僅標籤,推理走物理軸")

    water_temp_c: Optional[float] = Field(None, ge=70, le=100)
    brew_ratio: Optional[float] = Field(None, gt=0, description="水:粉,例 16 表 1:16")
    grind_um: Optional[float] = Field(None, gt=0, description="研磨中位粒徑(或刻度映射)")
    grinder: str = Field("", description="磨豆機型號,如 1Zpresso ZP6")
    contact_time_s: Optional[float] = Field(None, ge=0)
    agitation_level: Optional[int] = Field(None, ge=0, le=5)
    pressure_bar: Optional[float] = Field(None, ge=0, description="義式適用")

    # 派生(萃取樞紐)
    tds_pct: Optional[float] = Field(None, ge=0, le=20)
    ey_pct: Optional[float] = Field(None, ge=0, le=30, description="萃取率 extraction yield")


# ────────────────────────────── L3 風味(杯測量化) ──────────────────────────────

class FlavorProfile(BaseModel):
    """杯測量化風味。0-10 軸 + 風味/缺陷標籤。"""
    acidity: Optional[float] = Field(None, ge=0, le=10)
    acidity_type: AcidityType = AcidityType.NONE
    sweetness: Optional[float] = Field(None, ge=0, le=10)
    bitterness: Optional[float] = Field(None, ge=0, le=10)
    body: Optional[float] = Field(None, ge=0, le=10)
    aftertaste: Optional[float] = Field(None, ge=0, le=10)
    balance: Optional[float] = Field(None, ge=0, le=10)
    clarity: Optional[float] = Field(None, ge=0, le=10)
    flavor_notes: List[str] = Field(default_factory=list)
    defects: List[str] = Field(default_factory=list)

    def axis_vector(self) -> dict:
        """回傳有值的數值軸 {name: value}。"""
        return {a: getattr(self, a) for a in FLAVOR_AXES if getattr(self, a) is not None}


# ────────────────────────────── Record ──────────────────────────────

class Record(BaseModel):
    """一筆完整校準經驗。"""
    id: str = Field(default_factory=lambda: str(uuid4()))
    bean: BeanRoast = Field(default_factory=BeanRoast)
    water: WaterProfile = Field(default_factory=WaterProfile)
    params: BrewParams
    flavor: FlavorProfile = Field(default_factory=FlavorProfile)

    grade: Grade = Grade.C
    protocol: str = Field("", description="標籤產生協定,如 SCA_cupping")
    source: str = ""
    confidence: float = Field(0.5, ge=0, le=1)
    user_id: str = Field("self", description="self=個人偏好層;global=客觀因果層")
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    embedding_text: str = Field("", description="情境文字,供(重)建嵌入與模糊召回")

    @field_validator("grade")
    @classmethod
    def _prediction_needs_human(cls, v: Grade) -> Grade:
        # 規範提醒:A 級寫入須人類感官真值;此處僅型別層,實際門檻在 engine.log_calibration。
        return v

    def mechanism(self) -> BrewMechanism:
        return self.params.brew_mechanism

    def build_embedding_text(self) -> str:
        """把情境組成標準化文字(豆 + 風味敘述),供語意召回。
        數值不靠嵌入理解 — 數值走 payload 過濾與物理距離(設計 §4.1)。
        """
        if self.embedding_text:
            return self.embedding_text
        b, f = self.bean, self.flavor
        parts = [
            b.roast_band(), b.process.value, b.origin, b.variety,
            self.params.brew_mechanism.value,
            f.acidity_type.value if f.acidity_type != AcidityType.NONE else "",
            " ".join(f.flavor_notes),
            " ".join(f.defects),
        ]
        return " ".join(p for p in parts if p).strip()
