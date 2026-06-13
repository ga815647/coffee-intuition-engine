"""分層 k-fold CV 盲測 harness 測試(離線)。

對應升級需求:測試集改用「corpus/global.jsonl 的 A/B 級記錄、按機制分層」當 holdout
(取代撐不起結論的 5 筆合成 holdout)。斷言:
  - 每筆 A/B 記錄正好被評測一次(out-of-fold);C 級**永不**當 holdout 真值;
  - 分機制報告 n / MAE / 覆蓋 / 方向皆產得出;
  - 三道(+C 級)防洩漏全 True;
  - 分層 round-robin 大致均衡;切折確定性可重現(不靠隨機 uuid)。

依任務要求,**不對 MAE 下硬門檻**(離線雜湊嵌入本就不準)。
"""
from __future__ import annotations

import pytest

from cie.config import Config
from cie.portability import read_jsonl
from cie.schema import BrewMechanism
from eval.run import (CORPUS_PATH, HOLDOUT_GRADES, _holdout_signature,
                      _stratified_folds, run_cv_eval)

MECHS = [m.value for m in BrewMechanism]


def _cfg():
    return Config(embedding_provider="local", embedding_dim=128)


def _eligible():
    return [r for r in read_jsonl(CORPUS_PATH) if r.grade.value in HOLDOUT_GRADES]


@pytest.fixture(scope="module")
def cv():
    """整份 CV 只跑一次(較重),多個斷言共用。"""
    return run_cv_eval(k=5, config=_cfg())


def test_cv_holds_out_every_ab_record_once(cv):
    assert cv["mode"] == "cv_stratified"
    assert cv["k_folds"] == 5
    assert cv["n_holdout"] == len(_eligible())  # 每筆 A/B 評測一次(out-of-fold)
    assert cv["n_holdout"] > 6                   # 明確不是 5 筆合成 / 6 筆 seeds


def test_cv_c_grade_never_holdout(cv):
    # 報告層:C 級守衛旗標為真
    assert cv["leakage_checks"]["c_grade_never_holdout"] is True
    # 結構層:分機制 holdout 加總 == A/B 數(C 完全不在 holdout 池)
    assert sum(m["n_holdout"] for m in cv["by_mechanism"].values()) == len(_eligible())
    # 直接驗:語料含 C(否則此守衛空洞),且 eligible 不含任何 C
    grades = {r.grade.value for r in read_jsonl(CORPUS_PATH)}
    assert "C" in grades
    assert all(r.grade.value in HOLDOUT_GRADES for r in _eligible())


def test_cv_all_leakage_guards_pass(cv):
    lc = cv["leakage_checks"]
    assert lc["holdout_ids_excluded"] is True          # 留出豆不在召回庫
    assert lc["no_holdout_in_evidence"] is True         # 證據未含留出豆
    assert lc["predictions_not_written_back"] is True   # 預測不寫回
    assert lc["c_grade_never_holdout"] is True           # C 不當真值


def test_cv_reports_per_mechanism(cv):
    bm = cv["by_mechanism"]
    assert set(bm) == set(MECHS)  # 三機制都有
    for mech, m in bm.items():
        assert m["n_holdout"] > 0
        assert m["mae"] is not None      # 分機制 MAE 算得出
        assert m["coverage"] is not None  # 分機制覆蓋算得出
        assert m["direction_pairs"] >= 1  # 分機制方向(同機制配對)算得出
        assert m["direction_acc"] is not None


def test_cv_per_mechanism_counts_match_corpus(cv):
    """分機制 holdout 數 == 語料中該機制的 A/B 記錄數(分層不漏不重)。"""
    elig = _eligible()
    for mech in MECHS:
        want = sum(1 for r in elig if r.params.brew_mechanism.value == mech)
        assert cv["by_mechanism"][mech]["n_holdout"] == want


def test_cv_folds_stratified_balanced():
    folds = _stratified_folds(_eligible(), 5)
    for mech in MECHS:
        counts = [sum(1 for r in f if r.params.brew_mechanism.value == mech) for f in folds]
        assert max(counts) - min(counts) <= 1  # round-robin 分層:各折該機制數差 ≤ 1


def test_cv_fold_split_is_deterministic():
    """切折只靠內容指紋,不靠隨機 uuid:兩次獨立載入應得相同(內容)分割。"""
    a = _stratified_folds(_eligible(), 5)
    b = _stratified_folds(_eligible(), 5)
    sa = [sorted(repr(_holdout_signature(r)) for r in f) for f in a]
    sb = [sorted(repr(_holdout_signature(r)) for r in f) for f in b]
    assert sa == sb
