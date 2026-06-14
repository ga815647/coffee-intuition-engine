"""回歸:qdrant 點 id 必為 UUID,但 owner 策展條目可有刻意固定的可讀 id
(如 contested-acidity-direction-ucdavis)。`store.point_id` 把非 UUID 的 id 決定性映成
合法點 id,canonical 真實 id 不變。

防的 bug(實際發生過):單一可讀 id 讓 `upsert_many` 的 all-or-nothing qdrant upsert 整批崩潰
→ prime_serving_index 失敗 → 冷啟動 serving 索引全空 → 所有查詢退回物理先驗。
"""
from __future__ import annotations

import uuid

from cie.config import Config
from cie.schema import (
    AcidityType, BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process, Record,
)
from cie.store import VectorStore, point_id


def _mem_store(dim=256):
    return VectorStore(Config(embedding_provider="local", embedding_dim=dim))


def _rec(rid=None, origin="Ethiopia", mech=BrewMechanism.PERCOLATION, user_id="global"):
    kw = {} if rid is None else {"id": rid}
    return Record(
        bean=BeanRoast(origin=origin, variety="", process=Process.WASHED, roast_agtron=74),
        params=BrewParams(brew_mechanism=mech, method="V60", water_temp_c=92, brew_ratio=16.0),
        flavor=FlavorProfile(acidity=7.5, acidity_type=AcidityType.CITRIC),
        grade=Grade.B, protocol="x", user_id=user_id, **kw,
    )


def test_point_id_passthrough_for_uuid():
    rid = str(uuid.uuid4())
    assert point_id(rid) == rid  # 已是 UUID → 正規化原樣(冪等)
    assert point_id(point_id("contested-acidity-direction-ucdavis")) == point_id(
        point_id("contested-acidity-direction-ucdavis"))  # 第二次餵自身輸出仍穩定


def test_point_id_deterministic_for_non_uuid():
    a = point_id("contested-acidity-direction-ucdavis")
    b = point_id("contested-acidity-direction-ucdavis")
    assert a == b                       # 決定性
    uuid.UUID(a)                        # 是合法 UUID(qdrant 收得下),否則 raise


def test_non_uuid_id_does_not_break_batch_upsert():
    """關鍵回歸:可讀 id 與 UUID id 混批,all-or-nothing upsert 不該崩。"""
    store = _mem_store()
    recs = [
        _rec("contested-acidity-direction-ucdavis", mech=BrewMechanism.PERCOLATION),
        _rec(None, origin="Kenya", mech=BrewMechanism.PERCOLATION),
        _rec(None, origin="Colombia", mech=BrewMechanism.PERCOLATION),
    ]
    assert store.upsert_many(recs) == 3
    assert store.count() == 3            # 沒有一筆被靜默丟掉


def test_search_returns_real_id_not_point_id():
    """evidence 顯示的 id 必須是真實 canonical id(才對得上 delete/promote)。"""
    store = _mem_store()
    store.upsert(_rec("contested-acidity-direction-ucdavis", mech=BrewMechanism.PERCOLATION))
    hits = store.search("偏酸 percolation", BrewMechanism.PERCOLATION)
    assert hits
    assert any(h["id"] == "contested-acidity-direction-ucdavis" for h in hits)


def test_delete_by_real_id_hits_non_uuid_record():
    store = _mem_store()
    store.upsert(_rec("contested-acidity-direction-ucdavis", mech=BrewMechanism.PERCOLATION))
    assert store.count() == 1
    # owner 刪(user_id=None 不限);用真實可讀 id
    assert store.delete("contested-acidity-direction-ucdavis") == 1
    assert store.count() == 0


def test_member_confined_delete_still_respects_namespace():
    store = _mem_store()
    store.upsert(_rec("contested-acidity-direction-ucdavis", user_id="global",
                      mech=BrewMechanism.PERCOLATION))
    # member(self=alice)不可刪 global 條目,即使知道其 id
    assert store.delete("contested-acidity-direction-ucdavis", user_id="alice") == 0
    assert store.count() == 1
