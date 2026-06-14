"""對**已部署的遠端 URL**(Cloud Run 等)跑真 MCP streamable-http 驗證 —— 即任務 D 的「三命門 + 冷啟動持久」。

與 `tools/smoke_http.py` 的差別:後者自起本機 uvicorn;**本腳本不起伺服器**,只拿一支真
MCP client 打你給的公開 HTTPS URL,用你**實際部署的 token** 驗證線上實例。沒有金鑰寫死,
全部從環境變數讀。

設定(環境變數):
  CIE_SMOKE_URL            必填。服務 base,如 https://cie-mcp-xxxx.run.app(不含 /mcp)。
  CIE_SMOKE_MEMBER_TOKEN   必填(寫入模式)。你的 member token(= 部署的 CIE_MCP_AUTH_TOKEN 或某 guest）。
  CIE_SMOKE_MEMBER2_TOKEN  選填。第二個 member token(另一命名空間)→ 才能驗「A 讀不到 B 的 self」。
  CIE_SMOKE_READER_TOKEN   選填。reader token(無命名空間)→ 才能驗「reader 只讀 global」。
  CIE_SMOKE_NGUEST_TOKENS  選填(唯讀 N-guest 模式)。JSON {命名空間: token},如
                           {"henry1266":"<tok>","ga815647":"<tok>"};缺則退回 MEMBER(+MEMBER2)。

用法:
  # 唯讀 N-guest pairwise(**不寫入,絕不碰 live D1**;只 query/list_tools/health/401 探針):
  #   驗:啟動過唯一性守衛(/health 200 ⇒ 真實 guest 設定合法)、401 fail-closed、各 token=member
  #   受限工具面(無晉升)、機制硬分區、pairwise self 讀隔離(set-diff;界線見 run_readonly_nguest）。
  CIE_SMOKE_URL=https://... CIE_SMOKE_NGUEST_TOKENS='{"a":"...","b":"..."}' \
      python tools/smoke_remote.py --readonly-nguest

  # 完整三命門(**會寫**一筆帶唯一 tag 的 self 探針,印出 tag 供冷啟動驗證用):
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


# 唯讀模式用的中性常見豆況(只為從 global 共享語料召回到非空證據;不寫入)。
RO_BEAN = {"origin": "Ethiopia", "process": "washed", "roast_agtron": 70}


async def _query_ids(mcp_url: str, token: str, mechanism: str, bean: dict | None = None) -> set:
    """以 token 對某機制查一次(讀工具),回傳 evidence id 集。純讀,不寫。"""
    out = await _call(mcp_url, token, "query_flavor_map",
                      {"brew_mechanism": mechanism, "mode": "recommend", **(bean or RO_BEAN)})
    return {e["id"] for e in out.get("evidence", [])}


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


# ────────────────────────────── 唯讀 N-guest pairwise(不寫入,安全 live 確認) ──────────────────────────────

def _nguest_tokens() -> dict:
    """讀唯讀模式的 token 集:CIE_SMOKE_NGUEST_TOKENS(JSON {命名空間: token});
    缺則退回 CIE_SMOKE_MEMBER_TOKEN(+MEMBER2)。**金鑰不寫死,全從環境變數讀**。"""
    raw = _env("CIE_SMOKE_NGUEST_TOKENS")
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            sys.exit("CIE_SMOKE_NGUEST_TOKENS 非合法 JSON(應為 {命名空間: token} 物件)。")
        if not isinstance(data, dict) or not data:
            sys.exit("CIE_SMOKE_NGUEST_TOKENS 應為非空 {命名空間: token} 物件。")
        return {str(k): str(v) for k, v in data.items() if v}
    out: dict = {}
    if (m1 := _env("CIE_SMOKE_MEMBER_TOKEN")):
        out["member"] = m1
    if (m2 := _env("CIE_SMOKE_MEMBER2_TOKEN")):
        out["member2"] = m2
    if not out:
        sys.exit("唯讀 N-guest 需 token:設 CIE_SMOKE_NGUEST_TOKENS 或 CIE_SMOKE_MEMBER_TOKEN(+MEMBER2)。")
    return out


async def run_readonly_nguest(base: str, tokens: dict, reader: str = "") -> None:
    """**唯讀** N-guest pairwise 線上確認 —— 絕不寫入(只 query_flavor_map / list_tools / health / 401 探針)。

    證明範圍(read-only 可安全 live 確認的部分):
      (a) /health 200 → 實例啟動成功 ⇒ **真實 Secret Manager guest 設定通過唯一性守衛**
          (validate_guest_token_config;否則 build_app fail-closed,實例根本起不來)。
      (1) 無 / 壞 token → 401 fail-closed(認證閘)。
      (2) 每個 token → member 工具面(query/log/delete/swap),**無晉升工具**(= 解析為 member 非 owner)。
      (3) 機制硬分區:同豆查三機制,evidence id 集兩兩互斥(鐵則 1 線上仍守)。
      (4) pairwise self 讀隔離:各 token 查同一 query,比較 evidence id 集。有 reader(純 global 基線)
          時:扣除基線得各自 self 可見集,斷言**兩兩互斥** + 每個 member 都涵蓋基線;無 reader 時:
          以「全體共見集」近似 global、各自額外即 self 可見集,斷言兩兩互斥。

    讀路徑界線(誠實標註):evidence 不含 user_id,故無法逐筆讀出命名空間。set-diff 能證偽
    **非對稱**洩漏(某記錄被 A 見、B 不見、卻又被另一 token 見);但對「同一筆 self 被**所有** token
    都看到」的對稱洩漏,在無 reader 純基線時無法與 global 區分(N≥3 時須洩漏給全體才隱形,故 N 越大越嚴)。
    正向 airtight 證明(寫可辨識探針 → 只有自己讀得到)在本機 `smoke_http.py` [5] + 單元測試,
    以及之後的「寫入隔離專輪」(寫探針 + delete 收尾,另行授權)。
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    mcp_url = f"{base}/mcp"
    assert tokens, "需至少一個 token。"
    print(f"🔒 唯讀模式:只發 query_flavor_map / list_tools / health / 401 探針,**不寫入 live D1**。")

    # (a) /health 200 ⇒ 啟動成功 ⇒ 真實 guest 設定已過唯一性守衛(否則啟動即 fail-closed)。
    h = httpx.get(f"{base}/health", timeout=10.0)
    assert h.status_code == 200, f"/health 應 200,實得 {h.status_code}"
    print(f"{OK} [a] /health 200 → 實例啟動成功 ⇒ 真實 Secret guest 設定已過唯一性守衛"
          f"(validate_guest_token_config;否則 build_app fail-closed、起不來)。")

    # (1) 無 token / 壞 token → 401 fail-closed。
    for label, tok in (("無 token", None), ("壞 token", "definitely-not-a-valid-token")):
        url = mcp_url if tok is None else f"{mcp_url}?token={tok}"
        r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                       headers={"Accept": "application/json, text/event-stream"}, timeout=10.0)
        assert r.status_code == 401, f"{label} 應 401,實得 {r.status_code}"
    print(f"{OK} [1] 無 token / 壞 token → 401 fail-closed。")

    # (2) 每個 token → member 工具面,無晉升工具(非 owner)。
    for ns, tok in tokens.items():
        async with streamablehttp_client(f"{mcp_url}?token={tok}") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = sorted(t.name for t in (await session.list_tools()).tools)
        assert {"query_flavor_map", "log_calibration", "delete_calibration",
                "predict_method_swap"} <= set(names), f"token[{ns}] 工具面缺項:{names}"
        assert "promote_customization" not in names and "list_customizations" not in names, \
            f"token[{ns}] 不應看到晉升工具(網路面非 owner):{names}"
    print(f"{OK} [2] {len(tokens)} 個 token 皆 member 受限面(query/log/delete/swap),無晉升工具(非 owner)。")

    # (3) 機制硬分區(讀 global 共享語料):同豆查三機制,evidence id 集兩兩互斥。
    any_tok = next(iter(tokens.values()))
    mech_ids = {m: await _query_ids(mcp_url, any_tok, m)
                for m in ("immersion", "percolation", "pressure")}
    mechs = list(mech_ids)
    for i in range(len(mechs)):
        for j in range(i + 1, len(mechs)):
            a, b = mechs[i], mechs[j]
            assert mech_ids[a].isdisjoint(mech_ids[b]), \
                f"機制洩漏!{a} 與 {b} 共享 evidence:{mech_ids[a] & mech_ids[b]}"
    print(f"{OK} [3] 機制硬分區:三機制 evidence 兩兩互斥"
          f"(counts={ {m: len(s) for m, s in mech_ids.items()} })。")

    # (4) pairwise self 讀隔離:各 token 查同一 query,比較 evidence id 集。
    per = {ns: await _query_ids(mcp_url, tok, "percolation") for ns, tok in tokens.items()}
    if reader:
        base_set = await _query_ids(mcp_url, reader, "percolation")   # reader = 純 global 基線
        for ns, s in per.items():
            assert base_set <= s, f"token[{ns}] 未涵蓋 global 基線(reader 見、它卻沒見):{base_set - s}"
        self_vis = {ns: (s - base_set) for ns, s in per.items()}
        scope_note = "(有 reader 純 global 基線:扣除後即自有 self 可見集)"
    else:
        common = set.intersection(*per.values()) if per else set()
        self_vis = {ns: (s - common) for ns, s in per.items()}
        scope_note = "(無 reader 基線:全體共見集近似 global;對稱洩漏界線見 docstring)"
    labels = list(self_vis)
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            a, b = labels[i], labels[j]
            shared = self_vis[a] & self_vis[b]
            assert not shared, f"pairwise self 讀洩漏!token[{a}] 與 token[{b}] 共見非-global 記錄:{shared}"
    print(f"{OK} [4] pairwise self 讀隔離:任兩 token 自有可見集互斥 {scope_note};"
          f"self 可見數={ {ns: len(s) for ns, s in self_vis.items()} }。")
    if not any(self_vis.values()):
        print("    ⚠️  各 token 自有 self 可見集皆空 → 正向 pairwise 為 vacuous(各 self 層尚無資料或"
              "未被此 query 召回)。airtight 證明見本機 smoke_http [5] + 單元測試;真值待寫入隔離專輪。")

    print("\n✅ 唯讀 N-guest 線上確認通過:啟動過唯一性守衛、401 fail-closed、各 token=member 受限面、"
          "機制硬分區、pairwise self 讀隔離(讀路徑)。**全程未寫 live D1**;寫入隔離待專輪(寫探針 + delete 收尾)。")


def main() -> None:
    ap = argparse.ArgumentParser(description="遠端已部署 CIE MCP 的真 client 驗證(任務 D)。")
    ap.add_argument("--verify-persistence", metavar="TAG", default=None,
                    help="第二階段:查先前寫的 tag 探針,驗證撐過冷啟動。")
    ap.add_argument("--readonly-nguest", action="store_true",
                    help="**唯讀** N-guest pairwise 線上確認(不寫入 live D1);token 走 CIE_SMOKE_NGUEST_TOKENS。")
    a = ap.parse_args()

    base = _env("CIE_SMOKE_URL", required=True).rstrip("/")
    if a.readonly_nguest:
        asyncio.run(run_readonly_nguest(base, _nguest_tokens(), _env("CIE_SMOKE_READER_TOKEN")))
        return
    member = _env("CIE_SMOKE_MEMBER_TOKEN", required=True)
    if a.verify_persistence:
        asyncio.run(verify_persistence(base, member, a.verify_persistence))
    else:
        asyncio.run(run_three_gates(
            base, member, _env("CIE_SMOKE_MEMBER2_TOKEN"), _env("CIE_SMOKE_READER_TOKEN")))


if __name__ == "__main__":
    main()
