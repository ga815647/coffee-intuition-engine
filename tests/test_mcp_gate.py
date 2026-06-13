"""Remote MCP「兩扇門」治理:公開門唯讀、私有門(本機 owner)唯一寫入(設計 §16)。

全離線、確定性:直接建記憶體 engine + 顯式 principal,測 do_* 邏輯與寫入閘。
不碰 HTTP 傳輸(那在 test_mcp_http.py);證明治理規則本身結構性生效。
"""
from __future__ import annotations

import pytest

from cie.engine import Engine
from cie.mcp_principal import (
    GLOBAL_USER_ID, LOCAL_PRINCIPAL, apply_write_trust, make_reader_principal,
    resolve_principal,
)
from cie.mcp_tools import do_log_calibration, do_query
from cie.schema import (
    BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process, Record,
)
from cie.store import VectorStore


def _bp(mech=BrewMechanism.PERCOLATION, **kw):
    return BrewParams(brew_mechanism=mech, **kw)


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


# ────────────────────────────── token → principal 解析(HTTP 一切唯讀) ──────────────────────────────

def test_resolve_primary_auth_token_is_reader():
    p = resolve_principal("PRIMARY", auth_token="PRIMARY")
    assert p is not None and p.role == "reader"
    assert p.can_write is False              # HTTP 無寫入路徑
    assert p.read_user_ids is None           # 唯讀共享(global + owner 校準)


def test_resolve_extra_read_token_is_reader():
    p = resolve_principal("tok_a", read_tokens={"tok_a": "alice", "tok_b": "bob"})
    assert p is not None and p.role == "reader" and p.can_write is False
    assert "alice" in p.name                 # label 供稽核 / 撤銷


def test_resolve_read_tokens_array_form():
    p = resolve_principal("tok_x", read_tokens={"tok_x": "reader"})
    assert p is not None and p.can_write is False


def test_resolve_bad_token_returns_none():
    assert resolve_principal("nope", auth_token="PRIMARY") is None


def test_resolve_none_token_returns_none():
    assert resolve_principal(None, auth_token="PRIMARY") is None


def test_resolve_fail_closed_when_no_secret():
    # 未設任何密鑰 → 任何 token 都不通(含空字串比對)。
    assert resolve_principal("anything", auth_token="") is None
    assert resolve_principal("", auth_token="") is None


def test_local_principal_is_sole_writer():
    # 私有門 stdio 預設身分:owner、唯一可寫、讀不過濾(零回歸)。
    assert LOCAL_PRINCIPAL.role == "owner"
    assert LOCAL_PRINCIPAL.can_write is True
    assert LOCAL_PRINCIPAL.read_user_ids is None


# ────────────────────────────── 寫入閘(純函式:唯本機 owner 能寫) ──────────────────────────────

def test_gate_reader_cannot_write():
    # 公開門 reader 一律不得寫(防禦縱深:即便寫工具誤掛 HTTP 也擋下)。
    rec = Record(params=_bp(), grade=Grade.C)
    d = apply_write_trust(rec, make_reader_principal())
    assert d.ok is False and "唯讀" in d.error


def test_gate_rejects_client_injected_prediction_even_for_owner():
    # 內部保留級不得當人類真值注入——連 owner 自己也擋(防失誤)。
    rec = Record(params=_bp(), grade=Grade.PREDICTION)
    d = apply_write_trust(rec, LOCAL_PRINCIPAL)
    assert d.ok is False and "prediction" in d.error


def test_gate_owner_write_self_passes_unchanged():
    rec = Record(params=_bp(), grade=Grade.C, user_id="self")
    d = apply_write_trust(rec, LOCAL_PRINCIPAL)
    assert d.ok is True and d.record.user_id == "self"     # 不再做命名空間重導


def test_gate_owner_may_write_global():
    rec = Record(params=_bp(), grade=Grade.B, user_id=GLOBAL_USER_ID)
    d = apply_write_trust(rec, LOCAL_PRINCIPAL)
    assert d.ok is True and d.record.user_id == GLOBAL_USER_ID


# ────────────────────────────── do_log_calibration 端到端(閘 + engine) ──────────────────────────────

def test_log_reader_blocked_no_write(engine):
    # 公開門 reader 嘗試寫入 → 被閘擋、不寫入。
    before = engine.store.count()
    out = do_log_calibration(engine, make_reader_principal(),
                             brew_mechanism="percolation", grade="C", origin="Kenya")
    assert out["ok"] is False and out.get("gate") == "write_trust"
    assert "唯讀" in out["error"]
    assert engine.store.count() == before


def test_log_owner_prediction_injection_blocked_no_write(engine):
    before = engine.store.count()
    out = do_log_calibration(engine, LOCAL_PRINCIPAL,
                             brew_mechanism="percolation", grade="prediction")
    assert out["ok"] is False and out.get("gate") == "write_trust"
    assert engine.store.count() == before


def test_log_owner_a_without_protocol_rejected_by_engine(engine):
    # owner 過閘(可寫),但 engine 仍要求 A 須 protocol → 拒收(單一真相把關)。
    out = do_log_calibration(engine, LOCAL_PRINCIPAL,
                             brew_mechanism="percolation", grade="A", protocol="")
    assert out["ok"] is False and "protocol" in out["error"]


def test_log_owner_a_with_protocol_ok(engine):
    out = do_log_calibration(engine, LOCAL_PRINCIPAL,
                             brew_mechanism="percolation", grade="A", protocol="SCA_cupping",
                             origin="Ethiopia", roast_agtron=74, acidity=7.6)
    assert out["ok"] is True


def test_log_owner_write_self_ok(engine):
    out = do_log_calibration(engine, LOCAL_PRINCIPAL,
                             brew_mechanism="percolation", grade="C",
                             origin="Kenya", roast_agtron=70, user_id="self")
    assert out["ok"] is True
    assert _uid_of(engine, out["id"]) == "self"


def test_log_owner_write_global_ok(engine):
    out = do_log_calibration(engine, LOCAL_PRINCIPAL,
                             brew_mechanism="percolation", grade="B", user_id="global",
                             origin="Colombia", roast_agtron=68)
    assert out["ok"] is True
    assert _uid_of(engine, out["id"]) == "global"


def test_log_default_grade_is_c(engine):
    out = do_log_calibration(engine, LOCAL_PRINCIPAL,
                             brew_mechanism="percolation")  # 未指定 grade
    assert out["ok"] is True


# ────────────────────────────── 讀:機制硬分區 + 公開門共享唯讀 ──────────────────────────────

def test_read_mechanism_hard_partition(engine):
    """機制三軌硬隔離(鐵則 1):同情境查 immersion 不得混入 percolation 證據。"""
    # 寫一筆 immersion 真值(owner 私有門)。
    out = do_log_calibration(engine, LOCAL_PRINCIPAL,
                             brew_mechanism="immersion", grade="B",
                             origin="Brazil", process="natural", roast_agtron=58,
                             method="FrenchPress", grind_um=900, water_temp_c=92)
    imm_id = out["id"]

    def evidence_ids(mech):
        res = do_query(engine, make_reader_principal(), brew_mechanism=mech,
                       mode="recommend", origin="Brazil", process="natural", roast_agtron=58)
        return {e["id"] for e in res.get("evidence", [])}

    imm = evidence_ids("immersion")
    perc = evidence_ids("percolation")
    assert imm_id in imm                       # immersion 查得到那筆
    assert imm_id not in perc                  # percolation 絕不混入
    assert imm.isdisjoint(perc)                # 兩軌證據互斥


def test_reader_sees_shared_global(engine):
    """公開門 reader 讀範圍 = 共享真相(此處 global 客觀層);唯讀、不過濾。"""
    res = do_query(engine, make_reader_principal(), brew_mechanism="percolation",
                   mode="recommend", origin="Ethiopia", process="washed", roast_agtron=74)
    assert len(res.get("evidence", [])) > 0


# ────────────────────────────── 加性讀過濾機制仍就緒(未來 self 隔離用) ──────────────────────────────

def test_store_user_ids_read_filter_still_works(engine):
    """`store.search(user_ids=...)` 加性過濾(預設 None=不過濾)仍可運作——
    這是『未來如需再加』per-tenant self 讀隔離的就緒機制,不動既有檢索數學。"""
    # owner 寫一筆 self 記錄。
    out = do_log_calibration(engine, LOCAL_PRINCIPAL,
                             brew_mechanism="percolation", grade="C",
                             origin="Ethiopia", process="washed", roast_agtron=74,
                             method="V60", grind_um=652, user_id="self")
    self_id = out["id"]
    q = "Ethiopia washed light percolation"

    unfiltered = {h["id"] for h in engine.store.search(q, BrewMechanism.PERCOLATION, top_k=20)}
    global_only = {h["id"] for h in engine.store.search(
        q, BrewMechanism.PERCOLATION, top_k=20, user_ids=[GLOBAL_USER_ID])}

    assert self_id in unfiltered               # 不過濾 → 看得到 self
    assert self_id not in global_only          # 限定 global → 過濾掉 self
    assert all(h["payload"]["user_id"] == GLOBAL_USER_ID for h in engine.store.search(
        q, BrewMechanism.PERCOLATION, top_k=20, user_ids=[GLOBAL_USER_ID]))
