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

N-guest 設定面唯一性守衛(§16.3,啟動 fail-closed):**任兩個 guest token 的 user_id 必須全域
唯一**——兩 guest 對映同一 user_id 就是同一個 self、結構上無從分隔(= 跨 guest 混入)。
`validate_guest_token_config` 於啟動硬檢查(重複 user_id / 撞 primary token / 認領 owner 命名空間
→ 拒啟動),把『member A 讀不到 / 寫不到 / 刪不到 B 的 self』從 2 member 硬化到 N guest。
無共用 fallback:任何無法乾淨解析的 token 一律回 `None`(401),絕不退某個共用預設命名空間。

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

    **寬鬆**(per-request 熱路徑安全,永不 raise):此處不做 user_id 唯一性 / 撞 primary 檢查——
    那是啟動時 `validate_guest_token_config` 的 fail-closed 守衛(§16.3);啟動已驗過,熱路徑見到的
    設定即無破口。注意 user_id 一律取**設定裡的明確值**(`str(v).strip()`),絕不由 token 雜湊截斷 /
    顯示名衍生(可碰撞 → 跨 guest 混入)。
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


# ─────────────── 設定面唯一性守衛(§16.3:N-guest self 互不混入,啟動 fail-closed) ───────────────

class GuestTokenConfigError(RuntimeError):
    """guest token 設定有『會讓多 guest 靜默共用同一 self』的破口 → 拒絕啟動(fail-closed)。

    觸發(任一即拒,§16.3 唯一性守衛):
      1. 兩個 guest token 對映到**同一 user_id**(會擠進同一 self 命名空間 = 跨 guest 混入);
      2. guest token 與 **primary**(`CIE_MCP_AUTH_TOKEN`)token 字串相同(被 primary 規則搶先
         解析 → 該 guest 靜默落 owner 的 self 層,或 reader token 靜默升格為可寫 member);
      3. guest 認領 **primary 的命名空間**(owner 的 self,`auth_user_id`)。

    繼承 `RuntimeError`:沿用既有啟動 fail-closed 慣例(對齊 `server_http` 的 stateless 守衛)。
    保留字 `{global, self}` 的整體 reject 仍由 `_parse_member_tokens` 負責(skip + warn,沿用既有);
    無共用 fallback:任何無法乾淨解析的 token 由 `resolve_principal` 回 `None`(401),絕不退共用預設。
    """


def validate_guest_token_config(
    config=CONFIG,
    *,
    auth_user_id: str = OWNER_SELF_USER_ID,
) -> Dict[str, Optional[str]]:
    """啟動硬檢查(§16.3 設定面唯一性守衛,fail-closed):確認 guest token → self 命名空間
    『全域唯一、不撞 primary token / 命名空間』。有破口即 raise `GuestTokenConfigError` 拒啟動。

    這是把核心隔離(member A 讀不到 / 寫不到 / 刪不到 B 的 self)從『2 member』硬化到『N guest』
    的設定面補強:結構性隔離靠 user_id 區分命名空間,故**兩個 guest 絕不可對映到同一 user_id**
    (否則它們就是同一個 self,結構上無從分隔)。`server_http.build_app` 啟動時呼叫。

    回傳已驗證乾淨的 `{token: user_id|None}`(`None`=reader);reader(無命名空間)**不參與
    user_id 唯一性**(本就無 self 層),但仍受 primary-token 撞檢。寬鬆 `_parse_member_tokens`
    (per-request 熱路徑)不變——啟動時已驗過,熱路徑不再 raise。

    守衛:
      1. **user_id 全域唯一**:任兩個 guest token 對映同一 user_id → 拒(『靜默混入』主破口)。
      2. **不撞 primary token**:guest token == `CIE_MCP_AUTH_TOKEN` → 拒(否則 `resolve_principal`
         以 primary 規則搶先命中、該 guest 靜默落 owner 的 self;含 reader token 撞 → 靜默變可寫 member)。
      3. **不認領 primary 命名空間**:guest user_id == owner 的 self(`auth_user_id`)→ 拒
         (`{global, self}` 已由 `_parse_member_tokens` skip;此為 `auth_user_id` 可調時的補強)。
    """
    auth_token = config.mcp_auth_token
    tokens = _parse_member_tokens(config.mcp_guest_tokens)

    seen_user_ids: Dict[str, None] = {}
    for tok, ns in tokens.items():
        # (2) guest token 不得與 primary token 相同(常數時間比對;含 reader token)。
        if auth_token and _safe_eq(tok, auth_token):
            raise GuestTokenConfigError(
                "某 guest token 與 CIE_MCP_AUTH_TOKEN(primary)相同:該 token 會被 primary 規則"
                "搶先解析、靜默落 owner 的 self 層(reader token 撞則靜默升格為可寫 member)。"
                "請為每個 guest 用獨立 token。")
        if ns is None:
            continue  # reader:無 self 命名空間,不參與 user_id 唯一性
        # (3) guest 不得認領 owner 的個人命名空間。
        if ns == auth_user_id:
            raise GuestTokenConfigError(
                f"guest 不得認領 owner 的個人命名空間 {auth_user_id!r}(會與 owner 的 self 混入)。")
        # (1) user_id 全域唯一:重複即拒(否則多 guest 共用同一 self = 跨 guest 混入)。
        if ns in seen_user_ids:
            raise GuestTokenConfigError(
                f"兩個 guest token 對映到同一 user_id {ns!r}:多 guest 會靜默共用同一 self 命名空間"
                f"(跨 guest 混入)。每個 guest 的 user_id 必須全域唯一。")
        seen_user_ids[ns] = None
    return tokens


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


# ────────────────────────────── 刪除範圍(§16.2:刪除隔離與寫入隔離同源) ──────────────────────────────

@dataclass
class DeleteDecision:
    """刪除範圍判定。ok=False(reader)時 error 說明;ok=True 時 allowed_user_id=None 表示
    不限命名空間(owner 可刪任一),否則只能刪該命名空間自有(member confinement)。"""
    ok: bool
    allowed_user_id: Optional[str] = None
    error: Optional[str] = None


def resolve_delete_scope(principal: Principal) -> DeleteDecision:
    """決定一個 principal 可刪除的命名空間範圍(對稱 `apply_write_trust` 的寫入 confinement)。

      - reader(`can_write=False`)→ 拒絕。
      - member → 只能刪自有命名空間(`allowed_user_id = write_user_id`)。
      - owner → 不限(`allowed_user_id=None`,可刪任一;清理語料用,僅 stdio)。

    刪除隔離與寫入隔離同源:member 永遠刪不到 global / 他人 self——底層儲存層(D1 SQL 加
    `AND user_id=自有`;記憶體先驗命名空間)強制,即便 id 猜中也刪不掉。
    """
    if not principal.can_write:
        return DeleteDecision(
            ok=False,
            error=("此通道唯讀(reader),不可刪除。刪除需具命名空間的 member token"
                   "(只能刪自己的 self 層)或本機 owner(stdio)。"))
    if principal.role == "member":
        ns = principal.write_user_id
        if not ns:  # 防禦:member 必有命名空間(make_member_principal 恆設定)
            return DeleteDecision(ok=False, error="member 缺命名空間,拒收(內部錯誤)。")
        return DeleteDecision(ok=True, allowed_user_id=ns)
    # owner:可刪任一(僅本機 stdio;HTTP 永不解析為 owner)
    return DeleteDecision(ok=True, allowed_user_id=None)


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
