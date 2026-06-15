"""bootstrap 單元測試:把策展語料 corpus/global.jsonl 載入 canonical 真相層,
再 rebuild 出向量庫。核心驗收:**rebuild 後筆數 ≈ 語料行數(537),而非 6 筆 seeds**。

涵蓋:
  - 空 canonical 載入語料筆數正確、可無損列舉回;
  - 非空 canonical 未 force 拒絕(一次性初始化);force 整份覆寫;
  - bootstrap → rebuild 端到端筆數 == 語料行數(且 > 6,明確不是 seeds 行為)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cie.bootstrap import CORPUS_PATH, bootstrap, load_corpus
from cie.canonical import LocalJsonlCanonical
from cie.config import Config
from cie.portability import export_jsonl, read_jsonl
from cie.rebuild import rebuild
from cie.schema import BeanRoast, BrewMechanism, BrewParams, Grade, Record
from cie.store import VectorStore


def _rec(origin: str, mech: BrewMechanism = BrewMechanism.PERCOLATION) -> Record:
    return Record(
        bean=BeanRoast(origin=origin, roast_agtron=70),
        params=BrewParams(brew_mechanism=mech, method="V60", water_temp_c=92, brew_ratio=16.0),
        grade=Grade.B, user_id="global", source="unit-test",
    )


def _small_corpus(tmp_path: Path) -> Path:
    p = tmp_path / "global.jsonl"
    export_jsonl([_rec("Ethiopia"), _rec("Kenya"), _rec("Brazil", BrewMechanism.IMMERSION)], p)
    return p


def test_bootstrap_loads_corpus_into_empty_canonical(tmp_path: Path):
    canon = LocalJsonlCanonical(path=str(tmp_path / "c.jsonl"))
    n = bootstrap(canonical=canon, path=_small_corpus(tmp_path))
    assert n == 3
    assert sum(1 for _ in canon.iter_records()) == 3


def test_bootstrap_refuses_nonempty_without_force(tmp_path: Path):
    canon = LocalJsonlCanonical(path=str(tmp_path / "c.jsonl"))
    corpus = _small_corpus(tmp_path)
    bootstrap(canonical=canon, path=corpus)
    with pytest.raises(RuntimeError, match="一次性"):
        bootstrap(canonical=canon, path=corpus)  # 第二次:非空且未 force → 拒絕


def test_bootstrap_force_replaces_not_appends(tmp_path: Path):
    canon = LocalJsonlCanonical(path=str(tmp_path / "c.jsonl"))
    corpus = _small_corpus(tmp_path)
    bootstrap(canonical=canon, path=corpus)
    n = bootstrap(canonical=canon, path=corpus, force=True)
    assert n == 3
    assert sum(1 for _ in canon.iter_records()) == 3  # 覆寫,非 6


def test_bootstrap_then_rebuild_yields_corpus_count_not_six(tmp_path: Path):
    """端到端守門:從『真實』corpus/global.jsonl bootstrap → rebuild,
    向量庫筆數必須等於語料行數且遠多於 6(證明召回庫不是 seeds)。"""
    corpus_n = len(read_jsonl(CORPUS_PATH))
    assert corpus_n > 6, "前置:語料本應遠多於 6 筆 seeds"

    canon = LocalJsonlCanonical(path=str(tmp_path / "canonical.jsonl"))
    assert bootstrap(canonical=canon) == corpus_n  # 用真實 CORPUS_PATH

    store = VectorStore(Config(embedding_provider="local", embedding_dim=128))
    written = rebuild(store=store, canonical=canon)
    assert written == corpus_n
    assert store.count() == corpus_n
    assert store.count() > 6


def test_load_corpus_matches_file_lines():
    assert len(load_corpus()) == len(read_jsonl(CORPUS_PATH))
