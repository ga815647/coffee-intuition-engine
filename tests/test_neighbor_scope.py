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
from cie.retrieval import bean_match, origin_main_token
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
