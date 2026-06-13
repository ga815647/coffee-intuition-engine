"""高階引擎:recommend / predict / diagnose / method_swap。

把 store(召回)+ retrieval(加權收縮區間)+ physics(機制先驗)組裝起來。
所有輸出都帶 evidence 與 warnings,維持可解釋與防幻覺。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from . import physics
from .canonical import CanonicalStore, maybe_get_canonical
from .retrieval import RetrievalResult, assess, weighted_estimate
from .schema import (
    FLAVOR_AXES, BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Record,
)
from .store import StoreBackend, get_store

PARAM_TARGETS = ["water_temp_c", "brew_ratio", "grind_um", "contact_time_s"]


class Engine:
    def __init__(self, store: Optional[StoreBackend] = None,
                 canonical: Optional[CanonicalStore] = None):
        self.store = store or get_store()
        # canonical 真相 sink:僅當後端無法自存(Vectorize)時啟用,避免記憶體 /
        # Qdrant 的重複寫與測試副作用。可由呼叫端顯式注入(測試 / R2)。
        self.canonical = canonical if canonical is not None else maybe_get_canonical(self.store)

    # ── 召回 ──
    def _recall(self, bean: BeanRoast, mechanism: BrewMechanism, flavor: FlavorProfile,
                top_k: int = 20) -> List[dict]:
        query_text = Record(
            bean=bean, params=BrewParams(brew_mechanism=mechanism), flavor=flavor
        ).build_embedding_text()
        return self.store.search(
            query_text=query_text, mechanism=mechanism, top_k=top_k,
            process=bean.process.value if bean.process else None,
            roast_band=bean.roast_band() if bean.roast_band() != "unknown" else None,
            exclude_predictions=True,
        )

    # ── 推薦起手參數 ──
    def recommend(self, bean: BeanRoast, mechanism: BrewMechanism,
                  target_flavor: Optional[FlavorProfile] = None) -> dict:
        hits = self._recall(bean, mechanism, target_flavor or FlavorProfile())
        ratio, flag, warnings = assess(hits)
        gc = physics.golden_cup_target(mechanism)

        params: Dict[str, dict] = {}
        for key in PARAM_TARGETS:
            est = weighted_estimate(hits, key, prior_value=None)
            params[key] = est.__dict__
        # EY 目標來自物理先驗
        params["target_ey_pct"] = {"value": sum(gc["target_ey"]) / 2,
                                   "range": gc["target_ey"], "source": "prior"}

        return {
            "mode": "recommend",
            "mechanism": mechanism.value,
            "suggested_params": params,
            "physics_note": gc["note"],
            "confidence_flag": flag,
            "a_weight_ratio": ratio,
            "evidence": self._evidence(hits),
            "warnings": warnings + self._sparse_warning(hits),
        }

    # ── 預測風味 ──
    def predict(self, bean: BeanRoast, params: BrewParams) -> dict:
        hits = self._recall(bean, params.brew_mechanism, FlavorProfile())
        ratio, flag, warnings = assess(hits)
        flavor: Dict[str, dict] = {}
        for axis in FLAVOR_AXES:
            est = weighted_estimate(hits, f"flavor_{axis}", prior_value=None)
            if est.value is not None:
                flavor[axis] = est.__dict__
        extraction = physics.flavor_prior_from_extraction(params.tds_pct, params.ey_pct)
        return {
            "mode": "predict",
            "mechanism": params.brew_mechanism.value,
            "predicted_flavor": flavor,
            "extraction_prior": extraction,
            "confidence_flag": flag,
            "a_weight_ratio": ratio,
            "evidence": self._evidence(hits),
            "warnings": warnings + self._sparse_warning(hits)
                       + ["定位:方向/排序可信度 > 絕對數值(R² 天花板 ~0.5)。"],
        }

    # ── 診斷 ──
    def diagnose(self, mechanism: BrewMechanism, defect: str,
                 bean: Optional[BeanRoast] = None) -> dict:
        tips = physics.diagnose_prior(mechanism, defect)
        return {
            "mode": "diagnose",
            "mechanism": mechanism.value,
            "defect": defect,
            "suggested_adjustments": tips,
            "warnings": ["先驗層建議;記錄 TDS/EY 與校準後可加入經驗修正。"],
        }

    # ── 換泡法 ──
    def method_swap(self, bean: BeanRoast, from_params: BrewParams,
                    to_mechanism: BrewMechanism, to_method: str = "") -> dict:
        same_mech = from_params.brew_mechanism == to_mechanism
        fp = physics.PRIORS[from_params.brew_mechanism]
        tp = physics.PRIORS[to_mechanism]

        deltas: List[str] = []
        if tp.time_to_ey == "high" and fp.time_to_ey == "low":
            deltas.append("接觸時間敏感度上升")
        if to_mechanism == BrewMechanism.PRESSURE:
            deltas.append("研磨→萃取呈峰值,過細會通道效應;需重新 dial-in")
        if to_mechanism == BrewMechanism.IMMERSION and from_params.brew_mechanism != BrewMechanism.IMMERSION:
            deltas.append("研磨/溫度敏感度下降,改由浸泡時間與粉水比主導")

        uncertainty = "low" if same_mech else "high"
        warnings = []
        if not same_mech:
            warnings.append(
                "跨機制遷移:僅定性、高不確定。物理軸不足以涵蓋壓力/流動動力學(設計 §12.1)。"
            )
        return {
            "mode": "method_swap",
            "from_mechanism": from_params.brew_mechanism.value,
            "to_mechanism": to_mechanism.value,
            "to_method": to_method,
            "param_translation_notes": deltas or ["同機制:主要調整擾動/時間細節。"],
            "predicted_flavor_delta": "請配合 predict() 在目標機制下重新預測。",
            "uncertainty": uncertainty,
            "warnings": warnings,
        }

    # ── 寫回校準(防 model collapse) ──
    def log_calibration(self, record: Record) -> dict:
        # 鐵則:A 級寫入須人類感官真值;引擎自身預測不得標 A、不得進方向投票。
        if record.grade == Grade.A and not record.protocol:
            return {"ok": False,
                    "error": "A 級校準須附 protocol(人類感官真值來源,如 SCA_cupping)。"}
        if record.grade == Grade.PREDICTION:
            record.confidence = min(record.confidence, 0.3)
        rid = self.store.upsert(record)
        # 雙寫 canonical 真相層(向量為衍生物)。prediction 級為衍生物,不入真相、
        # 不被 rebuild 復活;只有人類/外部真值(A/B/C)才進 canonical。
        if self.canonical is not None and record.grade != Grade.PREDICTION:
            self.canonical.append(record)
        return {"ok": True, "id": rid, "count": self.store.count(),
                "note": "已寫入。prediction 級不參與方向投票。"}

    # ── 工具 ──
    @staticmethod
    def _evidence(hits: List[dict], k: int = 5) -> List[dict]:
        out = []
        for h in hits[:k]:
            p = h["payload"]
            out.append({
                "id": h["id"], "score": round(h.get("score", 0), 3),
                "grade": p.get("grade"), "method": p.get("method"),
                "origin": p.get("origin"), "tds_pct": p.get("tds_pct"),
                "ey_pct": p.get("ey_pct"),
            })
        return out

    @staticmethod
    def _sparse_warning(hits: List[dict]) -> List[str]:
        if not hits:
            return ["庫中無此機制/條件的經驗:目前僅物理先驗,請累積校準。"]
        return []
