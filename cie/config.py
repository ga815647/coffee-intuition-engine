"""集中設定。開發預設全部離線可跑。

後端選擇(自動偵測,可用環境變數覆寫):
  向量庫 store_backend:
    - 有 CF 金鑰 + Vectorize index   → "vectorize"
    - 有 CIE_QDRANT_URL               → "qdrant"
    - 皆無                            → "memory"(開發預設,離線)
  嵌入 embedding_provider:
    - "workers_ai" | "openai" | "voyage" | "local"(預設)
    - 雲端 provider 缺金鑰 → get_embedder() 自動退回 "local"(離線後備)

鐵則:機密只進環境變數 / .env,不入庫(見 .env.example)。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Config:
    # ── 向量庫:Qdrant(替代選項) ──
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    collection: str = "cie_records"

    # ── 嵌入 ──
    # workers_ai | local(預設) | openai | voyage
    embedding_provider: str = "local"
    embedding_model: str = ""        # 通用覆寫(openai/voyage 用)
    embedding_api_key: str = ""      # openai/voyage 金鑰
    embedding_dim: int = 256         # 僅 local 雜湊嵌入用;雲端維度由模型決定

    # ── Cloudflare 原生(Workers AI 嵌入 + Vectorize 向量庫) ──
    cf_account_id: str = ""
    cf_api_token: str = ""
    vectorize_index: str = "cie-records"
    workers_ai_embed_model: str = "@cf/baai/bge-m3"
    cf_timeout_s: float = 30.0
    cf_max_retries: int = 2

    # 後端覆寫(留空 = 自動偵測)
    store_backend_override: str = ""

    # ── Notion(選配) ──
    notion_token: str = ""
    notion_feedback_db: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            qdrant_url=_get("CIE_QDRANT_URL"),
            qdrant_api_key=_get("CIE_QDRANT_API_KEY"),
            collection=_get("CIE_COLLECTION", "cie_records"),
            embedding_provider=_get("CIE_EMBEDDING_PROVIDER", "local"),
            embedding_model=_get("CIE_EMBEDDING_MODEL"),
            embedding_api_key=_get("CIE_EMBEDDING_API_KEY"),
            embedding_dim=int(_get("CIE_EMBEDDING_DIM", "256")),
            cf_account_id=_get("CIE_CF_ACCOUNT_ID"),
            cf_api_token=_get("CIE_CF_API_TOKEN"),
            vectorize_index=_get("CIE_VECTORIZE_INDEX", "cie-records"),
            workers_ai_embed_model=_get("CIE_WORKERS_AI_EMBED_MODEL", "@cf/baai/bge-m3"),
            cf_timeout_s=float(_get("CIE_CF_TIMEOUT_S", "30")),
            cf_max_retries=int(_get("CIE_CF_MAX_RETRIES", "2")),
            store_backend_override=_get("CIE_STORE_BACKEND"),
            notion_token=_get("CIE_NOTION_TOKEN"),
            notion_feedback_db=_get("CIE_NOTION_FEEDBACK_DB"),
        )

    # ── 衍生判斷 ──
    @property
    def has_cf_creds(self) -> bool:
        return bool(self.cf_account_id and self.cf_api_token)

    @property
    def store_backend(self) -> str:
        """選定向量庫後端:vectorize | qdrant | memory。"""
        if self.store_backend_override:
            return self.store_backend_override
        if self.has_cf_creds and self.vectorize_index:
            return "vectorize"
        if self.qdrant_url:
            return "qdrant"
        return "memory"

    @property
    def use_memory_store(self) -> bool:
        """Qdrant 後端是否用記憶體模式(無 url 即記憶體)。"""
        return not self.qdrant_url


CONFIG = Config.from_env()
