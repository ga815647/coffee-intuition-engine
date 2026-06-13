"""可插拔嵌入。

開發預設 LocalHashEmbedder:離線、確定性、零依賴、零金鑰,讓骨架立即可跑。
上線改 CIE_EMBEDDING_PROVIDER=workers_ai(Cloudflare,多語 bge-m3,適合中文風味筆記),
openai / voyage 為選配。任何雲端 provider 缺金鑰 → 自動退回 LocalHashEmbedder。

注意(設計 §4.1):只嵌入『情境文字』做模糊召回;數值欄位走 payload 過濾與
物理距離,不靠嵌入理解數字(數值嵌入易失真)。

鐵則(§14.5):不同模型的向量空間不可混用。每個嵌入器帶 `model_id`,切模型須
從 canonical JSONL 重建索引(見 cie/portability.py)。
"""
from __future__ import annotations

import hashlib
import logging
import math
from typing import List, Protocol, runtime_checkable

from ._http import HttpError, post_json
from .cfapi import CloudflareClient, CloudflareError
from .config import CONFIG

logger = logging.getLogger("cie.embedding")


class EmbeddingError(RuntimeError):
    """嵌入後端錯誤(回傳格式非預期 / 上游失敗)。"""


@runtime_checkable
class Embedder(Protocol):
    dim: int
    model_id: str
    def embed(self, text: str) -> List[float]: ...
    def embed_batch(self, texts: List[str]) -> List[List[float]]: ...


# ────────────────────────────── 離線後備 ──────────────────────────────

class LocalHashEmbedder:
    """確定性雜湊袋詞嵌入。非語意最佳,但離線可跑、可重現,適合骨架與測試。

    作法:對每個 token 雜湊到固定維度的桶並累加,最後 L2 正規化。
    相近用詞 → 部分相同 token → 餘弦相似上升。
    """

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.model_id = f"local-hash:{dim}"

    def embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        toks = [t for t in text.lower().replace("/", " ").split() if t]
        for tok in toks:
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dim
            sign = 1.0 if (h >> 8) % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]


# ────────────────────────────── Cloudflare Workers AI ──────────────────────────────

# 已知 Workers AI 文字嵌入模型維度(避免硬寫死;未知模型預設 1024)。
WORKERS_AI_DIMS = {
    "@cf/baai/bge-m3": 1024,
    "@cf/baai/bge-large-en-v1.5": 1024,
    "@cf/baai/bge-base-en-v1.5": 768,
    "@cf/baai/bge-small-en-v1.5": 384,
}
_WORKERS_AI_BATCH = 100  # bge-m3 text 陣列上限/同步呼叫


class WorkersAIEmbedder:
    """Cloudflare Workers AI REST 嵌入(預設 @cf/baai/bge-m3,1024 維)。

    關鍵(研究確認):bge-m3 是多功能模型,**務必送 {"text": ...} 變體並讀
    result.data[i]**;送 query+contexts(rerank 變體)回傳的是分數不是向量。
    pooling 在寫入與查詢間必須一致(同一嵌入器實例保證一致;預設不送、用伺服器
    預設,可用 CIE_WORKERS_AI_POOLING 釘住)。
    """

    def __init__(self, model: str = "", client: CloudflareClient = None,
                 dim: int = 0, pooling: str = "", batch_size: int = _WORKERS_AI_BATCH,
                 config=CONFIG):
        self.model = model or config.workers_ai_embed_model
        self.model_id = f"workers_ai:{self.model}"
        self.dim = dim or WORKERS_AI_DIMS.get(self.model, 1024)
        self.pooling = pooling
        self.batch_size = max(1, batch_size)
        self.client = client or CloudflareClient(
            config.cf_account_id, config.cf_api_token,
            config.cf_timeout_s, config.cf_max_retries,
        )

    def embed(self, text: str) -> List[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        out: List[List[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._embed_chunk(texts[i:i + self.batch_size]))
        return out

    def _embed_chunk(self, chunk: List[str]) -> List[List[float]]:
        payload = {"text": chunk}
        if self.pooling:
            payload["pooling"] = self.pooling
        try:
            result = self.client.workers_ai_run(self.model, payload)
        except CloudflareError as e:
            raise EmbeddingError(f"Workers AI 嵌入失敗:{e}") from e
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list) or len(data) != len(chunk):
            raise EmbeddingError(
                f"Workers AI 回傳格式非預期(缺 result.data 或長度不符):{str(result)[:300]}"
            )
        vecs: List[List[float]] = []
        for row in data:
            if not isinstance(row, list) or not row:
                raise EmbeddingError(f"Workers AI data 列非向量:{str(row)[:120]}")
            vecs.append([float(x) for x in row])
        return vecs


# ────────────────────────────── 選配:OpenAI / Voyage(REST) ──────────────────────────────

class _OpenAICompatEmbedder:
    """OpenAI 相容 /embeddings 端點的共用實作(input 陣列 → data[].embedding)。"""

    def __init__(self, url: str, api_key: str, model: str, model_id: str,
                 dim: int, timeout_s: float = 30.0, max_retries: int = 2):
        self.url = url
        self.api_key = api_key
        self.model = model
        self.model_id = model_id
        self.dim = dim
        self.timeout_s = timeout_s
        self.max_retries = max_retries

    def embed(self, text: str) -> List[float]:  # pragma: no cover - 需金鑰
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: List[str]) -> List[List[float]]:  # pragma: no cover - 需金鑰
        if not texts:
            return []
        try:
            resp = post_json(
                self.url, payload={"model": self.model, "input": texts},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout_s=self.timeout_s, max_retries=self.max_retries,
            )
        except HttpError as e:
            raise EmbeddingError(f"{self.model_id} 嵌入失敗:{e}") from e
        data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingError(f"{self.model_id} 回傳格式非預期:{str(resp)[:300]}")
        return [[float(x) for x in row["embedding"]] for row in data]


def _openai_embedder(config=CONFIG) -> "_OpenAICompatEmbedder":  # pragma: no cover - 需金鑰
    model = config.embedding_model or "text-embedding-3-small"
    dim = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}.get(model, 1536)
    return _OpenAICompatEmbedder(
        "https://api.openai.com/v1/embeddings", config.embedding_api_key,
        model, f"openai:{model}", dim, config.cf_timeout_s, config.cf_max_retries,
    )


def _voyage_embedder(config=CONFIG) -> "_OpenAICompatEmbedder":  # pragma: no cover - 需金鑰
    model = config.embedding_model or "voyage-3"
    dim = {"voyage-3": 1024, "voyage-3-lite": 512}.get(model, 1024)
    return _OpenAICompatEmbedder(
        "https://api.voyageai.com/v1/embeddings", config.embedding_api_key,
        model, f"voyage:{model}", dim, config.cf_timeout_s, config.cf_max_retries,
    )


# ────────────────────────────── 工廠 ──────────────────────────────

def get_embedder(config=CONFIG) -> Embedder:
    """依 CIE_EMBEDDING_PROVIDER 選嵌入器;雲端缺金鑰一律退回 LocalHashEmbedder。"""
    provider = (config.embedding_provider or "local").lower()
    local = LocalHashEmbedder(dim=config.embedding_dim)

    if provider == "workers_ai":
        if not config.has_cf_creds:
            logger.warning("CIE_EMBEDDING_PROVIDER=workers_ai 但缺 CF 金鑰 → 退回 local 雜湊嵌入。")
            return local
        try:
            return WorkersAIEmbedder(config=config)
        except CloudflareError as e:  # pragma: no cover - 防禦
            logger.warning("Workers AI 嵌入器初始化失敗(%s)→ 退回 local。", e)
            return local

    if provider in ("openai", "voyage"):
        if not config.embedding_api_key:
            logger.warning("CIE_EMBEDDING_PROVIDER=%s 但缺 CIE_EMBEDDING_API_KEY → 退回 local。", provider)
            return local
        return _openai_embedder(config) if provider == "openai" else _voyage_embedder(config)

    if provider != "local":
        logger.warning("未知 CIE_EMBEDDING_PROVIDER=%s → 用 local。", provider)
    return local
