"""tools/add_guest 離線測試:token 產生、user_id slug、唯一性驗證委派、遮罩/輸出。

全離線、確定性:不碰 live、不讀真 .env(main() 測試 monkeypatch 掉 _load_dotenv + 顯式 setenv)。
核心鐵則覆蓋:① 撞 user_id / 撞 primary / 認領保留字 一律被擋、不產出;② 唯一性邏輯委派既有
`validate_guest_token_config`(本工具不複刻);③ token 不外漏(預設遮罩)。
"""
from __future__ import annotations

import json
import re

import pytest

from cie.mcp_principal import GuestTokenConfigError, _parse_member_tokens
from tools.add_guest import (
    AddGuestError,
    _read_existing,
    build_connector_url,
    build_guest,
    format_output,
    gcloud_steps,
    gen_token,
    main,
    mask,
    normalize_public_url,
    save_secret_file,
    slugify,
)

NEW_TOKEN = "NEWGUESTTOKEN_aaaaaaaaaaaaaaaaaaaaaaaa"
PRIMARY = "PRIMARYTOKEN_bbbbbbbbbbbbbbbbbbbbbbbb"
PUBLIC = "https://cie-mcp-xxx.asia-east1.run.app"


def _guest(user_id="carol", *, token=NEW_TOKEN, existing="", auth=PRIMARY,
           public=PUBLIC, auth_user_id="self"):
    return build_guest(
        user_id, token=token, existing_raw=existing, auth_token=auth,
        public_url=public, auth_user_id=auth_user_id,
    )


# ────────────────────────────── token / slug / 小工具 ──────────────────────────────

def test_gen_token_high_entropy_and_unique():
    a, b = gen_token(), gen_token()
    assert a != b                      # CSPRNG:兩次不同
    assert len(a) >= 40                 # ~256bit(token_urlsafe(32) ≈ 43 字元)
    # 正向字元集:URL-query 安全(進連接器 ?token=)且 JSON-key 安全(進 CIE_MCP_GUEST_TOKENS 物件)。
    assert re.fullmatch(r"[A-Za-z0-9_-]+", a)


@pytest.mark.parametrize("name,expected", [
    ("alice", "alice"),
    ("Henry Wang", "henry-wang"),
    ("  Alice__B  ", "alice-b"),
    ("用戶 99", "99"),                 # 非 ASCII 去除、保留英數
])
def test_slugify(name, expected):
    assert slugify(name) == expected


def test_slugify_empty_raises():
    with pytest.raises(AddGuestError):
        slugify("！！！")              # 無英數 → 空 slug


def test_mask_hides_middle():
    tok = "abcd1234efgh5678WXYZ"
    m = mask(tok)
    assert m.startswith("abcd") and m.endswith("WXYZ") and "…" in m
    assert tok not in m
    assert mask("short") == "*****"   # 短的全遮


def test_normalize_public_url_strips_slash_and_mcp():
    assert normalize_public_url("https://h/") == "https://h"
    assert normalize_public_url("https://h/mcp") == "https://h"
    assert normalize_public_url("https://h/mcp/") == "https://h"
    assert normalize_public_url("  ") == ""


def test_build_connector_url():
    assert build_connector_url(PUBLIC, "TOK") == f"{PUBLIC}/mcp?token=TOK"
    assert build_connector_url("", "TOK") is None


# ────────────────────────────── build_guest 正常路徑 ──────────────────────────────

def test_build_guest_happy_path_first_guest():
    art = _guest(user_id="carol", existing="")
    assert art.user_id == "carol"
    assert art.token == NEW_TOKEN
    # 併好的 JSON round-trips:新 token → carol。
    parsed = _parse_member_tokens(art.merged_json)
    assert parsed[NEW_TOKEN] == "carol"
    assert art.merged_tokens[NEW_TOKEN] == "carol"
    assert art.connector_url == f"{PUBLIC}/mcp?token={NEW_TOKEN}"


def test_build_guest_merges_with_existing_members():
    existing = json.dumps({"tok_alice": "alice", "tok_bob": "bob"})
    art = _guest(user_id="carol", existing=existing)
    parsed = _parse_member_tokens(art.merged_json)
    assert parsed == {"tok_alice": "alice", "tok_bob": "bob", NEW_TOKEN: "carol"}


def test_build_guest_preserves_reader_existing_array_form():
    """現有是 [token] 陣列(reader):正規化成物件形式、reader 值=""、不丟失。"""
    art = _guest(user_id="carol", existing=json.dumps(["reader_tok"]))
    parsed = _parse_member_tokens(art.merged_json)
    assert parsed["reader_tok"] is None        # reader 保留
    assert parsed[NEW_TOKEN] == "carol"


def test_build_guest_no_public_url_gives_none_connector():
    art = _guest(public="")
    assert art.connector_url is None


def test_build_guest_name_slug_via_caller():
    # build_guest 收已解析 user_id;slug 在 main 做。這裡確認 slug→build 串得起來。
    art = _guest(user_id=slugify("Henry Wang"))
    assert art.user_id == "henry-wang"


# ────────────────────────────── build_guest fail-closed(撞了不產出) ──────────────────────────────

@pytest.mark.parametrize("reserved", ["global", "self"])
def test_build_guest_rejects_reserved_namespace(reserved):
    # match 釘住「保留字」的早期 guard:validate 對保留字只 skip 不 raise,故必須是這道而非 step-5 fallback。
    with pytest.raises(AddGuestError, match="保留字"):
        _guest(user_id=reserved)


@pytest.mark.parametrize("bad", ["x/../../evil", "a/b", "a.b", "UPPER", "a b", "carol\\evil", ".."])
def test_build_guest_rejects_non_slug_user_id(bad):
    """user_id 字元集守衛:含路徑分隔 / 非 [a-z0-9-] → 擋(防 --save 路徑穿越 + 跨命名空間混淆)。"""
    with pytest.raises(AddGuestError, match="非法字元|slug"):
        _guest(user_id=bad)


def test_build_guest_rejects_empty_user_id():
    with pytest.raises(AddGuestError):
        _guest(user_id="   ")


def test_build_guest_rejects_claiming_owner_namespace():
    """guest 認領 owner 的個人命名空間(自訂 auth_user_id)→ AddGuestError。"""
    with pytest.raises(AddGuestError):
        _guest(user_id="ownerns", auth_user_id="ownerns")


def test_build_guest_rejects_duplicate_user_id_via_validator():
    """委派 validate_guest_token_config:撞既有 user_id → GuestTokenConfigError(不複刻邏輯)。"""
    existing = json.dumps({"tok_alice": "alice"})
    with pytest.raises(GuestTokenConfigError, match="user_id|alice"):
        _guest(user_id="alice", existing=existing)


def test_build_guest_rejects_token_colliding_with_primary():
    """新 token 撞 primary(CIE_MCP_AUTH_TOKEN)→ GuestTokenConfigError(委派守衛)。"""
    with pytest.raises(GuestTokenConfigError, match="primary"):
        _guest(user_id="carol", token=PRIMARY, auth=PRIMARY)


def test_build_guest_rejects_token_colliding_with_existing_guest():
    """新 token 撞現有 guest token → AddGuestError(防靜默覆寫)。"""
    existing = json.dumps({NEW_TOKEN: "alice"})
    with pytest.raises(AddGuestError):
        _guest(user_id="carol", token=NEW_TOKEN, existing=existing)


@pytest.mark.parametrize("bad_existing", [
    '{"tok_alice": "alice",',                                          # 語法錯(缺括號)
    '"just a string"',                                                 # 合法 JSON 但非物件/陣列
    "not json at all",
])
def test_build_guest_rejects_unparseable_existing(bad_existing):
    """命門:現有設定非空但無法解析 → fail-closed,不可靜默縮成只剩新 token(會撤銷其他 guest)。"""
    with pytest.raises(AddGuestError, match="覆蓋既有 guest|非合法 JSON|物件或"):
        _guest(user_id="carol", existing=bad_existing)


def test_build_guest_tolerates_bom_on_valid_existing():
    """BOM + 合法 JSON(PowerShell pipe 常注前導 BOM):容忍、strip 後正常併入,不誤擋。"""
    existing = "﻿" + json.dumps({"tok_alice": "alice", "tok_bob": "bob"})   # 前導 BOM
    art = _guest(user_id="carol", existing=existing)
    parsed = _parse_member_tokens(art.merged_json)
    assert parsed == {"tok_alice": "alice", "tok_bob": "bob", NEW_TOKEN: "carol"}


def test_build_guest_empty_brace_existing_is_legit_first_guest():
    """'{}' / '[]' 是合法的『真的沒有現有 guest』,不該被誤擋。"""
    art = _guest(user_id="carol", existing="{}")
    assert _parse_member_tokens(art.merged_json) == {NEW_TOKEN: "carol"}
    art2 = _guest(user_id="carol", existing="[]")
    assert _parse_member_tokens(art2.merged_json) == {NEW_TOKEN: "carol"}


# ────────────────────────────── 遮罩保真 / --save 路徑安全 ──────────────────────────────

def test_render_json_masked_preserves_all_tokens_on_first4_last4_collision():
    """遮罩顯示不可因兩 token 首4/尾4 相同而 collapse 漏顯;每筆都要在(值非機密)。"""
    # 兩個首4/尾4 相同、中段不同的 token(若以 mask() 當 dict key 會 collapse 成一筆)。
    t1, t2 = "abcdAAAAAAAAAAAAwxyz", "abcdBBBBBBBBBBBBwxyz"
    existing = json.dumps({t1: "alice", t2: "bob"})
    art = _guest(user_id="carol", existing=existing)
    masked = format_output(art, show=False)
    # 三筆 user_id 都要出現(顯示沒漏);完整 token 都不外漏。
    assert masked.count("carol") >= 1 and "alice" in masked and "bob" in masked
    assert t1 not in masked and t2 not in masked and NEW_TOKEN not in masked


def test_save_secret_file_refuses_path_escape(tmp_path, monkeypatch):
    """defense-in-depth:即便 user_id 帶穿越(理論上已被 build_guest 擋),save 也不寫 secrets/ 外。"""
    import tools.add_guest as ag
    monkeypatch.setattr(ag, "_ROOT", tmp_path)
    art = _guest(user_id="carol")
    art.user_id = "x/../../evil"            # 繞過 build_guest 直接構造惡意 art(GuestArtifacts 非 frozen)
    with pytest.raises(AddGuestError, match="secrets"):
        save_secret_file(art)
    assert not (tmp_path / "evil.env").exists()
    assert not (tmp_path.parent / "evil.env").exists()


# ────────────────────────────── 輸出:遮罩 / gcloud 範本 ──────────────────────────────

def test_format_output_masks_token_by_default():
    art = _guest(user_id="carol")
    out = format_output(art, show=False)
    assert NEW_TOKEN not in out                 # 完整 token 不外漏
    assert mask(NEW_TOKEN) in out
    assert "遮罩" in out


def test_format_output_show_reveals_token_and_url():
    art = _guest(user_id="carol")
    out = format_output(art, show=True)
    assert NEW_TOKEN in out
    assert f"{PUBLIC}/mcp?token={NEW_TOKEN}" in out


def test_format_output_no_public_url_prompts_to_set():
    art = _guest(public="")
    out = format_output(art, show=True)
    assert "CIE_PUBLIC_URL" in out


def test_gcloud_steps_reference_secret_and_service():
    art = _guest(user_id="carol", existing="")
    steps = gcloud_steps(art)
    assert "cie-mcp-guest-tokens" in steps
    assert "gcloud secrets versions add" in steps
    assert "gcloud run deploy cie-mcp" in steps
    assert "asia-east1" in steps


def test_save_secret_file_writes_to_gitignored_secrets(tmp_path, monkeypatch):
    import tools.add_guest as ag
    monkeypatch.setattr(ag, "_ROOT", tmp_path)
    art = _guest(user_id="carol")
    path = save_secret_file(art)
    assert path.parent.name == "secrets"
    content = path.read_text(encoding="utf-8")
    assert NEW_TOKEN in content                  # 安全保存處(gitignored)才放完整 token
    assert "carol" in content


# ────────────────────────────── main() CLI(隔離 env,不讀真 .env) ──────────────────────────────

@pytest.fixture()
def _isolated_env(monkeypatch):
    """不讀真 .env;顯式設 MCP env,讓 main()→Config.from_env() 在乾淨基準上跑。"""
    import tools.add_guest as ag
    monkeypatch.setattr(ag, "_load_dotenv", lambda: None)
    monkeypatch.setenv("CIE_MCP_AUTH_TOKEN", PRIMARY)
    monkeypatch.setenv("CIE_MCP_GUEST_TOKENS", json.dumps({"tok_alice": "alice"}))
    monkeypatch.setenv("CIE_PUBLIC_URL", PUBLIC)


def test_main_success_returns_0_and_masks(_isolated_env, capsys):
    rc = main(["--user-id", "carol"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "carol" in out                         # 新 user_id 出現
    assert "tok_alice" not in out                 # 既有 token key 也遮罩,不外漏(user_id 值非機密)


def test_main_show_reveals(_isolated_env, capsys):
    rc = main(["--user-id", "carol", "--show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "/mcp?token=" in out


def test_main_duplicate_user_id_returns_2(_isolated_env, capsys):
    rc = main(["--user-id", "alice"])            # 撞既有 alice
    assert rc == 2
    err = capsys.readouterr().err
    assert "不產出" in err


def test_main_reserved_user_id_returns_2(_isolated_env, capsys):
    rc = main(["--user-id", "global"])
    assert rc == 2


def test_main_name_is_slugified(_isolated_env, capsys):
    rc = main(["--name", "Henry Wang", "--show"])
    assert rc == 0
    assert "henry-wang" in capsys.readouterr().out


def test_main_existing_override_from_inline_json(monkeypatch, capsys):
    import tools.add_guest as ag
    monkeypatch.setattr(ag, "_load_dotenv", lambda: None)
    monkeypatch.setenv("CIE_MCP_AUTH_TOKEN", PRIMARY)
    monkeypatch.delenv("CIE_MCP_GUEST_TOKENS", raising=False)
    monkeypatch.setenv("CIE_PUBLIC_URL", PUBLIC)
    # --existing 直接給 JSON,內含 dave;再加 dave 應撞(證明 --existing 真的被採用)。
    rc = main(["--user-id", "dave", "--existing", json.dumps({"tok_dave": "dave"})])
    assert rc == 2
    assert "不產出" in capsys.readouterr().err


def test_main_save_writes_gitignored_file_and_reports(_isolated_env, monkeypatch, tmp_path, capsys):
    """--save:落進 monkeypatch 後的 secrets/,stdout 報相對路徑且 token 仍遮罩(完整 token 只在檔內)。"""
    import tools.add_guest as ag
    monkeypatch.setattr(ag, "_ROOT", tmp_path)
    rc = main(["--user-id", "carol", "--save"])
    assert rc == 0
    out = capsys.readouterr().out
    saved = tmp_path / "secrets" / "guest-carol.env"
    assert saved.exists()
    content = saved.read_text(encoding="utf-8")
    assert "CIE_GUEST_TOKEN=" in content and "carol" in content
    # stdout 報相對 gitignored 路徑;且預設無 --show → stdout 不外漏完整 token(只在檔內)。
    assert "secrets" in out and "guest-carol.env" in out
    assert content.split("CIE_GUEST_TOKEN=")[1].split("\n")[0] not in out


def test_main_public_url_override_wins(_isolated_env, capsys):
    """--public-url 覆寫 env 的 CIE_PUBLIC_URL,連接器 URL 用覆寫值。"""
    rc = main(["--user-id", "carol", "--public-url", "https://override.example", "--show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "https://override.example/mcp?token=" in out
    assert PUBLIC not in out                       # env 預設值未被採用


def test_main_requires_user_id_or_name():
    """argparse 互斥群組 required=True:兩者皆無 → SystemExit(不靜默產出)。"""
    with pytest.raises(SystemExit):
        main([])


def test_main_user_id_and_name_mutually_exclusive():
    with pytest.raises(SystemExit):
        main(["--user-id", "carol", "--name", "Carol"])


# ────────────────────────────── _read_existing(@file / 缺檔 / inline) ──────────────────────────────

def test_read_existing_none_uses_config_default():
    assert _read_existing(None, '{"tok_x":"x"}') == '{"tok_x":"x"}'


def test_read_existing_inline_json_passthrough():
    assert _read_existing('{"tok_x":"x"}', "DEFAULT") == '{"tok_x":"x"}'


def test_read_existing_at_file_reads_and_tolerates_bom(tmp_path):
    """@file:讀檔內容;utf-8-sig 容忍 PowerShell 常見前導 BOM(否則 JSON 解析失敗)。"""
    p = tmp_path / "cur.json"
    p.write_text(json.dumps({"tok_y": "y"}), encoding="utf-8-sig")   # 帶 BOM
    raw = _read_existing(f"@{p}", "DEFAULT")
    assert json.loads(raw) == {"tok_y": "y"}     # BOM 已被 utf-8-sig 吃掉,可直接解析


def test_read_existing_at_missing_file_raises():
    with pytest.raises(AddGuestError, match="不存在"):
        _read_existing("@/no/such/file-xyz.json", "DEFAULT")
