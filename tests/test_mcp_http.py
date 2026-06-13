"""Remote MCP 公開門(HTTP)= 唯讀:雙 token 認證、CORS 鎖 claude.ai、/health、401,
且**只掛讀工具**(無寫入路徑)。設計 §13/§16「兩扇門」。

用 Starlette TestClient(httpx,離線、不觸真網路)打 ASGI app。傳輸層關注點在此;
工具治理邏輯在 test_mcp_gate.py。MCP 協定本身的 JSON-RPC 握手在 tools/smoke_http.py 實打實驗。
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

PRIMARY = "primary-read-token"          # CIE_MCP_AUTH_TOKEN:主要唯讀 token
EXTRA = "extra-read-token"              # CIE_MCP_GUEST_TOKENS 內的額外唯讀 token


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


def test_root_public_no_auth(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["name"] == "coffee-intuition-engine"


# ────────────────────────────── 公開門唯讀:只掛讀工具 ──────────────────────────────

def test_http_registers_only_read_tools():
    """公開門(HTTP)**不掛 log_calibration**(寫工具);只有 query / method_swap(讀)。
    這是『網路上無寫入路徑』的第一道(§16「兩扇門」)。"""
    _, mcp = server_http.build_app(config=_cfg(), engine=Engine(VectorStore()), auto_seed=False)
    names = sorted(t.name for t in asyncio.run(mcp.list_tools()))
    assert "log_calibration" not in names
    assert names == ["predict_method_swap", "query_flavor_map"]


# ────────────────────────────── 認證閘(HTTP 一切 token 唯讀) ──────────────────────────────

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
    # 有效 Bearer(主要唯讀 token)→ 通過認證閘(不再是 401;進到 MCP 層)。
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


def test_key_query_param_alias_with_extra_read_token(client):
    # ?key= 別名 + 額外唯讀 token(供個別撤銷)皆通過。
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


# ────────────────────────────── 安全不變式:stateless 為前提 ──────────────────────────────

def test_build_app_refuses_stateful_mode():
    """有狀態模式下 per-request reader principal(contextvar)看不到 → 工具退回 owner 預設,
    瓦解唯讀門防禦縱深。build_app 須 fail-closed 拒啟動(見 server_http、DESIGN §16.3)。"""
    with pytest.raises(RuntimeError, match="stateless"):
        server_http.build_app(config=_cfg(mcp_stateless=False),
                              engine=Engine(VectorStore()), auto_seed=False)


# ────────────────────────────── stdio 私有門(唯一寫入,零回歸) ──────────────────────────────

def test_stdio_entry_registers_all_tools_and_owner_principal():
    """私有門 stdio(mcp_server)註冊**全部**工具(含 log_calibration 寫工具)、自動 seed、
    預設身分 = LOCAL_PRINCIPAL(owner、can_write、不施讀過濾)→ 唯一寫入門,零回歸。"""
    import importlib

    import mcp_server
    importlib.reload(mcp_server)

    names = sorted(t.name for t in asyncio.run(mcp_server.mcp.list_tools()))
    assert names == ["log_calibration", "predict_method_swap", "query_flavor_map"]
    assert mcp_server._engine.store.count() > 0  # 自動 seed

    from cie.mcp_principal import current_principal
    p = current_principal()  # 未設 contextvar → LOCAL_PRINCIPAL
    assert p.role == "owner" and p.read_user_ids is None and p.can_write is True
