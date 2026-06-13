"""對**已部署的遠端 URL**(Cloud Run 等)跑真 MCP streamable-http 驗證 —— 即任務 D 的「三命門 + 冷啟動持久」。

與 `tools/smoke_http.py` 的差別:後者自起本機 uvicorn;**本腳本不起伺服器**,只拿一支真
MCP client 打你給的公開 HTTPS URL,用你**實際部署的 token** 驗證線上實例。沒有金鑰寫死,
全部從環境變數讀。

設定(環境變數):
  CIE_SMOKE_URL            必填。服務 base,如 https://cie-mcp-xxxx.run.app(不含 /mcp)。
  CIE_SMOKE_MEMBER_TOKEN   必填。你的 member token(= 部署的 CIE_MCP_AUTH_TOKEN 或某個 guest member)。
  CIE_SMOKE_MEMBER2_TOKEN  選填。第二個 member token(另一命名空間)→ 才能驗「A 讀不到 B 的 self」。
  CIE_SMOKE_READER_TOKEN   選填。reader token(無命名空間)→ 才能驗「reader 只讀 global」。

用法:
  # 完整三命門(會寫一筆帶唯一 tag 的 self 探針,印出 tag 供冷啟動驗證用):
  CIE_SMOKE_URL=https://... CIE_SMOKE_MEMBER_TOKEN=... python tools/smoke_remote.py

  # 冷啟動持久:上面跑完 → 讓實例 scale-to-zero(閒置 ~15 分)或部署一個新 revision 強制冷啟 →
  # 再以同一 member 查那個 tag,證明該筆從 R2 重建後仍在(沒因 min-instances=0 丟失):
  CIE_SMOKE_URL=https://... CIE_SMOKE_MEMBER_TOKEN=... python tools/smoke_remote.py --verify-persistence <TAG>

退出碼:全綠 0;任一斷言失敗非 0(可塞進 CI / 部署後驗收)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time

import httpx

for _stream in (sys.stdout, sys.stderr):           # Windows 主控台 UTF-8
    try:
        _stream.reconfigure(encoding="utf-8")       # type: ignore[attr-defined]
    except Exception:
        pass
logging.getLogger("httpx").setLevel(logging.WARNING)

OK = "✅"

# 辨識度高的探針豆況(線上幾乎不會撞,召回乾淨可判定)。tag 進 origin 讓每次跑唯一。
PROBE_PROCESS = "anaerobic"
PROBE_AGTRON = 80


def _env(name: str, required: bool = False) -> str:
    v = os.environ.get(name, "").strip()
    if required and not v:
        sys.exit(f"缺少必填環境變數 {name}(見檔頭說明)。")
    return v


def _text(result) -> str:
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


async def _call(mcp_url: str, token: str, tool: str, args: dict) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(f"{mcp_url}?token={token}") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(tool, args)
            return json.loads(_text(res))


async def _evidence_ids(mcp_url: str, token: str, origin: str) -> set:
    out = await _call(mcp_url, token, "query_flavor_map", {
        "brew_mechanism": "percolation", "mode": "recommend",
        "origin": origin, "process": PROBE_PROCESS, "roast_agtron": PROBE_AGTRON,
    })
    return {e["id"] for e in out.get("evidence", [])}


def _probe_bean(tag: str) -> dict:
    return {"origin": f"Smoke-{tag} Estate", "process": PROBE_PROCESS, "roast_agtron": PROBE_AGTRON}


# ────────────────────────────── 冷啟動持久:第二階段 ──────────────────────────────

async def verify_persistence(base: str, member: str, tag: str) -> None:
    """以寫入者 member 查先前寫的 tag 探針 → 仍讀得到 = 撐過 scale-to-zero(R2 重建)。"""
    mcp_url = f"{base}/mcp"
    origin = _probe_bean(tag)["origin"]
    ids = await _evidence_ids(mcp_url, member, origin)
    print(f"{OK} 冷啟動持久驗證:member 查 tag='{tag}'(origin={origin})→ 召回 {len(ids)} 筆證據。")
    assert ids, (
        f"找不到 tag='{tag}' 的探針記錄。可能:(a) 寫入未落 R2;(b) 冷啟動未從 R2 重建;"
        f"(c) tag 打錯。請確認先前完整跑過一次寫入階段、且實例確已冷啟動。")
    print(f"{OK} 該 member 自有 self 探針撐過冷啟動(min-instances=0 不丟資料、R2 重建索引成功)。")
    print("\n✅ 冷啟動持久:通過。")


# ────────────────────────────── 三命門 + 寫探針:第一階段 ──────────────────────────────

async def run_three_gates(base: str, member: str, member2: str, reader: str) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    mcp_url = f"{base}/mcp"
    tag = str(int(time.time()))                     # 本次跑唯一(本機 script,可用 time)
    probe = _probe_bean(tag)

    # ── 0. /health ──
    h = httpx.get(f"{base}/health", timeout=10.0)
    assert h.status_code == 200, f"/health 應 200,實得 {h.status_code}"
    print(f"{OK} [0] /health 200:{h.text[:80]}")

    # ── 1. 無 token → 401 ──
    r = httpx.post(mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                   headers={"Accept": "application/json, text/event-stream"}, timeout=10.0)
    assert r.status_code == 401, f"無 token 應 401,實得 {r.status_code}"
    print(f"{OK} [1] 無 token → 401 fail-closed")

    # ── 2. ?token= 握手 + 工具集(member 有寫、無晉升)──
    async with streamablehttp_client(f"{mcp_url}?token={member}") as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"{OK} [2] ?token= 握手:server={init.serverInfo.name};tools={names}")
            assert {"query_flavor_map", "predict_method_swap", "log_calibration"} <= set(names)
            assert "promote_customization" not in names and "list_customizations" not in names, \
                "晉升工具不得暴露於 HTTP(只在 stdio owner 門)"
            print(f"{OK}     網路面有 log_calibration(member 寫),無晉升工具。")

    # ── 命門 A:member 寫自有 self(寫探針,供冷啟動驗證)──
    w1 = await _call(mcp_url, member, "log_calibration", {
        "brew_mechanism": "percolation", "grade": "C", "method": "V60",
        "grind_um": 600, "acidity": 9.4, "user_id": "self", **probe,
    })
    assert w1.get("ok"), f"member 寫自有 self 失敗:{w1}"
    print(f"{OK} [3a] member 寫自有 self 成功:id={w1['id'][:8]}… tag='{tag}'")

    # ── 命門 A':member 嘗試寫 global + A 級 → 被 confine 回自有 ns、降為 B(global 不被污染)──
    w2 = await _call(mcp_url, member, "log_calibration", {
        "brew_mechanism": "percolation", "grade": "A", "protocol": "SCA_cupping",
        "method": "V60", "grind_um": 605, "acidity": 9.5, "user_id": "global", **probe,
    })
    assert w2.get("ok"), f"member 寫(將被 confine)整筆失敗:{w2}"
    notes = w2.get("trust_notes") or []
    assert notes and any("global" in n or "自有" in n for n in notes), \
        f"member 寫 global 應回 trust_notes 說明已被 confine:{notes}"
    print(f"{OK} [3b] member 寫 global → confine 回自有 ns(A→B);trust_notes:{notes[0][:48]}…")

    # ── 命門 B:機制硬分區(同探針查 percolation 有、查 immersion 無)──
    perc = await _evidence_ids(mcp_url, member, probe["origin"])
    imm_out = await _call(mcp_url, member, "query_flavor_map", {
        "brew_mechanism": "immersion", "mode": "recommend", **probe})
    imm = {e["id"] for e in imm_out.get("evidence", [])}
    assert w1["id"] in perc, "percolation 探針應在 percolation 召回出現(對照才有意義)"
    assert perc.isdisjoint(imm), f"機制洩漏!探針跨機制出現:{perc & imm}"
    print(f"{OK} [3c] 機制硬分區:percolation 探針 {len(perc)} 筆,不漏進 immersion({len(imm)} 筆,互斥)。")

    # ── 命門 C:讀隔離(需第二 member / reader token 才驗;否則明確略過)──
    if member2 or reader:
        owner_sees = await _evidence_ids(mcp_url, member, probe["origin"])
        assert w1["id"] in owner_sees, "寫入者自己應讀得到自有 self 探針"
        if member2:
            other = await _evidence_ids(mcp_url, member2, probe["origin"])
            assert w1["id"] not in other, "讀隔離破!另一 member 讀到了這個 self 探針"
            print(f"{OK} [3d-i] 另一 member 讀不到此 self 探針({len(other)} 筆)。")
        if reader:
            rd = await _evidence_ids(mcp_url, reader, probe["origin"])
            assert w1["id"] not in rd, "讀隔離破!reader 讀到 self(或 global 被污染)"
            print(f"{OK} [3d-ii] reader(只讀 global)讀不到此 self 探針({len(rd)} 筆)→ global 未被污染。")
    else:
        print("⏭️  [3d] 略過跨身分讀隔離:未提供 CIE_SMOKE_MEMBER2_TOKEN / CIE_SMOKE_READER_TOKEN。"
              "\n     (本機 tools/smoke_http.py 已用三層 token 完整驗過讀隔離;線上補驗需多發一個 token。)")

    print("\n✅ 三命門通過:認證 fail-closed、member 受限寫(global 不被網路污染)、機制硬分區。")
    print(f"\n👉 冷啟動持久第二階段:讓實例 scale-to-zero(閒置 ~15 分)或部署新 revision 後,跑:")
    print(f"     CIE_SMOKE_URL=$CIE_SMOKE_URL CIE_SMOKE_MEMBER_TOKEN=*** "
          f"python tools/smoke_remote.py --verify-persistence {tag}")
    return tag


def main() -> None:
    ap = argparse.ArgumentParser(description="遠端已部署 CIE MCP 的真 client 驗證(任務 D)。")
    ap.add_argument("--verify-persistence", metavar="TAG", default=None,
                    help="第二階段:查先前寫的 tag 探針,驗證撐過冷啟動。")
    a = ap.parse_args()

    base = _env("CIE_SMOKE_URL", required=True).rstrip("/")
    member = _env("CIE_SMOKE_MEMBER_TOKEN", required=True)
    if a.verify_persistence:
        asyncio.run(verify_persistence(base, member, a.verify_persistence))
    else:
        asyncio.run(run_three_gates(
            base, member, _env("CIE_SMOKE_MEMBER2_TOKEN"), _env("CIE_SMOKE_READER_TOKEN")))


if __name__ == "__main__":
    main()
