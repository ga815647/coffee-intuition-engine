"""端到端 smoke:真起 uvicorn + 真 MCP streamable-http client,打通整條遠端鏈路。

「兩扇門」:本腳本驗的是**公開門(HTTP)= 唯讀**。寫入只在私有門(本機 stdio,owner)。

證明(不是 mock,是實打實的 HTTP + JSON-RPC + SSE):
  1. 無 token → 401(fail-closed)。
  2. 有 token(?token= query,模擬 claude.ai 網頁連接器)→ initialize 成功;
     list_tools **只有讀工具**(query_flavor_map / predict_method_swap),**無 log_calibration**。
  3. 讀工具 query_flavor_map:機制硬分區生效(查 percolation 不混 immersion 證據)。
  4. 公開門**無寫入路徑**:log_calibration 未在 HTTP 註冊,即便硬呼叫也被拒(寫不進去)。

跑:  python tools/smoke_http.py
不需外網、不需金鑰外掛:本腳本自帶唯讀 token 設定到記憶體 config。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import threading
import time
from contextlib import closing

import httpx
import uvicorn

# Windows 主控台預設 cp950,印不出 emoji / 中文 → 強制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
logging.getLogger("httpx").setLevel(logging.WARNING)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cie.config import Config
from cie.engine import Engine
from cie.store import VectorStore
import server_http

READ_TOKEN = "smoke-read-token"        # 主要唯讀 token(CIE_MCP_AUTH_TOKEN)
EXTRA_READ_TOKEN = "smoke-extra-read"  # 額外唯讀 token(供個別撤銷)


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_server(port: int) -> uvicorn.Server:
    cfg = Config(mcp_auth_token=READ_TOKEN,
                 mcp_guest_tokens=json.dumps({EXTRA_READ_TOKEN: "alice"}),
                 mcp_stateless=True)
    app, _ = server_http.build_app(config=cfg, engine=Engine(VectorStore()), auto_seed=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    return uvicorn.Server(config)


def _wait_ready(base: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base}/health", timeout=1.0)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("server 未就緒")


def _text(result) -> str:
    """從 MCP CallToolResult 取第一段 text。"""
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


async def _run_client(base: str) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    ok = "✅"
    mcp_url = f"{base}/mcp"

    # ── 1. 無 token → 401 ──
    r = httpx.post(mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                   headers={"Accept": "application/json, text/event-stream"}, timeout=5.0)
    assert r.status_code == 401, f"無 token 應 401,實得 {r.status_code}"
    print(f"{ok} [1] 無 token → 401 fail-closed:{r.json()['error']}")

    # ── 2. 有 token(query param,模擬 claude.ai 網頁)→ MCP 握手 ──
    authed_url = f"{mcp_url}?token={READ_TOKEN}"
    async with streamablehttp_client(authed_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"{ok} [2] ?token= 握手成功:server={init.serverInfo.name}")

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"{ok}     list_tools:{names}")
            assert {"query_flavor_map", "predict_method_swap"} <= set(names), "讀工具應齊全"
            assert "log_calibration" not in names, "公開門不得暴露寫工具(log_calibration)"
            print(f"{ok}     公開門唯讀:list_tools 無 log_calibration(寫工具不在 HTTP 暴露)。")

            # ── 3. 讀:機制硬分區(黑箱對照查)──
            # 同一支豆(冷啟動種子裡那筆 immersion:Brazil Cerrado natural 58)分別查
            # immersion 與 percolation。兩機制各有獨立物理先驗,證據絕不互通:
            # 該筆只該在 immersion 召回出現,在 percolation 召回必缺席(永不跨機制平均)。
            async def evidence_ids(mech: str) -> set:
                r = await session.call_tool("query_flavor_map", {
                    "brew_mechanism": mech, "mode": "recommend",
                    "origin": "Brazil Cerrado", "process": "natural", "roast_agtron": 58,
                })
                return {e["id"] for e in json.loads(_text(r)).get("evidence", [])}

            imm_ids = await evidence_ids("immersion")
            perc_ids = await evidence_ids("percolation")
            print(f"{ok} [3] 機制對照查(Brazil natural 58):immersion 證據 {len(imm_ids)} 筆 / "
                  f"percolation 證據 {len(perc_ids)} 筆")
            assert imm_ids, "immersion 應召回那筆 immersion 種子(對照才有意義)"
            assert imm_ids.isdisjoint(perc_ids), (
                f"機制洩漏!同一筆證據跨機制出現:{imm_ids & perc_ids}")
            print(f"{ok}     機制硬分區生效:immersion 那筆證據不漏進 percolation(兩集互斥)。")

            # ── 4. 公開門無寫入路徑:log_calibration 未在 HTTP 註冊,硬呼叫亦被拒 ──
            write_blocked = False
            detail = ""
            try:
                res = await session.call_tool("log_calibration", {
                    "brew_mechanism": "percolation", "grade": "C",
                    "origin": "Kenya", "roast_agtron": 70, "user_id": "global",
                })
                # 若 SDK 不拋例外,未知工具會回 isError 結果(仍非成功寫入)。
                write_blocked = bool(getattr(res, "isError", False))
                detail = f"isError={getattr(res, 'isError', None)}: {_text(res)[:48]}"
            except Exception as exc:  # 未知工具 → SDK 拋 McpError 等
                write_blocked = True
                detail = f"{type(exc).__name__}: {str(exc)[:48]}"
            assert write_blocked, f"公開門呼叫 log_calibration 竟未被拒?!唯讀門被破:{detail}"
            print(f"{ok} [4] 公開門無寫入路徑:log_calibration 未註冊,呼叫被拒({detail})。")

    print("\n✅ 全部 smoke 檢查通過。公開門(HTTP)= 唯讀:認證 + 機制硬分區 OK、無寫入路徑。"
          "\n   寫入只在私有門(本機 stdio,owner)。")


def main() -> None:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    server = _build_server(port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_ready(base)
        asyncio.run(_run_client(base))
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


if __name__ == "__main__":
    main()
