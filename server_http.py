"""CIE remote MCP(streamable-HTTP)= **HTTP 公開可寫端點(member 受限)**(設計 §13/§16「三層」)。

三層:本檔是**網路面**——claude.ai / 分享對象走這。掛讀工具 + `log_calibration`(寫),但
寫入受 member 治理**強制隔離**:只能落呼叫者**自己的 self 客製層**、`grade` 上限 B、**寫不到
global / 他人 self**。`global` 客觀真值與 self→global 晉升只在**本機 stdio owner 門**
(`mcp_server.py`)。所有檢索 / 收縮 / conformal / 機制三軌 / 物理先驗都留在既有 cie.* 模組,
本檔只做 HTTP 邊緣:

  - 掛讀工具 + 寫工具(`register_tools(..., include_writes=True, include_promotion=False)`):
    query/recommend/predict/diagnose、method_swap、log_calibration。**晉升工具不掛 HTTP**
    → 網路上沒有寫 global / 晉升的路徑。
  - 雙 token 認證:`Authorization: Bearer <token>` 與 `?token=`/`?key=` 皆可
    (claude.ai 網頁連接器只能用 query 那條);缺 / 錯 → 401;未設密鑰 fail-closed。
    token → member(具命名空間,寫自己的 self)或 reader(純讀)。**global 永無對應 token**。
  - CORS 鎖 `*.claude.ai`;`/health` + `/`(public 狀態)。
  - 每請求把 token 解析成 member / reader Principal 並設入 contextvar;工具據此套讀範圍 / 寫入閘。
    用 streamable-http **stateless** 模式:每請求自含、與工具同一 async task,principal 必可見。

本地跑:
    uvicorn server_http:app --host 0.0.0.0 --port 8000
    # 或   python server_http.py
連接(claude.ai 新增自訂連接器):URL = https://<host>/mcp?token=<CIE_MCP_AUTH_TOKEN>(member)
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Optional, Tuple

from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cie.config import CONFIG
from cie.engine import Engine
from cie.mcp_principal import (
    auth_is_configured, reset_principal, resolve_principal_from_config, set_principal,
)
from cie.mcp_tools import register_tools
from cie.seed import seed as seed_store

logger = logging.getLogger("cie.server_http")

# CORS 允許來源:claude.ai 及其子網域(*.claude.ai)。對齊 fellow-aiden-mcp。
CLAUDE_ORIGIN_REGEX = r"https://([a-z0-9-]+\.)*claude\.ai"
PUBLIC_PATHS = frozenset({"/", "/health"})


# ────────────────────────────── 認證(pure ASGI middleware) ──────────────────────────────

def _extract_token(scope) -> Optional[str]:
    """從 Bearer header 或 ?token= / ?key= query 取 token(claude.ai 網頁只能送 query)。"""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers", [])}
    auth = headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    params = urllib.parse.parse_qs(scope.get("query_string", b"").decode("latin-1"))
    for key in ("token", "key"):
        if params.get(key):
            return params[key][0]
    return None


async def _send_401(send) -> None:
    body = json.dumps({
        "error": "unauthorized",
        "message": ("缺少或無效的 MCP token。送 'Authorization: Bearer <token>' "
                    "或在 URL 後加 '?token=<token>'。"),
    }, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": 401, "headers": [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"www-authenticate", b'Bearer realm="coffee-intuition-engine"'),
    ]})
    await send({"type": "http.response.body", "body": body})


class TokenAuthMiddleware:
    """雙 token 認證閘:public 路徑 / 預檢放行,其餘須有效 token;設請求 principal。

    pure-ASGI(非 BaseHTTPMiddleware)以免緩衝 SSE 串流;設於 contextvar 的 principal
    在同一 async task 內被工具讀取(stateless streamable-http 保證 per-request 同 task)。
    """

    def __init__(self, app, config=CONFIG):
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("path") in PUBLIC_PATHS:
            return await self.app(scope, receive, send)  # public 狀態(/、/health)
        # 注意:**不**整批放行 OPTIONS。合法 CORS 預檢(帶 Origin + Access-Control-Request-Method)
        # 由外層 CORSMiddleware 直接終結、根本到不了這層;任何「漏到這層的 OPTIONS」一律當普通請求
        # 走 token 檢查 → 無 token 即 401。否則裸 OPTIONS(無 Origin)會無認證觸達 MCP 傳輸層。

        principal = resolve_principal_from_config(_extract_token(scope), self.config)
        if principal is None:
            return await _send_401(send)

        token = set_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_principal(token)


# ────────────────────────────── public 路由 ──────────────────────────────

def _health_handler(config, mcp_path: str):
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({
            "name": "coffee-intuition-engine",
            "mcp_endpoint": mcp_path,
            "auth": "Authorization: Bearer <token> 或 ?token=<token>",
            "auth_configured": auth_is_configured(config),
            "transport": "streamable-http",
            "status": "ok",
        })
    return health


# ────────────────────────────── 應用組裝 ──────────────────────────────

def build_app(config=CONFIG, engine: Optional[Engine] = None, auto_seed: bool = True):
    """組裝 ASGI app。回傳 (app, mcp)。

    auto_seed:僅在記憶體後端、且庫空時灌 6 筆冷啟動種子(本地 dev / smoke 便利)。
    正式部署(Vectorize)請走 cie.bootstrap + cie.rebuild,不在此自動 seed。
    """
    from mcp.server.fastmcp import FastMCP

    # 安全不變式(load-bearing,寫入隔離命門):每請求 member / reader principal 走 contextvar,須在
    # **stateless** streamable-http 下才保證可見——每請求自含、與工具同一 async 上下文。**有狀態**模式
    # (stateless=False)會把工具派發跑進「該 session 第一個請求建立的長壽任務」,後續請求中介層設的
    # principal 看不到 → 工具退回 contextvar 預設 = LOCAL_PRINCIPAL(**owner、可寫 global、無 grade 上限**)。
    # 這會讓網路呼叫者取得 owner 權限、繞過 member confinement 而**寫到 global**——直接瓦解三層寫入隔離。
    # 故本層 principal 模型不支援有狀態,**fail-closed 拒啟動**。
    if not config.mcp_stateless:
        raise RuntimeError(
            "CIE remote MCP 僅支援 stateless streamable-http(CIE_MCP_STATELESS=1)。"
            "有狀態模式下每請求 member / reader principal(contextvar)在 session 任務內看不到,工具會退回 "
            "owner 預設(可寫 global),網路呼叫者將繞過 member confinement 寫到 global(見 server_http "
            "安全註記、DESIGN §16.3)。請設 CIE_MCP_STATELESS=1。"
        )

    eng = engine or Engine()
    mcp = FastMCP("coffee-intuition-engine", stateless_http=config.mcp_stateless)
    # 網路面:掛讀工具 + log_calibration(member 受限寫:confinement + grade≤B)。
    # **不掛晉升工具**(include_promotion=False)→ 網路無 self→global / global 寫入路徑;晉升只在 stdio。
    register_tools(mcp, eng, include_writes=True, include_promotion=False)

    if auto_seed and config.store_backend == "memory":
        try:
            if eng.store.count() == 0:
                seed_store(eng.store)
        except Exception:  # pragma: no cover
            logger.warning("auto_seed 失敗(忽略)。", exc_info=True)

    if not auth_is_configured(config):
        logger.warning("未設定任何 MCP token(CIE_MCP_AUTH_TOKEN / CIE_MCP_GUEST_TOKENS);"
                       "所有 /mcp 請求將 fail-closed 回 401。")

    app = mcp.streamable_http_app()
    mcp_path = mcp.settings.streamable_http_path

    # public 路由插到最前(/ 與 /health 在 /mcp 之前比對)。
    health = _health_handler(config, mcp_path)
    app.router.routes.insert(0, Route("/health", health, methods=["GET"]))
    app.router.routes.insert(0, Route("/", health, methods=["GET"]))

    # 中介層:先 Auth(內層)、後 CORS(外層,確保 401 / 預檢也帶 CORS 標頭)。
    app.add_middleware(TokenAuthMiddleware, config=config)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=CLAUDE_ORIGIN_REGEX,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "Mcp-Session-Id", "Mcp-Protocol-Version"],
        expose_headers=["Mcp-Session-Id"],
        max_age=86400,
    )
    return app, mcp


app, mcp = build_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=CONFIG.mcp_host, port=CONFIG.mcp_port)
