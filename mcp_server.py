"""CIE MCP server(stdio)— 本地 / Claude Code 直連入口(設計 §6 / §13)。

工具(定義在 cie/mcp_tools.py,stdio 與 HTTP 共用一份):
  query_flavor_map     查相似情境 → 推薦 / 預測 / 診斷(讀)
  log_calibration      寫回一筆校準(寫,過寫入信任閘)
  predict_method_swap  換泡法推味道(讀)

執行:  python mcp_server.py   (stdio transport)

stdio 為本地單人、完全信任入口:未設定請求 principal → cie.mcp_tools 取
LOCAL_PRINCIPAL(owner、不施讀過濾),行為與直接呼叫 engine 一致(零回歸)。
遠端多租戶 / 認證 / CORS 走 server_http.py(streamable-http),兩者不互斥。

注意:記憶體向量庫不跨行程持久化。上線請設 Cloudflare(Vectorize + Workers AI)
或 CIE_QDRANT_URL,否則每次啟動需重新 seed / rebuild。
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from cie.engine import Engine
from cie.mcp_tools import register_tools
from cie.seed import seed as seed_store

mcp = FastMCP("coffee-intuition-engine")
_engine = Engine()
register_tools(mcp, _engine)

# 開發便利:啟動時若庫空,自動灌 6 筆冷啟動種子(正式載入走 cie.bootstrap + cie.rebuild)。
try:
    if _engine.store.count() == 0:
        seed_store(_engine.store)
except Exception:  # pragma: no cover
    pass


if __name__ == "__main__":
    mcp.run()
