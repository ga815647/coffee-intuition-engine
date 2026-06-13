"""Canonical 真相層 + rebuild 單元測試(全離線,假用戶端,不觸網路)。

驗證鐵則(§15 / §14.5):
  - canonical 為真相、向量為衍生物;Vectorize 後端務必有獨立 canonical sink。
  - log_calibration / seed 雙寫 canonical;prediction 級不入真相(不被 rebuild 復活)。
  - rebuild 一律用『當前』嵌入器重嵌、不搬舊向量(換維度也能重建並查得到)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cie.canonical import (
    CanonicalStore, LocalJsonlCanonical, R2Canonical, get_canonical, maybe_get_canonical,
)
from cie.config import Config
from cie.engine import Engine
from cie.rebuild import rebuild
from cie.schema import (
    AcidityType, BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process, Record,
)
from cie.store import VectorStore, VectorizeStore


# ────────────────────────────── 共用 fixtures ──────────────────────────────

def _rec(origin="Ethiopia", mech=BrewMechanism.PERCOLATION, grade=Grade.A, notes=("bergamot",)):
    return Record(
        bean=BeanRoast(origin=origin, variety="Heirloom", process=Process.WASHED, roast_agtron=74),
        params=BrewParams(brew_mechanism=mech, method="V60", water_temp_c=92,
                          brew_ratio=16.0, grind_um=650, tds_pct=1.38, ey_pct=20.4),
        flavor=FlavorProfile(acidity=7.5, acidity_type=AcidityType.CITRIC, sweetness=7.0,
                             flavor_notes=list(notes)),
        grade=grade, protocol="SCA_cupping" if grade == Grade.A else "", user_id="global",
    )


class FakeCF:
    """同時假裝 Vectorize 與 R2 的 CloudflareClient(in-memory)。"""

    def __init__(self, dim: int = 8):
        self.dim = dim
        self.upserts = []
        self.r2_objects: dict[tuple[str, str], str] = {}

    # Vectorize
    def vectorize_upsert(self, index, lines):
        self.upserts.append((index, list(lines)))
        return {"mutationId": "m-test"}

    def vectorize_query(self, index, body):
        return {"count": 0, "matches": []}

    def vectorize_info(self, index):
        return {"vectorCount": sum(len(l) for _, l in self.upserts)}

    # R2
    def r2_get_object(self, bucket, key):
        return self.r2_objects.get((bucket, key))  # None = 404

    def r2_put_object(self, bucket, key, body, content_type="application/x-ndjson"):
        self.r2_objects[(bucket, key)] = body


def _vectorize_store(client, dim=8):
    cfg = Config(embedding_provider="local", embedding_dim=dim, vectorize_index="cie-test")
    return VectorizeStore(config=cfg, client=client)


# ────────────────────────────── LocalJsonlCanonical ──────────────────────────────

def test_local_canonical_append_iter_lossless(tmp_path: Path):
    canon = LocalJsonlCanonical(path=str(tmp_path / "canonical.jsonl"))
    r1, r2 = _rec("Ethiopia"), _rec("Kenya", BrewMechanism.IMMERSION)
    canon.append(r1)
    canon.append(r2)
    back = list(canon.iter_records())
    assert [r.id for r in back] == [r1.id, r2.id]
    assert back[0].flavor.acidity_type == AcidityType.CITRIC
    assert back[1].params.brew_mechanism == BrewMechanism.IMMERSION


def test_local_canonical_iter_on_missing_file_is_empty(tmp_path: Path):
    canon = LocalJsonlCanonical(path=str(tmp_path / "nope.jsonl"))
    assert list(canon.iter_records()) == []


def test_local_canonical_extend_count(tmp_path: Path):
    canon = LocalJsonlCanonical(path=str(tmp_path / "c.jsonl"))
    assert canon.extend([_rec("A"), _rec("B"), _rec("C")]) == 3
    assert len(list(canon.iter_records())) == 3


# ────────────────────────────── 工廠 / sink 選擇 ──────────────────────────────

def test_get_canonical_defaults_to_local(tmp_path: Path):
    canon = get_canonical(Config(canonical_path=str(tmp_path / "c.jsonl")))
    assert isinstance(canon, LocalJsonlCanonical)


def test_get_canonical_r2_when_creds_and_bucket():
    cfg = Config(cf_account_id="a", cf_api_token="b", r2_bucket="cie-bucket")
    assert cfg.canonical_backend == "r2"
    canon = get_canonical_with_fake(cfg)
    assert isinstance(canon, R2Canonical)


def get_canonical_with_fake(cfg):
    # R2Canonical 需 client;直接注入假用戶端避免觸網路。
    return R2Canonical(config=cfg, client=FakeCF())


def test_maybe_get_canonical_none_for_memory_store():
    store = VectorStore(Config(embedding_provider="local", embedding_dim=64))
    # 記憶體後端自存 _canonical(有 iter_records)→ 不需獨立 sink
    assert maybe_get_canonical(store) is None


def test_maybe_get_canonical_active_for_vectorize(tmp_path: Path):
    store = _vectorize_store(FakeCF())
    canon = maybe_get_canonical(store, Config(canonical_path=str(tmp_path / "c.jsonl")))
    assert canon is not None  # Vectorize 無 iter_records → 必須有 sink


def test_engine_auto_wires_canonical_for_vectorize_store():
    """生產路徑迴歸防線:Engine(store=vectorize) 未顯式給 canonical 時,建構子
    須自動經 maybe_get_canonical 掛上 sink。若有人刪掉那行 else 分支,Vectorize
    會再次『無源』(本輪要防的失敗)——此測試會抓到。建構不觸發寫檔。"""
    engine = Engine(store=_vectorize_store(FakeCF()))  # 不傳 canonical
    assert engine.canonical is not None
    assert isinstance(engine.canonical, CanonicalStore)


def test_engine_no_canonical_sink_for_memory_store():
    """記憶體後端自存 _canonical(有 iter_records)→ 建構子不應另掛 sink,
    避免重複寫與測試副作用(不會憑空寫出 ./data/canonical.jsonl)。"""
    engine = Engine(store=VectorStore(Config(embedding_provider="local", embedding_dim=64)))
    assert engine.canonical is None


# ────────────────────────────── R2Canonical(假用戶端) ──────────────────────────────

def test_r2_canonical_append_read_modify_write():
    fake = FakeCF()
    canon = R2Canonical(bucket="b", key="canonical.jsonl", client=fake)
    assert list(canon.iter_records()) == []      # 物件不存在 → 空(404→None)
    canon.append(_rec("Ethiopia"))
    canon.append(_rec("Kenya", BrewMechanism.IMMERSION))
    back = list(canon.iter_records())
    assert len(back) == 2
    assert back[1].params.brew_mechanism == BrewMechanism.IMMERSION
    # 單一物件累積全文
    assert ("b", "canonical.jsonl") in fake.r2_objects


def test_r2_canonical_extend_then_append_accumulates():
    fake = FakeCF()
    canon = R2Canonical(bucket="b", key="c.jsonl", client=fake)
    canon.extend([_rec("A"), _rec("B")])
    canon.append(_rec("C"))
    assert len(list(canon.iter_records())) == 3


# ────────────────────────────── 雙寫:log_calibration ──────────────────────────────

def test_log_calibration_dual_writes_canonical(tmp_path: Path):
    store = VectorStore(Config(embedding_provider="local", embedding_dim=64))
    canon = LocalJsonlCanonical(path=str(tmp_path / "c.jsonl"))
    engine = Engine(store=store, canonical=canon)
    rec = _rec("Kenya", grade=Grade.A)
    out = engine.log_calibration(rec)
    assert out["ok"] is True
    ids = [r.id for r in canon.iter_records()]
    assert rec.id in ids


def test_log_calibration_prediction_not_written_to_canonical(tmp_path: Path):
    store = VectorStore(Config(embedding_provider="local", embedding_dim=64))
    canon = LocalJsonlCanonical(path=str(tmp_path / "c.jsonl"))
    engine = Engine(store=store, canonical=canon)
    pred = _rec("Guess", grade=Grade.PREDICTION)
    engine.log_calibration(pred)
    # prediction 為衍生物,不入真相、不被 rebuild 復活
    assert list(canon.iter_records()) == []


def test_log_calibration_isolates_truth_and_clamps_prediction(tmp_path: Path):
    """對比式(非空 canonical 仍排除 PREDICTION)+ 防 collapse 信心夾擠:
    同一 engine/canonical 先後餵 A 與 PREDICTION;真相只留 A,且 PREDICTION
    信心被夾到 ≤0.3。比空-vs-空更能證明『偵測的是內容、不是無寫入』。"""
    store = VectorStore(Config(embedding_provider="local", embedding_dim=64))
    canon = LocalJsonlCanonical(path=str(tmp_path / "c.jsonl"))
    engine = Engine(store=store, canonical=canon)
    a = _rec("Kenya", grade=Grade.A)
    pred = _rec("Guess", grade=Grade.PREDICTION)
    pred.confidence = 0.95  # 蓄意設高,驗證會被夾擠
    engine.log_calibration(a)
    engine.log_calibration(pred)
    assert [r.id for r in canon.iter_records()] == [a.id]  # 真相只含 A
    assert pred.confidence <= 0.3  # 鐵則 #5:預測信心被夾,不得偽裝高把握


def test_vectorize_log_calibration_routes_to_canonical(tmp_path: Path):
    """Vectorize 後端不再『無源』:寫入時 canonical sink 收到真相。"""
    fake = FakeCF()
    store = _vectorize_store(fake)
    canon = LocalJsonlCanonical(path=str(tmp_path / "c.jsonl"))
    engine = Engine(store=store, canonical=canon)
    rec = _rec("Ethiopia", grade=Grade.A)
    engine.log_calibration(rec)
    assert fake.upserts, "向量庫應有寫入"
    assert [r.id for r in canon.iter_records()] == [rec.id], "canonical 應有真相"


# ────────────────────────────── rebuild ──────────────────────────────

def test_rebuild_from_canonical_into_fresh_index(tmp_path: Path):
    """從 canonical 用『當前』嵌入器重建一個新(記憶體)索引並查得到。"""
    canon = LocalJsonlCanonical(path=str(tmp_path / "c.jsonl"))
    canon.extend([_rec("Ethiopia"), _rec("Kenya"), _rec("Brazil", BrewMechanism.IMMERSION)])

    dst = VectorStore(Config(embedding_provider="local", embedding_dim=64))  # 不同維度=不同模型
    n = rebuild(store=dst, canonical=canon)
    assert n == 3
    assert dst.count() == 3
    hits = dst.search("Ethiopia washed", BrewMechanism.PERCOLATION)
    assert hits, "重建後應可召回"
    # 機制硬分區仍成立
    assert all(h["payload"]["brew_mechanism"] == "percolation" for h in hits)


def test_vectorize_writes_are_recoverable_via_canonical(tmp_path: Path):
    """端到端:Vectorize 寫入 → canonical → rebuild 出可查的新索引(證明有源)。"""
    fake = FakeCF()
    vec_store = _vectorize_store(fake)
    canon = LocalJsonlCanonical(path=str(tmp_path / "c.jsonl"))
    engine = Engine(store=vec_store, canonical=canon)
    for r in [_rec("Ethiopia"), _rec("Colombia"), _rec("Brazil", BrewMechanism.IMMERSION)]:
        engine.log_calibration(r)

    dst = VectorStore(Config(embedding_provider="local", embedding_dim=32))
    assert rebuild(store=dst, canonical=canon) == 3
    assert dst.count() == 3
    assert dst.search("Ethiopia", BrewMechanism.PERCOLATION)
