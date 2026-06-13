"""呼叫者身分 + 寫入閘 + 讀範圍 —「三層 + 人工晉升」模型(設計 §13 / §16)。

三層 principal:
  - **owner(本機 stdio,`mcp_server.py`,`LOCAL_PRINCIPAL`)。** 靠「跑在你的機器上」授權,
    不經網路。可寫 `global`(客觀因果層)或任一 `self` 命名空間;讀**不過濾**(global +
    各 self,供晉升審查)。是唯一能寫 global、唯一能晉升的身分。
  - **member(HTTP token → 對映一個 `user_id` 命名空間)。** 公開可寫端點,但寫入**強制落自有
    命名空間**(self 客製層),`grade` 上限 `B`(永不 auto-A);讀 `global + 自己的 self`,
    **讀不到他人 self、寫不到 global**。命門:`global` / 他人 ns **絕不可從網路寫**。
  - **reader(HTTP token 無命名空間,可選)。** 純分享讀:`can_write=False`,只讀 `global`。

token 解析:`CIE_MCP_AUTH_TOKEN` = 你個人 member token(日常 claude.ai 用,寫自己的 `self`);
`CIE_MCP_GUEST_TOKENS` = `{token: user_id}` member 對映(值為 `user_id` 命名空間),或 `[token]`
陣列(無命名空間 → reader)。值為空 / null → reader;值為保留字(`global` / `self`)→ 拒收
(fail-closed)。身分解析以**常數時間**比對 token(`hmac.compare_digest`),未設密鑰一律 fail-closed。

寫入隔離是這條公開可寫端點的命門,靠三道**結構性**保證(非靠客戶端自律):
  1. **命名空間 confinement**:member 寫入一律 `model_copy` 改寫 `user_id = principal.write_user_id`,
     呼叫端指定什麼都蓋掉 → 寫不到 global / 他人 ns(`apply_write_trust`)。
  2. **grade 上限 B**:member 寫入 `grade>B` 一律降為 B;`A` 只能經 owner 晉升產生(同上)。
  3. **讀範圍加性過濾**:member 讀 `[global, own]`,reader 讀 `[global]`(`store.search(user_ids=)`)。
晉升(self→global)只在 owner 的 stdio 門,工具不在 HTTP 註冊(`include_promotion`,見 mcp_tools)。
"""
from __future__ import annotations

import contextvars
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import CONFIG
from .schema import Grade, Record

logger = logging.getLogger("cie.mcp_principal")

# global 客觀因果層的保留 user_id(策展語料 corpus/global.jsonl 即用此)。
# 只有本機 owner(stdio)能寫 / 晉升;member + reader 共享讀之。
GLOBAL_USER_ID = "global"
# 你個人 member token(CIE_MCP_AUTH_TOKEN,日常 claude.ai)寫入的命名空間 = owner 的 self 層。
OWNER_SELF_USER_ID = "self"
# 保留命名空間:訪客 member 不得認領(否則可寫 global 或 owner 的 self)。
RESERVED_NAMESPACES = frozenset({GLOBAL_USER_ID, OWNER_SELF_USER_ID})


@dataclass(frozen=True)
class Principal:
    """一個已認證呼叫者的權限快照。

    name           人類可讀標籤(log / 稽核 / 撤銷用)
    role           "owner"(本機 stdio)| "member"(HTTP 具命名空間)| "reader"(HTTP 純讀)
    can_write      能否寫入校準。owner / member=True;reader=False。
    write_user_id  member 寫入**強制落入**的命名空間(self 客製層);owner=None(可寫 global/任一 self);
                   reader=None(不可寫)。member confinement 的鎖。
    read_user_ids  讀取納入的 user_id 白名單(加性過濾);None = 不過濾(owner 全可見)。
                   member=[global, 自己];reader=[global]。
    max_grade      寫入分級上限;member=B(永不 auto-A,A 須 owner 晉升);owner=None(無額外上限)。
    """
    name: str
    role: str
    can_write: bool
    write_user_id: Optional[str] = None
    read_user_ids: Optional[List[str]] = None
    max_grade: Optional[Grade] = None


# 本地 / stdio 預設身分:owner,唯一能寫 global / 晉升,讀不過濾(= 既有行為,零回歸)。
LOCAL_PRINCIPAL = Principal(
    name="local",
    role="owner",
    can_write=True,
    write_user_id=None,     # owner 可寫 global 或任一 self(刻意校正),不受 confinement
    read_user_ids=None,     # 不過濾:見 global + 各 self(供晉升審查)
    max_grade=None,         # 無 grade 上限(A 須 protocol 由 engine 把關)
)


def make_member_principal(name: str, write_user_id: str) -> Principal:
    """HTTP member 身分:寫入**只落自有命名空間** `write_user_id`、grade 上限 B、讀 [global, 自己]。

    寫入隔離靠 `apply_write_trust` 的命名空間 confinement(改寫 user_id)+ grade clamp;
    讀隔離靠 `read_user_ids=[global, write_user_id]` 的加性過濾。member **寫不到 global、
    讀不到他人 self**。
    """
    return Principal(
        name=name, role="member", can_write=True,
        write_user_id=write_user_id,
        read_user_ids=[GLOBAL_USER_ID, write_user_id],
        max_grade=Grade.B,
    )


def make_reader_principal(name: str = "reader") -> Principal:
    """HTTP reader 身分(可選,純分享讀):`can_write=False`、只讀 `global` 共享真相。

    讀範圍 `[global]`——刻意**不含**任何 self,純讀 token 外洩也讀不到個人客製層。
    """
    return Principal(
        name=name, role="reader", can_write=False,
        write_user_id=None,
        read_user_ids=[GLOBAL_USER_ID],
        max_grade=None,
    )


# ────────────────────────────── token → principal 解析 ──────────────────────────────

def _safe_eq(a: str, b: str) -> bool:
    """常數時間字串比較(長度可洩漏、內容不可)。空字串一律不相等(fail-closed)。"""
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)


def _parse_member_tokens(raw: str) -> Dict[str, Optional[str]]:
    """解析 CIE_MCP_GUEST_TOKENS → {token: user_id|None}。None = reader(無命名空間)。

    接受兩種格式(格式錯誤 → 空,fail-closed):
      - 物件 `{"tok_a":"alice","tok_b":"bob"}`:值 = member 寫入命名空間(`user_id`)。
        值為空 / null → reader;值為保留字(global / self)→ **拒收該筆**(防訪客認領 owner 層)。
      - 陣列 `["tok_a","tok_b"]`:無命名空間 → 一律 reader。
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("CIE_MCP_GUEST_TOKENS 非合法 JSON,忽略(視為無額外 token)。")
        return {}

    out: Dict[str, Optional[str]] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if not k:
                continue
            ns = (str(v).strip() if v is not None else "")
            if not ns:                          # 空 / null → reader
                out[str(k)] = None
            elif ns in RESERVED_NAMESPACES:     # 保留字不得被訪客認領
                logger.warning(
                    "CIE_MCP_GUEST_TOKENS:訪客命名空間不得為保留字 %r,忽略該筆(fail-closed)。", ns)
            else:
                out[str(k)] = ns                # member 命名空間
        return out
    if isinstance(data, list):
        return {str(t): None for t in data if t}    # 陣列 → 一律 reader
    logger.warning("CIE_MCP_GUEST_TOKENS 應為 {token:user_id} 物件或 [token] 陣列,忽略。")
    return {}


def resolve_principal(
    token: Optional[str],
    *,
    auth_token: str = "",
    auth_user_id: str = OWNER_SELF_USER_ID,
    member_tokens: Optional[Dict[str, Optional[str]]] = None,
) -> Optional[Principal]:
    """把 token 對映成 Principal;無效 / 無密鑰 → None(401)。

    三層:
      1. auth_token(你個人 member token)命中 → member,寫入命名空間 = auth_user_id(預設 self)。
      2. member_tokens 任一命中 → 值為命名空間則 member;值為 None 則 reader。
      3. 皆不中 / 未設密鑰 → None(fail-closed)。
    `global` 永遠沒有對應 token → **無法從網路寫**(命門)。
    """
    if not token:
        return None
    member_tokens = member_tokens or {}

    if auth_token and _safe_eq(token, auth_token):
        return make_member_principal("member:primary", auth_user_id)

    # 逐一常數時間比對(避免以 dict 命中時間側洩漏)。
    for t, ns in member_tokens.items():
        if _safe_eq(token, t):
            if ns is None:
                return make_reader_principal("reader")
            return make_member_principal(f"member:{ns}", ns)

    return None


def resolve_principal_from_config(token: Optional[str], config=CONFIG) -> Optional[Principal]:
    """以 CONFIG 的 MCP 密鑰解析 token(server_http 用)。"""
    return resolve_principal(
        token,
        auth_token=config.mcp_auth_token,
        auth_user_id=OWNER_SELF_USER_ID,
        member_tokens=_parse_member_tokens(config.mcp_guest_tokens),
    )


def auth_is_configured(config=CONFIG) -> bool:
    """是否設了任一可用密鑰;沒有則 server fail-closed(全部 401)。"""
    return bool(config.mcp_auth_token or _parse_member_tokens(config.mcp_guest_tokens))


# ────────────────────────────── 寫入閘(§16.2:寫入分層,global 不可從網路寫) ──────────────────────────────

# grade 排序(供 member 上限 clamp);prediction 另行擋下,不入此序。
_GRADE_ORDER: Dict[Grade, int] = {Grade.C: 0, Grade.B: 1, Grade.A: 2}


def _grade_rank(g: Grade) -> int:
    return _GRADE_ORDER.get(g, -1)


@dataclass
class WriteDecision:
    """寫入閘判定結果。ok=False 時 record 為 None、error 說明拒收原因。
    ok=True 時 record 為(可能經 confinement / clamp 改寫的)放行記錄,notes 說明改寫了什麼。"""
    ok: bool
    record: Optional[Record] = None
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)


def apply_write_trust(record: Record, principal: Principal) -> WriteDecision:
    """寫入前的結構性把關(三層)。global 永遠寫不到網路面;member 寫入受限。

    規則:
      1. **不可寫**:`principal.can_write=False`(reader)→ 拒收。
      2. `grade=prediction` → 拒收(內部保留級;即便 owner 也不得把引擎自身預測當人類真值
         注入,擋失誤;`prediction` 不入真相 / 不進方向投票的既有保證未改)。
      3. **member confinement(命門)**:
         a. 寫入命名空間**強制** = `principal.write_user_id`(呼叫端指定 global / 他人 ns →
            一律改寫回自有 + note)→ member 寫不到 global / 他人 self。
         b. `grade` 上限 = `principal.max_grade`(B);超過一律降級 + note。`A` 只能經 owner 晉升。
      4. **owner**:不做命名空間強制、無 grade 上限(可寫 global / 任一 self);A 須 protocol
         由 `engine.log_calibration` 單一把關(不在此複刻)。

    通過者(可能經改寫)交還 `decision.record`;呼叫端須用它(而非原 record)寫入。
    """
    # 1) reader 一律不得寫
    if not principal.can_write:
        return WriteDecision(
            ok=False,
            error=("此通道唯讀(reader)。寫入需具命名空間的 member token(寫自己的 self 層)"
                   "或本機 owner(stdio)。"),
        )

    # 2) 內部保留級不得當人類真值注入(任何角色,含 owner)
    if record.grade == Grade.PREDICTION:
        return WriteDecision(
            ok=False,
            error="grade=prediction 為內部保留級(引擎自身預測),不得當人類真值注入。",
        )

    notes: List[str] = []

    # 3) member:命名空間 confinement + grade 上限(寫入隔離命門)
    if principal.role == "member":
        ns = principal.write_user_id
        if not ns:  # 防禦:member 必有命名空間(make_member_principal 恆設定)
            return WriteDecision(ok=False, error="member 缺命名空間,拒收(內部錯誤)。")
        if record.user_id != ns:
            notes.append(
                f"member 寫入強制落自有命名空間 '{ns}'(原指定 '{record.user_id}' 已忽略);"
                f"member 不可寫 global / 他人 self。")
            record = record.model_copy(update={"user_id": ns})
        ceiling = principal.max_grade
        if ceiling is not None and _grade_rank(record.grade) > _grade_rank(ceiling):
            notes.append(
                f"member 寫入分級上限 {ceiling.value};原 '{record.grade.value}' 降為 "
                f"'{ceiling.value}'。A 級(客觀真值)須經 owner 在本機晉升。")
            record = record.model_copy(update={"grade": ceiling})

    return WriteDecision(ok=True, record=record, notes=notes)


# ────────────────────────────── 寫入流量上限(防公開端被灌爆) ──────────────────────────────

# 每命名空間每行程的寫入次數上限(簡單計數;owner=本機,豁免)。read 路徑不受限。
# 這是防灌爆的粗閘,非精準配額;真正的隔離靠 apply_write_trust 的 confinement。
MEMBER_WRITE_LIMIT = 500
_write_counts: Dict[str, int] = {}


def register_write(principal: Principal) -> bool:
    """登記一次 member 寫入,回傳是否仍在上限內(超過 → False,呼叫端拒收)。

    owner(本機 stdio)豁免——本機刻意校正不該被流量閘擋。讀 `MEMBER_WRITE_LIMIT`
    模組全域(便於測試 monkeypatch)。
    """
    if principal.role == "owner":
        return True
    key = principal.write_user_id or principal.name
    n = _write_counts.get(key, 0)
    if n >= MEMBER_WRITE_LIMIT:
        return False
    _write_counts[key] = n + 1
    return True


def reset_write_counters() -> None:
    """清空寫入計數(測試 / 重啟用)。"""
    _write_counts.clear()


# ────────────────────────────── 請求範圍 principal(contextvar) ──────────────────────────────

# HTTP 認證中介層於每個請求設定(member / reader);工具(同一 async task 內聯執行)讀取以套
# 讀範圍 / 寫入閘。stdio 永不設定 → 取預設 LOCAL_PRINCIPAL(owner、完全信任,零回歸)。
# **安全要點**:per-request principal 必須抵達工具;否則退回 owner 預設 → member 取得 owner
# 權限(可寫 global)。故 server_http 對 stateless=False fail-closed(見 server_http、§16.3)。
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
