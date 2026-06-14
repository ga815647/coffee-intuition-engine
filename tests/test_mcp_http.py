"""Remote MCP 網路面(HTTP)傳輸層:雙 token 認證、CORS 鎖 claude.ai、/health、401,
且**掛 member 受限寫工具(log_calibration)但不掛晉升工具**。設計 §13/§16「三層 + 人工晉升」。

用 Starlette TestClient(httpx,離線、不觸真網路)打 ASGI app。傳輸層關注點在此;
member confinement / 讀隔離 / 晉升等治理邏輯在 test_mcp_gate.py。MCP 協定本身的
JSON-RPC 握手在 tools/smoke_http.py 實打實驗。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from cie.config import Config
from cie.engine import Engine
from cie.store import VectorStore

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient  # noqa: E402

import server_http  # noqa: E402

PRIMARY = "primary-member-token"        # CIE_MCP_AUTH_TOKEN:你個人 member token(寫自己的 self)
EXTRA = "extra-member-token"            # CIE_MCP_GUEST_TOKENS 內的訪客 member token(寫 alice 命名空間)


def _cfg(**kw):
    base = dict(
        mcp_auth_token=PRIMARY,
        mcp_guest_tokens=json.dumps({EXTRA: "alice"}),
        mcp_stateless=True,
    )
    base.update(kw)  # 呼叫端覆寫(如 mcp_stateless=False)優先
    return Config(**base)


@pytest.fixture()
def client():
    # 注入記憶體 engine,避免動到全域庫;auto_seed 灌冷啟動種子供讀工具有資料。
    app, _ = server_http.build_app(config=_cfg(), engine=Engine(VectorStore()), auto_seed=True)
    with TestClient(app) as c:
        yield c


# ────────────────────────────── public / health ──────────────────────────────

def test_health_public_no_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mcp_endpoint"] == "/mcp"
    assert body["auth_configured"] is True


def test_health_reports_serving_and_canonical_counts(client):
    """PR6:/health 永遠回報 serving 索引筆數(+ 冷啟動 canonical 基準),讓「空 / 短缺索引」可見。"""
    body = client.get("/health").json()
    assert "serving_index_count" in body and "canonical_count" in body
    assert body["serving_index_count"] >= 1          # 離線開發 auto_seed 灌了冷啟動種子
    # 離線開發(memory + 本地 canonical)不 prime → canonical_count 為 None(非外部共用真相)。
    assert body["canonical_count"] is None


def test_root_public_no_auth(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["name"] == "coffee-intuition-engine"


# ────────────────────────────── 網路面:讀 + member 受限寫,但無晉升 ──────────────────────────────

def test_http_registers_member_write_but_no_promotion():
    """網路面(HTTP)掛讀工具 + member 受限寫(`log_calibration` / `delete_calibration`,
    後者只能刪自有 self),但**不掛晉升工具**。晉升 / 寫 global 只在本機 stdio owner 門 →
    網路上沒有寫 global 的路徑(§16「三層」)。"""
    _, mcp = server_http.build_app(config=_cfg(), engine=Engine(VectorStore()), auto_seed=False)
    names = sorted(t.name for t in asyncio.run(mcp.list_tools()))
    assert names == ["delete_calibration", "log_calibration",
                     "predict_method_swap", "query_flavor_map"]
    # 晉升工具(self→global)永不在 HTTP 出現。
    assert "promote_customization" not in names
    assert "list_customizations" not in names


# ────────────────────────────── 認證閘(token → member / reader) ──────────────────────────────

def test_mcp_without_token_is_401(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"
    assert "www-authenticate" in {k.lower() for k in r.headers}


def test_mcp_bad_token_is_401(client):
    r = client.post("/mcp?token=wrong", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401


def test_bearer_header_token_passes_auth(client):
    # 有效 Bearer(個人 member token)→ 通過認證閘(不再是 401;進到 MCP 層)。
    r = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {PRIMARY}",
                 "Accept": "application/json, text/event-stream",
                 "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert r.status_code != 401


def test_query_param_token_passes_auth(client):
    # claude.ai 網頁連接器只能用 ?token= 這條 → 必須有效。
    r = client.post(
        f"/mcp?token={PRIMARY}",
        headers={"Accept": "application/json, text/event-stream",
                 "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert r.status_code != 401


def test_key_query_param_alias_with_extra_member_token(client):
    # ?key= 別名 + 訪客 member token(寫 alice 命名空間;供個別撤銷)皆通過。
    r = client.post(
        f"/mcp?key={EXTRA}",
        headers={"Accept": "application/json, text/event-stream",
                 "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert r.status_code != 401


# ────────────────────────────── CORS 鎖 claude.ai ──────────────────────────────

def test_cors_preflight_allows_claude_ai(client):
    r = client.options("/mcp", headers={
        "Origin": "https://foo.claude.ai",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "https://foo.claude.ai"


def test_cors_preflight_allows_bare_claude_ai(client):
    r = client.options("/mcp", headers={
        "Origin": "https://claude.ai",
        "Access-Control-Request-Method": "POST",
    })
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "https://claude.ai"


def test_cors_rejects_other_origin(client):
    r = client.options("/mcp", headers={
        "Origin": "https://evil.example.com",
        "Access-Control-Request-Method": "POST",
    })
    assert r.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_cors_header_on_401(client):
    r = client.post("/mcp", headers={"Origin": "https://x.claude.ai",
                                     "Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin") == "https://x.claude.ai"


def test_bare_options_without_origin_is_401(client):
    """裸 OPTIONS /mcp(無 Origin → 非合法 CORS 預檢)不得無認證觸達 MCP 傳輸層。"""
    r = client.options("/mcp")  # 無 Origin、無 token
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


# ────────────────────────────── 傳輸層 DNS-rebinding(公開部署 Host 防 421) ──────────────────────────────

# 真 MCP initialize:走完傳輸層 → 才看得到「被 DNS-rebinding 防護擋掉(421)」與否。
# (上面 ping 測試只斷言 != 401;421 也 != 401,故擋不住此回歸 —— 線上 Cloud Run 曾因此 421。)
_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "regression", "version": "0"}},
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream",
                "Content-Type": "application/json"}


def test_arbitrary_host_not_misdirected(client):
    """公開部署回歸:FastMCP 預設 host=127.0.0.1 會自動開 DNS-rebinding 防護、只准 localhost,
    雲端真實 Host(如 *.run.app)→ 421 Misdirected Request,連 initialize 都到不了工具。
    build_app 顯式關閉內建 allowlist(預設),故任意 Host 應成功握手(200,非 421)。"""
    r = client.post(f"/mcp?token={PRIMARY}",
                    headers={**_MCP_HEADERS, "Host": "cie-mcp-abc123.asia-east1.run.app"},
                    json=_INIT)
    assert r.status_code != 421, "雲端 Host 被 DNS-rebinding 防護擋下(回歸!見 _transport_security)"
    assert r.status_code == 200, f"initialize 應成功握手,實得 {r.status_code}:{r.text[:200]}"


def test_allowed_hosts_lock_enforced_when_configured():
    """進階硬化:設 CIE_MCP_ALLOWED_HOSTS 後 → 開啟防護;未列 Host → 421,列入的 Host → 200。"""
    cfg = _cfg(mcp_allowed_hosts="cie.example.com, cie.example.com:*")
    app, _ = server_http.build_app(config=cfg, engine=Engine(VectorStore()), auto_seed=True)
    with TestClient(app) as c:
        bad = c.post(f"/mcp?token={PRIMARY}",
                     headers={**_MCP_HEADERS, "Host": "evil.example.com"}, json=_INIT)
        assert bad.status_code == 421, f"未列 Host 應被擋(421),實得 {bad.status_code}"
        good = c.post(f"/mcp?token={PRIMARY}",
                      headers={**_MCP_HEADERS, "Host": "cie.example.com"}, json=_INIT)
        assert good.status_code == 200, f"列入的 Host 應通過,實得 {good.status_code}:{good.text[:200]}"


# ────────────────────────────── 安全不變式:stateless 為前提(命門) ──────────────────────────────

def test_build_app_refuses_stateful_mode():
    """有狀態模式下 per-request member principal(contextvar)看不到 → 工具退回 owner 預設
    (可寫 global),網路呼叫者繞過 member confinement 寫到 global。build_app 須 fail-closed
    拒啟動(見 server_http、DESIGN §16.3)。"""
    with pytest.raises(RuntimeError, match="stateless"):
        server_http.build_app(config=_cfg(mcp_stateless=False),
                              engine=Engine(VectorStore()), auto_seed=False)


# ───────── 設定面唯一性守衛:build_app 啟動 fail-closed(§16.3,N-guest self 不混入) ─────────

def test_build_app_refuses_duplicate_guest_user_ids():
    """兩個 guest token 對映同一 user_id → build_app fail-closed 拒啟動
    (否則多 guest 靜默共用同一 self = 跨 guest 混入,§16.3 唯一性守衛)。"""
    cfg = _cfg(mcp_guest_tokens=json.dumps({"tokA": "alice", "tokB": "alice"}))
    with pytest.raises(RuntimeError, match="user_id"):
        server_http.build_app(config=cfg, engine=Engine(VectorStore()), auto_seed=False)


def test_build_app_refuses_guest_token_colliding_with_primary():
    """guest token 撞 primary(CIE_MCP_AUTH_TOKEN)→ 拒啟動(否則該 guest 靜默落 owner 的 self)。"""
    cfg = _cfg(mcp_guest_tokens=json.dumps({PRIMARY: "alice"}))
    with pytest.raises(RuntimeError, match="primary"):
        server_http.build_app(config=cfg, engine=Engine(VectorStore()), auto_seed=False)


def test_build_app_accepts_unique_n_guests():
    """≥3 個 user_id 互異的 guest → 正常啟動(守衛只擋破口,不擋合法多 guest)。"""
    cfg = _cfg(mcp_guest_tokens=json.dumps({"t1": "g1", "t2": "g2", "t3": "g3"}))
    app, _ = server_http.build_app(config=cfg, engine=Engine(VectorStore()), auto_seed=False)
    assert app is not None


# ────────────────────────────── stdio owner 門(唯一寫 global / 晉升,零回歸) ──────────────────────────────

def test_stdio_entry_registers_all_tools_and_owner_principal():
    """owner 門 stdio(mcp_server)註冊**全部 6 個**工具(讀 + 寫 log/delete + 晉升 list/promote)、
    自動 seed、預設身分 = LOCAL_PRINCIPAL(owner、can_write、不施讀過濾)→ 唯一能寫 global /
    刪任一 / 晉升,零回歸。"""
    import importlib

    import mcp_server
    importlib.reload(mcp_server)

    names = sorted(t.name for t in asyncio.run(mcp_server.mcp.list_tools()))
    assert names == [
        "delete_calibration", "list_customizations", "log_calibration",
        "predict_method_swap", "promote_customization", "query_flavor_map",
    ]
    assert mcp_server._engine.store.count() > 0  # 自動 seed

    from cie.mcp_principal import current_principal
    p = current_principal()  # 未設 contextvar → LOCAL_PRINCIPAL
    assert p.role == "owner" and p.read_user_ids is None and p.can_write is True
