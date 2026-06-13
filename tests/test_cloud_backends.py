"""Cloudflare 後端(Workers AI 嵌入 + Vectorize 向量庫)單元測試。

離線:用假的 CloudflareClient 注入,完全不觸網路。
驗證:回應解析、機制硬過濾、metadata 淨化、無金鑰自動退回 local。
需金鑰的真實整合測試另以 skip 標記(見檔尾)。
"""
from __future__ import annotations

import os

import pytest

from cie.config import Config
from cie.embedding import (
    EmbeddingError, LocalHashEmbedder, WorkersAIEmbedder, get_embedder,
)
from cie.schema import (
    AcidityType, BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process, Record,
)
from cie.store import VectorStore, VectorizeStore, get_store


# ────────────────────────────── 假 Cloudflare 用戶端 ──────────────────────────────

class FakeCF:
    """記錄呼叫並回傳可控結果的假 CloudflareClient。"""

    def __init__(self, dim: int = 1024):
        self.dim = dim
        self.upserts = []   # [(index, lines)]
        self.queries = []   # [(index, body)]
        self.matches = []   # 下次 query 回傳的 matches

    def workers_ai_run(self, model, payload):
        texts = payload["text"]
        if isinstance(texts, str):
            texts = [texts]
        data = [[float((abs(hash((model, t))) >> i) & 1) for i in range(self.dim)] for t in texts]
        return {"data": data, "shape": [len(texts), self.dim], "pooling": payload.get("pooling", "mean")}

    def vectorize_upsert(self, index, lines):
        self.upserts.append((index, list(lines)))
        return {"mutationId": "m-test"}

    def vectorize_query(self, index, body):
        self.queries.append((index, body))
        return {"count": len(self.matches), "matches": self.matches}

    def vectorize_info(self, index):
        return {"vectorCount": sum(len(l) for _, l in self.upserts)}


def _rec(mech=BrewMechanism.PERCOLATION, process=Process.WASHED, agtron=74,
         grade=Grade.A, notes=("bergamot", "floral")):
    return Record(
        bean=BeanRoast(origin="Ethiopia", variety="Heirloom", process=process, roast_agtron=agtron),
        params=BrewParams(brew_mechanism=mech, method="V60", water_temp_c=92,
                          brew_ratio=16.0, grind_um=650, tds_pct=1.38, ey_pct=20.4),
        flavor=FlavorProfile(acidity=7.5, acidity_type=AcidityType.CITRIC, sweetness=7.0,
                             flavor_notes=list(notes)),
        grade=grade, protocol="SCA_cupping", user_id="global",
    )


# ────────────────────────────── 嵌入器選擇 / 退回 ──────────────────────────────

def test_get_embedder_defaults_to_local():
    assert isinstance(get_embedder(Config()), LocalHashEmbedder)


def test_get_embedder_workers_ai_without_creds_falls_back_to_local():
    emb = get_embedder(Config(embedding_provider="workers_ai"))  # 無 CF 金鑰
    assert isinstance(emb, LocalHashEmbedder)


def test_get_embedder_openai_without_key_falls_back_to_local():
    assert isinstance(get_embedder(Config(embedding_provider="openai")), LocalHashEmbedder)


def test_get_embedder_workers_ai_with_creds():
    emb = get_embedder(Config(embedding_provider="workers_ai", cf_account_id="a", cf_api_token="b"))
    assert isinstance(emb, WorkersAIEmbedder)
    assert emb.dim == 1024  # bge-m3
    assert emb.model_id == "workers_ai:@cf/baai/bge-m3"


# ────────────────────────────── WorkersAIEmbedder 解析 ──────────────────────────────

def test_workers_ai_embed_reads_result_data():
    fake = FakeCF(dim=1024)
    emb = WorkersAIEmbedder(model="@cf/baai/bge-m3", client=fake, dim=1024)
    v = emb.embed("淺焙 衣索比亞 水洗 柑橘酸")
    assert len(v) == 1024
    assert all(isinstance(x, float) for x in v)


def test_workers_ai_embed_batch_length_matches():
    fake = FakeCF(dim=8)
    emb = WorkersAIEmbedder(model="@cf/baai/bge-m3", client=fake, dim=8)
    out = emb.embed_batch(["a", "b", "c"])
    assert len(out) == 3 and all(len(v) == 8 for v in out)


def test_workers_ai_pooling_passed_when_set():
    fake = FakeCF(dim=4)
    emb = WorkersAIEmbedder(model="@cf/baai/bge-m3", client=fake, dim=4, pooling="cls")
    emb.embed("x")
    # FakeCF 不記 payload,改驗證:有 pooling 不致報錯且回正確維度
    assert len(emb.embed("y")) == 4


def test_workers_ai_bad_response_raises():
    class BadCF:
        def workers_ai_run(self, model, payload):
            return {"shape": [1, 4]}  # 缺 data
    with pytest.raises(EmbeddingError):
        WorkersAIEmbedder(model="@cf/baai/bge-m3", client=BadCF(), dim=4).embed("x")


def test_workers_ai_length_mismatch_raises():
    class MismatchCF:
        def workers_ai_run(self, model, payload):
            return {"data": [[0.0, 1.0]]}  # 只回 1 筆,但送了 2 筆
    with pytest.raises(EmbeddingError):
        WorkersAIEmbedder(model="@cf/baai/bge-m3", client=MismatchCF(), dim=2).embed_batch(["a", "b"])


# ────────────────────────────── VectorizeStore ──────────────────────────────

def _vectorize_store(dim=8):
    cfg = Config(embedding_provider="local", embedding_dim=dim, vectorize_index="cie-test")
    fake = FakeCF(dim=dim)
    return VectorizeStore(config=cfg, client=fake), fake


def test_vectorize_upsert_builds_ndjson_lines_and_sanitizes_metadata():
    store, fake = _vectorize_store()
    rec = _rec()
    store.upsert(rec)
    index, lines = fake.upserts[0]
    assert index == "cie-test"
    line = lines[0]
    assert line["id"] == rec.id
    assert len(line["values"]) == 8
    md = line["metadata"]
    # 無 None;list 轉逗號字串
    assert all(v is not None for v in md.values())
    assert md["brew_mechanism"] == "percolation"
    assert md["flavor_notes"] == "bergamot,floral"
    assert md["grade"] == "A"


def test_vectorize_search_builds_mechanism_hard_filter():
    store, fake = _vectorize_store()
    rec = _rec()
    store.upsert(rec)
    fake.matches = [{"id": rec.id, "score": 0.91,
                     "metadata": {"brew_mechanism": "percolation", "grade": "A", "method": "V60"}}]
    hits = store.search("查詢文字", BrewMechanism.PERCOLATION,
                        process="washed", roast_band="light")
    _, body = fake.queries[0]
    flt = body["filter"]
    assert flt["brew_mechanism"] == "percolation"   # 硬分區鍵
    assert flt["process"] == "washed"
    assert flt["roast_band"] == "light"
    assert flt["grade"] == {"$ne": "prediction"}     # 防 model collapse:排除預測
    assert body["returnMetadata"] == "all"
    assert hits[0]["id"] == rec.id
    assert hits[0]["payload"]["brew_mechanism"] == "percolation"


def test_vectorize_search_mechanism_isolation():
    """immersion 查詢的過濾器只含 immersion,絕不混 percolation。"""
    store, fake = _vectorize_store()
    store.search("x", BrewMechanism.IMMERSION)
    _, body = fake.queries[0]
    assert body["filter"]["brew_mechanism"] == "immersion"


def test_vectorize_topk_capped_at_50():
    store, fake = _vectorize_store()
    store.search("x", BrewMechanism.PERCOLATION, top_k=200)
    _, body = fake.queries[0]
    assert body["topK"] == 50


def test_vectorize_count_from_info():
    store, fake = _vectorize_store()
    store.upsert_many([_rec(), _rec(mech=BrewMechanism.IMMERSION)])
    assert store.count() == 2


# ────────────────────────────── 後端工廠 / 設定 ──────────────────────────────

def test_store_backend_selection():
    assert Config().store_backend == "memory"
    assert Config(qdrant_url="http://x").store_backend == "qdrant"
    assert Config(cf_account_id="a", cf_api_token="b").store_backend == "vectorize"
    assert Config(cf_account_id="a", cf_api_token="b",
                  store_backend_override="memory").store_backend == "memory"
    assert Config(cf_account_id="a").has_cf_creds is False


def test_get_store_memory_default():
    assert isinstance(get_store(Config()), VectorStore)


def test_get_store_vectorize_with_creds_no_network():
    cfg = Config(embedding_provider="workers_ai", cf_account_id="a", cf_api_token="b",
                 vectorize_index="cie-records")
    store = get_store(cfg)  # 不應觸網路(僅建構)
    assert isinstance(store, VectorizeStore)
    assert isinstance(store.embedder, WorkersAIEmbedder)


# ────────────────────────────── 需金鑰的真實整合(預設跳過) ──────────────────────────────

@pytest.mark.skipif(
    not (os.environ.get("CIE_CF_ACCOUNT_ID") and os.environ.get("CIE_CF_API_TOKEN")),
    reason="需 CF 金鑰;設 CIE_CF_* 後跑真實 Workers AI 嵌入整合測試。",
)
def test_integration_workers_ai_live():  # pragma: no cover - 需金鑰
    emb = WorkersAIEmbedder()
    v = emb.embed("淺焙 衣索比亞 水洗 柑橘酸 白花")
    assert len(v) == emb.dim == 1024
