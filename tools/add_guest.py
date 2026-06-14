"""新增一個 guest 分享:產 member token + 驗證唯一性 + 印出可分享物。**不碰 live。**

CIE 的 remote MCP 是「三層 + 人工晉升」(設計 §16):每個 guest 是一個 **member**,寫/刪只
落自有 `self` 命名空間、`grade≤B`、讀 `[global, 自己的 self]`,**寫不到 global、讀不到他人 self**。
要安全地多發一個 guest,只需把 `{token: user_id}` 併進 `CIE_MCP_GUEST_TOKENS`、確認**全域唯一**
(任兩個 guest 的 user_id 絕不可相同,否則共用同一 self = 跨 guest 混入),再更新 Secret Manager
並冷啟動 / 重部署。本工具把這串「產生 + 驗證 + 印指令」做成一鍵,**但刻意不碰 live**:

  - **產**:`secrets.token_urlsafe` 產一個高熵 member token。
  - **驗**:把新 `{token: user_id}` 併進現有 `CIE_MCP_GUEST_TOKENS`,跑**既有**
    `cie.mcp_principal.validate_guest_token_config`(不複刻唯一性邏輯)——user_id 全域唯一、
    不撞 primary(`CIE_MCP_AUTH_TOKEN`)、不認領保留字 `global`/`self`。撞了即 fail-closed、
    報哪裡撞、**不產出**(exit≠0)。
  - **印**:① 新 token(預設遮罩,`--show` 才全顯)② 併好的 `CIE_MCP_GUEST_TOKENS` JSON
    ③ 更新 Secret Manager + 重部署的 gcloud 指令範本 ④ 可分享連接器 URL
    `https://<host>/mcp?token=<token>`(host 讀 `CIE_PUBLIC_URL`;沒設就提示填)。

**不做 live 寫入**:不呼叫 gcloud、不寫 Secret Manager——只產生 + 驗證 + 印指令,prod 操作要人手。
token **不寫進任何被追蹤的檔**;`--save` 可把它落進 gitignored `secrets/`(安全保存處),否則只進
stdout(operator 自行貼進 Secret Manager)。機密不進日誌。

用法:
  python -m tools.add_guest --user-id alice             # 指定命名空間
  python -m tools.add_guest --name "Henry Wang"         # 顯示名 → 自動轉 user_id(henry-wang)
  python -m tools.add_guest --user-id alice --show      # 全顯 token(供實際複製)
  python -m tools.add_guest --user-id alice --save      # 另把 token 落進 gitignored secrets/
  python -m tools.add_guest --user-id alice --existing @current-secret.json  # 以指定現有設定為基準

`--existing` 預設讀 `CIE_MCP_GUEST_TOKENS`(由 .env / shell env);realistic 流程是先把 Secret
Manager 現值拉下來用 `--existing @file` / `--existing '<json>'` 餵進來,拿回併好的新值。

退出碼:成功 0;唯一性/保留字撞、設定錯誤非 0(可塞進腳本)。詳見 docs/SHARING.md。
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

_ROOT = Path(__file__).resolve().parent.parent
# 允許 `python tools/add_guest.py` 直接跑(補 sys.path);`-m tools.add_guest` 不需。
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cie.config import Config  # noqa: E402
from cie.mcp_principal import (  # noqa: E402
    OWNER_SELF_USER_ID,
    RESERVED_NAMESPACES,
    GuestTokenConfigError,
    _parse_member_tokens,
    validate_guest_token_config,
)

# 部署事實(可用旗標覆寫,不硬綁):Cloud Run 服務 / 區域 / 掛 CIE_MCP_GUEST_TOKENS 的 secret 名。
DEFAULT_SERVICE = "cie-mcp"
DEFAULT_REGION = "asia-east1"
DEFAULT_SECRET_NAME = "cie-mcp-guest-tokens"
TOKEN_BYTES = 32  # secrets.token_urlsafe(32) ≈ 43 字元 base64url(~256 bits 熵)
# 合法 user_id 命名空間:小寫 slug。slugify 的輸出恆滿足此式;--user-id 直傳則由 build_guest 把關。
# 限字元集杜絕 '/'、'\'、'.'(--save 路徑穿越)與跨命名空間混淆。
_USER_ID_RE = re.compile(r"\A[a-z0-9-]+\Z")


class AddGuestError(RuntimeError):
    """add_guest 自身的前置把關失敗(保留字 / 空 user_id / token 撞既有)。

    繼承 `RuntimeError`,與 `GuestTokenConfigError`(唯一性守衛)一致;main() 兩者皆 fail-closed
    印出『撞哪裡 + 不產出』。**唯一性/撞 primary 邏輯一律委派 `validate_guest_token_config`,不在此複刻。**
    """


# ────────────────────────────── 純函式核心(離線可測,不碰 env / live) ──────────────────────────────

def gen_token(nbytes: int = TOKEN_BYTES) -> str:
    """產一個高熵 URL-safe member token(`secrets.token_urlsafe`,CSPRNG)。"""
    return secrets.token_urlsafe(nbytes)


def slugify(name: str) -> str:
    """顯示名 → user_id 命名空間 slug:小寫、非 [a-z0-9] 併為單一 '-'、去頭尾 '-'。

    例:'Henry Wang' → 'henry-wang';'alice' → 'alice'。空結果 → AddGuestError。
    """
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not s:
        raise AddGuestError(f"無法從顯示名 {name!r} 產生有效 user_id(需含英數字元)。")
    return s


def mask(token: str) -> str:
    """遮罩 token 供畫面顯示(預設):短的全遮,長的留首 4 + 尾 4。**遮罩後不可複製貼上**。"""
    if len(token) <= 10:
        return "*" * len(token)
    return f"{token[:4]}…{token[-4:]}"


def normalize_public_url(public_url: str) -> str:
    """正規化 public base:去頭尾空白 / 尾斜線 / 尾段 '/mcp'(避免 //mcp 或 /mcp/mcp)。"""
    u = public_url.strip().rstrip("/")
    if u.endswith("/mcp"):
        u = u[: -len("/mcp")]
    return u


def build_connector_url(public_url: str, token: str) -> Optional[str]:
    """組可分享連接器 URL `<public_url>/mcp?token=<token>`;public_url 空 → None(提示填)。"""
    base = normalize_public_url(public_url)
    if not base:
        return None
    return f"{base}/mcp?token={token}"


def _tokens_to_object_json(tokens: Dict[str, Optional[str]]) -> str:
    """把 {token: user_id|None} 正規化成 `CIE_MCP_GUEST_TOKENS` 物件 JSON(reader 的 None → "")。

    一律輸出物件形式(即使現有是 `[token]` 陣列):member 值=命名空間、reader 值=""。round-trips
    回 `_parse_member_tokens`(空字串視為 reader)。`ensure_ascii=False` 讓非 ASCII user_id 可讀。
    """
    obj = {tok: (ns if ns is not None else "") for tok, ns in tokens.items()}
    return json.dumps(obj, ensure_ascii=False)


@dataclass
class GuestArtifacts:
    """build_guest 的產物(供 main 印出 / --save 落檔;不含任何 live 副作用)。"""
    user_id: str
    token: str
    merged_tokens: Dict[str, Optional[str]]   # {token: user_id|None}(已驗證乾淨)
    merged_json: str                          # 併好的 CIE_MCP_GUEST_TOKENS(物件形式)
    connector_url: Optional[str]              # public_url 空 → None
    public_url: str
    secret_name: str
    service: str
    region: str
    notes: List[str] = field(default_factory=list)


def build_guest(
    user_id: str,
    *,
    token: str,
    existing_raw: str,
    auth_token: str,
    public_url: str,
    secret_name: str = DEFAULT_SECRET_NAME,
    service: str = DEFAULT_SERVICE,
    region: str = DEFAULT_REGION,
    auth_user_id: str = OWNER_SELF_USER_ID,
) -> GuestArtifacts:
    """產一個新 guest 的併好設定 + 連接器 URL,**經既有唯一性守衛驗證**;有破口即 raise、不產出。

    純函式:所有輸入顯式傳入(不讀 env、不碰 live),便於離線單測。

    把關順序:
      0. user_id 為小寫 slug `[a-z0-9-]`(非空)→ 否則 `AddGuestError`(避免 --save 路徑穿越 +
         跨命名空間混淆;validate 不限字元集)。
      1. user_id 非保留字(`global`/`self`)、非 owner 的個人命名空間(`auth_user_id`)
         → 否則 `AddGuestError`(保留字會被 `_parse_member_tokens` 靜默 skip,故須在此明擋)。
      2. 現有設定須可解析(strict JSON,容忍 BOM):非空但無法解析 → `AddGuestError`、不產出
         (`_parse_member_tokens` 寬鬆地把 malformed 視為空 → 會靜默吃掉既有 guest)。
      3. 新 token 不得撞現有 guest token(防靜默覆寫;隨機 token 實質不會撞,defensive)。
      4. 併入後跑 **`validate_guest_token_config`**(唯一性 / 撞 primary / 認領 owner ns)
         → `GuestTokenConfigError`(不在此複刻其邏輯)。
      5. 確認新 token 在驗證後的乾淨對映中仍指向新 user_id(defensive;否則表示被 skip)。
    """
    user_id = user_id.strip()
    if not user_id:
        raise AddGuestError("user_id 不可為空。")
    # (0) 字元集守衛:命名空間須為小寫 slug [a-z0-9-]。validate 不限字元集,但 user_id 之後會進
    #     檔名(--save 落 secrets/guest-<user_id>.env)——不擋會被 '/'、'..' 等做路徑穿越,把含完整
    #     token 的檔寫到 secrets/ 之外(可能 git-tracked)。同時也避免跨命名空間混淆。--name 走 slugify
    #     恆滿足此式;--user-id 直傳則在此把關。
    if not _USER_ID_RE.match(user_id):
        raise AddGuestError(
            f"user_id {user_id!r} 含非法字元;命名空間須為小寫 slug(僅 [a-z0-9-],避免路徑分隔 / "
            f"跨命名空間混淆)。用 --name 可從顯示名自動轉 slug。")
    # (1) 保留字 / owner 命名空間:validate 仰賴 _parse 靜默 skip 保留字、不會 raise,故在此明擋。
    if user_id in RESERVED_NAMESPACES:
        raise AddGuestError(
            f"user_id {user_id!r} 是保留字 {sorted(RESERVED_NAMESPACES)};訪客不得認領 "
            f"global(共享客觀層)或 owner 的 self。請換一個命名空間。")
    if user_id == auth_user_id:
        raise AddGuestError(
            f"user_id {user_id!r} 是 owner 的個人命名空間(auth_user_id);guest 不得認領"
            f"(會與 owner 的 self 混入)。")

    # (2) 防『非空但無法解析』靜默吃掉既有 guest(命門):strict 解析現有設定,malformed → 拒、不產出。
    #     _parse_member_tokens 刻意寬鬆(任何 parse 失敗回 {});若就這樣併入,merged 只剩新 token =
    #     貼進 secret 後撤銷所有其他 guest。前導 BOM(PowerShell pipe 常注)會讓 json.loads 整份失敗,
    #     故先容忍 BOM、再 strict 驗結構。注意:'{}' / '[]' 為合法的『真的沒有現有 guest』,放行。
    existing_clean = (existing_raw or "").lstrip("﻿")
    if existing_clean.strip():
        try:
            structure = json.loads(existing_clean)
        except (ValueError, TypeError) as e:
            raise AddGuestError(
                f"現有 token 設定(--existing / CIE_MCP_GUEST_TOKENS)非合法 JSON:{e}。"
                f"拒絕產出以免覆蓋既有 guest(請確認來源無 BOM / 語法錯)。")
        if not isinstance(structure, (dict, list)):
            raise AddGuestError(
                "現有 token 設定須為 {token:user_id} 物件或 [token] 陣列;"
                "拒絕產出以免覆蓋既有 guest。")
    existing = _parse_member_tokens(existing_clean)
    # (3) token 撞現有 → 拒(否則 dict 併入會靜默覆寫某既有 guest)。
    if token in existing:
        raise AddGuestError("產生的 token 與現有某筆相同(極罕見);請重跑以重新產生。")

    merged: Dict[str, Optional[str]] = dict(existing)
    merged[token] = user_id
    merged_json = _tokens_to_object_json(merged)

    # (4) 委派既有唯一性守衛:user_id 全域唯一、不撞 primary、不認領 owner ns。撞 → 拒、不產出。
    cfg = Config(mcp_auth_token=auth_token, mcp_guest_tokens=merged_json)
    cleaned = validate_guest_token_config(cfg, auth_user_id=auth_user_id)

    # (5) defensive:新 token 須在乾淨對映中指向新 user_id(否則被 skip,設定不會生效)。
    if cleaned.get(token) != user_id:
        raise AddGuestError(
            f"新 token 未通過設定解析(user_id {user_id!r} 可能被視為無效)。請檢查 user_id。")

    notes: List[str] = []
    reader_count = sum(1 for v in existing.values() if v is None)
    member_count = sum(1 for v in existing.values() if v is not None)
    notes.append(f"併入前現有 token:{member_count} member + {reader_count} reader。")

    return GuestArtifacts(
        user_id=user_id,
        token=token,
        merged_tokens=cleaned,
        merged_json=merged_json,
        connector_url=build_connector_url(public_url, token),
        public_url=normalize_public_url(public_url),
        secret_name=secret_name,
        service=service,
        region=region,
        notes=notes,
    )


# ────────────────────────────── 展示(gcloud 範本 / 遮罩 JSON / 輸出) ──────────────────────────────

def gcloud_steps(art: GuestArtifacts) -> str:
    """更新 Secret Manager 新版本 + 重部署的 gcloud 指令範本(operator 自行套用,本工具不執行)。

    BOM 注意:用 Python utf-8 寫暫存檔(PowerShell pipe 會注 BOM);secret 加新版本後重部署。
    暖實例讀的是舊 secret——新 token 要冷啟動 / 重部署才生效。
    """
    return f"""\
# 1) 把上面【併好的 CIE_MCP_GUEST_TOKENS JSON】寫進無 BOM 暫存檔(--show 取得完整 JSON):
python -c "import pathlib,sys; pathlib.Path('guest-tokens.json').write_text(sys.stdin.read(),encoding='utf-8')"
# (或手動把 JSON 存成 utf-8 無 BOM 檔 guest-tokens.json)

# 2) 加一個新版本到既有 secret(不覆寫舊版、可回滾):
gcloud secrets versions add {art.secret_name} --data-file=guest-tokens.json

# 3) 重部署讓新 secret 生效(暖實例吃舊值,須冷啟動 / 重部署);ships 本地碼 + 冷啟動從 D1 重建:
gcloud run deploy {art.service} --source . --region {art.region} \\
    --update-secrets CIE_MCP_GUEST_TOKENS={art.secret_name}:latest \\
    --max-instances=1 --min-instances=0

# 4) 用後刪暫存檔(token 不留在工作目錄):
rm guest-tokens.json
# 提醒:Windows 上 gcloud 可能不在 PATH(見 memory gcloud-deploy-ops 的完整路徑);redeploy 需人工授權。"""


def _render_json(art: GuestArtifacts, *, show: bool) -> str:
    """併好的 CIE_MCP_GUEST_TOKENS:show=全顯(可貼進 secret);否則遮罩 token(不可貼)。

    遮罩走『在權威 merged_json 字串上逐一把完整 token 換成其遮罩』,而非以 mask(tok) 當 dict key——
    後者會在兩 token 的首4/尾4 相同時 collapse、靜默漏顯一筆。先替換較長者以避免子字串誤replace。
    """
    if show:
        return art.merged_json
    disp = art.merged_json
    for tok in sorted(art.merged_tokens, key=len, reverse=True):
        disp = disp.replace(tok, mask(tok))
    return disp


def format_output(art: GuestArtifacts, *, show: bool) -> str:
    """組整段 stdout 報告(① token ② JSON ③ gcloud ④ 連接器 URL)。預設遮罩 token。"""
    tok_disp = art.token if show else mask(art.token)
    url_disp = art.connector_url
    if url_disp and not show:
        url_disp = build_connector_url(art.public_url, mask(art.token))

    lines: List[str] = []
    lines.append("=" * 70)
    lines.append(f"新 guest:user_id = {art.user_id}  (member:寫/刪只落自有 self、grade≤B、")
    lines.append("           讀 global + 自己;寫不到 global、讀不到他人 self)")
    lines.append("=" * 70)
    if not show:
        lines.append("⚠ token 已遮罩(預設)。實際複製請加 --show;或 --save 落進 gitignored secrets/。")
    for n in art.notes:
        lines.append(f"  · {n}")
    lines.append("")
    lines.append("① 新 member token:")
    lines.append(f"     {tok_disp}")
    lines.append("")
    lines.append("② 併好的 CIE_MCP_GUEST_TOKENS(更新 secret 用):")
    lines.append(f"     {_render_json(art, show=show)}")
    lines.append("")
    lines.append("③ 更新 Secret Manager + 重部署(gcloud 範本,自行套用;本工具不執行):")
    for ln in gcloud_steps(art).splitlines():
        lines.append(f"     {ln}")
    lines.append("")
    lines.append("④ 可分享連接器 URL(貼進 claude.ai 自訂連接器):")
    if url_disp:
        lines.append(f"     {url_disp}")
        if not show:
            lines.append("     (token 已遮罩;--show 取完整 URL)")
    else:
        lines.append("     (未設 CIE_PUBLIC_URL → 無法組 URL。請設環境變數,例如:")
        lines.append("      CIE_PUBLIC_URL=https://cie-mcp-xxxxx.asia-east1.run.app)")
        lines.append("      連接器 URL 形式:https://<host>/mcp?token=<token>")
    lines.append("")
    lines.append("下一步:更新 secret → 冷啟動 / 重部署 → 把 URL + token + coffee-intuition skill")
    lines.append("        交給 guest。完整 runbook 見 docs/SHARING.md。")
    lines.append("=" * 70)
    return "\n".join(lines)


def save_secret_file(art: GuestArtifacts) -> Path:
    """把完整 token + 連接器 URL 落進 gitignored `secrets/guest-<user_id>.env`(安全保存處)。

    `secrets/` 已在 .gitignore;**不入庫、不入日誌**。供 operator 之後查回 / 撤銷對照。
    """
    secrets_dir = (_ROOT / "secrets").resolve()
    secrets_dir.mkdir(exist_ok=True)
    path = (secrets_dir / f"guest-{art.user_id}.env").resolve()
    # 防禦(雙保險;user_id 已 slug 化):最終路徑必須落在 secrets/ 內,絕不寫到外面(完整 token 在此)。
    if secrets_dir not in path.parents:
        raise AddGuestError(f"拒絕把含 token 的檔寫到 secrets/ 之外:{path}")
    body = [
        f"# CIE guest — user_id={art.user_id}  (gitignored;機密,勿入庫)",
        f"CIE_GUEST_USER_ID={art.user_id}",
        f"CIE_GUEST_TOKEN={art.token}",
    ]
    if art.connector_url:
        body.append(f"CIE_GUEST_CONNECTOR_URL={art.connector_url}")
    body.append("")
    path.write_text("\n".join(body), encoding="utf-8")
    return path


# ────────────────────────────── env 載入 + CLI ──────────────────────────────

def _load_dotenv() -> None:
    """把 repo 根 .env 注入 os.environ(只補未設定的鍵;shell env 優先)。

    add_guest 需讀現有 `CIE_MCP_GUEST_TOKENS` / `CIE_MCP_AUTH_TOKEN` / `CIE_PUBLIC_URL` 當預設;
    本 repo 無 python-dotenv 自動載入。**在 main() 內呼叫後才 `Config.from_env()`**(非 import 期),
    確保新值被讀到,且不在被 import 時造成副作用(利於測試)。token 只進 os.environ,絕不印出。
    """
    import os
    env_path = _ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _read_existing(arg: Optional[str], cfg_default: str) -> str:
    """解析 --existing:None → 用 config 預設;'@path' → 讀檔;否則當 JSON 字串原樣用。"""
    if arg is None:
        return cfg_default
    if arg.startswith("@"):
        p = Path(arg[1:])
        if not p.exists():
            raise AddGuestError(f"--existing 檔不存在:{p}")
        # utf-8-sig:容忍 PowerShell 寫檔常見的前導 BOM(否則 JSON 解析失敗 → 靜默吃掉既有 guest)。
        return p.read_text(encoding="utf-8-sig").strip()
    return arg


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m tools.add_guest",
        description="產 CIE remote MCP guest member token + 驗證唯一性 + 印可分享物(不碰 live)。",
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--user-id", help="guest 的 self 命名空間(全域唯一;非保留字 global/self)。")
    g.add_argument("--name", help="顯示名 → 自動轉 user_id slug(如 'Henry Wang' → henry-wang)。")
    ap.add_argument("--show", action="store_true",
                    help="全顯 token(預設遮罩)。實際複製進 secret / URL 時用。")
    ap.add_argument("--save", action="store_true",
                    help="另把完整 token 落進 gitignored secrets/guest-<user_id>.env(安全保存)。")
    ap.add_argument("--existing", default=None,
                    help="現有 CIE_MCP_GUEST_TOKENS 基準:JSON 字串或 @檔路徑;預設讀 env/.env。")
    ap.add_argument("--public-url", default=None,
                    help="覆寫 CIE_PUBLIC_URL(組連接器 URL 用),如 https://host。")
    ap.add_argument("--secret-name", default=DEFAULT_SECRET_NAME, help="Secret Manager secret 名。")
    ap.add_argument("--service", default=DEFAULT_SERVICE, help="Cloud Run 服務名。")
    ap.add_argument("--region", default=DEFAULT_REGION, help="Cloud Run 區域。")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    for _stream in (sys.stdout, sys.stderr):           # Windows 主控台 UTF-8
        try:
            _stream.reconfigure(encoding="utf-8")        # type: ignore[attr-defined]
        except Exception:
            pass

    ap = _build_arg_parser()
    ns = ap.parse_args(argv)

    _load_dotenv()
    cfg = Config.from_env()  # _load_dotenv 之後重讀,確保 .env 值被看到

    try:
        user_id = ns.user_id.strip() if ns.user_id else slugify(ns.name)
        existing_raw = _read_existing(ns.existing, cfg.mcp_guest_tokens)
        public_url = ns.public_url if ns.public_url is not None else cfg.public_url
        art = build_guest(
            user_id,
            token=gen_token(),
            existing_raw=existing_raw,
            auth_token=cfg.mcp_auth_token,
            public_url=public_url,
            secret_name=ns.secret_name,
            service=ns.service,
            region=ns.region,
        )
    except (AddGuestError, GuestTokenConfigError) as e:
        print(f"✗ 不產出(設定撞到):{e}", file=sys.stderr)
        return 2

    print(format_output(art, show=ns.show))
    if ns.save:
        path = save_secret_file(art)
        rel = path.relative_to(_ROOT) if path.is_relative_to(_ROOT) else path
        print(f"\n✓ 完整 token 已存至 gitignored {rel}(勿入庫)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
