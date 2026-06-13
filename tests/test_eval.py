"""盲測評測 harness 單元測試(離線)。

依任務要求,**不對 MAE 下硬門檻**(離線雜湊嵌入本就不準)。只斷言:
  - harness 能跑、留出豆數正確;
  - 留出豆確實被排除於召回庫、任何證據都不含留出豆(防洩漏);
  - 覆蓋率算得出、方向(配對排序)指標產得出;
  - 方向只在**同機制**內配對(不跨機制);
  - 預測**結構上不吃真值風味**(竄改真值風味,預測不變)、且不寫回(庫筆數不變)。
"""
from __future__ import annotations

from dataclasses import replace

from cie.config import Config
from cie.engine import Engine
from cie.portability import read_jsonl
from cie.schema import FLAVOR_AXES
from cie.store import VectorStore

from eval.run import (CORPUS_PATH, DATASET_PATH, _holdout_signature,
                      build_library_store, run_eval)


def _cfg():
    return Config(embedding_provider="local", embedding_dim=256)


def test_harness_runs_and_counts_holdouts():
    r = run_eval(config=_cfg())
    assert r["n_holdout"] == 5
    # 召回庫來自 corpus/global.jsonl(扣除 holdout),不是 6 筆 seeds/anchors.jsonl。
    corpus_n = len(read_jsonl(CORPUS_PATH))
    assert corpus_n - 5 <= r["library_count"] <= corpus_n
    assert r["library_count"] > 6  # 明確不是「只灌 6 筆 seeds」的舊行為


def test_library_is_corpus_minus_holdout_by_content():
    """召回庫源自策展語料且按內容扣除 holdout:把一筆 holdout 偽裝成語料的
    完整副本(沿用其 bean+params 內容指紋),它必須不出現在召回庫裡。"""
    holdouts = read_jsonl(DATASET_PATH)
    holdout_ids = {h.id for h in holdouts}
    store = build_library_store(holdout_ids, _cfg(), holdout_records=holdouts)
    lib_sigs = {_holdout_signature(r) for r in store.iter_records()}
    for h in holdouts:
        assert _holdout_signature(h) not in lib_sigs  # 內容指紋層級確被扣除


def test_leakage_checks_all_pass():
    r = run_eval(config=_cfg())
    lc = r["leakage_checks"]
    assert lc["holdout_ids_excluded"] is True
    assert lc["no_holdout_in_evidence"] is True
    assert lc["predictions_not_written_back"] is True


def test_holdouts_truly_absent_from_library():
    holdouts = read_jsonl(DATASET_PATH)
    holdout_ids = {r.id for r in holdouts}
    store = build_library_store(holdout_ids, _cfg())
    store_ids = {r.id for r in store.iter_records()}
    assert holdout_ids.isdisjoint(store_ids)


def test_coverage_is_computed():
    r = run_eval(config=_cfg())
    assert r["overall"]["coverage"] is not None
    assert 0.0 <= r["overall"]["coverage"] <= 1.0
    # 至少一軸算得出覆蓋率
    assert any(r["axes"][a]["coverage"] is not None for a in FLAVOR_AXES)


def test_direction_metric_is_produced():
    r = run_eval(config=_cfg())
    acid = r["direction"]["acidity"]
    assert acid["n_pairs"] >= 1
    assert acid["pairwise_accuracy"] is not None
    assert 0.0 <= acid["pairwise_accuracy"] <= 1.0


def test_direction_pairs_are_within_mechanism_only():
    """資料集有 3 筆 percolation 留出豆 → acidity 最多 C(3,2)=3 對,
    遠少於跨機制全配對 C(5,2)=10:證明方向指標不跨機制(鐵則 §12.1)。"""
    r = run_eval(config=_cfg())
    assert r["direction"]["acidity"]["n_pairs"] == 3


def test_prediction_ignores_true_flavor():
    """結構防洩漏:predict() 只吃 bean+params;竄改真值風味,預測完全不變。"""
    holdouts = read_jsonl(DATASET_PATH)
    store = build_library_store({h.id for h in holdouts}, _cfg())
    engine = Engine(store=store, canonical=None)
    h = holdouts[0]
    p1 = engine.predict(h.bean, h.params)
    h2 = h.model_copy(deep=True)
    h2.flavor.acidity = 0.0
    h2.flavor.sweetness = 0.0
    h2.flavor.body = 0.0
    p2 = engine.predict(h2.bean, h2.params)
    assert p1["predicted_flavor"] == p2["predicted_flavor"]


def test_leakage_detector_flips_when_holdout_leaks_into_store(tmp_path):
    """主動證明偵測器非虛設:蓄意把一筆留出豆灌進召回庫,run_eval 必須把
    holdout_ids_excluded 與 no_holdout_in_evidence **都標 False**。

    註:正式 dataset 用人類可讀 id(holdout-*),而向量庫點 id 需 UUID,故那些
    留出豆**結構上根本塞不進庫**(更強的防洩漏)。此處改用 UUID id 的臨時資料集
    才能讓洩漏發生,以驗證偵測器確實會翻 False(回應審查:綠斷言非虛設)。"""
    leaked = read_jsonl(DATASET_PATH)[0].model_copy(deep=True)
    leaked.id = "00000000-0000-0000-0000-0000000000aa"  # 向量庫點 id 需合法 UUID
    ds = tmp_path / "leak.jsonl"
    ds.write_text(leaked.model_dump_json() + "\n", encoding="utf-8")

    cfg = _cfg()
    store = VectorStore(replace(cfg, store_backend_override="memory"))
    store.upsert(leaked)  # 蓄意洩漏:庫裡含這筆「留出豆」
    r = run_eval(dataset_path=ds, store=store, config=cfg)
    lc = r["leakage_checks"]
    assert lc["holdout_ids_excluded"] is False     # id 互斥檢查抓到
    assert lc["no_holdout_in_evidence"] is False   # 證據檢查抓到(縱深防禦有效)


def test_eval_does_not_mutate_store():
    holdouts = read_jsonl(DATASET_PATH)
    store = build_library_store({h.id for h in holdouts}, _cfg())
    before = store.count()
    run_eval(store=store, config=_cfg())
    assert store.count() == before  # 評測純讀,不寫回
