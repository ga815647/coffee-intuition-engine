"""CIE MCP server(stdio)— 本地 / Claude Code 直連 = **owner 門**(設計 §6 / §13 / §16「三層」)。

工具(定義在 cie/mcp_tools.py,stdio 與 HTTP 共用一份):
  query_flavor_map     查相似情境 → 推薦 / 預測 / 診斷(讀)
  log_calibration      寫回一筆校準(寫;owner 可寫 global 或任一 self,A 須 protocol)
  predict_method_swap  換泡法推味道(讀)
  list_customizations  列 self 客製層待審記錄(晉升審查;**owner / stdio 限定**)
  promote_customization  把 self 記錄晉升為 global 客觀真值(**owner / stdio 限定**)

執行:  python mcp_server.py   (stdio transport)

stdio 為本地單人、完全信任的 **owner** 入口:未設定請求 principal → cie.mcp_tools 取
LOCAL_PRINCIPAL(owner、可寫 global、可晉升、不施讀過濾),行為與直接呼叫 engine 一致(零回歸)。
晉升工具(`include_promotion=True`)**只在這裡掛**——self→global / global 寫入是 owner 的本機特權,
網路面(server_http.py,member 受限)永遠沒有這條路徑。遠端認證 / CORS / member 治理走
server_http.py(streamable-http),兩者不互斥。

注意:記憶體向量庫不跨行程持久化。上線請設 Cloudflare(Vectorize + Workers AI)
或 CIE_QDRANT_URL,否則每次啟動需重新 seed / rebuild。
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from cie.engine import Engine
from cie.mcp_tools import register_tools
from cie.rebuild import prime_serving_index
from cie.seed import seed as seed_store

mcp = FastMCP("coffee-intuition-engine")
_engine = Engine()
# owner 門:掛全部工具(讀 + 寫 + 晉升)。include_promotion=True 只在 stdio,網路面不掛。
register_tools(mcp, _engine, include_writes=True, include_promotion=True)

# 啟動載入:生產(memory + R2)從共用 canonical 重建 in-memory 索引(owner 讀得到 global
# 全量、可審查晉升,本機寫 global 下次 Cloud Run 冷啟動讀得到);離線開發無 R2 → 庫空時
# 灌 6 筆冷啟動種子(正式載入走 cie.bootstrap + cie.rebuild)。
try:
    if prime_serving_index(_engine) is None and _engine.store.count() == 0:
        seed_store(_engine.store)
except Exception:  # pragma: no cover
    pass


if __name__ == "__main__":
    mcp.run()
