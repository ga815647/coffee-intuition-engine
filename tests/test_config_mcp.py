"""Config.from_env 的 Remote MCP 欄位解析(尤其 PORT coalesce 與 stateless 旗標)。

回歸保護:PaaS(Render/Railway/Cloud Run…)常注入 `PORT`;若注入「存在但空」的
`PORT=`,舊版 `int(_get("PORT","8000"))` 會在 import 期炸 `int("")`。用 `or` 串接
coalesce 後,空字串退回 8000。
"""
from __future__ import annotations

import pytest

from cie.config import Config


def test_port_present_but_empty_falls_back_to_8000(monkeypatch):
    monkeypatch.delenv("CIE_MCP_PORT", raising=False)
    monkeypatch.setenv("PORT", "")           # present-but-empty(PaaS footgun)
    cfg = Config.from_env()
    assert cfg.mcp_port == 8000


def test_paas_port_used_when_set(monkeypatch):
    monkeypatch.delenv("CIE_MCP_PORT", raising=False)
    monkeypatch.setenv("PORT", "5123")
    assert Config.from_env().mcp_port == 5123


def test_explicit_cie_mcp_port_wins_over_paas_port(monkeypatch):
    monkeypatch.setenv("CIE_MCP_PORT", "9001")
    monkeypatch.setenv("PORT", "5123")
    assert Config.from_env().mcp_port == 9001


def test_no_port_env_defaults_8000(monkeypatch):
    monkeypatch.delenv("CIE_MCP_PORT", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    assert Config.from_env().mcp_port == 8000


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("yes", True), ("on-by-default-unset", True),
    ("0", False), ("false", False), ("no", False), ("", False),
])
def test_stateless_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.delenv("CIE_MCP_STATELESS", raising=False)
    if raw != "on-by-default-unset":
        monkeypatch.setenv("CIE_MCP_STATELESS", raw)
    assert Config.from_env().mcp_stateless is expected
