"""端到端 smoke:真起 uvicorn + 真 MCP streamable-http client,打通整條遠端鏈路。

「三層 + 人工晉升」:本腳本驗的是**網路面(HTTP)= member 受限寫**。寫 global / 晉升只在
本機 stdio owner 門。寫入隔離是公開可寫端點的命門,這裡實打實證它。

證明(不是 mock,是實打實的 HTTP + JSON-RPC + SSE):
  1. 無 token → 401(fail-closed)。
  2. 有 token(?token= query,模擬 claude.ai 網頁連接器)→ initialize 成功;
     list_tools 有讀工具 + `log_calibration`(member 寫),但**無晉升工具**
     (list_customizations / promote_customization 只在 stdio owner 門)。
  3. 讀工具 query_flavor_map:機制硬分區生效(查 percolation 不混 immersion 證據)。
  4. member 命名空間 confinement(命門):
     a. member 寫入落**自己的 self 命名空間**(成功)。
     b. member 嘗試寫 global → 被強制改寫回自有 ns(trust_notes),**global 未被污染**。
     c. **讀隔離**:純讀 token(reader,只讀 global)與另一個 member 都**讀不到**該筆 self,
        只有寫入者自己讀得到。

跑:  python tools/smoke_http.py
不需外網、不需金鑰外掛:本腳本自帶三層 token 設定到記憶體 config。
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

PRIMARY_TOKEN = "smoke-primary-member"   # CIE_MCP_AUTH_TOKEN:你個人 member(寫自己的 self)
ALICE_TOKEN = "smoke-alice-member"       # 訪客 member,寫 "alice" 命名空間
READER_TOKEN = "smoke-reader"            # 純讀 token(無命名空間 → reader,只讀 global)

# [4] 用的辨識度高的豆況(冷啟動種子幾乎不會撞,讓召回乾淨可判定)。
DISTINCT_BEAN = {"origin": "Zzz Smoketest Estate", "process": "anaerobic", "roast_agtron": 80}
QUERY_ARGS = {"brew_mechanism": "percolation", "mode": "recommend", **DISTINCT_BEAN}


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_server(port: int) -> uvicorn.Server:
    cfg = Config(
        mcp_auth_token=PRIMARY_TOKEN,
        # 物件形式:值=命名空間 → member;值為空字串 → reader(只讀 global)。
        mcp_guest_tokens=json.dumps({ALICE_TOKEN: "alice", READER_TOKEN: ""}),
        mcp_stateless=True,
    )
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


async def _call(mcp_url: str, token: str, tool: str, args: dict) -> dict:
    """以指定 token 開一條 session,呼叫單一工具,回傳解析後的 dict。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(f"{mcp_url}?token={token}") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(tool, args)
            return json.loads(_text(res))


async def _evidence_ids(mcp_url: str, token: str, args: dict) -> set:
    out = await _call(mcp_url, token, "query_flavor_map", args)
    return {e["id"] for e in out.get("evidence", [])}


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

    # ── 2. 有 token(query param,模擬 claude.ai 網頁)→ MCP 握手 + 工具集 ──
    authed_url = f"{mcp_url}?token={PRIMARY_TOKEN}"
    async with streamablehttp_client(authed_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"{ok} [2] ?token= 握手成功:server={init.serverInfo.name}")

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"{ok}     list_tools:{names}")
            assert {"query_flavor_map", "predict_method_swap", "log_calibration"} <= set(names), \
                "讀工具 + member 寫工具應齊全"
            assert "promote_customization" not in names, "晉升工具不得暴露於 HTTP(只在 stdio owner)"
            assert "list_customizations" not in names, "晉升審查工具不得暴露於 HTTP"
            print(f"{ok}     網路面:有 log_calibration(member 寫),無晉升工具(寫 global / 晉升只在 stdio)。")

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

    # ── 4. member 命名空間 confinement + 讀隔離(命門) ──
    # 4a. alice(member)寫一筆自有 self 校準 → 成功,落 "alice" 命名空間。
    w1 = await _call(mcp_url, ALICE_TOKEN, "log_calibration", {
        "brew_mechanism": "percolation", "grade": "C", "method": "V60", "grind_um": 600,
        "acidity": 9.4, **DISTINCT_BEAN,
    })
    assert w1.get("ok"), f"member 寫自有 ns 竟失敗:{w1}"
    alice_rec_id = w1["id"]
    print(f"{ok} [4a] member(alice)寫自有 self 層成功:id={alice_rec_id[:8]}…")

    # 4b. alice 嘗試寫 global + A 級 → 被強制 confine 回自有 ns、降為 B(global 未被污染)。
    w2 = await _call(mcp_url, ALICE_TOKEN, "log_calibration", {
        "brew_mechanism": "percolation", "grade": "A", "protocol": "SCA_cupping",
        "method": "V60", "grind_um": 605, "acidity": 9.5, "user_id": "global", **DISTINCT_BEAN,
    })
    assert w2.get("ok"), f"member 寫(將被 confine)竟整筆失敗:{w2}"
    notes = w2.get("trust_notes") or []
    assert notes, "member 寫 global 應回 trust_notes 說明已被 confine,卻沒有"
    assert any("global" in n or "自有" in n for n in notes), f"trust_notes 未提及 confine:{notes}"
    print(f"{ok} [4b] member 寫 global → 被強制改寫回自有 ns(grade A→B);trust_notes:{notes[0][:42]}…")

    # 4c. 讀隔離:reader(只讀 global)與另一 member(self)都讀不到 alice 的 self;只有 alice 自己讀得到。
    alice_sees = await _evidence_ids(mcp_url, ALICE_TOKEN, QUERY_ARGS)
    reader_sees = await _evidence_ids(mcp_url, READER_TOKEN, QUERY_ARGS)
    self_sees = await _evidence_ids(mcp_url, PRIMARY_TOKEN, QUERY_ARGS)
    assert alice_rec_id in alice_sees, "alice 應讀得到自己的 self 校準"
    assert alice_rec_id not in reader_sees, (
        "讀隔離破!reader(只讀 global)竟讀到 alice 的 self(或 global 被污染了)")
    assert alice_rec_id not in self_sees, (
        "讀隔離破!另一 member 竟讀到 alice 的 self")
    print(f"{ok} [4c] 讀隔離生效:alice 自己看得到({len(alice_sees)} 筆),"
          f"reader 看不到({len(reader_sees)} 筆,global 未被污染)、另一 member 看不到。")

    print("\n✅ 全部 smoke 檢查通過。網路面(HTTP)= member 受限寫:認證 + 機制硬分區 OK、"
          "\n   member 寫入強制落自有 ns(global 永不被網路污染)、self 讀取互相隔離。"
          "\n   寫 global / 晉升只在本機 stdio owner 門。")


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
