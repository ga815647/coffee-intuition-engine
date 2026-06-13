"""CachingEmbedder 單元測試(離線、零金鑰)。

驗收(對應上線任務 D.8):
  - 包住任一 Embedder,回傳向量與內層**完全一致**(快取透明,不改結果);
  - **去重複**:同一文字只讓內層嵌入一次(跨呼叫 + 批內);
  - 鐵則 §14.5:**快取鍵含 model_id**——不同模型即使共用同一 cache dict 也不互相污染。
"""
from __future__ import annotations

from typing import List

from cie.embedding import CachingEmbedder, Embedder


class CountingEmbedder:
    """確定性假嵌入器,記錄內層實際嵌入了哪些文字(用來證明快取省呼叫)。"""

    def __init__(self, model_id: str = "fake:1", dim: int = 4):
        self.model_id = model_id
        self.dim = dim
        self.embedded: List[str] = []  # 內層實際看到的每一筆文字(含重複才算沒省)

    def _vec(self, text: str) -> List[float]:
        # 向量取決於 model_id + 文字:仿真不同模型對同一文字產生不同向量。
        h = sum(ord(c) for c in (self.model_id + "|" + text))
        return [float((h + i) % 7) for i in range(self.dim)]

    def embed(self, text: str) -> List[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        self.embedded.extend(texts)
        return [self._vec(t) for t in texts]


def test_caching_embedder_is_a_valid_embedder():
    c = CachingEmbedder(CountingEmbedder())
    assert isinstance(c, Embedder)  # 滿足 Protocol(dim/model_id/embed/embed_batch)
    assert c.dim == 4 and c.model_id == "fake:1"  # 委派內層


def test_vectors_identical_to_inner():
    inner = CountingEmbedder()
    c = CachingEmbedder(inner)
    for t in ["柑橘酸 明亮", "chocolate body", "x"]:
        assert c.embed(t) == inner._vec(t)
        assert c.embed_batch([t]) == [inner._vec(t)]


def test_cache_avoids_recomputation_across_calls():
    inner = CountingEmbedder()
    c = CachingEmbedder(inner)
    c.embed_batch(["a", "b"])      # 內層看到 a,b
    c.embed_batch(["a", "c"])      # a 命中快取 → 內層只新看到 c
    c.embed("b")                    # 命中
    assert inner.embedded == ["a", "b", "c"]   # 每個唯一文字只嵌一次
    info = c.cache_info()
    assert info["size"] == 3
    assert info["misses"] == 3
    assert info["hits"] == 2                    # 第二批的 a + 單筆 b


def test_within_batch_dedup_preserves_order_and_length():
    inner = CountingEmbedder()
    c = CachingEmbedder(inner)
    out = c.embed_batch(["x", "x", "y", "x"])
    assert inner.embedded == ["x", "y"]        # 批內重複只嵌一次
    assert len(out) == 4                         # 輸出對齊輸入長度/順序
    assert out[0] == out[1] == out[3]
    assert out[2] != out[0]


def test_empty_batch():
    c = CachingEmbedder(CountingEmbedder())
    assert c.embed_batch([]) == []


def test_model_id_in_key_prevents_cross_model_collision():
    """鐵則:不同模型向量空間不可混用。兩個不同 model_id 的 wrapper 共用同一
    cache dict,對**同一文字**各自嵌入一次、互不污染(鍵含 model_id 才成立)。"""
    shared: dict = {}
    a = CachingEmbedder(CountingEmbedder(model_id="model-A", dim=4), cache=shared)
    b = CachingEmbedder(CountingEmbedder(model_id="model-B", dim=4), cache=shared)

    va = a.embed("同一段文字")
    vb = b.embed("同一段文字")

    assert a.inner.embedded == ["同一段文字"]   # A 嵌一次
    assert b.inner.embedded == ["同一段文字"]   # B 也嵌一次(沒被 A 的快取偷走)
    assert len(shared) == 2                       # 兩個不同鍵(model_id 入鍵)
    assert {k[0] for k in shared} == {"model-A", "model-B"}
    assert va != vb                               # 不同模型 → 不同向量,未被污染
