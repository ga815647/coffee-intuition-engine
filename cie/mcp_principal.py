"""呼叫者身分 + 寫入閘 + 讀範圍 —「兩扇門」模型(設計 §13 / §16)。

兩扇門:
  - **公開門(HTTP,`server_http.py`)= 唯讀。** 日常與分享都走這;**所有 token 一律
    `can_write=False`**(無 owner-over-HTTP)。token 外洩最壞只是被讀,寫不進去 →
    global 共享真相從網路面**不可污染**。
  - **私有門(本機 stdio,`mcp_server.py`)= owner 唯一寫入身分。** 靠「跑在你的機器上」
    授權,不經網路;刻意校正 session 才寫。寫入完整性鐵則(A 須 protocol、拒 prediction
    注入)套用於 owner 自己,擋自己的失誤。

寫入工具(`log_calibration`)**只在 stdio 註冊**(`register_tools(..., include_writes)`),
HTTP 傳輸根本不掛它;`apply_write_trust` 的 `can_write` 閘是第二道(防禦縱深:即便日後
誤把 write 工具掛上 HTTP,reader principal 仍 `can_write=False` → 拒絕)。

身分解析以**常數時間**比對 token(`hmac.compare_digest`),未設密鑰一律 fail-closed。
"""
from __future__ import annotations

import contextvars
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import CONFIG
from .schema import Grade, Record

logger = logging.getLogger("cie.mcp_principal")

# global 客觀因果層的保留 user_id(策展語料 corpus/global.jsonl 即用此)。
# 只有本機 owner(stdio)能寫;HTTP 唯讀共享之。
GLOBAL_USER_ID = "global"


@dataclass(frozen=True)
class Principal:
    """一個已認證呼叫者的權限快照。

    name           人類可讀標籤(log / 稽核 / 撤銷用)
    role           "owner"(本機 stdio)| "reader"(HTTP token)
    can_write      能否寫入校準。**只有本機 stdio owner=True**;一切 HTTP token=False。
    read_user_ids  讀取納入的 user_id 白名單(加性過濾);None = 不過濾(見全庫)。
    """
    name: str
    role: str
    can_write: bool
    read_user_ids: Optional[List[str]] = None


# 本地 / stdio 預設身分:owner,唯一寫入門,不施讀過濾(= 既有行為,零回歸)。
LOCAL_PRINCIPAL = Principal(
    name="local",
    role="owner",
    can_write=True,
    read_user_ids=None,
)


def make_reader_principal(name: str = "reader") -> Principal:
    """HTTP 唯讀身分:`can_write=False`、讀不過濾。

    兩扇門模型下 HTTP = 共享唯讀:日常與分享都見同一份(owner 透過 stdio 寫入的
    global 客觀層 + 其 self 校準)。`read_user_ids=None`(不過濾)——per-tenant self
    讀隔離為**未來如需再加**的功能,加性過濾機制(`store.search(..., user_ids=)`)
    已就緒、預設關閉,不動既有檢索 / 收縮 / conformal 數學。
    """
    return Principal(name=name, role="reader", can_write=False, read_user_ids=None)


# ────────────────────────────── token → principal 解析 ──────────────────────────────

def _safe_eq(a: str, b: str) -> bool:
    """常數時間字串比較(長度可洩漏、內容不可)。空字串一律不相等(fail-closed)。"""
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)


def _parse_read_tokens(raw: str) -> Dict[str, str]:
    """解析 CIE_MCP_GUEST_TOKENS → {token: label}。皆為**唯讀** token(label 僅供稽核 / 撤銷)。

    接受兩種格式(格式錯誤 → 空,fail-closed):
      - 物件 `{"tok_a":"alice","tok_b":"bob"}`(label = 值;沿用舊格式,值改作純標籤)。
      - 陣列 `["tok_a","tok_b"]`(label 預設 "reader")。
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("CIE_MCP_GUEST_TOKENS 非合法 JSON,忽略(視為無額外唯讀 token)。")
        return {}
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items() if k}
    if isinstance(data, list):
        return {str(t): "reader" for t in data if t}
    logger.warning("CIE_MCP_GUEST_TOKENS 應為 {token:label} 物件或 [token] 陣列,忽略。")
    return {}


def resolve_principal(
    token: Optional[str],
    *,
    auth_token: str = "",
    read_tokens: Optional[Dict[str, str]] = None,
) -> Optional[Principal]:
    """把 token 對映成**唯讀** Principal;無效 / 無密鑰 → None(401)。

    兩扇門:HTTP 一切 token 皆唯讀(無 owner-over-HTTP)。寫入只在本機 stdio。
      1. auth_token(主要唯讀 token,日常 + 分享)命中 → reader。
      2. read_tokens(額外唯讀 token,供個別發放 / 撤銷)任一命中 → reader。
      3. 皆不中 / 未設密鑰 → None(fail-closed)。
    """
    if not token:
        return None
    read_tokens = read_tokens or {}

    if auth_token and _safe_eq(token, auth_token):
        return make_reader_principal("reader:primary")

    # 逐一常數時間比對(避免以 dict 命中時間側洩漏)。
    for t, label in read_tokens.items():
        if _safe_eq(token, t):
            return make_reader_principal(f"reader:{label}")

    return None


def resolve_principal_from_config(token: Optional[str], config=CONFIG) -> Optional[Principal]:
    """以 CONFIG 的 MCP 密鑰解析 token(server_http 用)。HTTP 一切身分皆唯讀。"""
    return resolve_principal(
        token,
        auth_token=config.mcp_auth_token,
        read_tokens=_parse_read_tokens(config.mcp_guest_tokens),
    )


def auth_is_configured(config=CONFIG) -> bool:
    """是否設了任一可用唯讀密鑰;沒有則 server fail-closed(全部 401)。"""
    return bool(config.mcp_auth_token or _parse_read_tokens(config.mcp_guest_tokens))


# ────────────────────────────── 寫入閘(§16.2:唯一寫入門 = 本機 owner) ──────────────────────────────

@dataclass
class WriteDecision:
    """寫入閘判定結果。ok=False 時 record 為 None、error 說明拒收原因。"""
    ok: bool
    record: Optional[Record] = None
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)


def apply_write_trust(record: Record, principal: Principal) -> WriteDecision:
    """寫入前的結構性把關。兩扇門:**唯有本機 owner 能寫**。

    規則:
      1. **唯讀門**:`principal.can_write=False` → 拒收。HTTP 一切 token 皆唯讀,寫入(校準
         回饋)只在本機 stdio owner 通道。**防禦縱深**:即便日後誤把 write 工具掛上 HTTP,
         此處仍擋下(write 工具本就不在 HTTP 註冊,這是第二道)。
      2. `grade=prediction` → 拒收(內部保留級;即便 owner 也不得把引擎自身預測當人類真值
         注入,擋自己失誤;`prediction` 不入真相 / 不進方向投票的既有保證未改)。

    通過者原樣交還;A 級「須附 protocol」由 `engine.log_calibration` 單一把關(不在此複刻)。
    owner 寫 global 或 self 皆放行(本機刻意校正),不再做命名空間重導(HTTP 不寫,moot)。
    """
    # 1) 唯讀門:非 owner 一律不得寫
    if not principal.can_write:
        return WriteDecision(
            ok=False,
            error=("此通道唯讀(HTTP 公開門)。寫入(校準回饋)只在本機 Claude Code stdio "
                   "owner 通道進行,不開放任何網路 token。"),
        )

    # 2) 內部保留級不得當人類真值注入(擋 owner 自己的失誤)
    if record.grade == Grade.PREDICTION:
        return WriteDecision(
            ok=False,
            error="grade=prediction 為內部保留級(引擎自身預測),不得當人類真值注入。",
        )

    return WriteDecision(ok=True, record=record, notes=[])


# ────────────────────────────── 請求範圍 principal(contextvar) ──────────────────────────────

# HTTP 認證中介層於每個請求設定(reader);工具(同一 async task 內聯執行)讀取以套讀範圍。
# stdio 永不設定 → 取預設 LOCAL_PRINCIPAL(owner、完全信任,零回歸)。
_CURRENT: contextvars.ContextVar[Principal] = contextvars.ContextVar(
    "cie_current_principal", default=LOCAL_PRINCIPAL
)


def current_principal() -> Principal:
    """取得當前請求的呼叫者身分(未設定 → 本地 owner 預設)。"""
    return _CURRENT.get()


def set_principal(principal: Principal) -> "contextvars.Token[Principal]":
    """設定當前 principal,回傳可 reset 的 token(中介層 finally 時還原)。"""
    return _CURRENT.set(principal)


def reset_principal(token: "contextvars.Token[Principal]") -> None:
    _CURRENT.reset(token)
