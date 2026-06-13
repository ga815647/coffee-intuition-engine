"""生產自幹 index 單元測試:記憶體後端 + R2 共用 canonical(Cloud Run scale-to-zero)。

驗證本輪上線改動的命門(全離線,假 R2 用戶端,不觸網路):
  - **maybe_get_canonical**:R2 已設定時,**即便記憶體後端有 iter_records 也強掛 sink**——
    記憶體 `_canonical` 不跨行程持久化,R2 才是單一共用真相。漏掉 → member 寫入在 scale-to-zero 前丟失。
  - **prime_serving_index**:只在「memory + R2 + 有 canonical」觸發,冷啟動從 R2 重嵌重建 in-memory 索引。
  - **member 寫入撐過冷啟動**:member 經 HTTP 寫自有 self → 同步落 R2 → 新行程冷啟動重建後該筆仍在,
    且**仍受讀隔離**(他人 / reader 讀不到)、**未污染 global**。這是「$0 scale-to-zero 不丟資料」的核心保證。
  - **持久化順序(durability)**:canonical(R2)append 失敗會在 store.upsert 前拋例外 → 不回假成功。
  - **build_app 冷啟動**:走 prime(從 R2 載入)而非灌 6 筆種子。

不碰真 HTTP 傳輸(那在 test_mcp_http.py);直接建記憶體 engine + 假 R2,測載入 / 寫入 / 重建邏輯。
"""
from __future__ import annotations

import pytest

from cie.canonical import R2Canonical, get_canonical, maybe_get_canonical
from cie.config import Config
from cie.engine import Engine
from cie.mcp_principal import (
    GLOBAL_USER_ID, LOCAL_PRINCIPAL, make_member_principal, make_reader_principal,
    reset_write_counters,
)
from cie.mcp_tools import (
    do_log_calibration, do_promote_customization, do_query,
)
from cie.rebuild import prime_serving_index
from cie.schema import (
    AcidityType, BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process, Record,
)
from cie.store import VectorStore


# ────────────────────────────── 假 R2(跨冷啟動共用的單一真相物件) ──────────────────────────────

class FakeR2:
    """In-memory R2 用戶端(只實作 R2Canonical 用到的 get/put)。

    同一個 FakeR2 實例被多個「行程」(= 多個 R2Canonical / 多個記憶體 store)共用,
    模擬 Cloud Run 多次冷啟動讀寫同一個 R2 bucket 物件。
    """

    def __init__(self):
        self.objects: dict[tuple[str, str], str] = {}

    def r2_get_object(self, bucket, key):
        return self.objects.get((bucket, key))  # None = 404

    def r2_put_object(self, bucket, key, body, content_type="application/x-ndjson"):
        self.objects[(bucket, key)] = body


def _prod_cfg(dim: int = 64) -> Config:
    """生產組合:CF 金鑰 + R2 bucket(→ canonical_backend=r2)+ 記憶體向量庫(override)。

    embedding_provider=local 讓嵌入離線(維度 dim);store_backend_override=memory 蓋掉
    「有 CF 金鑰→vectorize」的自動偵測,精準重現本輪「記憶體自幹 index + R2 canonical」。
    """
    return Config(
        cf_account_id="acct", cf_api_token="tok", r2_bucket="cie-canon",
        store_backend_override="memory",
        embedding_provider="local", embedding_dim=dim,
        mcp_auth_token="PRIMARY", mcp_stateless=True,
    )


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


# ────────────────────────────── maybe_get_canonical:R2 強掛 sink ──────────────────────────────

def test_maybe_get_canonical_forces_r2_sink_even_for_memory_store():
    """命門:生產用記憶體後端(有 iter_records)但 canonical_backend=r2 → **仍須掛 R2 sink**。

    記憶體 _canonical 不跨行程(Cloud Run scale-to-zero 即丟),R2 才是單一共用真相。
    若這裡回 None(舊離線行為),member 寫入只進易失記憶體、scale-to-zero 前丟失。
    """
    cfg = _prod_cfg()
    assert cfg.canonical_backend == "r2"
    store = VectorStore(cfg)                       # 記憶體;自帶 iter_records
    canon = maybe_get_canonical(store, cfg)
    assert canon is not None
    assert isinstance(canon, R2Canonical)


def test_engine_auto_wires_r2_canonical_for_memory_store():
    """迴歸防線:Engine(memory store, prod cfg) 未顯式給 canonical 時,建構子須自動掛 R2 sink。

    用 get_canonical 注入假 R2 用戶端,避免建構真 CloudflareClient(仍不觸網路,但更乾淨)。
    """
    cfg = _prod_cfg()
    canon = R2Canonical(config=cfg, client=FakeR2())
    eng = Engine(store=VectorStore(cfg), canonical=canon)
    assert eng.canonical is not None


# ────────────────────────────── prime_serving_index 觸發條件 ──────────────────────────────

def test_prime_serving_index_none_for_offline_dev():
    """離線開發(記憶體 + 本地 canonical,無 CF 金鑰)→ 不 prime(回 None,由呼叫端決定灌種子)。"""
    cfg = Config(embedding_provider="local", embedding_dim=64)  # 無 CF 金鑰 → canonical=local
    eng = Engine(store=VectorStore(cfg))                        # canonical=None(memory 自存)
    assert prime_serving_index(eng, cfg) is None


def test_prime_serving_index_none_when_no_canonical():
    """memory + r2 設定但 engine.canonical 為 None(防禦)→ 回 None,不炸。"""
    cfg = _prod_cfg()
    eng = Engine(store=VectorStore(cfg), canonical=None)
    eng.canonical = None  # 強制清掉(模擬未掛 sink 的異常情形)
    assert prime_serving_index(eng, cfg) is None


def test_prime_serving_index_rebuilds_from_r2():
    """memory + R2 + 有 canonical:從 R2 重嵌重建 in-memory 索引,且機制硬分區仍成立。"""
    cfg = _prod_cfg()
    fake = FakeR2()
    canon = R2Canonical(config=cfg, client=fake)
    canon.extend([
        _rec("Ethiopia", BrewMechanism.PERCOLATION),
        _rec("Kenya", BrewMechanism.PERCOLATION),
        _rec("Brazil", BrewMechanism.IMMERSION),
    ])
    eng = Engine(store=VectorStore(cfg), canonical=canon)
    assert eng.store.count() == 0                  # 冷啟動初始空

    n = prime_serving_index(eng, cfg)
    assert n == 3
    assert eng.store.count() == 3
    hits = eng.store.search("Ethiopia washed", BrewMechanism.PERCOLATION)
    assert hits
    assert all(h["payload"]["brew_mechanism"] == "percolation" for h in hits)  # 不混 immersion


# ────────────────────────────── 命門:member 寫入撐過冷啟動 ──────────────────────────────

def test_member_write_survives_cold_start_and_stays_isolated():
    """端到端命門:member 經 HTTP 寫自有 self → 落共用 R2 → 新行程冷啟動重建後仍在,
    且**仍受讀隔離**(他人 member / reader 讀不到)、**未污染 global**。

    這證明 $0 scale-to-zero(min-instances=0)不丟 member 寫入,且重建不破壞 §16.3 self 隔離。
    """
    cfg = _prod_cfg()
    fake = FakeR2()                                # 跨「行程」共用的單一 R2 真相

    # ── 行程 1(寫入實例):member alice 寫一筆 self 校準 ──
    eng1 = Engine(store=VectorStore(cfg), canonical=R2Canonical(config=cfg, client=fake))
    alice = make_member_principal("member:alice", "alice")
    out = do_log_calibration(eng1, alice, brew_mechanism="percolation", grade="C",
                             origin="Ethiopia", process="washed", roast_agtron=74,
                             method="V60", grind_um=651, acidity=7.4, user_id="self")
    assert out["ok"] is True
    alice_id = out["id"]

    # R2 收到真相(撐過 scale-to-zero 的前提),且 user_id 被 confine 回 alice。
    r2_back = list(R2Canonical(config=cfg, client=fake).iter_records())
    assert [r.id for r in r2_back] == [alice_id]
    assert r2_back[0].user_id == "alice"           # 落自有 ns(寫 global 也會被改寫回 alice)
    assert r2_back[0].user_id != GLOBAL_USER_ID

    # ── 行程 2(冷啟動實例):全新記憶體 store,同一個 R2 ──
    eng2 = Engine(store=VectorStore(cfg), canonical=R2Canonical(config=cfg, client=fake))
    assert eng2.store.count() == 0                 # 冷啟動:in-memory 空
    primed = prime_serving_index(eng2, cfg)
    assert primed == 1
    assert eng2.store.count() == 1                 # 從 R2 重建回 alice 的那筆

    # 重建後仍受讀隔離:alice 讀得到自己;bob / reader 讀不到。
    def evidence_ids(principal):
        res = do_query(eng2, principal, brew_mechanism="percolation", mode="recommend",
                       origin="Ethiopia", process="washed", roast_agtron=74)
        return {e["id"] for e in res.get("evidence", [])}

    assert alice_id in evidence_ids(alice)                         # 自己讀得到(撐過冷啟動)
    assert alice_id not in evidence_ids(make_member_principal("member:bob", "bob"))  # 他人讀不到
    assert alice_id not in evidence_ids(make_reader_principal())   # reader 只讀 global,讀不到


def test_promotion_survives_cold_start_keeps_global_not_revert():
    """命門(防晉升靜默回退):member 寫 self(C)→ owner 就地晉升 global(A)→ canonical
    append-only 留下**同 id 兩版**→ 冷啟動重建後須是 **global/A 那版**、且**不重複**(count=1)。

    若重建未明確同 id 去重(改依賴後端 batch upsert 隱性語意),晉升過的 global 記錄可能
    靜默回退成舊的 self/C 版——既破壞晉升(鐵則 5:A 級真相)又破壞 self 隔離(回到 member ns)。
    """
    cfg = _prod_cfg()
    fake = FakeR2()                                # 跨「行程」共用的單一 R2 真相

    # ── 行程 1:member alice 寫 self(C),owner 就地晉升為 global(A) ──
    eng1 = Engine(store=VectorStore(cfg), canonical=R2Canonical(config=cfg, client=fake))
    src = do_log_calibration(eng1, make_member_principal("member:alice", "alice"),
                             brew_mechanism="percolation", grade="C",
                             origin="Ethiopia", process="washed", roast_agtron=74,
                             method="V60", grind_um=651, acidity=7.4, user_id="self")
    rid = src["id"]
    promo = do_promote_customization(eng1, LOCAL_PRINCIPAL, record_id=rid,
                                     grade="A", protocol="SCA_cupping")
    assert promo["ok"] is True and promo["promoted_id"] == rid

    # R2 canonical 因 append-only 留下同 id 兩版(self/C 在前、global/A 在後)。
    r2_raw = list(R2Canonical(config=cfg, client=fake).iter_records())
    assert [r.id for r in r2_raw] == [rid, rid]                    # 同 id 兩筆
    assert r2_raw[-1].user_id == GLOBAL_USER_ID and r2_raw[-1].grade == Grade.A

    # ── 行程 2:冷啟動全新 store,從同一 R2 重建 ──
    eng2 = Engine(store=VectorStore(cfg), canonical=R2Canonical(config=cfg, client=fake))
    assert prime_serving_index(eng2, cfg) == 1                     # 去重後 1 筆(非 2)
    assert eng2.store.count() == 1
    rebuilt = {r.id: r for r in eng2.store.iter_records()}[rid]
    assert rebuilt.user_id == GLOBAL_USER_ID                       # 是 global 版,**沒回退**
    assert rebuilt.grade == Grade.A                                # 晉升後的 A 級保住

    # 晉升後是 global → reader(只讀 global)現在讀得到。
    res = do_query(eng2, make_reader_principal(), brew_mechanism="percolation",
                   mode="recommend", origin="Ethiopia", process="washed", roast_agtron=74)
    assert rid in {e["id"] for e in res.get("evidence", [])}


def test_member_write_does_not_reach_global_across_cold_start():
    """member 嘗試寫 global → confine 回自有 ns;冷啟動重建後 global 仍乾淨(reader 讀不到該筆)。"""
    cfg = _prod_cfg()
    fake = FakeR2()
    eng1 = Engine(store=VectorStore(cfg), canonical=R2Canonical(config=cfg, client=fake))
    out = do_log_calibration(eng1, make_member_principal("member:alice", "alice"),
                             brew_mechanism="percolation", grade="A", protocol="SCA_cupping",
                             origin="Ethiopia", process="washed", roast_agtron=74,
                             method="V60", grind_um=651, user_id="global")
    assert out["ok"] is True

    r2_back = list(R2Canonical(config=cfg, client=fake).iter_records())
    assert all(r.user_id != GLOBAL_USER_ID for r in r2_back)       # global 永不被網路寫
    assert all(r.grade != Grade.A for r in r2_back)                # A 也被降為 B(member 上限)

    # 冷啟動重建後,reader(只讀 global)讀不到該 member 自有層記錄。
    eng2 = Engine(store=VectorStore(cfg), canonical=R2Canonical(config=cfg, client=fake))
    prime_serving_index(eng2, cfg)
    res = do_query(eng2, make_reader_principal(), brew_mechanism="percolation",
                   mode="recommend", origin="Ethiopia", process="washed", roast_agtron=74)
    assert res.get("evidence", []) == []                           # global 為空 → reader 無證據


# ────────────────────────────── 持久化順序(durability) ──────────────────────────────

class _RaisingCanonical:
    """模擬 R2 寫入失敗:append 直接拋例外。"""
    def append(self, record):
        raise RuntimeError("R2 寫入失敗(模擬)")

    def extend(self, records):
        raise RuntimeError("R2 寫入失敗(模擬)")

    def iter_records(self):
        return iter(())

    def replace_all(self, records):
        return 0


def test_log_calibration_canonical_failure_blocks_store_upsert():
    """durability 命門:canonical(R2)append 失敗 → 在 store.upsert 前拋例外,
    呼叫端**不會收到假成功**,易失記憶體也不被寫入(回 success ⟹ R2 已有)。"""
    cfg = _prod_cfg()
    eng = Engine(store=VectorStore(cfg), canonical=_RaisingCanonical())
    before = eng.store.count()
    with pytest.raises(RuntimeError):
        eng.log_calibration(_rec("Kenya", grade=Grade.B, user_id="self"))
    assert eng.store.count() == before             # store 未被寫入(順序保證)


# ────────────────────────────── build_app 冷啟動走 prime(非灌種子) ──────────────────────────────

def test_build_app_cold_start_primes_from_r2_not_seed():
    """build_app(memory + R2 cfg, 注入 engine):冷啟動從 R2 載入(prime)而非灌 6 筆種子。

    即便 auto_seed=True,prime 命中(primed 非 None)就不灌種子;store 內容 = R2 真相。
    """
    from server_http import build_app

    cfg = _prod_cfg()
    fake = FakeR2()
    R2Canonical(config=cfg, client=fake).extend([
        _rec("ZZZ-R2-Origin", BrewMechanism.PERCOLATION),
        _rec("Colombia", BrewMechanism.IMMERSION),
    ])
    eng = Engine(store=VectorStore(cfg), canonical=R2Canonical(config=cfg, client=fake))

    app, mcp = build_app(config=cfg, engine=eng, auto_seed=True)
    assert eng.store.count() == 2                   # 從 R2 載入兩筆
    origins = {r.bean.origin for r in eng.store.iter_records()}
    assert "ZZZ-R2-Origin" in origins               # 是 R2 真相,不是 6 筆 seed 錨點
