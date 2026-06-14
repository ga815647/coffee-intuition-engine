"""Remote MCP「三層 + 人工晉升」治理:owner / member / reader 的寫入隔離與讀隔離(設計 §16)。

不變量(命門):member 寫**只落自有命名空間**、**寫不到 global**、**讀不到他人 self**、
grade≤B、拒 prediction、寫入流量上限生效;owner 晉升套 A-protocol、預設留個人;reader 不可寫;
機制硬分區;token 解析 fail-closed。全離線、確定性:直接建記憶體 engine + 顯式 principal,
測 do_* 邏輯與寫入閘。不碰 HTTP 傳輸(那在 test_mcp_http.py)。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from cie.config import Config
from cie.mcp_principal import (
    GLOBAL_USER_ID, LOCAL_PRINCIPAL, OWNER_SELF_USER_ID, RESERVED_NAMESPACES,
    GuestTokenConfigError, apply_write_trust, current_principal, make_member_principal,
    make_reader_principal, register_write, reset_principal, reset_write_counters,
    resolve_delete_scope, resolve_principal, resolve_principal_from_config, set_principal,
    validate_guest_token_config,
)
from cie.engine import Engine
from cie.mcp_tools import (
    do_delete_calibration, do_list_customizations, do_log_calibration,
    do_promote_customization, do_query,
)
from cie.schema import (
    BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process, Record,
)
from cie.store import VectorStore


def _bp(mech=BrewMechanism.PERCOLATION, **kw):
    return BrewParams(brew_mechanism=mech, **kw)


@pytest.fixture(autouse=True)
def _reset_counters():
    # 寫入計數為模組全域;每測前後清空,避免跨測污染。
    reset_write_counters()
    yield
    reset_write_counters()


@pytest.fixture()
def engine():
    store = VectorStore()  # 記憶體;canonical sink = None(自存 _canonical)
    eng = Engine(store)
    # 灌幾筆 global 真值(模擬策展語料 corpus/global.jsonl,user_id=global)。
    for i in range(4):
        eng.store.upsert(Record(
            bean=BeanRoast(origin="Ethiopia", process=Process.WASHED, roast_agtron=74),
            params=_bp(method="V60", grind_um=650 + i, water_temp_c=92, tds_pct=1.38, ey_pct=20.4),
            flavor=FlavorProfile(acidity=7.5, sweetness=7.0, body=5.0),
            grade=Grade.B, user_id=GLOBAL_USER_ID,
        ))
    return eng


def _uid_of(engine, rid):
    for r in engine.store.iter_records():
        if r.id == rid:
            return r.user_id
    return None


def _rec_of(engine, rid):
    for r in engine.store.iter_records():
        if r.id == rid:
            return r
    return None


# ────────────────────────────── token → principal 解析(三層) ──────────────────────────────

def test_resolve_primary_token_is_member_to_self():
    """CIE_MCP_AUTH_TOKEN = 你個人 member token → 寫自己的 self 層、grade 上限 B。"""
    p = resolve_principal("PRIMARY", auth_token="PRIMARY")
    assert p is not None and p.role == "member"
    assert p.can_write is True
    assert p.write_user_id == OWNER_SELF_USER_ID
    assert p.read_user_ids == [GLOBAL_USER_ID, OWNER_SELF_USER_ID]
    assert p.max_grade == Grade.B


def test_resolve_guest_object_is_member_to_its_namespace():
    p = resolve_principal("tok_a", member_tokens={"tok_a": "alice", "tok_b": "bob"})
    assert p is not None and p.role == "member"
    assert p.write_user_id == "alice"
    assert p.read_user_ids == [GLOBAL_USER_ID, "alice"]


def test_resolve_namespaceless_token_is_reader():
    # 值為 None(陣列形式或空值)→ reader:只讀 global、不可寫。
    p = resolve_principal("tok_r", member_tokens={"tok_r": None})
    assert p is not None and p.role == "reader"
    assert p.can_write is False
    assert p.read_user_ids == [GLOBAL_USER_ID]


def test_resolve_bad_token_returns_none():
    assert resolve_principal("nope", auth_token="PRIMARY") is None


def test_resolve_none_token_returns_none():
    assert resolve_principal(None, auth_token="PRIMARY") is None


def test_resolve_fail_closed_when_no_secret():
    # 未設任何密鑰 → 任何 token 都不通(含空字串比對)。
    assert resolve_principal("anything", auth_token="") is None
    assert resolve_principal("", auth_token="") is None


def test_global_has_no_token_path():
    """命門:global 永遠沒有對應 token。primary 與訪客皆 member(寫自有 self),
    保留命名空間 {global, self} 不得被訪客認領。"""
    assert GLOBAL_USER_ID in RESERVED_NAMESPACES and OWNER_SELF_USER_ID in RESERVED_NAMESPACES
    # 訪客嘗試認領保留字 → 該筆被拒(fail-closed),token 無效。
    assert resolve_principal("g", member_tokens=_parse_guest("{\"g\":\"global\"}")) is None
    assert resolve_principal("s", member_tokens=_parse_guest("{\"s\":\"self\"}")) is None


def _parse_guest(raw):
    from cie.mcp_principal import _parse_member_tokens
    return _parse_member_tokens(raw)


def test_guest_reserved_namespace_rejected():
    parsed = _parse_guest('{"ok":"alice","bad1":"global","bad2":"self"}')
    assert parsed == {"ok": "alice"}    # 保留字兩筆都被踢掉


def test_local_principal_is_owner_sole_global_writer():
    assert LOCAL_PRINCIPAL.role == "owner"
    assert LOCAL_PRINCIPAL.can_write is True
    assert LOCAL_PRINCIPAL.write_user_id is None     # 不受 confinement(可寫 global/任一 self)
    assert LOCAL_PRINCIPAL.read_user_ids is None     # 讀不過濾(供晉升審查)
    assert LOCAL_PRINCIPAL.max_grade is None


# ────────────────────────────── 寫入閘:member confinement(命門) ──────────────────────────────

def test_gate_reader_cannot_write():
    rec = Record(params=_bp(), grade=Grade.C)
    d = apply_write_trust(rec, make_reader_principal())
    assert d.ok is False and "唯讀" in d.error


def test_gate_member_write_to_global_is_confined_to_own_ns():
    """命門:member 指定 user_id=global → 強制改寫回自有命名空間,絕不寫 global。"""
    rec = Record(params=_bp(), grade=Grade.C, user_id=GLOBAL_USER_ID)
    d = apply_write_trust(rec, make_member_principal("member:alice", "alice"))
    assert d.ok is True
    assert d.record.user_id == "alice"               # 被 confine 回自有
    assert d.record.user_id != GLOBAL_USER_ID
    assert any("global" in n or "自有" in n for n in d.notes)


def test_gate_member_write_to_other_self_is_confined():
    """member 指定他人 ns(bob)→ 一樣強制回自有(alice)。寫不到他人 self。"""
    rec = Record(params=_bp(), grade=Grade.C, user_id="bob")
    d = apply_write_trust(rec, make_member_principal("member:alice", "alice"))
    assert d.ok is True and d.record.user_id == "alice"


def test_gate_member_grade_capped_at_B():
    """member 寫 A → 降為 B(永不 auto-A;A 只能經 owner 晉升)。"""
    rec = Record(params=_bp(), grade=Grade.A, protocol="SCA_cupping", user_id="alice")
    d = apply_write_trust(rec, make_member_principal("member:alice", "alice"))
    assert d.ok is True and d.record.grade == Grade.B
    assert any("上限" in n or "晉升" in n for n in d.notes)


def test_gate_member_grade_b_and_c_unchanged():
    for g in (Grade.B, Grade.C):
        rec = Record(params=_bp(), grade=g, user_id="alice")
        d = apply_write_trust(rec, make_member_principal("member:alice", "alice"))
        assert d.ok is True and d.record.grade == g


def test_gate_rejects_prediction_injection_for_any_role():
    for p in (LOCAL_PRINCIPAL, make_member_principal("member:alice", "alice")):
        rec = Record(params=_bp(), grade=Grade.PREDICTION)
        d = apply_write_trust(rec, p)
        assert d.ok is False and "prediction" in d.error


def test_gate_owner_write_global_and_self_unconfined():
    for uid in (GLOBAL_USER_ID, "self", "anyone"):
        rec = Record(params=_bp(), grade=Grade.B, user_id=uid)
        d = apply_write_trust(rec, LOCAL_PRINCIPAL)
        assert d.ok is True and d.record.user_id == uid   # owner 不被 confine
        assert d.notes == []


# ────────────────────────────── 寫入流量上限(防灌爆) ──────────────────────────────

def test_member_write_rate_limit(monkeypatch, engine):
    monkeypatch.setattr("cie.mcp_principal.MEMBER_WRITE_LIMIT", 2)
    reset_write_counters()
    m = make_member_principal("member:flood", "flood")
    oks = [do_log_calibration(engine, m, brew_mechanism="percolation", origin="Kenya",
                              roast_agtron=70)["ok"] for _ in range(3)]
    assert oks == [True, True, False]                # 第 3 筆超過上限被擋
    last = do_log_calibration(engine, m, brew_mechanism="percolation", origin="Kenya",
                              roast_agtron=70)
    assert last["ok"] is False and last["gate"] == "rate_limit"


def test_owner_exempt_from_rate_limit(monkeypatch, engine):
    monkeypatch.setattr("cie.mcp_principal.MEMBER_WRITE_LIMIT", 1)
    reset_write_counters()
    for _ in range(3):  # owner(本機)豁免:都成功
        out = do_log_calibration(engine, LOCAL_PRINCIPAL, brew_mechanism="percolation",
                                 origin="Kenya", roast_agtron=70, user_id="global")
        assert out["ok"] is True


# ────────────────────────────── do_log_calibration 端到端(閘 + engine) ──────────────────────────────

def test_log_member_write_lands_in_own_self_layer(engine):
    out = do_log_calibration(engine, make_member_principal("member:alice", "alice"),
                             brew_mechanism="percolation", grade="C",
                             origin="Kenya", roast_agtron=70, user_id="self")
    assert out["ok"] is True
    assert _uid_of(engine, out["id"]) == "alice"     # 落自有 ns(指定 self 也被 confine 回 alice)


def test_log_member_write_global_does_not_pollute_global(engine):
    """端到端命門:member 想寫 global → 實際落自有 ns;global 未被污染。"""
    before_global = sum(1 for r in engine.store.iter_records() if r.user_id == GLOBAL_USER_ID)
    out = do_log_calibration(engine, make_member_principal("member:alice", "alice"),
                             brew_mechanism="percolation", grade="A", protocol="SCA_cupping",
                             origin="Kenya", roast_agtron=70, user_id="global")
    assert out["ok"] is True
    assert _uid_of(engine, out["id"]) == "alice"
    assert _rec_of(engine, out["id"]).grade == Grade.B         # A 也被降為 B
    after_global = sum(1 for r in engine.store.iter_records() if r.user_id == GLOBAL_USER_ID)
    assert after_global == before_global                       # global 筆數不變
    assert "trust_notes" in out


def test_log_reader_blocked_no_write(engine):
    before = engine.store.count()
    out = do_log_calibration(engine, make_reader_principal(),
                             brew_mechanism="percolation", grade="C", origin="Kenya")
    assert out["ok"] is False and out.get("gate") == "write_trust"
    assert engine.store.count() == before


def test_log_owner_a_without_protocol_rejected_by_engine(engine):
    out = do_log_calibration(engine, LOCAL_PRINCIPAL,
                             brew_mechanism="percolation", grade="A", protocol="")
    assert out["ok"] is False and "protocol" in out["error"]


def test_log_owner_write_global_ok(engine):
    out = do_log_calibration(engine, LOCAL_PRINCIPAL,
                             brew_mechanism="percolation", grade="B", user_id="global",
                             origin="Colombia", roast_agtron=68)
    assert out["ok"] is True and _uid_of(engine, out["id"]) == "global"


# ────────────────────────────── 讀隔離:member 讀不到他人 self(隱私命門) ──────────────────────────────

def test_member_cannot_read_other_members_self(engine):
    """隱私命門:member A 查詢讀不到 member B 的 self 校準。"""
    # member B(bob)寫一筆 self 校準(同一查詢豆況,故若洩漏必出現在 A 的證據)。
    bob = make_member_principal("member:bob", "bob")
    out = do_log_calibration(engine, bob, brew_mechanism="percolation", grade="C",
                             origin="Ethiopia", process="washed", roast_agtron=74,
                             method="V60", grind_um=651, acidity=7.4)
    bob_id = out["id"]
    assert _uid_of(engine, bob_id) == "bob"

    def evidence_ids(principal):
        res = do_query(engine, principal, brew_mechanism="percolation", mode="recommend",
                       origin="Ethiopia", process="washed", roast_agtron=74)
        return {e["id"] for e in res.get("evidence", [])}

    alice = make_member_principal("member:alice", "alice")
    assert bob_id not in evidence_ids(alice)         # A 讀不到 B 的 self
    assert bob_id in evidence_ids(bob)               # B 自己讀得到
    assert bob_id in evidence_ids(LOCAL_PRINCIPAL)   # owner 讀得到(供晉升審查)


def test_reader_reads_only_global(engine):
    """reader 讀範圍 = [global];讀得到共享 global,讀不到任何 self。"""
    out = do_log_calibration(engine, make_member_principal("member:alice", "alice"),
                             brew_mechanism="percolation", grade="C",
                             origin="Ethiopia", process="washed", roast_agtron=74,
                             method="V60", grind_um=652)
    alice_self_id = out["id"]
    res = do_query(engine, make_reader_principal(), brew_mechanism="percolation",
                   mode="recommend", origin="Ethiopia", process="washed", roast_agtron=74)
    ev = {e["id"] for e in res.get("evidence", [])}
    assert len(ev) > 0                               # 讀得到共享 global
    assert alice_self_id not in ev                   # 讀不到 alice 的 self


def test_read_mechanism_hard_partition(engine):
    """機制三軌硬隔離(鐵則 1):同情境查 immersion 不得混入 percolation 證據。"""
    out = do_log_calibration(engine, LOCAL_PRINCIPAL,
                             brew_mechanism="immersion", grade="B", user_id="global",
                             origin="Brazil", process="natural", roast_agtron=58,
                             method="FrenchPress", grind_um=900, water_temp_c=92)
    imm_id = out["id"]

    def evidence_ids(mech):
        res = do_query(engine, make_reader_principal(), brew_mechanism=mech,
                       mode="recommend", origin="Brazil", process="natural", roast_agtron=58)
        return {e["id"] for e in res.get("evidence", [])}

    imm = evidence_ids("immersion")
    perc = evidence_ids("percolation")
    assert imm_id in imm
    assert imm_id not in perc
    assert imm.isdisjoint(perc)


# ────────────────────────────── 晉升(owner / stdio 限定) ──────────────────────────────

def test_owner_promotes_self_record_to_global(engine):
    """晉升:owner 把 member/self 記錄就地升格 global(同 id 覆寫,非重複)。"""
    src = do_log_calibration(engine, make_member_principal("member:alice", "alice"),
                             brew_mechanism="immersion", grade="C",
                             origin="Brazil", roast_agtron=58, acidity=6.0, user_id="self")
    sid = src["id"]
    assert _uid_of(engine, sid) == "alice"

    listed = do_list_customizations(engine, LOCAL_PRINCIPAL)
    assert any(c["id"] == sid for c in listed["customizations"])   # 出現在待審清單

    out = do_promote_customization(engine, LOCAL_PRINCIPAL, record_id=sid,
                                   grade="A", protocol="SCA_cupping")
    assert out["ok"] is True and out["promoted_id"] == sid
    assert _uid_of(engine, sid) == GLOBAL_USER_ID                  # 就地升格為 global
    assert _rec_of(engine, sid).grade == Grade.A


def test_promote_A_requires_protocol(engine):
    src = do_log_calibration(engine, LOCAL_PRINCIPAL, brew_mechanism="immersion", grade="C",
                             origin="Peru", roast_agtron=60, user_id="self")
    out = do_promote_customization(engine, LOCAL_PRINCIPAL, record_id=src["id"],
                                   grade="A", protocol="")
    assert out["ok"] is False and "protocol" in out["error"]


def test_promote_rejects_non_ab_grade(engine):
    src = do_log_calibration(engine, LOCAL_PRINCIPAL, brew_mechanism="immersion", grade="C",
                             origin="Peru", roast_agtron=60, user_id="self")
    out = do_promote_customization(engine, LOCAL_PRINCIPAL, record_id=src["id"], grade="C")
    assert out["ok"] is False and "A 或 B" in out["error"]


def test_member_cannot_list_or_promote(engine):
    """晉升工具雖只在 stdio 註冊,do_* 仍 owner-only(防禦縱深)。"""
    m = make_member_principal("member:alice", "alice")
    assert do_list_customizations(engine, m)["ok"] is False
    out = do_promote_customization(engine, m, record_id="whatever", grade="B")
    assert out["ok"] is False and out["gate"] == "promote"


def test_list_customizations_excludes_global_and_prediction(engine):
    """待審清單只列 self 客製層;global 與 prediction 不入。"""
    do_log_calibration(engine, make_member_principal("member:alice", "alice"),
                       brew_mechanism="percolation", grade="C", origin="Kenya",
                       roast_agtron=70, user_id="self")
    listed = do_list_customizations(engine, LOCAL_PRINCIPAL)
    assert listed["count"] >= 1
    assert all(c["user_id"] != GLOBAL_USER_ID for c in listed["customizations"])


# ────────────────────────────── 加性讀過濾機制(store 層單測) ──────────────────────────────

def test_store_user_ids_read_filter_enforces_isolation(engine):
    """`store.search(user_ids=...)` 加性過濾是 member/reader 讀隔離的底層機制。"""
    out = do_log_calibration(engine, make_member_principal("member:alice", "alice"),
                             brew_mechanism="percolation", grade="C",
                             origin="Ethiopia", process="washed", roast_agtron=74,
                             method="V60", grind_um=652, user_id="self")
    alice_id = out["id"]
    q = "Ethiopia washed light percolation"

    unfiltered = {h["id"] for h in engine.store.search(q, BrewMechanism.PERCOLATION, top_k=20)}
    global_only = {h["id"] for h in engine.store.search(
        q, BrewMechanism.PERCOLATION, top_k=20, user_ids=[GLOBAL_USER_ID])}

    assert alice_id in unfiltered                    # 不過濾 → 看得到 alice
    assert alice_id not in global_only               # 限定 global → 過濾掉 alice
    assert all(h["payload"]["user_id"] == GLOBAL_USER_ID for h in engine.store.search(
        q, BrewMechanism.PERCOLATION, top_k=20, user_ids=[GLOBAL_USER_ID]))


# ────────────────────────────── 刪除範圍閘:resolve_delete_scope(對稱寫入) ──────────────────────────────

def test_delete_scope_reader_denied():
    d = resolve_delete_scope(make_reader_principal())
    assert d.ok is False and "唯讀" in d.error


def test_delete_scope_member_confined_to_own_ns():
    d = resolve_delete_scope(make_member_principal("member:alice", "alice"))
    assert d.ok is True and d.allowed_user_id == "alice"


def test_delete_scope_owner_unconfined():
    d = resolve_delete_scope(LOCAL_PRINCIPAL)
    assert d.ok is True and d.allowed_user_id is None   # owner 可刪任一


# ────────────────────────────── 刪除端到端:member 只能刪自有 self(命門) ──────────────────────────────

def test_delete_member_deletes_own_self_record(engine):
    """member 刪自有 self → 成功;該筆從記憶體索引消失。"""
    alice = make_member_principal("member:alice", "alice")
    out = do_log_calibration(engine, alice, brew_mechanism="percolation", grade="C",
                             origin="Ethiopia", process="washed", roast_agtron=74,
                             method="V60", grind_um=652, user_id="self")
    rid = out["id"]
    assert _uid_of(engine, rid) == "alice"

    res = do_delete_calibration(engine, alice, record_id=rid)
    assert res["ok"] is True and res["deleted_memory"] == 1
    assert _rec_of(engine, rid) is None              # 已從索引刪除


def test_delete_member_cannot_delete_global(engine):
    """命門:member 刪 global id → 命名空間不符,刪不到(ok=False),global 仍在。"""
    # 取 fixture 既有的一筆 global id。
    gid = next(r.id for r in engine.store.iter_records() if r.user_id == GLOBAL_USER_ID)
    alice = make_member_principal("member:alice", "alice")
    res = do_delete_calibration(engine, alice, record_id=gid)
    assert res["ok"] is False and res["deleted_memory"] == 0
    assert _rec_of(engine, gid) is not None          # global 未被刪


def test_delete_member_cannot_delete_other_members_self(engine):
    """命門:member A 拿到 member B 的 self id 也刪不掉(底層命名空間驗證)。"""
    bob = make_member_principal("member:bob", "bob")
    out = do_log_calibration(engine, bob, brew_mechanism="percolation", grade="C",
                             origin="Ethiopia", process="washed", roast_agtron=74,
                             method="V60", grind_um=653, user_id="self")
    bob_id = out["id"]
    alice = make_member_principal("member:alice", "alice")
    res = do_delete_calibration(engine, alice, record_id=bob_id)
    assert res["ok"] is False and res["deleted_memory"] == 0
    assert _rec_of(engine, bob_id) is not None       # bob 的 self 未被刪
    assert do_delete_calibration(engine, bob, record_id=bob_id)["ok"] is True  # bob 自己刪得掉


def test_delete_reader_blocked(engine):
    gid = next(r.id for r in engine.store.iter_records() if r.user_id == GLOBAL_USER_ID)
    res = do_delete_calibration(engine, make_reader_principal(), record_id=gid)
    assert res["ok"] is False and res.get("gate") == "write_trust"
    assert _rec_of(engine, gid) is not None


def test_delete_owner_can_delete_any(engine):
    """owner 不受 confinement:可刪 member 的 self,也可刪 global。"""
    out = do_log_calibration(engine, make_member_principal("member:alice", "alice"),
                             brew_mechanism="percolation", grade="C",
                             origin="Kenya", roast_agtron=70, user_id="self")
    alice_id = out["id"]
    assert do_delete_calibration(engine, LOCAL_PRINCIPAL, record_id=alice_id)["ok"] is True
    assert _rec_of(engine, alice_id) is None

    gid = next(r.id for r in engine.store.iter_records() if r.user_id == GLOBAL_USER_ID)
    assert do_delete_calibration(engine, LOCAL_PRINCIPAL, record_id=gid)["ok"] is True
    assert _rec_of(engine, gid) is None


def test_delete_empty_record_id_rejected(engine):
    res = do_delete_calibration(engine, make_member_principal("member:alice", "alice"),
                                record_id="   ")
    assert res["ok"] is False and "record_id" in res["error"]


def test_delete_counts_against_rate_limit(monkeypatch, engine):
    """刪除也算一次寫入(防灌爆);超限被擋。"""
    monkeypatch.setattr("cie.mcp_principal.MEMBER_WRITE_LIMIT", 1)
    reset_write_counters()
    alice = make_member_principal("member:alice", "alice")
    out = do_log_calibration(engine, alice, brew_mechanism="percolation", grade="C",
                             origin="Kenya", roast_agtron=70, user_id="self")  # 用掉額度
    res = do_delete_calibration(engine, alice, record_id=out["id"])
    assert res["ok"] is False and res["gate"] == "rate_limit"


# ───────── 設定面唯一性守衛:N-guest self 互不混入(§16.3,啟動 fail-closed) ─────────

def _cfg(auth_token: str = "PRIMARY", guests=None):
    """建一個只關心 MCP token 欄位的 Config(其餘走預設)。guests 可給 dict / list / str。"""
    raw = guests if isinstance(guests, str) else (json.dumps(guests) if guests is not None else "")
    return Config(mcp_auth_token=auth_token, mcp_guest_tokens=raw)


def test_guard_accepts_unique_n_guests():
    """≥3 個 user_id 互異的 guest → 通過,回乾淨對映(不 raise)。"""
    tokens = validate_guest_token_config(_cfg(guests={"t1": "g1", "t2": "g2", "t3": "g3"}))
    assert tokens == {"t1": "g1", "t2": "g2", "t3": "g3"}


def test_guard_rejects_duplicate_user_id():
    """主破口:兩個 guest token 對映同一 user_id → fail-closed 拒絕(否則共用同一 self)。"""
    with pytest.raises(GuestTokenConfigError, match="user_id"):
        validate_guest_token_config(_cfg(guests={"t1": "alice", "t2": "alice"}))


def test_guard_rejects_duplicate_among_three_guests():
    """N>2:g1=alice、g2=bob、g3=bob(第三個撞第二個)→ 拒。"""
    with pytest.raises(GuestTokenConfigError, match="bob"):
        validate_guest_token_config(_cfg(guests={"t1": "alice", "t2": "bob", "t3": "bob"}))


def test_guard_is_runtime_error_subclass():
    """GuestTokenConfigError 為 RuntimeError 子類(沿用啟動 fail-closed 慣例,可用 RuntimeError 接)。"""
    assert issubclass(GuestTokenConfigError, RuntimeError)
    with pytest.raises(RuntimeError):
        validate_guest_token_config(_cfg(guests={"a": "x", "b": "x"}))


def test_guard_rejects_guest_token_colliding_with_primary():
    """guest token 字串撞 primary(CIE_MCP_AUTH_TOKEN)→ 拒(否則被 primary 規則搶先 → owner 的 self)。"""
    with pytest.raises(GuestTokenConfigError, match="primary"):
        validate_guest_token_config(_cfg(auth_token="SHARED", guests={"SHARED": "alice"}))


def test_guard_rejects_reader_token_colliding_with_primary():
    """連 reader token 撞 primary 也拒:否則純讀 token 靜默升格為可寫 owner-self member。"""
    with pytest.raises(GuestTokenConfigError, match="primary"):
        validate_guest_token_config(_cfg(auth_token="SHARED", guests={"SHARED": ""}))


def test_guard_rejects_guest_claiming_owner_self_namespace():
    """guest 認領 owner 的個人命名空間(auth_user_id,預設 'self')→ 拒。
    'self' 本即保留字(_parse 會 skip),故顯式傳一個非保留的 auth_user_id 驗這道補強。"""
    with pytest.raises(GuestTokenConfigError, match="owner"):
        validate_guest_token_config(_cfg(guests={"t1": "ownerns"}), auth_user_id="ownerns")


def test_guard_reserved_namespaces_skipped_with_n_guests():
    """保留字 global / self 沿用既有 reject(skip);N guest 下被踢除、合法者保留、不誤判重複。"""
    tokens = validate_guest_token_config(
        _cfg(guests={"t1": "global", "t2": "self", "t3": "carol", "t4": "dave"}))
    assert tokens == {"t3": "carol", "t4": "dave"}     # 兩個保留字筆被剔除


def test_guard_two_guests_claiming_reserved_not_treated_as_dup():
    """兩 guest 都認領保留字 → 都被 skip(各自 401),不算『重複 user_id』而誤 raise。"""
    tokens = validate_guest_token_config(_cfg(guests={"t1": "global", "t2": "global"}))
    assert tokens == {}                                # 皆 skip,無破口


def test_guard_readers_do_not_collide_on_uniqueness():
    """多個 reader(無命名空間)不參與 user_id 唯一性:可並存,不誤判重複。"""
    tokens = validate_guest_token_config(_cfg(guests={"r1": "", "r2": "", "g1": "alice"}))
    assert tokens == {"r1": None, "r2": None, "g1": "alice"}


def test_guard_user_id_is_explicit_config_value_not_derived():
    """user_id 取設定明確值,不由 token 衍生:兩 token 共享長前綴但 user_id 互異 → 各自解析為
    明確命名空間(證明非雜湊截斷 / 顯示名衍生,否則會碰撞成同一 self)。"""
    long_a = "tok_shared_prefix_AAAAAAAAAAAAAAAAAAAA_a"
    long_b = "tok_shared_prefix_AAAAAAAAAAAAAAAAAAAA_b"
    tokens = validate_guest_token_config(_cfg(guests={long_a: "alice", long_b: "bob"}))
    assert tokens == {long_a: "alice", long_b: "bob"}
    members = {"alice": _parse_guest(json.dumps({long_a: "alice"})),
               "bob": _parse_guest(json.dumps({long_b: "bob"}))}
    assert resolve_principal(long_a, member_tokens=members["alice"]).write_user_id == "alice"
    assert resolve_principal(long_b, member_tokens=members["bob"]).write_user_id == "bob"


def test_guard_empty_config_is_noop():
    """無 guest token → 空對映,不 raise(守衛對空設定無副作用)。"""
    assert validate_guest_token_config(_cfg(guests=None)) == {}


def test_no_shared_fallback_unmatched_token_is_none():
    """無共用 fallback:N guest 設定下,任何不命中的 token 一律 None(401),絕不退某共用預設 ns。"""
    cfg = _cfg(auth_token="PRIMARY", guests={"t1": "g1", "t2": "g2", "t3": "g3"})
    assert resolve_principal_from_config("does-not-match-anything", cfg) is None
    assert resolve_principal_from_config("", cfg) is None
    assert resolve_principal_from_config(None, cfg) is None
    # 命中者各自解析到自己的命名空間(無人退回共用預設)。
    assert resolve_principal_from_config("t2", cfg).write_user_id == "g2"


# ───────── N-guest pairwise 隔離(≥3 guest + owner,本輪把 2-member 硬化到 N) ─────────

GUEST_NS = ("g1", "g2", "g3")


def _guest(ns):
    return make_member_principal(f"member:{ns}", ns)


def _write_self_probe(engine, principal, grind):
    """同一查詢豆況、僅 grind 微異 → 若讀隔離洩漏,該筆必出現在他人證據裡。"""
    out = do_log_calibration(engine, principal, brew_mechanism="percolation", grade="C",
                             origin="Ethiopia", process="washed", roast_agtron=74,
                             method="V60", grind_um=grind, acidity=7.4, user_id="self")
    assert out["ok"], out
    return out["id"]


def _evidence_ids(engine, principal):
    res = do_query(engine, principal, brew_mechanism="percolation", mode="recommend",
                   origin="Ethiopia", process="washed", roast_agtron=74)
    return {e["id"] for e in res.get("evidence", [])}


def test_n_guest_self_read_pairwise_isolation(engine):
    """≥3 guest:每個 guest 讀只見 global + 自己的 self,讀不到另外任一 guest 的 self(pairwise)。"""
    ids = {}
    for i, ns in enumerate(GUEST_NS):
        ids[ns] = _write_self_probe(engine, _guest(ns), 651 + i)
        assert _uid_of(engine, ids[ns]) == ns
    for ns in GUEST_NS:
        sees = _evidence_ids(engine, _guest(ns))
        assert ids[ns] in sees, f"{ns} 應讀得到自己的 self 校準"
        for other in GUEST_NS:
            if other != ns:
                assert ids[other] not in sees, f"跨 guest 洩漏:{ns} 讀到 {other} 的 self!"
    owner_sees = _evidence_ids(engine, LOCAL_PRINCIPAL)   # owner 讀不過濾:全可見(供晉升審查)
    assert all(ids[ns] in owner_sees for ns in GUEST_NS)


def test_n_guest_write_to_other_ns_is_confined(engine):
    """guest 指定他人 ns / global 寫 → 一律 confine 回自有;他人 self 筆數不增(pairwise)。"""
    base = {ns: _write_self_probe(engine, _guest(ns), 660 + i) for i, ns in enumerate(GUEST_NS)}
    assert len(base) == 3

    def count_ns(ns):
        return sum(1 for r in engine.store.iter_records() if r.user_id == ns)

    before = {ns: count_ns(ns) for ns in GUEST_NS}
    before_global = count_ns(GLOBAL_USER_ID)
    # g1 嘗試寫到 g2 / g3 / global → 全部落 g1 自有 ns。
    for target in ("g2", "g3", GLOBAL_USER_ID):
        out = do_log_calibration(engine, _guest("g1"), brew_mechanism="percolation", grade="C",
                                 origin="Ethiopia", process="washed", roast_agtron=74,
                                 method="V60", grind_um=700, user_id=target)
        assert out["ok"] and _uid_of(engine, out["id"]) == "g1"
    assert count_ns("g2") == before["g2"]                # g2 未因 g1 的寫入增加
    assert count_ns("g3") == before["g3"]                # g3 亦然
    assert count_ns(GLOBAL_USER_ID) == before_global     # global 未被污染


def test_n_guest_delete_others_blocked_pairwise(engine):
    """guest 拿他人 record id 刪 → 落空(底層 user_id 不符);他人 self 不變;自刪成功。"""
    ids = {ns: _write_self_probe(engine, _guest(ns), 670 + i) for i, ns in enumerate(GUEST_NS)}
    for victim in ("g2", "g3"):
        res = do_delete_calibration(engine, _guest("g1"), record_id=ids[victim])
        assert res["ok"] is False and res["deleted_memory"] == 0
        assert _rec_of(engine, ids[victim]) is not None, f"g1 竟刪掉了 {victim} 的 self!"
    assert do_delete_calibration(engine, _guest("g1"), record_id=ids["g1"])["ok"] is True
    assert _rec_of(engine, ids["g1"]) is None            # g1 刪自己 → 成功


def test_n_guest_principal_no_bleed_under_concurrency():
    """重申 HIGH principal-bleed 守衛在 N>2 仍成立:N 個並發 task 各設自己的 principal,
    互不洩漏(contextvar 每 asyncio Task 獨立 copy;stateless streamable-http 即靠此隔離)。"""
    principals = [make_member_principal(f"member:g{i}", f"g{i}") for i in range(5)]

    async def one(p):
        tok = set_principal(p)
        try:
            await asyncio.sleep(0)                        # 讓出排程,逼出潛在跨 task 洩漏
            assert current_principal().write_user_id == p.write_user_id
            await asyncio.sleep(0)
            assert current_principal() is p               # 仍是自己的,未被別的 task 蓋掉
            return current_principal().write_user_id
        finally:
            reset_principal(tok)

    async def run():
        return await asyncio.gather(*(asyncio.create_task(one(p)) for p in principals))

    assert asyncio.run(run()) == [f"g{i}" for i in range(5)]
