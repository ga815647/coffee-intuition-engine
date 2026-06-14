"""Tier-1 分層覆蓋 + 硬湊率(hard-stretch)度量測試(§4)。

驗:
  - `_hard_stretch` 整體 / 分機制率正確,且**硬湊旗標**綁真實引擎行為
    (無同豆→全軸物理 prior=硬湊;有同豆→非硬湊)。
  - `coverage_report.build_report`:空格計數、★ 優先缺口浮出、單元錨點偵測。
  - `seed_tier1.build_records`:全 C 級 / variety="" / user_id=global / 來源誠實標記;
    Kenya×natural(★)橫跨三機制都補;對自身輸出冪等。
  - **單一真相一致性**:seed_tier1 補的筆數 == coverage_report 報的缺錨點格數
    (兩工具量同一張網,不漂移)。
"""
from __future__ import annotations

import pytest

from cie.engine import Engine
from cie.schema import (BeanRoast, BrewMechanism, BrewParams, FlavorProfile,
                        Grade, Process, Record)
from cie.store import VectorStore
from eval.run import _hard_stretch
from tools.coverage_report import build_report
from tools.seed_tier1 import (_load_corpus, anchored_cells, build_records,
                              origin_token)


# ────────────────────────────── 硬湊率度量(synthetic) ──────────────────────────────

def test_hard_stretch_metric_overall_and_per_mechanism():
    per_record = [
        {"mechanism": "percolation", "grade": "B", "hard_stretch": True},
        {"mechanism": "percolation", "grade": "B", "hard_stretch": False},
        {"mechanism": "immersion", "grade": "A", "hard_stretch": True},
    ]
    hs = _hard_stretch(per_record)
    assert hs["n"] == 3 and hs["n_hard_stretch"] == 2
    assert hs["rate"] == round(2 / 3, 4)
    assert hs["by_mechanism"]["percolation"] == {"n": 2, "n_hard_stretch": 1, "rate": 0.5}
    assert hs["by_mechanism"]["immersion"]["rate"] == 1.0


def test_hard_stretch_metric_empty():
    hs = _hard_stretch([])
    assert hs["n"] == 0 and hs["rate"] is None


# ── 硬湊旗標綁真實引擎:無同豆=全軸物理 prior(硬湊);有同豆=非硬湊 ──

def _yirg_geisha() -> BeanRoast:
    return BeanRoast(origin="Ethiopia Yirgacheffe", variety="Geisha",
                     process=Process.WASHED, roast_agtron=74.0)


def _perc() -> BrewParams:
    return BrewParams(brew_mechanism=BrewMechanism.PERCOLATION, water_temp_c=92.0,
                      brew_ratio=15.0, grind_um=300.0, tds_pct=1.35, ey_pct=20.0)


def _rec(origin: str, variety: str, *, grade=Grade.B) -> Record:
    return Record(
        bean=BeanRoast(origin=origin, variety=variety, process=Process.WASHED, roast_agtron=74.0),
        params=BrewParams(brew_mechanism=BrewMechanism.PERCOLATION, method="V60", grind_um=300.0,
                          water_temp_c=92.0, brew_ratio=15.0, contact_time_s=150.0,
                          tds_pct=1.35, ey_pct=20.0),
        flavor=FlavorProfile(acidity=7.5, sweetness=6.5, body=4.5, flavor_notes=["floral"]),
        grade=grade, confidence=0.6, user_id="global",
    )


def _is_hard_stretch(pred: dict) -> bool:
    """複刻 eval._score_holdouts 的硬湊判定:全軸 source==prior。"""
    pf = pred["predicted_flavor"]
    return bool(pf) and all(e.get("source") == "prior" for e in pf.values())


def test_hard_stretch_flag_true_when_no_same_bean():
    store = VectorStore()
    for _ in range(3):  # 只有跨豆 B(巴拿馬藝妓);查耶加藝妓 → 無同豆
        store.upsert(_rec("Panama", "Geisha"))
    out = Engine(store).predict(_yirg_geisha(), _perc())
    assert _is_hard_stretch(out) is True  # 全軸物理退回 = 硬湊


def test_hard_stretch_flag_false_when_same_bean_present():
    store = VectorStore()
    store.upsert(_rec("Ethiopia Yirgacheffe", "Geisha"))  # 同豆
    out = Engine(store).predict(_yirg_geisha(), _perc())
    assert _is_hard_stretch(out) is False  # 有同豆鄰居 = 非硬湊


# ────────────────────────────── coverage_report ──────────────────────────────

def test_coverage_report_empty_corpus_all_gaps():
    rep = build_report([])
    op = rep["origin_process"]
    # 空語料:seeded=covered=0,空格=未達共識=全格(誠實兩層皆為 0)
    assert op["seeded"] == 0 and op["covered"] == 0
    assert op["seeded_gaps"] == op["total"] > 0
    assert op["covered_gaps"] == op["total"]
    assert rep["mechanism_anchor"]["anchored"] == 0
    assert rep["mechanism_anchor"]["missing"] == rep["mechanism_anchor"]["total"] > 0
    assert "Kenya×natural" in rep["gaps"]            # 空格清單(0 筆)
    # ★ 優先缺口浮出且標為未補(空、未 seeded、未 covered)
    pg = rep["priority_gaps"]
    assert pg and pg[0]["cell"] == "Kenya×natural" and pg[0]["total"] == 0
    assert pg[0]["seeded"] is False and pg[0]["covered"] is False


def test_coverage_report_counts_grades_and_anchor():
    rows = [
        # kenya×natural percolation 單元錨點(variety="")
        {"bean": {"origin": "Kenya Nyeri", "variety": "", "process": "natural"},
         "params": {"brew_mechanism": "percolation"}, "grade": "C"},
        # kenya×natural immersion 特定品種(非單元錨點)
        {"bean": {"origin": "Kenya Nyeri", "variety": "SL28", "process": "natural"},
         "params": {"brew_mechanism": "immersion"}, "grade": "B"},
    ]
    rep = build_report(rows)
    kn = next(r for r in rep["grid"] if r["origin_token"] == "kenya" and r["process"] == "natural")
    assert kn["covered"] is True and kn["total"] == 2
    assert kn["counts"]["C"] == 1 and kn["counts"]["B"] == 1
    # 只有 percolation 有 variety="" 錨點;immersion(SL28)與 pressure 仍缺
    assert ("kenya", "natural", "percolation") in anchored_cells(rows)
    assert "percolation" not in kn["missing_anchor_mechs"]
    assert "immersion" in kn["missing_anchor_mechs"]
    assert kn["anchored_mechs"] == 1


# ──────────── 誠實分級:seeded ≠ covered(PR3 §A)+ 單錨點不假信心(PR3 §B) ────────────

def test_coverage_seeded_not_covered_single_c_anchor():
    """單一 derived C 單元錨點 → 該格 seeded(有同豆)但 **未 covered**(未達共識品質)。

    共識門檻 §4.1 = ≥1 A/B 同豆 **或** ≥3 C 同豆;一筆泛用 C 原型是 seed、不是 consensus。
    """
    rows = [
        # kenya×natural:單一 C 單元錨點 → seeded 但 ~weak(未達共識)
        {"bean": {"origin": "Kenya Nyeri", "variety": "", "process": "natural"},
         "params": {"brew_mechanism": "percolation"}, "grade": "C"},
        # ethiopia×washed:3 筆 C → 達 ≥3 C 共識門檻 → covered
        *[{"bean": {"origin": "Ethiopia Yirgacheffe", "variety": "", "process": "washed"},
           "params": {"brew_mechanism": "percolation"}, "grade": "C"} for _ in range(3)],
        # colombia×washed:1 筆 B(文獻/協定級)→ covered
        {"bean": {"origin": "Colombia Huila", "variety": "Caturra", "process": "washed"},
         "params": {"brew_mechanism": "percolation"}, "grade": "B"},
    ]
    rep = build_report(rows)
    by_cell = {(r["origin_token"], r["process"]): r for r in rep["grid"]}

    kn = by_cell[("kenya", "natural")]
    assert kn["seeded"] is True and kn["covered"] is False     # 單 C 錨點 = seeded、非 covered
    assert "Kenya×natural" in rep["weak_cells"]                 # 列在「seeded·未達共識」
    assert "Kenya×natural" not in rep["gaps"]                   # 不是空格(已有 seed)

    assert by_cell[("ethiopia", "washed")]["covered"] is True   # ≥3 C → covered
    assert by_cell[("colombia", "washed")]["covered"] is True   # ≥1 B → covered

    # seeded 永遠 ≥ covered;且 covered 誠實 < seeded(本例有 ~weak 格)
    op = rep["origin_process"]
    assert op["seeded"] >= op["covered"]
    assert op["covered"] < op["seeded"]


def test_single_c_anchor_predict_confidence_stays_honest():
    """PR3 §B:只有單一 C 同豆錨點 → predicted_flavor 雖 source=neighbors,信心仍誠實低。

    結構保證:`assess` 的 high 需 A 級權重佔比 ≥ 0.30;全 C(a_total=0)→ ratio=0 → 永不 high。
    (PR4:用**同品種** Geisha C 錨點才算同豆風味——variety 嚴格化後空白錨點不再餵指名品種查詢。)
    """
    store = VectorStore()
    store.upsert(_rec("Ethiopia Yirgacheffe", "Geisha", grade=Grade.C))  # 單一同品種 C 錨點
    out = Engine(store).predict(_yirg_geisha(), _perc())

    # 同豆錨點把 predicted_flavor 抬離物理 prior(source=neighbors)…
    assert _is_hard_stretch(out) is False
    assert out["predicted_flavor"]["acidity"]["source"] == "neighbors"
    # …但信心**誠實低**:flag 不得 high,且有效樣本數很小
    assert out["confidence_flag"] != "high"
    assert out["confidence_flag"] == "low"                       # 僅 1 鄰居 → low
    assert out["predicted_flavor"]["acidity"]["n_effective"] < 1.0
    # 稀疏 + A 級權重低警告皆在(誠實轉達不確定)
    assert any("鄰居過少" in w for w in out["warnings"])
    assert any("A 級權重佔比低" in w for w in out["warnings"])


def test_c_volume_never_yields_high_confidence():
    """大量 C(同豆單錨點 + 眾多跨豆 C)仍不得 high:C 永不洗出高信心(防假確定)。"""
    store = VectorStore()
    store.upsert(_rec("Ethiopia Yirgacheffe", "", grade=Grade.C))   # 同豆單錨點
    for i in range(20):                                              # 大量跨豆 C
        store.upsert(_rec(f"Brazil Cerrado {i}", "Catuai", grade=Grade.C))
    out = Engine(store).predict(_yirg_geisha(), _perc())
    assert out["confidence_flag"] in ("low", "medium")              # 至多 medium
    assert out["confidence_flag"] != "high"


# ────────────────────────────── seed_tier1 產出性質 ──────────────────────────────

@pytest.fixture(scope="module")
def tier1_recs():
    # hermetic:對**空語料**生成 → 整個 Tier-1 全格(99),不依賴會變動的 live 語料。
    return build_records([])


def test_seed_tier1_records_are_c_grade_global_anchors(tier1_recs):
    assert tier1_recs, "空語料應生成整個 Tier-1 全格"
    for r in tier1_recs:
        assert r["grade"] == "C"                       # 社群傾向、永不當 holdout
        assert r["bean"]["variety"] == ""              # 單元級錨點
        assert r["user_id"] == "global"
        assert "not a roaster" in r["source"]          # 來源誠實:非抄烘豆商專有詞
        et = r["embedding_text"].lower()
        assert r["bean"]["origin"].split()[0].lower() in et  # 含產地
        assert r["bean"]["process"] in et                     # 含處理法


def test_seed_tier1_fills_kenya_natural_priority_all_mechs(tier1_recs):
    kn = [r for r in tier1_recs
          if origin_token(r["bean"]["origin"]) == "kenya" and r["bean"]["process"] == "natural"]
    mechs = {r["params"]["brew_mechanism"] for r in kn}
    assert mechs == {"percolation", "immersion", "pressure"}  # ★ 三機制全補


def test_seed_tier1_idempotent_against_own_output(tier1_recs):
    """把自身輸出當已存在語料 → 該再跑就跳過全部(冪等)。"""
    produced = {(origin_token(r["bean"]["origin"]), r["bean"]["process"],
                 r["params"]["brew_mechanism"]) for r in tier1_recs}
    anchored = anchored_cells(tier1_recs)  # 自身即 variety="" 錨點
    assert produced <= anchored            # 每個產出格都成了錨點 → 二次跑會跳過


def test_seed_tier1_matches_coverage_missing_anchors(tier1_recs):
    """單一真相:seed_tier1 對某語料補的筆數 == coverage_report 對同語料報的缺錨點格數。

    以**空語料**驗此不變式(兩工具同一張 Tier-1 網、不漂移),不依賴 live 語料當下已填多少。
    """
    rep = build_report([])
    assert len(tier1_recs) == rep["mechanism_anchor"]["missing"] > 0
