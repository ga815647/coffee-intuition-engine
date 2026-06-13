"""端到端冒煙測試:灌種子 → 推薦 → 預測 → 換泡法 → 診斷 → 寫回。"""
from __future__ import annotations

import pytest

from cie.engine import Engine
from cie.schema import BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process, Record
from cie.seed import seed
from cie.store import VectorStore


@pytest.fixture()
def engine():
    store = VectorStore()           # 記憶體模式
    seed(store)
    return Engine(store)


def test_seed_loaded(engine):
    assert engine.store.count() >= 6


def test_recommend_percolation(engine):
    bean = BeanRoast(origin="Ethiopia Yirgacheffe", process=Process.WASHED, roast_agtron=74)
    out = engine.recommend(bean, BrewMechanism.PERCOLATION)
    assert out["mode"] == "recommend"
    assert "suggested_params" in out
    # 有相符種子 → 應給出研磨建議值
    assert out["suggested_params"]["grind_um"]["value"] is not None
    assert out["confidence_flag"] in {"low", "medium", "high"}


def test_mechanism_isolation(engine):
    """機制硬分區:用 immersion 查不應撈到 percolation 的種子去污染。"""
    bean = BeanRoast(origin="Ethiopia Yirgacheffe", process=Process.WASHED, roast_agtron=74)
    out = engine.recommend(bean, BrewMechanism.IMMERSION)
    for ev in out["evidence"]:
        assert ev["method"] != "V60"  # V60 屬 percolation,不該出現


def test_predict_flavor(engine):
    bean = BeanRoast(origin="Colombia Huila", process=Process.WASHED, roast_agtron=68)
    params = BrewParams(brew_mechanism=BrewMechanism.PERCOLATION, water_temp_c=92,
                        brew_ratio=15.5, grind_um=680, tds_pct=1.42, ey_pct=21.0)
    out = engine.predict(bean, params)
    assert out["mode"] == "predict"
    assert "extraction_prior" in out


def test_diagnose(engine):
    out = engine.diagnose(BrewMechanism.PERCOLATION, "尖酸、收尾水")
    assert any("細" in t or "溫" in t or "時間" in t for t in out["suggested_adjustments"])


def test_method_swap_cross_mechanism_high_uncertainty(engine):
    bean = BeanRoast(origin="Ethiopia", process=Process.WASHED, roast_agtron=74)
    out = engine.method_swap(bean, BrewParams(brew_mechanism=BrewMechanism.PERCOLATION),
                             BrewMechanism.PRESSURE, "Espresso")
    assert out["uncertainty"] == "high"
    assert out["warnings"]


def test_log_calibration_a_grade_requires_protocol(engine):
    rec = Record(params=BrewParams(brew_mechanism=BrewMechanism.PERCOLATION), grade=Grade.A)
    out = engine.log_calibration(rec)
    assert out["ok"] is False  # A 級無 protocol 應被擋


def test_log_calibration_ok(engine):
    rec = Record(
        bean=BeanRoast(origin="Kenya", process=Process.WASHED, roast_agtron=70),
        params=BrewParams(brew_mechanism=BrewMechanism.PERCOLATION, method="V60",
                          grind_um=640, water_temp_c=94, tds_pct=1.4, ey_pct=20.5),
        flavor=FlavorProfile(acidity=8.5, sweetness=6.5, body=5.0),
        grade=Grade.A, protocol="SCA_cupping", user_id="self",
    )
    before = engine.store.count()
    out = engine.log_calibration(rec)
    assert out["ok"] is True
    assert engine.store.count() == before + 1


def test_empty_store_falls_back_to_prior():
    """空庫不應崩,應退回物理先驗 + 警告(防幻覺)。"""
    engine = Engine(VectorStore())  # 不 seed
    bean = BeanRoast(origin="Nowhere", process=Process.WASHED, roast_agtron=72)
    out = engine.recommend(bean, BrewMechanism.PERCOLATION)
    assert out["confidence_flag"] == "low"
    assert out["warnings"]
    assert out["suggested_params"]["target_ey_pct"]["source"] == "prior"
