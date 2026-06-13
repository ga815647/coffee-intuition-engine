"""D1Canonical(Cloudflare D1 / SQLite-over-HTTP)單元測試 — 全離線,假 D1 用戶端,不觸網路。

驗證 canonical 改用 D1 的命門(§15.1):
  - **round-trip 無損**:append/extend → iter 還原(列舉、枚舉欄、機制保留)。
  - **INSERT OR REPLACE 冪等**:同 id 後寫者勝 → canonical 只留一列(晉升不需事後去重,
    且無 R2 單物件 read-modify-write 的整檔覆寫 race)。
  - **批次分割**:extend 大量(> 單批上限)仍全數寫入(SQLite 變數上限不撞)。
  - **user_id 過濾**:select_by_user = SELECT WHERE user_id(list_customizations 之 SQL 對應)。
  - **工廠 / sink**:canonical_backend=d1 → get_canonical=D1Canonical;maybe_get_canonical 對
    記憶體後端**仍強掛 sink**(D1 為跨行程單一真相,記憶體 _canonical 不持久)。
  - **冷啟動**:memory + d1 → prime_serving_index 從 D1 重嵌重建 in-memory 索引;
    **member 寫入撐過冷啟動**且仍受讀隔離、未污染 global。

假 D1 = in-memory dict(id→列),實作 d1_query 用到的 CREATE/INSERT OR REPLACE/DELETE/SELECT 子集。
同一 FakeD1 被多個「行程」(多個 D1Canonical / 多個記憶體 store)共用,模擬 Cloud Run 冷啟動讀寫同一 DB。
"""
from __future__ import annotations

import pytest

from cie.canonical import (
    CanonicalStore, D1Canonical, LocalJsonlCanonical, get_canonical, maybe_get_canonical,
)
from cie.config import Config
from cie.engine import Engine
from cie.mcp_principal import (
    GLOBAL_USER_ID, make_member_principal, make_reader_principal, reset_write_counters,
)
from cie.mcp_tools import do_log_calibration, do_query
from cie.rebuild import prime_serving_index
from cie.schema import (
    AcidityType, BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process, Record,
)
from cie.store import VectorStore


# ────────────────────────────── 假 D1 用戶端 ──────────────────────────────

class FakeD1:
    """In-memory D1 用戶端:實作 D1Canonical 用到的 d1_query 子集。

    rows: id → {col: val},dict 保插入序(SELECT ... ORDER BY rowid 用);INSERT OR REPLACE
    對既有 id 先 pop 再寫 → 模擬 SQLite REPLACE 換新 rowid(移到末端)。
    """

    _NCOLS = 6  # id, user_id, grade, mechanism, payload, ts

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.calls: list[str] = []          # 記每次 SQL,供斷言批次數 / DDL 等

    def d1_query(self, database_id, sql, params=None):
        self.calls.append(sql)
        head = sql.strip().upper()
        if head.startswith("CREATE"):
            return []
        if head.startswith("DELETE"):
            self.rows.clear()
            return [{"results": [], "success": True, "meta": {"changes": 0}}]
        if head.startswith("INSERT"):
            p = list(params or [])
            cols = ("id", "user_id", "grade", "mechanism", "payload", "ts")
            written = 0
            for i in range(0, len(p), self._NCOLS):
                row = dict(zip(cols, p[i:i + self._NCOLS]))
                rid = row["id"]
                self.rows.pop(rid, None)     # REPLACE:移到末端
                self.rows[rid] = row
                written += 1
            return [{"results": [], "success": True, "meta": {"changes": written}}]
        if head.startswith("SELECT"):
            rows = list(self.rows.values())
            if "WHERE USER_ID" in head:
                uid = (params or [None])[0]
                rows = [r for r in rows if r.get("user_id") == uid]
            return [{"results": [{"payload": r["payload"]} for r in rows],
                     "success": True, "meta": {}}]
        return []


# ────────────────────────────── 共用 fixtures ──────────────────────────────

def _d1_cfg(dim: int = 64, override: bool = False) -> Config:
    """生產組合:CF 金鑰 + D1 db_id(→ canonical_backend=d1)+ 記憶體向量庫(override)。

    override=True 額外設 CIE_CANONICAL_BACKEND=d1(顯式),驗證 override 路徑;
    否則走自動偵測(金鑰 + d1_database_id → d1)。store_backend_override=memory 蓋掉
    「有 CF 金鑰→vectorize」,精準重現「記憶體自幹 index + D1 canonical」。
    """
    return Config(
        cf_account_id="acct", cf_api_token="tok", d1_database_id="db-test",
        canonical_backend_override="d1" if override else "",
        store_backend_override="memory",
        embedding_provider="local", embedding_dim=dim,
        mcp_auth_token="PRIMARY", mcp_stateless=True,
    )


def _canon(fake: FakeD1, cfg: Config | None = None) -> D1Canonical:
    return D1Canonical(config=cfg or _d1_cfg(), client=fake)


def _rec(origin="Ethiopia", mech=BrewMechanism.PERCOLATION, grade=Grade.B,
         user_id="global", grind_um=650.0):
    return Record(
        bean=BeanRoast(origin=origin, variety="Heirloom", process=Process.WASHED, roast_agtron=74),
        params=BrewParams(brew_mechanism=mech, method="V60", water_temp_c=92,
                          brew_ratio=16.0, grind_um=grind_um, tds_pct=1.38, ey_pct=20.4),
        flavor=FlavorProfile(acidity=7.5, acidity_type=AcidityType.CITRIC, sweetness=7.0, body=5.0),
        grade=grade, protocol="SCA_cupping" if grade == Grade.A else "", user_id=user_id,
    )


@pytest.fixture(autouse=True)
def _reset_counters():
    reset_write_counters()
    yield
    reset_write_counters()


# ────────────────────────────── round-trip / 寫讀 ──────────────────────────────

def test_d1_append_iter_lossless():
    canon = _canon(FakeD1())
    assert list(canon.iter_records()) == []            # 空表 → 空
    r1, r2 = _rec("Ethiopia"), _rec("Kenya", BrewMechanism.IMMERSION)
    canon.append(r1)
    canon.append(r2)
    back = list(canon.iter_records())
    assert [r.id for r in back] == [r1.id, r2.id]
    assert back[0].flavor.acidity_type == AcidityType.CITRIC
    assert back[1].params.brew_mechanism == BrewMechanism.IMMERSION


def test_d1_extend_count():
    canon = _canon(FakeD1())
    assert canon.extend([_rec("A"), _rec("B"), _rec("C")]) == 3
    assert len(list(canon.iter_records())) == 3


def test_d1_extend_empty_is_noop():
    fake = FakeD1()
    assert _canon(fake).extend([]) == 0
    assert not any(c.upper().startswith("INSERT") for c in fake.calls)


def test_d1_lazy_schema_no_network_on_construct():
    """建構不觸網路(惰性 schema):與 R2Canonical 一致,工廠/isinstance 測試離線可跑。"""
    fake = FakeD1()
    _ = D1Canonical(config=_d1_cfg(), client=fake)
    assert fake.calls == []                            # 尚未發任何 query
    _.append(_rec("Ethiopia"))
    assert any(c.upper().startswith("CREATE TABLE") for c in fake.calls)  # 首次寫才建表


def test_d1_requires_database_id():
    with pytest.raises(ValueError):
        D1Canonical(database_id="", client=FakeD1(),
                    config=Config(cf_account_id="a", cf_api_token="b"))


# ────────────────────────────── INSERT OR REPLACE 冪等(晉升不重複) ──────────────────────────────

def test_d1_insert_or_replace_same_id_keeps_one_row():
    """同 id 後寫者勝:append 兩版同 id → canonical 只留一列(最後一版)。

    這是 D1 相對 R2(append-only,留兩列待事後去重)的關鍵差異:晉升 / 修正天然冪等,
    無整檔覆寫 race。"""
    canon = _canon(FakeD1())
    r = _rec("Ethiopia", grade=Grade.C, user_id="alice")
    canon.append(r)
    promoted = r.model_copy(update={"user_id": GLOBAL_USER_ID, "grade": Grade.A,
                                    "protocol": "SCA_cupping"})
    canon.append(promoted)
    back = list(canon.iter_records())
    assert len(back) == 1                              # 不重複(非兩列)
    assert back[0].user_id == GLOBAL_USER_ID and back[0].grade == Grade.A


def test_d1_replace_all_clears_then_writes():
    fake = FakeD1()
    canon = _canon(fake)
    canon.extend([_rec("A"), _rec("B")])
    assert canon.replace_all([_rec("X"), _rec("Y"), _rec("Z")]) == 3
    back = [r.bean.origin for r in canon.iter_records()]
    assert back == ["X", "Y", "Z"]                     # 舊的被清掉
    assert any(c.upper().startswith("DELETE") for c in fake.calls)


# ────────────────────────────── 批次分割(SQLite 變數上限) ──────────────────────────────

def test_d1_extend_batches_large_input():
    """extend 大量(跨單批上限 150 列)仍全數寫入,且確實分多批(SQL 變數不撞 999)。"""
    fake = FakeD1()
    canon = _canon(fake)
    n = 320                                            # 150 + 150 + 20 → 3 批
    assert canon.extend([_rec(f"bean-{i}") for i in range(n)]) == n
    assert len(list(canon.iter_records())) == n
    inserts = [c for c in fake.calls if c.upper().startswith("INSERT")]
    assert len(inserts) == 3                           # 分 3 批(非單批塞 320×6 參數)


# ────────────────────────────── user_id 過濾(list_customizations 對應) ──────────────────────────────

def test_d1_select_by_user_filters():
    canon = _canon(FakeD1())
    canon.extend([
        _rec("Ethiopia", user_id="alice"),
        _rec("Kenya", user_id="bob"),
        _rec("Brazil", user_id="alice", mech=BrewMechanism.IMMERSION),
        _rec("Colombia", user_id=GLOBAL_USER_ID),
    ])
    alice = canon.select_by_user("alice")
    assert {r.bean.origin for r in alice} == {"Ethiopia", "Brazil"}
    assert canon.select_by_user("nobody") == []


# ────────────────────────────── 工廠 / sink 選擇 ──────────────────────────────

def test_canonical_backend_auto_detects_d1():
    cfg = Config(cf_account_id="a", cf_api_token="b", d1_database_id="db1")  # 無 override
    assert cfg.canonical_backend == "d1"


def test_canonical_backend_override_wins():
    # 即便同時設了 R2 bucket,顯式 override=d1 仍勝出。
    cfg = Config(cf_account_id="a", cf_api_token="b", r2_bucket="bkt",
                 d1_database_id="db1", canonical_backend_override="d1")
    assert cfg.canonical_backend == "d1"


def test_get_canonical_returns_d1():
    canon = get_canonical(_d1_cfg())                   # 建構惰性,不觸網路
    assert isinstance(canon, D1Canonical)
    assert isinstance(canon, CanonicalStore)


def test_maybe_get_canonical_forces_d1_sink_even_for_memory_store():
    """命門:記憶體後端(有 iter_records)但 canonical_backend=d1 → **仍須掛 D1 sink**。

    記憶體 _canonical 不跨行程(Cloud Run scale-to-zero 即丟),D1 才是單一共用真相。"""
    cfg = _d1_cfg()
    store = VectorStore(cfg)                           # 記憶體;自帶 iter_records
    canon = maybe_get_canonical(store, cfg)
    assert isinstance(canon, D1Canonical)


def test_engine_auto_wires_d1_canonical_for_memory_store():
    cfg = _d1_cfg()
    eng = Engine(store=VectorStore(cfg), canonical=_canon(FakeD1(), cfg))
    assert isinstance(eng.canonical, D1Canonical)


# ────────────────────────────── 冷啟動:從 D1 重建 ──────────────────────────────

def test_prime_serving_index_rebuilds_from_d1():
    """memory + D1 + 有 canonical:從 D1 重嵌重建 in-memory 索引,機制硬分區仍成立。"""
    cfg = _d1_cfg()
    fake = FakeD1()
    canon = _canon(fake, cfg)
    canon.extend([
        _rec("Ethiopia", BrewMechanism.PERCOLATION),
        _rec("Kenya", BrewMechanism.PERCOLATION),
        _rec("Brazil", BrewMechanism.IMMERSION),
    ])
    eng = Engine(store=VectorStore(cfg), canonical=canon)
    assert eng.store.count() == 0                      # 冷啟動初始空
    assert prime_serving_index(eng, cfg) == 3
    assert eng.store.count() == 3
    hits = eng.store.search("Ethiopia washed", BrewMechanism.PERCOLATION)
    assert hits and all(h["payload"]["brew_mechanism"] == "percolation" for h in hits)


def test_prime_serving_index_none_for_offline_dev():
    """離線開發(記憶體 + 本地 canonical,無金鑰)→ 不 prime(回 None)。"""
    cfg = Config(embedding_provider="local", embedding_dim=64)
    eng = Engine(store=VectorStore(cfg))
    assert prime_serving_index(eng, cfg) is None


# ────────────────────────────── 命門:member 寫入撐過冷啟動(D1) ──────────────────────────────

def test_member_write_survives_cold_start_via_d1_and_stays_isolated():
    """端到端命門(D1 版):member 經 HTTP 寫自有 self → 落共用 D1 → 新行程冷啟動重建後仍在,
    且**仍受讀隔離**(他人 / reader 讀不到)、**未污染 global**。"""
    cfg = _d1_cfg()
    fake = FakeD1()                                    # 跨「行程」共用的單一 D1 真相

    # ── 行程 1(寫入實例):member alice 寫一筆 self 校準 ──
    eng1 = Engine(store=VectorStore(cfg), canonical=_canon(fake, cfg))
    alice = make_member_principal("member:alice", "alice")
    out = do_log_calibration(eng1, alice, brew_mechanism="percolation", grade="C",
                             origin="Ethiopia", process="washed", roast_agtron=74,
                             method="V60", grind_um=651, acidity=7.4, user_id="self")
    assert out["ok"] is True
    alice_id = out["id"]

    # D1 收到真相,且 user_id 被 confine 回 alice(寫 global 也會被改寫回自有 ns)。
    d1_back = list(_canon(fake, cfg).iter_records())
    assert [r.id for r in d1_back] == [alice_id]
    assert d1_back[0].user_id == "alice" != GLOBAL_USER_ID

    # ── 行程 2(冷啟動實例):全新記憶體 store,同一個 D1 ──
    eng2 = Engine(store=VectorStore(cfg), canonical=_canon(fake, cfg))
    assert eng2.store.count() == 0
    assert prime_serving_index(eng2, cfg) == 1
    assert eng2.store.count() == 1

    def evidence_ids(principal):
        res = do_query(eng2, principal, brew_mechanism="percolation", mode="recommend",
                       origin="Ethiopia", process="washed", roast_agtron=74)
        return {e["id"] for e in res.get("evidence", [])}

    assert alice_id in evidence_ids(alice)                                       # 自己讀得到
    assert alice_id not in evidence_ids(make_member_principal("member:bob", "bob"))  # 他人讀不到
    assert alice_id not in evidence_ids(make_reader_principal())                 # reader 讀不到


def test_member_write_does_not_reach_global_across_cold_start_d1():
    """member 嘗試寫 global → confine 回自有 ns;冷啟動重建後 global 仍乾淨(reader 讀不到)。"""
    cfg = _d1_cfg()
    fake = FakeD1()
    eng1 = Engine(store=VectorStore(cfg), canonical=_canon(fake, cfg))
    out = do_log_calibration(eng1, make_member_principal("member:alice", "alice"),
                             brew_mechanism="percolation", grade="A", protocol="SCA_cupping",
                             origin="Ethiopia", process="washed", roast_agtron=74,
                             method="V60", grind_um=651, user_id="global")
    assert out["ok"] is True
    back = list(_canon(fake, cfg).iter_records())
    assert all(r.user_id != GLOBAL_USER_ID for r in back)   # global 永不被網路寫
    assert all(r.grade != Grade.A for r in back)            # A 降為 B(member 上限)

    eng2 = Engine(store=VectorStore(cfg), canonical=_canon(fake, cfg))
    prime_serving_index(eng2, cfg)
    res = do_query(eng2, make_reader_principal(), brew_mechanism="percolation",
                   mode="recommend", origin="Ethiopia", process="washed", roast_agtron=74)
    assert res.get("evidence", []) == []                    # global 空 → reader 無證據
