"""可攜性:canonical JSONL 匯出/匯入 + 跨模型重建。

驗證鐵則(§14.5):canonical 是真相、向量是衍生物;
換嵌入模型(維度不同)時,從 JSONL 重新嵌入即可重建索引,不靠搬舊向量。
"""
from __future__ import annotations

from pathlib import Path

from cie.config import Config
from cie.portability import export_jsonl, export_store, import_jsonl, read_jsonl
from cie.schema import (
    AcidityType, BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process,
    Record, WaterProfile,
)
from cie.seed import SEED_PATH
from cie.store import VectorStore


def _rec(origin="Ethiopia", mech=BrewMechanism.PERCOLATION):
    return Record(
        bean=BeanRoast(origin=origin, variety="Heirloom", process=Process.WASHED, roast_agtron=74),
        water=WaterProfile(gh=68, kh=40, recipe_name="SCA-target"),
        params=BrewParams(brew_mechanism=mech, method="V60", water_temp_c=92,
                          brew_ratio=16.0, grind_um=650, tds_pct=1.38, ey_pct=20.4),
        flavor=FlavorProfile(acidity=7.5, acidity_type=AcidityType.CITRIC, sweetness=7.0,
                             flavor_notes=["bergamot", "white_floral"]),
        grade=Grade.A, protocol="SCA_cupping", user_id="global",
    )


def _mem_store(dim=256):
    return VectorStore(Config(embedding_provider="local", embedding_dim=dim))


def test_export_then_read_is_lossless(tmp_path: Path):
    recs = [_rec("Ethiopia"), _rec("Colombia", BrewMechanism.IMMERSION)]
    path = tmp_path / "out.jsonl"
    n = export_jsonl(recs, path)
    assert n == 2
    back = read_jsonl(path)
    assert len(back) == 2
    # 全量保真:water、acidity_type、notes、id 都還在
    assert back[0].id == recs[0].id
    assert back[0].water.gh == 68
    assert back[0].water.recipe_name == "SCA-target"
    assert back[0].flavor.acidity_type == AcidityType.CITRIC
    assert back[0].flavor.flavor_notes == ["bergamot", "white_floral"]
    assert back[1].params.brew_mechanism == BrewMechanism.IMMERSION


def test_export_store_roundtrip_queryable(tmp_path: Path):
    src = _mem_store()
    src.upsert_many([_rec("Ethiopia"), _rec("Kenya")])
    path = tmp_path / "dump.jsonl"
    assert export_store(src, path) == 2

    dst = _mem_store()
    assert import_jsonl(path, dst) == 2
    assert dst.count() == 2
    hits = dst.search("Ethiopia washed", BrewMechanism.PERCOLATION)
    assert hits, "重建後應可召回"


def test_rebuild_across_embedding_models(tmp_path: Path):
    """模擬切換嵌入模型(維度 256→64):從 canonical JSONL 重嵌即可重建。"""
    src = _mem_store(dim=256)
    src.upsert_many([_rec("Ethiopia"), _rec("Brazil", BrewMechanism.IMMERSION)])
    path = tmp_path / "canonical.jsonl"
    export_store(src, path)

    dst = _mem_store(dim=64)  # 不同『模型』= 不同維度的新索引
    assert import_jsonl(path, dst) == 2
    assert dst.embedder.dim == 64
    # 機制硬分區仍成立:percolation 查詢不會撈到 immersion
    hits = dst.search("Ethiopia", BrewMechanism.PERCOLATION)
    assert all(h["payload"]["brew_mechanism"] == "percolation" for h in hits)


def test_import_seed_anchors_canonical_format(tmp_path: Path):
    """seeds/anchors.jsonl 即 canonical 格式,應可直接 import 重建。"""
    dst = _mem_store()
    n = import_jsonl(SEED_PATH, dst)
    assert n >= 6
    assert dst.count() >= 6
