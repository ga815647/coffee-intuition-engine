"""誠實區間 + eval 量尺硬化(鐵則 §4/§5/§6)。

對應 PR「冷啟動誠實區間 + eval 量尺硬化」三項:
  1. 冷啟動 predict 每軸回**有限** `lower < upper` 寬區間(非 `None`)——`physics.coarse_flavor_axes`
     由物理先驗導出保守寬區間,`source` 仍標 `prior`(非實測)。
  2. `weighted_estimate` 對 0-10 風味軸的 margin 設絕對下限(≥0.5)——近重複鄰居 spread→0
     不得造出假精確窄區間;參數軸(尺度迥異)不套此地板。
  3. eval 冷啟動判定改用 `has_AB_neighbor`——一顆 C 同豆鄰居翻 `has_any_neighbor` 但**不**翻
     `has_AB_neighbor`(C 只壓量級、不定方向),冷啟動標記不被翻掉。

全離線(記憶體向量庫 + 雜湊嵌入);依任務要求**不對 MAE 下硬門檻**。
"""
from __future__ import annotations

import pytest

from cie import physics
from cie.engine import Engine
from cie.retrieval import (FLAVOR_FIELD_KEYS, MIN_FLAVOR_MARGIN, weighted_estimate)
from cie.schema import (BeanRoast, BrewMechanism, BrewParams, FlavorProfile,
                        FLAVOR_AXES, Grade, Process, Record)
from cie.store import VectorStore
from eval.run import _neighbor_grounding, _score_holdouts


# ────────────────────────────── 共用建構 ──────────────────────────────

def _yirg_geisha() -> BeanRoast:
    return BeanRoast(origin="Ethiopia Yirgacheffe", variety="Geisha",
                     process=Process.WASHED, roast_agtron=74.0)


def _perc() -> BrewParams:
    return BrewParams(brew_mechanism=BrewMechanism.PERCOLATION, water_temp_c=92.0,
                      brew_ratio=15.0, grind_um=300.0, tds_pct=1.35, ey_pct=20.0)


def _same_bean_rec(grade: Grade) -> Record:
    """與 `_yirg_geisha()` 同豆(同 origin/variety/process)的一筆記錄,帶風味值。"""
    return Record(
        bean=_yirg_geisha(),
        params=BrewParams(brew_mechanism=BrewMechanism.PERCOLATION, method="V60",
                          water_temp_c=92.0, brew_ratio=15.0, grind_um=300.0,
                          contact_time_s=150.0, tds_pct=1.35, ey_pct=20.0),
        flavor=FlavorProfile(acidity=7.5, sweetness=6.5, body=4.5, aftertaste=6.0,
                             balance=6.5, clarity=7.0, bitterness=3.0,
                             flavor_notes=["floral"]),
        grade=grade, confidence=0.6, user_id="global",
    )


def _hit(value, *, field="flavor_acidity", grade="A", score=0.9, conf=0.7, hid="h"):
    return {"id": hid, "payload": {"grade": grade, "confidence": conf, field: value},
            "score": score}


# ────────────────── Item 1:冷啟動寬區間(physics + engine) ──────────────────

def test_coarse_flavor_axes_returns_finite_wide_interval():
    """物理粗略回 (value, lower, upper):每軸有限、lower < upper、clamp 在 0-10。"""
    axes = physics.coarse_flavor_axes(_yirg_geisha(), _perc())
    assert set(axes) == set(FLAVOR_AXES)
    for a, triple in axes.items():
        val, lo, hi = triple
        assert lo is not None and hi is not None      # 非 None(鐵則 §4)
        assert 0.0 <= lo < hi <= 10.0                  # 有限寬區間、夾在 0-10
        assert lo <= val <= hi                          # 點值落在區間內


def test_coarse_margin_widens_when_no_info():
    """焙度帶與 EY 皆未知 → 資訊近零 → 帶寬放大到 COARSE_MARGIN_NO_INFO(較有資訊時更寬)。"""
    no_info_bean = BeanRoast(origin="Somewhere", variety="X", process=Process.OTHER)  # 無 agtron
    no_info_params = BrewParams(brew_mechanism=BrewMechanism.PERCOLATION)             # 無 ey
    wide = physics.coarse_flavor_axes(no_info_bean, no_info_params)
    info = physics.coarse_flavor_axes(_yirg_geisha(), _perc())                        # 有焙度帶 + ey

    # acidity 無資訊時:value=5.0(base,無調整)、半寬 3.0 → [2.0, 8.0] 寬 6.0
    val, lo, hi = wide["acidity"]
    assert (val, lo, hi) == (5.0, 2.0, 8.0)
    assert hi - lo == pytest.approx(2 * physics.COARSE_MARGIN_NO_INFO)
    # 有資訊時 acidity 半寬 = COARSE_MARGIN(2.5)< 無資訊半寬
    iv, il, ih = info["acidity"]
    assert ih - iv == pytest.approx(physics.COARSE_MARGIN)
    assert (ih - iv) < (hi - val)                       # 有資訊 → 區間較窄


def test_predict_cold_start_every_axis_has_finite_interval():
    """空庫冷啟動 predict:每軸 source='prior' 且回有限 lower < upper(非裸點值)。"""
    eng = Engine(VectorStore())                          # 空庫 → 無同豆 → 冷啟動
    out = eng.predict(_yirg_geisha(), _perc())
    pf = out["predicted_flavor"]
    assert pf and set(pf) == set(FLAVOR_AXES)
    for a, est in pf.items():
        assert est["source"] == "prior"                 # 仍標非實測
        assert est["lower"] is not None and est["upper"] is not None
        assert est["lower"] < est["upper"]              # 寬區間,非 None 邊界
        assert est["lower"] <= est["value"] <= est["upper"]
    assert out["confidence_flag"] == "low"              # 無同豆 → 誠實低信心
    assert any("保守寬區間" in w for w in out["warnings"])


# ────────────────── Item 2:weighted_estimate margin 地板 ──────────────────

def test_near_duplicate_neighbors_flavor_margin_floored():
    """近重複鄰居(spread→0)的 0-10 風味軸:margin 被地板撐到 ≥0.5(非假精確窄區間)。"""
    hits = [_hit(5.00, hid="a"), _hit(5.04, hid="b"), _hit(5.08, hid="c")]  # 幾乎重複
    est = weighted_estimate(hits, "flavor_acidity")
    assert est.value is not None and est.source == "neighbors"
    # 原始 margin(1.64×pstdev≈0.05)遠 < 0.5;地板撐到半寬 = MIN_FLAVOR_MARGIN
    assert est.upper - est.value == pytest.approx(MIN_FLAVOR_MARGIN, abs=0.01)
    assert est.value - est.lower == pytest.approx(MIN_FLAVOR_MARGIN, abs=0.01)
    assert est.upper - est.lower >= 2 * MIN_FLAVOR_MARGIN - 1e-9


def test_margin_floor_only_floors_not_caps():
    """地板是『下限』非『上限』:真實 spread 大時 margin 照常變寬(不被 0.5 壓住)。"""
    hits = [_hit(2.0, hid="a"), _hit(5.0, hid="b"), _hit(8.0, hid="c")]  # 大離散
    est = weighted_estimate(hits, "flavor_acidity")
    assert est.upper - est.value > MIN_FLAVOR_MARGIN     # 寬離散 → margin 遠大於地板


def test_param_axis_not_floored():
    """地板只套 0-10 風味軸;參數軸(尺度迥異,如水溫)不套 → 近重複仍給窄 margin。"""
    assert "water_temp_c" not in FLAVOR_FIELD_KEYS
    hits = [_hit(92.00, field="water_temp_c", hid="a"),
            _hit(92.04, field="water_temp_c", hid="b"),
            _hit(92.08, field="water_temp_c", hid="c")]
    est = weighted_estimate(hits, "water_temp_c")
    assert (est.upper - est.value) < MIN_FLAVOR_MARGIN   # 未被地板撐寬(證明地板是風味軸專屬)


def test_neighbor_flavor_interval_clamped_to_0_10():
    """主鄰居路徑的 0-10 風味軸:大離散把裸區間推出軸域 → 夾回 [0,10](鐵則 §4 不超域)。"""
    # 高值 + 大離散 → raw upper > 10、低值 + 大離散 → raw lower < 0
    high = [_hit(9.5, field="flavor_balance", hid="a"),
            _hit(8.5, field="flavor_balance", hid="b"),
            _hit(9.8, field="flavor_balance", hid="c")]
    est_hi = weighted_estimate(high, "flavor_balance")
    assert est_hi.upper <= 10.0                          # 上界夾回 10
    assert est_hi.lower >= 0.0
    assert est_hi.value == pytest.approx(round(est_hi.value, 2))  # 點估不被夾改

    low = [_hit(0.4, field="flavor_bitterness", hid="a"),
           _hit(0.2, field="flavor_bitterness", hid="b"),
           _hit(1.5, field="flavor_bitterness", hid="c")]
    est_lo = weighted_estimate(low, "flavor_bitterness")
    assert est_lo.lower >= 0.0                           # 下界夾回 0(不再出現 -0.4)
    assert est_lo.upper <= 10.0


def test_neighbor_param_axis_interval_not_clamped():
    """夾域只套風味軸:參數軸(水溫 ~95、尺度非 0-10)的區間絕不被夾回 [0,10]。"""
    assert "water_temp_c" not in FLAVOR_FIELD_KEYS
    hits = [_hit(95.0, field="water_temp_c", hid="a"),
            _hit(93.0, field="water_temp_c", hid="b"),
            _hit(97.0, field="water_temp_c", hid="c")]
    est = weighted_estimate(hits, "water_temp_c")
    assert est.upper > 10.0                              # 證明參數軸未被夾(上界遠超 10)
    assert est.value > 10.0


# ────────────────── Item 3:eval 冷啟動判定改用 has_AB_neighbor ──────────────────

def _score_one(neighbor_grade: Grade):
    """庫中放一筆同豆鄰居(指定級別),對同豆 holdout 跑 _score_holdouts,回該筆 per_record。"""
    store = VectorStore()
    store.upsert(_same_bean_rec(neighbor_grade))
    holdout = _same_bean_rec(Grade.B)                    # holdout 本身是 A/B 真值(C 永不當 holdout)
    eng = Engine(store, canonical=None)
    per_record, _ = _score_holdouts(eng, [holdout], {holdout.id})
    return per_record[0]


def test_single_c_neighbor_does_not_flip_has_AB():
    """一顆 C 同豆鄰居:has_any_neighbor=True(抬離物理粗略)但 has_AB_neighbor=False
    → 冷啟動標記(hard_stretch)**不被翻掉**(C 只壓量級、不定方向,鐵則 §3/§5/§6)。"""
    rec = _score_one(Grade.C)
    assert rec["has_any_neighbor"] is True               # 有 C 同豆鄰居 → 非全軸物理 prior
    assert rec["has_AB_neighbor"] is False               # 但 C 不算方向級接地
    assert rec["hard_stretch"] is True                   # 冷啟動標記 = 非 has_AB,未被 C 翻掉


def test_ab_neighbor_lifts_cold_start():
    """對照:一顆 B 同豆鄰居 → has_AB_neighbor=True → 非冷啟動(hard_stretch=False)。"""
    rec = _score_one(Grade.B)
    assert rec["has_any_neighbor"] is True
    assert rec["has_AB_neighbor"] is True
    assert rec["hard_stretch"] is False


def test_no_neighbor_is_cold_start():
    """全無同豆鄰居:has_any=has_AB=False → 冷啟動(物理粗略退回)。"""
    store = VectorStore()
    holdout = _same_bean_rec(Grade.B)
    eng = Engine(store, canonical=None)
    per_record, _ = _score_holdouts(eng, [holdout], {holdout.id})
    rec = per_record[0]
    assert rec["has_any_neighbor"] is False
    assert rec["has_AB_neighbor"] is False
    assert rec["hard_stretch"] is True


def test_neighbor_grounding_reports_both_rates():
    """_neighbor_grounding 並列報 has_any 與 has_AB,兩率之差 = 『只有 C 鄰居』占比。"""
    per_record = [
        {"mechanism": "percolation", "has_any_neighbor": True, "has_AB_neighbor": False},  # 只有 C
        {"mechanism": "percolation", "has_any_neighbor": True, "has_AB_neighbor": True},    # 有 A/B
        {"mechanism": "immersion", "has_any_neighbor": False, "has_AB_neighbor": False},    # 全無
    ]
    ng = _neighbor_grounding(per_record)
    assert ng["n"] == 3
    assert ng["has_any_neighbor"] == 2 and ng["has_AB_neighbor"] == 1
    assert ng["any_rate"] == round(2 / 3, 4) and ng["ab_rate"] == round(1 / 3, 4)
    # 分機制:percolation 2 筆(any=2, ab=1);immersion 1 筆(any=0, ab=0)
    assert ng["by_mechanism"]["percolation"]["has_any_neighbor"] == 2
    assert ng["by_mechanism"]["percolation"]["has_AB_neighbor"] == 1
    assert ng["by_mechanism"]["immersion"]["any_rate"] == 0.0
