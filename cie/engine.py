"""高階引擎:recommend / predict / diagnose / method_swap。

把 store(召回)+ retrieval(加權收縮區間)+ physics(機制先驗)組裝起來。
所有輸出都帶 evidence 與 warnings,維持可解釋與防幻覺。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from . import physics
from .canonical import CanonicalStore, maybe_get_canonical
from .retrieval import (
    Estimate, RetrievalResult, assess, bean_match, social_tendency, weighted_estimate,
)
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

    # ── 召回(分級召回:防大量低級料把少數同豆 A/B 擠出 top-k;§3.1) ──
    def _recall(self, bean: BeanRoast, mechanism: BrewMechanism, flavor: FlavorProfile,
                top_k: int = 20, user_ids: Optional[List[str]] = None) -> List[dict]:
        query_text = Record(
            bean=bean, params=BrewParams(brew_mechanism=mechanism), flavor=flavor
        ).build_embedding_text()
        # 先召回較大集合,再對 A/B 與其餘各取 top_k 合併,確保少數同豆 A/B 不被 C 量壓掉。
        pool = self.store.search(
            query_text=query_text, mechanism=mechanism, top_k=max(top_k * 3, 60),
            process=bean.process.value if bean.process else None,
            roast_band=bean.roast_band() if bean.roast_band() != "unknown" else None,
            exclude_predictions=True,
            user_ids=user_ids,  # 多租戶讀範圍(§16.3);None=不過濾(本地/owner 全可見)
        )
        ab = [h for h in pool if (h.get("payload") or {}).get("grade") in ("A", "B")]
        rest = [h for h in pool if (h.get("payload") or {}).get("grade") not in ("A", "B")]
        # 只「救援」少數 A/B 不被大量 C 擠出 top_k——保留各取 top_k 的聯集,但**仍依 pool 的
        # 原生分數序回傳**(不把 A/B 在同分時硬排到 C 前面;否則 owner 讀證據時 C 自有 self 會
        # 被同分 B 擠掉,破多租戶讀可見性)。pool 已由 store.search 依分數排序。
        keep = {h["id"] for h in ab[:top_k]} | {h["id"] for h in rest[:top_k]}
        return [h for h in pool if h["id"] in keep]

    @staticmethod
    def _same_bean(bean: BeanRoast, hits: List[dict]) -> List[dict]:
        """風味同豆鄰居(§3.2):origin 主產地 token + variety + process 皆符。

        **`strict_variety=True`**:風味特色越特異越不可借鄰居——查詢指名了 variety(如耶加藝妓)時,
        variety 空白的泛用單元錨點**不算同豆風味**(藝妓≠一般耶加;否則 §4.2 那批 variety="" 錨點會
        變成所有指名品種的萬用風味捐贈者=破鐵則)。此閘只管 `predict` 的風味;recommend/diagnose 的
        沖煮大方向仍用全鄰居(物理可遷移)。被踢出者落 social_tendency(共用述詞,不致消失)。
        """
        proc = bean.process.value if bean.process else ""
        return [h for h in hits
                if bean_match(bean.origin, bean.variety, proc, h.get("payload"),
                              strict_variety=True)[0]]

    # ── 推薦起手參數 ──
    def recommend(self, bean: BeanRoast, mechanism: BrewMechanism,
                  target_flavor: Optional[FlavorProfile] = None,
                  user_ids: Optional[List[str]] = None) -> dict:
        hits = self._recall(bean, mechanism, target_flavor or FlavorProfile(), user_ids=user_ids)
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
            "suggested_params": params,  # 大方向:全鄰居(跨產地/品種可,物理可遷移;§3.2)
            "social_tendency": social_tendency(hits, bean),  # 風味參考;不影響 suggested_params
            "physics_note": gc["note"],
            "confidence_flag": flag,
            "a_weight_ratio": ratio,
            "evidence": self._evidence(hits),
            "warnings": warnings + self._sparse_warning(hits),
        }

    # ── 預測風味 ──
    def predict(self, bean: BeanRoast, params: BrewParams,
                user_ids: Optional[List[str]] = None) -> dict:
        hits = self._recall(bean, params.brew_mechanism, FlavorProfile(), user_ids=user_ids)
        ratio, flag, warnings = assess(hits)
        # 風味「這隻豆的特色」只信同豆(§3.2):predicted_flavor 只吃 bean_match=True 鄰居;
        # 跨豆(含 A/B)與 C 的風味降級進 social_tendency,永不寫進 predicted_flavor 特色。
        same_bean = self._same_bean(bean, hits)
        flavor: Dict[str, dict] = {}
        flavor_warnings: List[str] = []
        if same_bean:
            for axis in FLAVOR_AXES:
                est = weighted_estimate(same_bean, f"flavor_{axis}", prior_value=None)
                if est.value is not None:
                    flavor[axis] = est.__dict__
        else:
            # 無同豆鄰居 → predicted_flavor 走物理粗略(coarse、無區間);特色交給 social_tendency。
            for axis, val in physics.coarse_flavor_axes(bean, params).items():
                flavor[axis] = Estimate(val, None, None, 0.0, "prior").__dict__
            flavor_warnings.append(
                "風味特色無同豆校準:predicted_flavor 為物理粗略(generic 大方向、無精確區間),"
                "特色見 social_tendency(跨豆/社群參考、非本豆實測)。"
            )
        extraction = physics.flavor_prior_from_extraction(params.tds_pct, params.ey_pct)
        return {
            "mode": "predict",
            "mechanism": params.brew_mechanism.value,
            "predicted_flavor": flavor,
            "social_tendency": social_tendency(hits, bean),  # additive 跨豆/社群風味參考(§16.4)
            "extraction_prior": extraction,
            "confidence_flag": flag,
            "a_weight_ratio": ratio,
            "evidence": self._evidence(hits),
            "warnings": warnings + flavor_warnings + self._sparse_warning(hits)
                       + ["定位:方向/排序可信度 > 絕對數值(R² 天花板 ~0.5)。"],
        }

    # ── 診斷 ──
    def diagnose(self, mechanism: BrewMechanism, defect: str,
                 bean: Optional[BeanRoast] = None) -> dict:
        tips = physics.diagnose_prior(mechanism, defect)
        out: dict = {
            "mode": "diagnose",
            "mechanism": mechanism.value,
            "defect": defect,
            "suggested_adjustments": tips,
            "warnings": ["先驗層建議;記錄 TDS/EY 與校準後可加入經驗修正。"],
        }
        # 偏酸:已知爭議 → 兩方向並陳 + 寬區間 + 低信心 + 閉環 A/B 旗標(不選邊;§3/§12.2 同型處置)。
        contested = physics.contested_diagnosis(mechanism, defect)
        if contested is None:
            out["contested"] = False
            return out
        out["contested"] = True
        out["contested_topic"] = contested["topic"]
        out["open_question"] = contested["question"]
        out["confidence_flag"] = contested["confidence"]            # "low":誠實寬區間
        out["interval_note"] = contested["interval_note"]
        out["directions"] = contested["directions"]                # working_prior + second_signal(B)
        out["second_signal"] = contested["directions"][1]          # 明示 Cotter B 級第二訊號
        out["needs_ab_test"] = contested["needs_ab_test"]          # 閉環 A/B 旗標(舌頭裁決)
        out["resolution"] = contested["resolution"]                # A 級保留給使用者 A/B
        # warnings 前置爭議旗標:**一定轉達**,別只報單一方向(對齊 SKILL.md)。
        out["warnings"] = [
            "⚠️ 偏酸的 fix 方向是【已知爭議】:兩個先驗分歧、未定論——別只報一個方向。",
            "working prior=增萃降酸(convergent + 物理先驗);second signal=拆濃度/萃取軸:"
            "降 TDS(濃度)才是降酸最穩的一手,『一味增萃』在 drip 常同時升 TDS、可能反升酸"
            "(UC Davis B 級,單源/drip 限定,不覆蓋)。",
            contested["interval_note"],
            contested["note"],
            f"請跑閉環 A/B 由你的舌頭裁決 → {contested['needs_ab_test']}",
        ] + out["warnings"]
        return out

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
        # 持久化順序(load-bearing):先把真相落到 canonical(R2),確認後才更新**易失的**
        # in-memory 索引——「回 success ⟹ R2 已有」,撐過 Cloud Run scale-to-zero(member
        # 寫入不丟)。canonical.append 失敗會拋例外、store.upsert 不執行,呼叫端不會收到假成功。
        # prediction 級為衍生物,不入真相、不被 rebuild 復活;只有人類/外部真值(A/B/C)才進。
        if self.canonical is not None and record.grade != Grade.PREDICTION:
            self.canonical.append(record)
        rid = self.store.upsert(record)
        return {"ok": True, "id": rid, "count": self.store.count(),
                "note": "已寫入。prediction 級不參與方向投票。"}

    # ── 刪除校準(member 只刪自有 self;owner 可刪任一) ──
    def delete_calibration(self, record_id: str, *,
                           allowed_user_id: Optional[str] = None) -> dict:
        """刪一筆校準。`allowed_user_id`=None → owner(可刪任一);否則只刪該命名空間自有
        (member confinement:即便 id 猜中,非自有命名空間也刪不掉)。

        持久化順序(對稱 log_calibration 的「真相先行」):**先刪 canonical 真相**(D1,權威 +
        命名空間 confinement;其刪除列數是「是否真的擁有並刪掉」的權威信號),**再刪易失的記憶體
        索引**。萬一只刪了 canonical → 下次冷啟動從 D1 重建即不復活(最終一致朝『已刪』收斂);
        反序(先記憶體後 canonical)則會在冷啟動復活,故不採。
        """
        n_canon = 0
        if self.canonical is not None and hasattr(self.canonical, "delete"):
            n_canon = self.canonical.delete(record_id, allowed_user_id)
        n_mem = 0
        if hasattr(self.store, "delete"):
            n_mem = self.store.delete(record_id, allowed_user_id)
        deleted = (n_canon > 0) or (n_mem > 0)
        return {
            "ok": deleted, "id": record_id,
            "deleted_canonical": n_canon, "deleted_memory": n_mem,
            "count": self.store.count(),
            "note": ("已刪除。冷啟動從 canonical 重建不復活。" if deleted
                     else "找不到該記錄,或不在你的命名空間(member 只能刪自有 self)。"),
        }

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
