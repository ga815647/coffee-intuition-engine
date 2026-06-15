"""按機制 split-conformal q̂ 的單元測試(P1)。

守住的鐵則:
  §1 機制硬隔離 — 某機制的 q̂ 絕不影響別機制(同軸亦然)。
  §4 誠實不確定、寧過勿欠 — 表缺席/壞檔 → 退 1.64 逐位相同;MIN_FLAVOR_MARGIN 地板與
      [0,10] 夾域在 q̂ 之後照舊套用;校準用保守有限樣本 conformal 分位。
  §15.2 無洩漏 — 校準只取走 z 路徑(neighbors/shrunk)、未夾域、未頂地板的點。

涵蓋:conformal_z 解析、weighted_estimate 注入 q̂(收緊/放寬/地板/夾域/跨機制隔離)、
_load_q_artifact 壞檔降級 + embedder 解析、conformal_active_for 讀取閘、以及校準工具的
純函式(分位/收分/建表)。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import cie.retrieval as retrieval
from cie.portability import read_jsonl
from cie.retrieval import (
    CONFORMAL_Z_FALLBACK,
    MIN_FLAVOR_MARGIN,
    conformal_z,
    weighted_estimate,
)
from cie.schema import BrewMechanism
from eval.run import CORPUS_PATH
from tools.calibrate_conformal import (
    _conformal_quantile,
    build_table,
    collect_scores,
)


# ────────────────────────────── 測試夾具 ──────────────────────────────

def _hits(field_key: str, values, *, grade="A", conf=1.0, score=1.0):
    """造一組決定性鄰居:等權、可控 spread。預設 3 筆(MIN_NEIGHBORS)→ widen=1.0。"""
    return [{"payload": {field_key: v, "grade": grade, "confidence": conf}, "score": score}
            for v in values]


@pytest.fixture
def empty_table(monkeypatch):
    """強制空 q̂ 表 → 全退 1.64(模擬無 conformal_q.json)。"""
    monkeypatch.setattr(retrieval, "_Q_TABLE", {})
    return retrieval


@pytest.fixture
def tight_immersion_acidity(monkeypatch):
    """只給 immersion×acidity 一個收緊的 q̂(1.0 < 1.64);其餘退 fallback。"""
    monkeypatch.setattr(retrieval, "_Q_TABLE", {"immersion": {"acidity": 1.0}})
    return retrieval


# ────────────────────────────── conformal_z 解析 ──────────────────────────────

def test_conformal_z_fallback_when_no_mechanism(empty_table):
    assert conformal_z(None, "flavor_acidity") == CONFORMAL_Z_FALLBACK


def test_conformal_z_fallback_for_non_flavor_field(tight_immersion_acidity):
    # 參數軸(非 FLAVOR_FIELD_KEYS)恆走 1.64,縱使機制有表。
    assert conformal_z(BrewMechanism.IMMERSION, "water_temp_c") == CONFORMAL_Z_FALLBACK
    assert conformal_z(BrewMechanism.IMMERSION, "brew_ratio") == CONFORMAL_Z_FALLBACK


def test_conformal_z_reads_table_entry(tight_immersion_acidity):
    assert conformal_z(BrewMechanism.IMMERSION, "flavor_acidity") == 1.0


def test_conformal_z_accepts_enum_or_str(tight_immersion_acidity):
    # mechanism 可為 BrewMechanism 或其 .value 字串。
    assert conformal_z("immersion", "flavor_acidity") == 1.0


def test_conformal_z_unknown_cell_falls_back(tight_immersion_acidity):
    # 表裡無該軸 / 無該機制 → fallback。
    assert conformal_z(BrewMechanism.IMMERSION, "flavor_body") == CONFORMAL_Z_FALLBACK
    assert conformal_z(BrewMechanism.PERCOLATION, "flavor_acidity") == CONFORMAL_Z_FALLBACK


# ────────────────────────────── §4:表缺席 → 逐位相同 ──────────────────────────────

def test_no_table_byte_identical_to_legacy(empty_table):
    """空表時,帶 mechanism 與不帶 mechanism 的估計**逐位相同**(= 校準前的 1.64 行為)。"""
    hits = _hits("flavor_acidity", [5.0, 6.0, 7.0])
    legacy = weighted_estimate(hits, "flavor_acidity")  # 無 mechanism → 1.64
    with_mech = weighted_estimate(hits, "flavor_acidity", mechanism=BrewMechanism.IMMERSION)
    assert (with_mech.value, with_mech.lower, with_mech.upper) == \
           (legacy.value, legacy.lower, legacy.upper)


def test_fallback_absolute_baseline(empty_table):
    """對固定 fixture 釘死 1.64-fallback 的絕對 (value, lower, upper)——逐位比**校準前**的
    硬編基線,而非另一條新碼路徑(防兩條新路徑一起偏移仍互等的盲點)。

    [5,6,7]:mean=6.0、pstdev=√(2/3)≈0.81650、widen=1.0、z=1.64 →
    margin=1.64×0.81650≈1.33905 → lo=4.66、hi=7.34(round 2 位)。
    """
    hits = _hits("flavor_acidity", [5.0, 6.0, 7.0])
    est = weighted_estimate(hits, "flavor_acidity", mechanism=BrewMechanism.IMMERSION)
    assert est.value == 6.0
    assert est.lower == 4.66
    assert est.upper == 7.34
    assert est.source == "neighbors"


# ────────────────────────────── q̂ 收緊 / 放寬 ──────────────────────────────

def test_qhat_tightens_interval(tight_immersion_acidity):
    """q̂=1.0 < 1.64 → immersion×acidity 區間比 fallback 窄(點估不變)。"""
    hits = _hits("flavor_acidity", [5.0, 6.0, 7.0])  # spread≈0.816,widen=1.0
    base = weighted_estimate(hits, "flavor_acidity", mechanism=BrewMechanism.PERCOLATION)  # 1.64
    tight = weighted_estimate(hits, "flavor_acidity", mechanism=BrewMechanism.IMMERSION)   # 1.0
    assert tight.value == base.value                       # 點估不動
    width_tight = tight.upper - tight.lower
    width_base = base.upper - base.lower
    assert width_tight < width_base                        # 確實收緊
    # 半寬 = q̂ * spread * widen,且未頂地板(spread≈0.816 → 1.0*0.816 > 0.5)。
    assert width_tight == pytest.approx(2 * 1.0 * 0.8165, abs=0.02)


def test_qhat_widens_interval(monkeypatch):
    """q̂=2.5 > 1.64 → 區間比 fallback 寬(§4:某機制 z 路徑欠覆蓋時誠實放寬)。"""
    monkeypatch.setattr(retrieval, "_Q_TABLE", {"immersion": {"acidity": 2.5}})
    hits = _hits("flavor_acidity", [5.0, 6.0, 7.0])
    base = weighted_estimate(hits, "flavor_acidity", mechanism=BrewMechanism.PERCOLATION)
    wide = weighted_estimate(hits, "flavor_acidity", mechanism=BrewMechanism.IMMERSION)
    assert wide.value == base.value
    assert (wide.upper - wide.lower) > (base.upper - base.lower)


# ────────────────────────────── §1:機制硬隔離 ──────────────────────────────

def test_qhat_does_not_cross_mechanism(tight_immersion_acidity):
    """immersion×acidity 的 q̂ 絕不影響 percolation/pressure 的同軸(鐵則 §1)。"""
    hits = _hits("flavor_acidity", [5.0, 6.0, 7.0])
    imm = weighted_estimate(hits, "flavor_acidity", mechanism=BrewMechanism.IMMERSION)
    perc = weighted_estimate(hits, "flavor_acidity", mechanism=BrewMechanism.PERCOLATION)
    pres = weighted_estimate(hits, "flavor_acidity", mechanism=BrewMechanism.PRESSURE)
    none = weighted_estimate(hits, "flavor_acidity")
    # 三個非 immersion 的路徑彼此逐位相同(都走 1.64),且與 immersion 不同。
    assert (perc.lower, perc.upper) == (pres.lower, pres.upper) == (none.lower, none.upper)
    assert (imm.lower, imm.upper) != (perc.lower, perc.upper)


def test_qhat_does_not_cross_axis(tight_immersion_acidity):
    """immersion 只校了 acidity → 同機制的 body 仍走 1.64(軸亦是硬鍵)。"""
    hits_ac = _hits("flavor_acidity", [5.0, 6.0, 7.0])
    hits_bo = _hits("flavor_body", [5.0, 6.0, 7.0])
    ac = weighted_estimate(hits_ac, "flavor_acidity", mechanism=BrewMechanism.IMMERSION)
    bo = weighted_estimate(hits_bo, "flavor_body", mechanism=BrewMechanism.IMMERSION)
    bo_fallback = weighted_estimate(hits_bo, "flavor_body")
    assert (bo.lower, bo.upper) == (bo_fallback.lower, bo_fallback.upper)
    assert (ac.lower, ac.upper) != (bo.lower, bo.upper)


# ────────────────────────────── §4:地板 / 夾域在 q̂ 之後照舊 ──────────────────────────────

def test_floor_holds_after_tiny_qhat(monkeypatch):
    """極小 q̂ 也不得造出窄於 ±MIN_FLAVOR_MARGIN 的區間(地板在 q̂ 後套用)。"""
    monkeypatch.setattr(retrieval, "_Q_TABLE", {"immersion": {"acidity": 0.01}})
    hits = _hits("flavor_acidity", [5.0, 6.0, 7.0])  # value=6.0
    est = weighted_estimate(hits, "flavor_acidity", mechanism=BrewMechanism.IMMERSION)
    assert est.value == 6.0
    # 半寬被地板撐到 0.5(0.01*0.816≈0.008 << 0.5)。
    assert est.lower == pytest.approx(6.0 - MIN_FLAVOR_MARGIN, abs=1e-6)
    assert est.upper == pytest.approx(6.0 + MIN_FLAVOR_MARGIN, abs=1e-6)


def test_clamp_holds_after_large_qhat(monkeypatch):
    """大 q̂ 把區間推出 [0,10] → 夾回域內(夾域在 q̂ 後套用,§4 覆蓋只增不減)。"""
    monkeypatch.setattr(retrieval, "_Q_TABLE", {"immersion": {"acidity": 9.0}})
    hits = _hits("flavor_acidity", [8.0, 9.0, 10.0])  # value=9.0,spread≈0.816
    est = weighted_estimate(hits, "flavor_acidity", mechanism=BrewMechanism.IMMERSION)
    # margin = 9*0.816 ≈ 7.35 → hi=16.35 夾到 10、lo=1.65。
    assert est.upper == 10.0
    assert est.lower >= 0.0
    assert est.lower < est.upper


# ────────────────────────────── _load_q_artifact 降級 ──────────────────────────────

def test_load_q_table_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(retrieval, "_Q_TABLE_PATH", tmp_path / "nope.json")
    assert retrieval._load_q_artifact() == ({}, None)


def test_load_q_table_corrupt_json(monkeypatch, tmp_path):
    p = tmp_path / "conformal_q.json"
    p.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(retrieval, "_Q_TABLE_PATH", p)
    assert retrieval._load_q_artifact() == ({}, None)


def test_load_q_table_filters_bad_values(monkeypatch, tmp_path):
    p = tmp_path / "conformal_q.json"
    p.write_text(json.dumps({"q": {
        "immersion": {"acidity": 1.5, "body": -3.0, "clarity": "oops", "balance": 0,
                      "sweetness": float("inf")},
        "percolation": "not-a-dict",
        "pressure": {},
    }}), encoding="utf-8")
    monkeypatch.setattr(retrieval, "_Q_TABLE_PATH", p)
    table, _ = retrieval._load_q_artifact()
    # 只留有限正數;負/零/非數值/Infinity 剔除;非 dict 機制剔除;空 dict 機制不留。
    assert table == {"immersion": {"acidity": 1.5}}


def test_load_q_table_no_q_key(monkeypatch, tmp_path):
    p = tmp_path / "conformal_q.json"
    p.write_text(json.dumps({"provenance": {"x": 1}}), encoding="utf-8")
    monkeypatch.setattr(retrieval, "_Q_TABLE_PATH", p)
    assert retrieval._load_q_artifact() == ({}, None)


def test_load_q_artifact_parses_embedder(monkeypatch, tmp_path):
    """provenance.embedder 被解出,供讀取閘比對線上嵌入器。"""
    p = tmp_path / "conformal_q.json"
    p.write_text(json.dumps({
        "provenance": {"embedder": "workers_ai:@cf/baai/bge-m3"},
        "q": {"immersion": {"acidity": 1.5}},
    }), encoding="utf-8")
    monkeypatch.setattr(retrieval, "_Q_TABLE_PATH", p)
    table, embedder = retrieval._load_q_artifact()
    assert table == {"immersion": {"acidity": 1.5}}
    assert embedder == "workers_ai:@cf/baai/bge-m3"


def test_load_q_artifact_missing_embedder_is_none(monkeypatch, tmp_path):
    """有 q 但 provenance 無 embedder → embedder=None(讀取閘保守視為不適用)。"""
    p = tmp_path / "conformal_q.json"
    p.write_text(json.dumps({"q": {"immersion": {"acidity": 1.5}}}), encoding="utf-8")
    monkeypatch.setattr(retrieval, "_Q_TABLE_PATH", p)
    _, embedder = retrieval._load_q_artifact()
    assert embedder is None


# ──────────────────── conformal_active_for 讀取閘(§4 嵌入器比對) ────────────────────

def test_conformal_active_for_matches_calibration_embedder(monkeypatch):
    """線上嵌入器 == 校準嵌入器 → 適用。"""
    monkeypatch.setattr(retrieval, "_Q_TABLE", {"immersion": {"acidity": 1.0}})
    monkeypatch.setattr(retrieval, "_Q_TABLE_EMBEDDER", "workers_ai:@cf/baai/bge-m3")
    assert retrieval.conformal_active_for("workers_ai:@cf/baai/bge-m3") is True


def test_conformal_active_for_mismatch_falls_back(monkeypatch):
    """線上嵌入器 != 校準嵌入器(離線 hash)→ 不適用(呼叫端退 1.64)。"""
    monkeypatch.setattr(retrieval, "_Q_TABLE", {"immersion": {"acidity": 1.0}})
    monkeypatch.setattr(retrieval, "_Q_TABLE_EMBEDDER", "workers_ai:@cf/baai/bge-m3")
    assert retrieval.conformal_active_for("local-hash:256") is False
    assert retrieval.conformal_active_for(None) is False


def test_conformal_active_for_empty_table_or_no_embedder(monkeypatch):
    """表空 / 無 embedder 記錄 → 保守不適用,縱使 model_id 給了值。"""
    monkeypatch.setattr(retrieval, "_Q_TABLE", {})
    monkeypatch.setattr(retrieval, "_Q_TABLE_EMBEDDER", "workers_ai:@cf/baai/bge-m3")
    assert retrieval.conformal_active_for("workers_ai:@cf/baai/bge-m3") is False
    monkeypatch.setattr(retrieval, "_Q_TABLE", {"immersion": {"acidity": 1.0}})
    monkeypatch.setattr(retrieval, "_Q_TABLE_EMBEDDER", None)
    assert retrieval.conformal_active_for("workers_ai:@cf/baai/bge-m3") is False


# ──────────────── q_artifact_status 開機觀測(化暗為明,§4) ────────────────

def test_q_artifact_status_reports_entries_md5_embedder(monkeypatch, tmp_path):
    """開機觀測:回報 q̂ 條目數 / 機制數 / 檔案 md5 / 校準嵌入器,且 md5 取自磁碟 conformal_q.json。"""
    p = tmp_path / "conformal_q.json"
    p.write_bytes(b"some-bytes")
    monkeypatch.setattr(retrieval, "_Q_TABLE_PATH", p)
    monkeypatch.setattr(retrieval, "_Q_TABLE",
                        {"immersion": {"acidity": 1.0, "body": 2.0}, "percolation": {"acidity": 1.5}})
    monkeypatch.setattr(retrieval, "_Q_TABLE_EMBEDDER", "workers_ai:@cf/baai/bge-m3")
    st = retrieval.q_artifact_status()
    assert st["entries"] == 3 and st["mechanisms"] == 2
    assert st["calibrated_embedder"] == "workers_ai:@cf/baai/bge-m3"
    assert st["md5"] == hashlib.md5(b"some-bytes").hexdigest()
    assert "active" not in st  # 未給 model_id → 不報 active


def test_q_artifact_status_active_tracks_embedder_match(monkeypatch, tmp_path):
    """給 model_id 時附 active:線上嵌入器 == 校準嵌入器才適用(對齊 conformal_active_for)。"""
    monkeypatch.setattr(retrieval, "_Q_TABLE_PATH", tmp_path / "nope.json")  # 缺檔
    monkeypatch.setattr(retrieval, "_Q_TABLE", {"immersion": {"acidity": 1.0}})
    monkeypatch.setattr(retrieval, "_Q_TABLE_EMBEDDER", "workers_ai:@cf/baai/bge-m3")
    assert retrieval.q_artifact_status("workers_ai:@cf/baai/bge-m3")["active"] is True
    assert retrieval.q_artifact_status("local-hash:256")["active"] is False
    assert retrieval.q_artifact_status("workers_ai:@cf/baai/bge-m3")["md5"] is None  # 缺檔 → None


# ────────────────────────────── 校準工具純函式 ──────────────────────────────

def test_conformal_quantile_finite_sample_rank():
    """保守有限樣本分位 = 第 ceil((n+1)(1-α)) 小(1-indexed)。"""
    scores = [float(i) for i in range(1, 21)]  # 1..20
    # n=20, alpha=0.1 → rank=ceil(21*0.9)=ceil(18.9)=19 → 第 19 小 = 19.0
    assert _conformal_quantile(scores, 0.1) == 19.0


def test_conformal_quantile_insufficient_returns_none():
    # n=8,rank=ceil(9*0.9)=9 > 8 → 資料不足保證 → None(呼叫端退 1.64)。
    assert _conformal_quantile([1.0] * 8, 0.1) is None
    assert _conformal_quantile([], 0.1) is None


def test_conformal_quantile_min_n_for_guarantee():
    # n=9 剛好可保證:rank=ceil(10*0.9)=9 ≤ 9 → 最大值。
    assert _conformal_quantile([float(i) for i in range(1, 10)], 0.1) == 9.0


def _per_record_entry(mech, axis, *, true, pred, lower, upper, source="neighbors"):
    return {"mechanism": mech, "axes": {axis: {
        "true": true, "pred": pred, "lower": lower, "upper": upper, "source": source,
        "abs_err": abs(pred - true), "covered": lower <= true <= upper}}}


def test_collect_scores_excludes_non_z_path():
    """source=prior 的點(物理粗略/先驗路徑)不用 z → 不進校準。"""
    recs = [
        _per_record_entry("immersion", "acidity", true=6.0, pred=6.0, lower=4.0, upper=8.0,
                          source="prior"),
    ]
    scores, stats = collect_scores(recs)
    assert scores.get("immersion", {}).get("acidity", []) == []
    assert stats["immersion"]["acidity"]["skip_not_z_path"] == 1


def test_collect_scores_excludes_clamped_and_floored():
    """夾域(不對稱)與頂地板(半寬≈0.5)的點排除(原始半寬不可復原)。"""
    recs = [
        # 夾域:上界頂 10 → (hi-pred)≠(pred-lo)。
        _per_record_entry("immersion", "acidity", true=9.0, pred=9.0, lower=6.0, upper=10.0),
        # 頂地板:半寬=0.5。
        _per_record_entry("immersion", "acidity", true=5.0, pred=5.0, lower=4.5, upper=5.5),
    ]
    scores, stats = collect_scores(recs)
    assert scores.get("immersion", {}).get("acidity", []) == []
    assert stats["immersion"]["acidity"]["skip_clamped"] == 1
    assert stats["immersion"]["acidity"]["skip_floored"] == 1


def test_collect_scores_normalized_score_formula():
    """乾淨點:s = 1.64 * |true-pred| / 半寬。"""
    # 半寬=1.0(對稱、未頂地板),err=0.5 → s = 1.64*0.5/1.0 = 0.82。
    recs = [_per_record_entry("percolation", "body", true=5.5, pred=5.0, lower=4.0, upper=6.0)]
    scores, _ = collect_scores(recs)
    assert scores["percolation"]["body"] == [pytest.approx(1.64 * 0.5 / 1.0, abs=1e-6)]


def test_build_table_ceil_rounds_up_and_min_n():
    """q̂ 向上取 4 位(§4 不可捨到分位之下);未達 min_n → 不寫條目。"""
    # 造 12 個分數,使 conformal 分位落在需進位的小數。
    scores = {"percolation": {"acidity": [0.123451 + i for i in range(12)]}}
    # min_n=12 → 寫;min_n=13 → 不寫。
    t_written = build_table(scores, 0.1, min_n=12)
    assert "percolation" in t_written and "acidity" in t_written["percolation"]
    q = t_written["percolation"]["acidity"]
    # 對應 _conformal_quantile 的值,且為其 ceil-to-4dp(≥ 原始分位)。
    raw = _conformal_quantile(scores["percolation"]["acidity"], 0.1)
    assert q >= raw
    assert q == pytest.approx(__import__("math").ceil(raw * 10000) / 10000, abs=1e-9)

    t_skip = build_table(scores, 0.1, min_n=13)
    assert t_skip == {}


# ──────────────── staleness 護欄:q̂ provenance 必須對齊 live 語料 ────────────────

def test_conformal_q_provenance_matches_live_corpus():
    """conformal_q.json 記的 corpus md5/n_records 必須 == 當前 corpus/global.jsonl(§4 staleness 護欄)。

    log_calibration 累積 A/B 真值進 canonical;rebuild 後殘差分佈漂移,逐格 q̂ 可能變太緊而無
    訊號 → 靜默侵蝕 §4 覆蓋保證。把 q̂ 的 provenance 綁死 live 語料:動過 corpus 就**強制**重校
    (否則此測試紅 → CI 擋下未重校的變更)。1.64 fallback 防災,但緊掉的 q̂ 仍可能下探,故以
    失敗測試硬性執行。動語料後修法:重跑 `python -m tools.calibrate_conformal`(workers_ai)。
    """
    q_path = Path(retrieval.__file__).resolve().parent / "conformal_q.json"
    if not q_path.exists():
        pytest.skip("無 conformal_q.json(全退 1.64 fallback;無 q̂ 可過期)")
    prov = json.loads(q_path.read_text(encoding="utf-8")).get("provenance", {})
    corp = prov.get("corpus", {})
    live_md5 = hashlib.md5(CORPUS_PATH.read_bytes()).hexdigest()
    live_n = len(read_jsonl(CORPUS_PATH))
    assert corp.get("md5") == live_md5, (
        f"conformal_q.json 過期:provenance corpus.md5={corp.get('md5')} 但 live "
        f"corpus md5={live_md5}。動過 corpus/global.jsonl → 須重跑 "
        f"`python -m tools.calibrate_conformal`(workers_ai 嵌入)重校 q̂ 並一併提交。"
    )
    assert corp.get("n_records") == live_n, (
        f"conformal_q.json provenance n_records={corp.get('n_records')} != live {live_n}"
    )
