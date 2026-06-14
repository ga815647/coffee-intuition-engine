"""PR6 冷啟動完整性護欄:防「一筆壞記錄 → serving 索引靜默歸零 → 全退物理先驗、/health 仍 200」。

兩道結構防線(root cause 已由 store.point_id 修;本檔測拆彈後的失效模式):
  Item 1 — `upsert_many(skip_errors=True)`:批次失敗降級逐筆隔離,壞記錄 log + skip、好記錄照進,
           絕不靜默(每筆失敗印 id),也絕不歸零整批。預設 skip_errors=False 仍 fail loud。
  Item 2 — `prime_serving_index` 載入後比對 store 筆數 vs canonical 應載入量;落差超門檻
           → `ServingIndexIntegrityError` fail-closed 拒啟動(Cloud Run 續用舊健康版)。
           空-canonical 合法不誤殺;少量 skip 在門檻內容忍。
"""
from __future__ import annotations

import logging

import pytest

from cie.canonical import R2Canonical
from cie.config import Config
from cie.engine import Engine
from cie.rebuild import ServingIndexIntegrityError, prime_serving_index
from cie.schema import (
    AcidityType, BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process, Record,
)
from cie.store import VectorStore, point_id


# ────────────────────────────── 共用建構 ──────────────────────────────

def _mem_store(dim: int = 128) -> VectorStore:
    return VectorStore(Config(embedding_provider="local", embedding_dim=dim))


def _prod_cfg(dim: int = 64) -> Config:
    """生產組合:CF 金鑰 + R2 bucket(→ canonical_backend=r2)+ 記憶體向量庫(override)。"""
    return Config(
        cf_account_id="acct", cf_api_token="tok", r2_bucket="cie-canon",
        store_backend_override="memory",
        embedding_provider="local", embedding_dim=dim,
        mcp_auth_token="PRIMARY", mcp_stateless=True,
    )


def _rec(origin: str = "Ethiopia", mech: BrewMechanism = BrewMechanism.PERCOLATION,
         rid=None) -> Record:
    kw = {} if rid is None else {"id": rid}
    return Record(
        bean=BeanRoast(origin=origin, variety="Heirloom", process=Process.WASHED, roast_agtron=74),
        params=BrewParams(brew_mechanism=mech, method="V60", water_temp_c=92, brew_ratio=16.0,
                          grind_um=650.0, tds_pct=1.38, ey_pct=20.4),
        flavor=FlavorProfile(acidity=7.5, acidity_type=AcidityType.CITRIC),
        grade=Grade.B, protocol="x", user_id="global", **kw,
    )


class FakeR2:
    """跨「行程」共用的單一 R2 真相物件(只實作 R2Canonical 用到的 get/put)。"""

    def __init__(self):
        self.objects: dict = {}

    def r2_get_object(self, bucket, key):
        return self.objects.get((bucket, key))

    def r2_put_object(self, bucket, key, body, content_type="application/x-ndjson"):
        self.objects[(bucket, key)] = body


def _poison_upsert(store: VectorStore, poison_pid: str):
    """把 store.client.upsert 包成:批次含某「壞點 id」即丟例外(模擬 qdrant 拒收單一壞點)。"""
    real = store.client.upsert

    def flaky(*, collection_name, points, **kw):
        if any(p.id == poison_pid for p in points):
            raise RuntimeError("qdrant 拒收(模擬壞點)")
        return real(collection_name=collection_name, points=points, **kw)

    store.client.upsert = flaky


# ────────────────────────────── Item 1:upsert_many 對單筆壞記錄 resilient ──────────────────────────────

def test_skip_errors_isolates_bad_record_and_keeps_the_good(caplog):
    """混合批(1 筆注入會失敗 + 2 筆好):skip_errors=True → 載入 2、壞的跳過、count≠0、有 WARNING。

    這正是 c3aff37 後實際發生的失效模式(單一壞記錄炸掉 all-or-nothing 批次)被拆彈的證明。
    """
    store = _mem_store()
    poison_pid = point_id("POISON-RECORD")
    _poison_upsert(store, poison_pid)

    recs = [_rec(rid="POISON-RECORD"), _rec(origin="Kenya"), _rec(origin="Colombia")]
    with caplog.at_level(logging.WARNING, logger="cie.store"):
        loaded = store.upsert_many(recs, skip_errors=True)

    assert loaded == 2
    assert store.count() == 2                       # 好記錄照進,沒有歸零整批
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("跳過壞記錄" in m and "POISON-RECORD" in m for m in msgs)   # 絕不靜默:印了 id
    assert any("逐筆隔離" in m for m in msgs)                              # 有 (loaded, skipped) 彙總


def test_strict_default_raises_and_writes_nothing():
    """預設 skip_errors=False:單一壞記錄 → 整批 fail loud(raise),且全有全無不留半筆。

    正常寫入(member log_calibration)走這條:錯誤要浮上來,不該被靜默吞掉。
    """
    store = _mem_store()
    _poison_upsert(store, point_id("POISON-RECORD"))
    recs = [_rec(rid="POISON-RECORD"), _rec(origin="Kenya")]
    with pytest.raises(RuntimeError):
        store.upsert_many(recs)                     # skip_errors 預設 False
    assert store.count() == 0                        # all-or-nothing:批次失敗即一筆不留


# ────────────────────────────── Item 2:冷啟動斷言 serving 筆數 ≈ canonical ──────────────────────────────

def test_prime_fail_closed_on_severe_shortfall():
    """D1/R2 有料但 index 嚴重短缺(全 upsert 失敗 → 全 skip)→ 啟動斷言 raise(fail-closed)。"""
    cfg = _prod_cfg()
    canon = R2Canonical(config=cfg, client=FakeR2())
    canon.extend([_rec(f"Origin{i}", BrewMechanism.PERCOLATION) for i in range(5)])
    eng = Engine(store=VectorStore(cfg), canonical=canon)

    def always_fail(*a, **k):
        raise RuntimeError("qdrant 不可用(模擬)")

    eng.store.client.upsert = always_fail            # 逐筆隔離也全跳 → served=0
    with pytest.raises(ServingIndexIntegrityError):
        prime_serving_index(eng, cfg)
    assert eng.serving_canonical_count == 5          # 落差基準仍被暫存(供 /health 可見)


def test_prime_empty_canonical_does_not_fail_closed():
    """空-canonical(全新 / 未 bootstrap)→ expected=0 → 不誤殺(0 < 0.9×0 不成立)。"""
    cfg = _prod_cfg()
    eng = Engine(store=VectorStore(cfg), canonical=R2Canonical(config=cfg, client=FakeR2()))
    assert prime_serving_index(eng, cfg) == 0        # 不 raise
    assert eng.store.count() == 0
    assert eng.serving_canonical_count == 0


def test_prime_full_load_passes_and_stashes_count():
    """健康全載:served == expected → 不 raise,且把 canonical 基準暫存供 /health。"""
    cfg = _prod_cfg()
    canon = R2Canonical(config=cfg, client=FakeR2())
    canon.extend([_rec("Ethiopia", BrewMechanism.PERCOLATION),
                  _rec("Kenya", BrewMechanism.PERCOLATION)])
    eng = Engine(store=VectorStore(cfg), canonical=canon)
    assert prime_serving_index(eng, cfg) == 2
    assert eng.store.count() == 2
    assert eng.serving_canonical_count == 2


def test_prime_tolerates_small_skip_within_threshold():
    """少量 skip(門檻內,11/12 ≥ 0.9×12)→ 不 fail-closed:容忍個別壞記錄,只攔空 / 嚴重短缺。"""
    cfg = _prod_cfg()
    canon = R2Canonical(config=cfg, client=FakeR2())
    recs = [_rec(f"Origin{i}", BrewMechanism.PERCOLATION) for i in range(12)]
    canon.extend(recs)
    eng = Engine(store=VectorStore(cfg), canonical=canon)
    _poison_upsert(eng.store, point_id(recs[0].id))  # 只毒一筆

    loaded = prime_serving_index(eng, cfg)           # 11 ≥ 10.8 → 不該 raise
    assert loaded == 11
    assert eng.store.count() == 11
