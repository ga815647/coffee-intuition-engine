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
    # D1(選配,canonical 生產後端):SQLite-over-HTTP,逐筆 INSERT OR REPLACE。
    # 相對 R2 單物件 read-modify-write,D1 無整檔覆寫 race(同 id 後寫者勝、多寫者各寫各列)。
    d1_database_id: str = ""

    # 後端覆寫(留空 = 自動偵測)
    store_backend_override: str = ""
    # canonical 後端覆寫:local | r2 | d1(留空 = 自動偵測,見 canonical_backend)。
    canonical_backend_override: str = ""

    # ── Notion(選配) ──
    notion_token: str = ""
    notion_feedback_db: str = ""

    # ── Remote MCP(HTTP = member 受限寫入,§13/§16「三層」) ──
    # 你個人 member token(日常 claude.ai;對應 Aiden 的 MCP_AUTH_TOKEN);Bearer 與 ?token= 皆可。
    # 解析為 member,寫入**強制落自己的 self 客製層**、grade 上限 B;寫不到 global(global / 晉升只在本機 stdio)。
    mcp_auth_token: str = ""
    # 額外 token(JSON `{token:user_id}` 物件:值=member 寫入命名空間;或 `["token",...]` 陣列:無命名空間→reader)。
    # 例:{"tok_alice":"alice","tok_bob":"bob"}(各寫自己的 self 層、硬隔離);值為保留字 global/self 會被拒。
    mcp_guest_tokens: str = ""
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000
    # streamable-http 無狀態模式:每請求自含(host-agnostic、可橫向擴展);
    # 也是「認證中介層設的 per-request principal 能被工具看到」的前提(見 server_http)。
    mcp_stateless: bool = True
    # MCP 傳輸層 DNS-rebinding 防護的 Host allowlist(逗號分隔,支援 ':*' 埠萬用)。
    # 留空(預設)= 關閉內建 host/origin allowlist:公開 token-gated 服務的真正邊界是 fail-closed
    # token 認證 + CORS 鎖 *.claude.ai;DNS rebinding(借瀏覽器受害者網路位置打 localhost)對公開
    # 可路由服務無增益。**且 FastMCP 在 host=127.0.0.1 時會自動開此防護、allowlist 只含 localhost,
    # 擋掉雲端 Host → 421**;故公開部署須顯式關閉(見 server_http._transport_security)。
    # 綁定固定自有網域時可填(如 "cie.example.com,cie.example.com:*")硬化。
    mcp_allowed_hosts: str = ""
    # 公開服務 base URL(含 scheme,不含 /mcp),如 https://cie-mcp-xxx.run.app。
    # 純展示用:tools/add_guest 用它組可分享連接器 URL(`<public_url>/mcp?token=<token>`);
    # 留空 → add_guest 提示填入。不影響伺服器行為(伺服器綁 host/port,非此值)。
    public_url: str = ""

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
            d1_database_id=_get("CIE_D1_DATABASE_ID"),
            store_backend_override=_get("CIE_STORE_BACKEND"),
            canonical_backend_override=_get("CIE_CANONICAL_BACKEND"),
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
            mcp_allowed_hosts=_get("CIE_MCP_ALLOWED_HOSTS"),
            public_url=_get("CIE_PUBLIC_URL"),
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
    def mcp_allowed_hosts_list(self) -> list[str]:
        """解析 CIE_MCP_ALLOWED_HOSTS(逗號分隔)為去空白清單;空 = 關閉 host allowlist。"""
        return [h.strip() for h in self.mcp_allowed_hosts.split(",") if h.strip()]

    @property
    def use_memory_store(self) -> bool:
        """Qdrant 後端是否用記憶體模式(無 url 即記憶體)。"""
        return not self.qdrant_url

    @property
    def canonical_backend(self) -> str:
        """canonical 真相層後端:顯式 override > d1(金鑰+db_id)> r2(金鑰+bucket)> local。

        生產定案走 d1(已啟用、免綁卡,逐筆 INSERT 無整檔 race);R2 留作選項。
        顯式 CIE_CANONICAL_BACKEND 最高優先(部署時權威設定)。
        """
        if self.canonical_backend_override:
            return self.canonical_backend_override
        if self.has_cf_creds and self.d1_database_id:
            return "d1"
        if self.has_cf_creds and self.r2_bucket:
            return "r2"
        return "local"


CONFIG = Config.from_env()
