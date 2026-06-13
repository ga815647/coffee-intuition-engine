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

    # ── Canonical 真相層(JSONL;向量為衍生物,可重嵌重建) ──
    # 本地 JSONL 路徑(預設);Vectorize 後端必走此 sink 才不會「無源」。
    canonical_path: str = "./data/canonical.jsonl"
    # R2(選配):有 CF 金鑰 + bucket → 用 R2 物件存 canonical JSONL。
    r2_bucket: str = ""
    r2_canonical_key: str = "canonical.jsonl"

    # 後端覆寫(留空 = 自動偵測)
    store_backend_override: str = ""

    # ── Notion(選配) ──
    notion_token: str = ""
    notion_feedback_db: str = ""

    # ── Remote MCP(HTTP 公開門 = 唯讀,§13/§16「兩扇門」) ──
    # 主要唯讀 token(日常 + 分享都用這條;對應 Aiden 的 MCP_AUTH_TOKEN);Bearer 與 ?token= 皆可。
    # **HTTP 一切 token 皆唯讀**;寫入(校準)只在本機 Claude Code stdio(owner),不需網路 token。
    mcp_auth_token: str = ""
    # 額外唯讀 token(JSON `{token:label}` 物件或 `["token",...]` 陣列);供個別發放 / 撤銷。皆唯讀。
    # 例:{"tok_alice":"alice","tok_bob":"bob"}(值僅作稽核標籤,不再是寫入命名空間)。
    mcp_guest_tokens: str = ""
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    # streamable-http 無狀態模式:每請求自含(host-agnostic、可橫向擴展);
    # 也是「認證中介層設的 per-request principal 能被工具看到」的前提(見 server_http)。
    mcp_stateless: bool = True

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
            canonical_path=_get("CIE_CANONICAL_PATH", "./data/canonical.jsonl"),
            r2_bucket=_get("CIE_R2_BUCKET"),
            r2_canonical_key=_get("CIE_R2_CANONICAL_KEY", "canonical.jsonl"),
            store_backend_override=_get("CIE_STORE_BACKEND"),
            notion_token=_get("CIE_NOTION_TOKEN"),
            notion_feedback_db=_get("CIE_NOTION_FEEDBACK_DB"),
            mcp_auth_token=_get("CIE_MCP_AUTH_TOKEN"),
            mcp_guest_tokens=_get("CIE_MCP_GUEST_TOKENS"),
            mcp_host=_get("CIE_MCP_HOST", "0.0.0.0"),
            # CIE_MCP_PORT 優先;否則用 PaaS(Render/Railway/Cloud Run…)注入的 $PORT;再否則 8000。
            # 用 `or` 串接「coalesce 空字串」:`export PORT=`(present-but-empty)也退回 8000,
            # 不會讓 int("") 在 import 期炸掉(_get 對「存在但空」回 "",default 只在 key 缺席時生效)。
            mcp_port=int(_get("CIE_MCP_PORT") or _get("PORT") or "8000"),
            mcp_stateless=_get("CIE_MCP_STATELESS", "1").lower() not in ("0", "false", "no", ""),
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

    @property
    def canonical_backend(self) -> str:
        """canonical 真相層後端:r2(有 CF 金鑰 + bucket)| local(預設 JSONL)。"""
        if self.has_cf_creds and self.r2_bucket:
            return "r2"
        return "local"


CONFIG = Config.from_env()
