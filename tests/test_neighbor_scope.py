"""召回範圍依特異度分流(§3.2)+ social_tendency(§16.4)測試。

驗收(§3.7):
  - bean_match:耶加藝妓 vs 巴拿馬藝妓(差 origin)、vs 耶加一般豆(差 variety)→ 皆 False;
    blank-origin 泛用料不是同豆;子屬性未指定放行(specificity=low)。
  - flavor 分流:有 cross-bean A/B、無同豆 → predicted_flavor 走物理 prior(不含跨豆特徵),
    它們現身 social_tendency(grades 反映 B)。
  - params 不分流:cross-bean 鄰居仍進 recommend.suggested_params。
  - 分級召回:大量 C + 少數同豆 A/B → hits 仍含同豆 A/B。
  - social_tendency 標籤齊;只剩同豆、無跨豆/C → None。
"""
from __future__ import annotations

import pytest

from cie.engine import Engine
from cie.retrieval import assess, bean_match, origin_main_token
from cie.schema import BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process, Record
from cie.store import VectorStore


def _rec(origin: str, variety: str, *, grade: Grade = Grade.B, notes=None,
         acidity=None, sweetness=None, body=None, grind=300.0,
         process: Process = Process.WASHED, agtron: float = 74.0,
         mech: BrewMechanism = BrewMechanism.PERCOLATION) -> Record:
    return Record(
        bean=BeanRoast(origin=origin, variety=variety, process=process, roast_agtron=agtron),
        params=BrewParams(brew_mechanism=mech, method="V60", grind_um=grind,
                          water_temp_c=92.0, brew_ratio=15.0, contact_time_s=150.0,
                          tds_pct=1.35, ey_pct=20.0),
        flavor=FlavorProfile(acidity=acidity, sweetness=sweetness, body=body,
                             flavor_notes=notes or []),
        grade=grade, confidence=0.6, user_id="global",
    )


def _yirg_geisha() -> BeanRoast:
    return BeanRoast(origin="Ethiopia Yirgacheffe", variety="Geisha",
                     process=Process.WASHED, roast_agtron=74.0)


def _perc_params() -> BrewParams:
    return BrewParams(brew_mechanism=BrewMechanism.PERCOLATION, water_temp_c=92.0,
                      brew_ratio=15.0, grind_um=300.0, tds_pct=1.35, ey_pct=20.0)


@pytest.fixture()
def store():
    return VectorStore()  # 記憶體模式,離線雜湊嵌入


def _engine(store: VectorStore, records) -> Engine:
    for r in records:
        store.upsert(r)
    return Engine(store)


# ────────────────────────────── bean_match 單元 ──────────────────────────────

def test_bean_match_origin_variety_process():
    q = ("Ethiopia Yirgacheffe", "Geisha", "washed")
    pana = {"origin": "Panama", "variety": "Geisha", "process": "washed"}
    heir = {"origin": "Ethiopia Yirgacheffe", "variety": "Heirloom", "process": "washed"}
    same = {"origin": "Ethiopia Yirgacheffe", "variety": "Geisha", "process": "washed"}
    blank = {"origin": "", "variety": "", "process": "washed"}

    assert bean_match(*q, pana)[0] is False          # 差 origin(藝妓但巴拿馬)
    assert bean_match(*q, heir)[0] is False           # 差 variety(同耶加但 Heirloom)
    ok, spec = bean_match(*q, same)
    assert ok is True and spec == "high"              # 三欄皆具體且符
    assert bean_match(*q, blank)[0] is False           # blank-origin 泛用料不是「這支豆」


def test_bean_match_unspecified_subattr_passes_low_specificity():
    heir = {"origin": "Ethiopia Yirgacheffe", "variety": "Heirloom", "process": "washed"}
    ok, spec = bean_match("Ethiopia Yirgacheffe", "", "washed", heir)  # 查詢未填 variety
    assert ok is True and spec == "low"               # 子屬性未指定 → 放行,特異度降 low


def test_origin_main_token():
    assert origin_main_token("Ethiopia Yirgacheffe") == "ethiopia"
    assert origin_main_token("Kenya Nyeri") == "kenya"
    assert origin_main_token("") == ""
    assert origin_main_token("single origin Panama") == "panama"  # 去通用詞


# ────────────────────────────── flavor 只同豆 / social_tendency ──────────────────────────────

def test_flavor_only_from_same_bean_falls_to_physics_and_social(store):
    # cross-bean B(巴拿馬藝妓);query 耶加藝妓 → 無同豆。風味特色不得借跨豆。
    recs = [_rec("Panama", "Geisha", grade=Grade.B, notes=["jasmine", "bergamot"],
                 acidity=8.0, sweetness=7.0, body=4.0) for _ in range(3)]
    eng = _engine(store, recs)
    out = eng.predict(_yirg_geisha(), _perc_params())

    pf = out["predicted_flavor"]
    assert pf and all(v["source"] == "prior" for v in pf.values())  # 全走物理粗略
    assert pf["acidity"]["value"] != 8.0                            # 沒抄跨豆的酸度

    st = out["social_tendency"]
    assert st is not None and st["reputed"] is True and st["confidence"] == "low"
    assert st["bean_match_any"] is False
    assert st["grades"].get("B") == 3                                # 跨豆 B 降級進此處
    assert "jasmine" in st["flavor_notes"]
    assert "Panama" in st["origins"]
    assert any("無同豆校準" in w for w in out["warnings"])


def test_same_bean_defines_flavor_and_social_none(store):
    # 只有同豆 B、無跨豆 / 無 C → predicted_flavor 來自同豆;social_tendency=None。
    recs = [_rec("Ethiopia Yirgacheffe", "Geisha", grade=Grade.B, notes=["floral"],
                 acidity=7.5, sweetness=6.5, body=4.5) for _ in range(2)]
    eng = _engine(store, recs)
    out = eng.predict(_yirg_geisha(), _perc_params())

    assert out["social_tendency"] is None
    assert out["predicted_flavor"]["acidity"]["source"] != "prior"   # 同豆鄰居,非物理 prior


# ─────────────── 冷啟動群組均值先驗(機制分軌;治『硬編 ~5 中點偏低』,§1/§6) ───────────────

def test_coldstart_uses_group_mean_prior_not_flat_midpoint(store):
    """機制資料足夠時,冷啟動 predicted_flavor 走『經驗群組均值』而非硬編 ~5 中點。

    15 筆跨豆 percolation B(acidity=7.0)→ 機制根層有效權重 > MIN_GROUP_WEIGHT;查一支
    全新冷門豆(無同豆)→ acidity 應拉到群組均值 ~7,而非物理常數 5.0。source 仍 'prior'、附寬區間。
    """
    recs = [_rec(f"Origin{i}", "", grade=Grade.B, acidity=7.0) for i in range(15)]
    eng = _engine(store, recs)
    novel = BeanRoast(origin="Narnia", variety="", process=Process.WASHED, roast_agtron=74.0)
    out = eng.predict(novel, _perc_params())

    pf = out["predicted_flavor"]
    assert pf["acidity"]["source"] == "prior"                  # 冷啟動仍走先驗(非鄰居)
    assert 6.0 <= pf["acidity"]["value"] <= 8.0                # 拉到群組均值 ~7,非硬編 5.0
    assert pf["acidity"]["value"] != 5.0
    assert pf["acidity"]["lower"] is not None and pf["acidity"]["upper"] is not None  # 誠實寬區間
    assert any("無同豆校準" in w for w in out["warnings"])


def test_coldstart_group_prior_never_crosses_mechanism(store):
    """§1 鐵則:群組均值先驗永不跨機制平均。

    percolation(acidity=7)與 immersion(acidity=2)各自成軌;查 percolation 冷啟動豆,
    acidity 應反映 percolation 的 ~7,**不被** immersion 的 2 污染(反之亦然)。
    """
    perc = [_rec(f"P{i}", "", grade=Grade.B, acidity=7.0,
                 mech=BrewMechanism.PERCOLATION) for i in range(15)]
    imm = [_rec(f"I{i}", "", grade=Grade.B, acidity=2.0,
                mech=BrewMechanism.IMMERSION) for i in range(15)]
    eng = _engine(store, perc + imm)
    novel = BeanRoast(origin="Narnia", variety="", process=Process.WASHED, roast_agtron=74.0)

    perc_out = eng.predict(novel, BrewParams(brew_mechanism=BrewMechanism.PERCOLATION,
                                             ey_pct=20.0))
    imm_out = eng.predict(novel, BrewParams(brew_mechanism=BrewMechanism.IMMERSION,
                                            ey_pct=20.0))
    assert perc_out["predicted_flavor"]["acidity"]["value"] >= 6.0   # 走 percolation ~7
    assert imm_out["predicted_flavor"]["acidity"]["value"] <= 4.0    # 走 immersion ~2,未被 7 拉高


# ────────────────────────────── params 不分流(借廣鄰居) ──────────────────────────────

def test_params_borrow_cross_bean(store):
    recs = [_rec("Panama", "Geisha", grade=Grade.B, notes=["jasmine"], acidity=8.0, grind=305.0)]
    eng = _engine(store, recs)
    out = eng.recommend(_yirg_geisha(), BrewMechanism.PERCOLATION)
    # 大方向參數可借跨產地鄰居(物理可遷移)
    assert out["suggested_params"]["grind_um"]["value"] is not None
    # 但仍附 social_tendency 當風味參考(跨豆、不影響 suggested_params)
    assert out["social_tendency"] is not None
    assert out["social_tendency"]["bean_match_any"] is False


# ────────────────────────────── 分級召回:同豆 A/B 不被大量 C 擠掉 ──────────────────────────────

def test_graded_recall_keeps_same_bean_ab(store):
    recs = [_rec("Ethiopia Yirgacheffe", "Geisha", grade=Grade.B, notes=["floral"], acidity=7.5)]
    for i in range(25):  # 大量跨豆 C(壓量級)
        recs.append(_rec(f"Brazil Cerrado {i}", "Catuai", grade=Grade.C,
                         notes=["nutty"], acidity=4.0))
    eng = _engine(store, recs)
    bean = _yirg_geisha()
    hits = eng._recall(bean, BrewMechanism.PERCOLATION, FlavorProfile())
    same = eng._same_bean(bean, hits)
    assert len(same) >= 1                                        # 同豆 B 仍在召回內
    assert any(h["payload"].get("grade") == "B" for h in same)   # 且確為 A/B


def test_graded_recall_rescues_low_score_same_bean_ab(store):
    """分級召回 **load-bearing**:同豆 A/B 即便相似度分數**低於** top_k 名 C,仍被救回。

    用**受控召回池**直接驗 stratification 邏輯(不靠雜湊嵌入分數的偶然:上面的功能測試裡
    同豆 B 其實天生高分、即使不分級也會在 top_k,故證不到分級是必要的)。pool 依分數排序為
    [5×跨豆 C(高分), 1×同豆 B(最低分)];naive `pool[:top_k]` 會把同豆 B 擠掉,
    分級召回(`ab[:k] ∪ rest[:k]`)把它救回——這正是大量低訊號 C 擴量時的安全閥。
    """
    eng = Engine(store)  # memory store → canonical=None,不碰 D1

    def _hit(i: int, grade: str, origin: str, variety: str, score: float) -> dict:
        return {"id": f"h{i}",
                "payload": {"grade": grade, "origin": origin, "variety": variety,
                            "process": "washed"},
                "score": score}

    pool = [_hit(i, "C", f"Brazil {i}", "Catuai", 0.9 - i * 0.05) for i in range(5)]
    pool.append(_hit(99, "B", "Ethiopia Yirgacheffe", "Geisha", 0.05))  # 同豆 B、最低分
    eng.store.search = lambda **kw: pool                            # 受控池(已依分數排序)

    hits = eng._recall(_yirg_geisha(), BrewMechanism.PERCOLATION, FlavorProfile(), top_k=3)
    ids = {h["id"] for h in hits}
    assert "h99" in ids                                            # 同豆 B 被救回
    assert any(h["payload"]["grade"] == "B" for h in hits)
    # 證明分級是必要的:純分數 top-3 會排除它(B 分數排第 6)
    naive_top3 = {h["id"] for h in sorted(pool, key=lambda h: -h["score"])[:3]}
    assert "h99" not in naive_top3


# ────────────── variety specificity 嚴格化:空白錨點不得當特異品種風味真值(PR4 §1) ──────────────

def test_bean_match_strict_variety_blank_neighbor():
    """`strict_variety`:查詢指名品種 + 鄰居空白品種 → 非同豆(只收緊 variety 這條)。"""
    q = ("Ethiopia Yirgacheffe", "Geisha", "washed")
    blank_var = {"origin": "Ethiopia Yirgacheffe", "variety": "", "process": "washed"}
    # 預設寬鬆:放行,specificity low(同產地泛用基準)
    assert bean_match(*q, blank_var) == (True, "low")
    # 嚴格:查詢指名 Geisha、鄰居空白品種 → 非同豆(藝妓≠泛用耶加)
    assert bean_match(*q, blank_var, strict_variety=True)[0] is False
    # 嚴格但查詢未指名品種 → 仍放行(只收緊「查詢指名品種」這條,不破壞泛用查詢)
    q_generic = ("Ethiopia Yirgacheffe", "", "washed")
    assert bean_match(*q_generic, blank_var, strict_variety=True)[0] is True
    # 嚴格 + 雙方皆具體且符 → 仍同豆 high(不誤殺真同品種)
    same = {"origin": "Ethiopia Yirgacheffe", "variety": "Geisha", "process": "washed"}
    assert bean_match(*q, same, strict_variety=True) == (True, "high")


def test_strict_variety_blank_anchor_excluded_from_flavor(store):
    """§4.2 單元錨點皆 variety="";耶加藝妓 predict 不得借「泛用耶加」風味寫進 predicted_flavor。

    空白錨點改現身 social_tendency(同產地 reputed 參考,共用述詞不致消失),predicted_flavor
    退回物理粗略(全軸 source=prior)+ 低信心。
    """
    recs = [_rec("Ethiopia Yirgacheffe", "", grade=Grade.C, notes=["citrus", "tea"],
                 acidity=6.5, sweetness=6.0, body=4.0) for _ in range(3)]
    eng = _engine(store, recs)
    out = eng.predict(_yirg_geisha(), _perc_params())            # 查詢 variety=Geisha

    pf = out["predicted_flavor"]
    assert pf and all(v["source"] == "prior" for v in pf.values())  # 空白錨點不入 → 物理粗略
    assert out["confidence_flag"] != "high"

    st = out["social_tendency"]
    assert st is not None and st["reputed"] is True              # 空白錨點落社群傾向(沒消失)
    assert st["bean_match_any"] is False                          # 嚴格化下不算同豆
    assert "citrus" in st["flavor_notes"]
    assert "Ethiopia Yirgacheffe" in st["origins"]


def test_generic_variety_query_still_uses_blank_anchor(store):
    """不回歸:查詢**未指名**品種 → 空白品種錨點仍是合法同產地基準,可入 predicted_flavor。"""
    recs = [_rec("Ethiopia Yirgacheffe", "", grade=Grade.B, notes=["citrus"],
                 acidity=6.5, sweetness=6.0, body=4.0) for _ in range(2)]
    eng = _engine(store, recs)
    generic = BeanRoast(origin="Ethiopia Yirgacheffe", variety="",
                        process=Process.WASHED, roast_agtron=74.0)
    out = eng.predict(generic, _perc_params())
    assert out["predicted_flavor"]["acidity"]["source"] != "prior"   # 空白錨點入風味(同產地基準)
    assert out["social_tendency"] is None                            # 同豆且非 C → 無社群池


# ────────────── n_eff<1 強制 low:數量湊夠但有效樣本趨零的假信心壓回(PR4 §2) ──────────────

def _hit(grade: str, score: float, conf: float = 0.6) -> dict:
    return {"id": "x", "payload": {"grade": grade, "confidence": conf}, "score": score}


def test_assess_small_effective_weight_forces_low():
    """2 鄰居(count→medium)但聚合有效權重 <1(全 C、低相似)→ 強制 low + warning。"""
    hits = [_hit("C", 0.3), _hit("C", 0.3)]      # eff ≈ 2×0.1×0.6×0.3 ≈ 0.036
    _ratio, flag, warnings = assess(hits)
    assert flag == "low"
    assert any("有效樣本過小" in w for w in warnings)


def test_assess_sufficient_effective_weight_keeps_medium():
    """充足有效權重(eff≈2)→ 不被壓回,維持 medium(不誤殺真有料的)。"""
    hits = [_hit("B", 1.0, conf=1.0) for _ in range(5)]   # eff = 5×0.4×1.0×1.0 = 2.0
    _ratio, flag, warnings = assess(hits)
    assert flag == "medium"                              # len≥3 無 A → 非 high;eff≥1 → 不壓
    assert not any("有效樣本過小" in w for w in warnings)


def test_predict_empty_same_bean_low_and_evidence_excludes_cross_bean(store):
    """PR5:肯亞日曬 predict 只撈到 2 筆跨產地 C(無同豆)→ assess([]) → low;且
    evidence **不列跨豆**(Item 2),跨豆參考改由 social_tendency 呈現(Item 1 信心 + Item 2
    evidence 都以同豆子集為準)。"""
    recs = [_rec("Brazil Cerrado", "Catuai", grade=Grade.C, process=Process.NATURAL, acidity=4.0),
            _rec("Colombia Huila", "Caturra", grade=Grade.C, process=Process.NATURAL, acidity=5.0)]
    eng = _engine(store, recs)
    kenya = BeanRoast(origin="Kenya Nyeri", variety="SL28",
                      process=Process.NATURAL, roast_agtron=74.0)
    params = BrewParams(brew_mechanism=BrewMechanism.PERCOLATION, water_temp_c=92.0,
                        brew_ratio=15.0, grind_um=300.0, tds_pct=1.35, ey_pct=20.0)
    out = eng.predict(kenya, params)
    assert out["confidence_flag"] == "low"                       # 空同豆 → assess([]) → low
    assert any("無同豆校準" in w for w in out["warnings"])       # 維持「無同豆校準」現行為
    assert out["evidence"] == []                                 # Item 2:evidence 不列跨豆
    st = out["social_tendency"]                                  # 跨豆參考仍由 social_tendency 呈現
    assert st is not None and st["bean_match_any"] is False
    assert "Brazil Cerrado" in st["origins"] and "Colombia Huila" in st["origins"]


def test_predict_confidence_and_evidence_reflect_same_bean_subset(store):
    """PR5(核心):predict 的信心 / n_eff floor / evidence 算**同豆子集**,非整個召回池。

    受控池 = 2 同豆 C(Σ權重<1)+ 5 跨豆 B(撐起全池);全池 assess→medium(PR5 前的脫鉤
    假信心),但真正餵 predicted_flavor 的只有 2 同豆 C(n_eff<1)→ predict 應誠實報 low,
    且 evidence 只列那 2 筆同豆(不印跨豆 B)。predicted_flavor 內容不變(仍由同豆 C 餵)。
    """
    eng = Engine(store)  # memory store → canonical=None,不碰 D1

    def _ph(i: int, grade: str, origin: str, variety: str, score: float,
            conf: float, acidity: float) -> dict:
        return {"id": f"h{i}",
                "payload": {"grade": grade, "origin": origin, "variety": variety,
                            "process": "washed", "confidence": conf,
                            "flavor_acidity": acidity},
                "score": score}

    same = [_ph(i, "C", "Ethiopia Yirgacheffe", "Geisha", 0.95, 0.6, 6.5) for i in range(2)]
    cross = [_ph(10 + i, "B", "Panama", "Geisha", 0.8, 1.0, 8.0) for i in range(5)]
    pool = same + cross                                          # 已依分數序(C 高分在前無妨)
    eng.store.search = lambda **kw: pool                         # 受控召回池

    bean = _yirg_geisha()
    # 對照:全池 assess 會給 medium(PR5 前 predict 算錯集合的脫鉤答案)
    assert assess(eng._recall(bean, BrewMechanism.PERCOLATION, FlavorProfile()))[1] == "medium"

    out = eng.predict(bean, _perc_params())
    assert out["confidence_flag"] == "low"                       # 同豆子集 n_eff<1 → 強制 low
    assert any("有效樣本過小" in w for w in out["warnings"])      # PR4 floor 經同豆子集仍生效
    # predicted_flavor 內容不變:仍由同豆 C 餵(非物理 prior),且取同豆值非跨豆 8.0
    assert out["predicted_flavor"]["acidity"]["source"] != "prior"
    assert out["predicted_flavor"]["acidity"]["value"] == 6.5
    # Item 2:evidence 只列同豆那 2 筆(不印跨豆 B)
    assert {e["id"] for e in out["evidence"]} == {"h0", "h1"}
    assert all(e["origin"] == "Ethiopia Yirgacheffe" for e in out["evidence"])
