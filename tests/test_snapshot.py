"""備份基建測試 — 全離線(假 D1 / 本地 JSONL),不觸網路。

涵蓋:
  §A global → git(`cie.snapshot`):
    - `export_global`:**只匯出 global**(self 被排除,不入公開 git)、依 **id 確定性排序**
      (重跑位元組相同 → 乾淨 diff)、對 D1 **唯讀**(無 INSERT/DELETE/REPLACE)。
    - `restore_global`:**upsert 還原 global、不清掉 self**(刻意非 replace_all);拒絕非 global 列。
    - **round-trip**:D1-A(global+self)→ export → restore 進 fresh D1-B → global 一致、self 不外洩。
    - 晉升綁定:`do_promote_customization` 成功回傳帶 `snapshot_reminder`(提醒跑 snapshot;工具不碰 git)。
  §B 全量私密備份(`cie.export`):
    - `export_all`:**含 self + global**、依 (user_id,id) 確定性排序、對 D1 唯讀。
"""
from __future__ import annotations

import pytest

from cie.canonical import D1Canonical, LocalJsonlCanonical
from cie.config import Config
from cie.engine import Engine
from cie.export import _all_sorted, export_all
from cie.mcp_principal import (
    GLOBAL_USER_ID, LOCAL_PRINCIPAL, make_member_principal, reset_write_counters,
)
from cie.mcp_tools import do_log_calibration, do_promote_customization
from cie.portability import read_jsonl
from cie.schema import (
    AcidityType, BeanRoast, BrewMechanism, BrewParams, FlavorProfile, Grade, Process, Record,
)
from cie.snapshot import (
    GLOBAL_EXPORT_PATH, _global_sorted, export_global, git_commit_snapshot, restore_global,
)
from cie.store import VectorStore


# ────────────────────────────── 假 D1(精簡版,只實作這些測試用到的子集) ──────────────────────────────

class FakeD1:
    """In-memory D1:CREATE(no-op)/ INSERT OR REPLACE(依 id upsert)/ SELECT / DELETE。
    `calls` 記每句 SQL,供斷言唯讀(export 不得發 INSERT/DELETE)。"""

    _NCOLS = 6  # id, user_id, grade, mechanism, payload, ts

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.calls: list[str] = []

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
            for i in range(0, len(p), self._NCOLS):
                row = dict(zip(cols, p[i:i + self._NCOLS]))
                self.rows.pop(row["id"], None)   # REPLACE → 移末端
                self.rows[row["id"]] = row
            return [{"results": [], "success": True, "meta": {"changes": len(p) // self._NCOLS}}]
        if head.startswith("SELECT"):
            rows = list(self.rows.values())
            if "WHERE USER_ID" in head:
                uid = (params or [None])[0]
                rows = [r for r in rows if r.get("user_id") == uid]
            return [{"results": [{"payload": r["payload"]} for r in rows], "meta": {}}]
        return []

    def writes(self) -> list[str]:
        """資料變動語句(INSERT/REPLACE/DELETE/UPDATE);CREATE IF NOT EXISTS 不算。"""
        return [c for c in self.calls
                if c.strip().upper().split(" ", 1)[0] in ("INSERT", "DELETE", "UPDATE", "REPLACE")]


def _cfg(dim: int = 64) -> Config:
    return Config(cf_account_id="a", cf_api_token="b", d1_database_id="db",
                  canonical_backend_override="d1", store_backend_override="memory",
                  embedding_provider="local", embedding_dim=dim,
                  mcp_auth_token="PRIMARY", mcp_stateless=True)


def _canon(fake: FakeD1, cfg: Config | None = None) -> D1Canonical:
    return D1Canonical(config=cfg or _cfg(), client=fake)


def _rec(origin="Ethiopia", user_id=GLOBAL_USER_ID, grade=Grade.B,
         mech=BrewMechanism.PERCOLATION, rid: str | None = None) -> Record:
    r = Record(
        bean=BeanRoast(origin=origin, process=Process.WASHED, roast_agtron=74),
        params=BrewParams(brew_mechanism=mech, method="V60", water_temp_c=92,
                          brew_ratio=16.0, grind_um=650.0),
        flavor=FlavorProfile(acidity=7.4, acidity_type=AcidityType.CITRIC),
        grade=grade, protocol="SCA_cupping" if grade == Grade.A else "", user_id=user_id,
    )
    return r.model_copy(update={"id": rid}) if rid else r


@pytest.fixture(autouse=True)
def _reset():
    reset_write_counters()
    yield
    reset_write_counters()


# ────────────────────────────── §A export_global ──────────────────────────────

def test_export_global_excludes_self(tmp_path):
    """只匯出 global;各 self 命名空間列**不入檔**(不進公開 git)。"""
    fake = FakeD1()
    canon = _canon(fake)
    canon.extend([
        _rec("Ethiopia", user_id=GLOBAL_USER_ID),
        _rec("Kenya", user_id="alice"),        # self
        _rec("Brazil", user_id=GLOBAL_USER_ID),
        _rec("Colombia", user_id="bob"),       # self
    ])
    out = tmp_path / "global.export.jsonl"
    n = export_global(canonical=canon, path=out)
    assert n == 2
    recs = read_jsonl(out)
    assert {r.user_id for r in recs} == {GLOBAL_USER_ID}
    assert {r.bean.origin for r in recs} == {"Ethiopia", "Brazil"}


def test_export_global_deterministic_id_order(tmp_path):
    """依 id 排序 → 與插入序無關、重跑位元組相同(乾淨 diff)。"""
    fake = FakeD1()
    canon = _canon(fake)
    canon.extend([
        _rec("A", rid="ccc"), _rec("B", rid="aaa"), _rec("C", rid="bbb"),
    ])
    out = tmp_path / "g.jsonl"
    export_global(canonical=canon, path=out)
    ids = [r.id for r in read_jsonl(out)]
    assert ids == ["aaa", "bbb", "ccc"]              # 排序,非插入序
    first = out.read_bytes()
    export_global(canonical=canon, path=out)         # 重跑
    assert out.read_bytes() == first                 # 位元組相同


def test_export_global_is_read_only_on_d1(tmp_path):
    """命門:匯出對 D1 唯讀 — 不發任何 INSERT/DELETE/UPDATE/REPLACE。"""
    fake = FakeD1()
    canon = _canon(fake)
    canon.extend([_rec("Ethiopia"), _rec("Kenya", user_id="alice")])
    fake.calls.clear()                               # 只看匯出階段
    export_global(canonical=canon, path=tmp_path / "g.jsonl")
    assert fake.writes() == []                       # 零資料變動語句
    assert any(c.strip().upper().startswith("SELECT") for c in fake.calls)


def test_global_sorted_helper_filters_and_orders():
    recs = [_rec("x", user_id="alice", rid="2"), _rec("y", rid="9"),
            _rec("z", rid="1"), _rec("w", user_id="bob", rid="0")]
    out = _global_sorted(recs)
    assert [r.id for r in out] == ["1", "9"]          # 只 global、依 id


# ────────────────────────────── §A restore_global ──────────────────────────────

def test_restore_global_upsert_does_not_clobber_self(tmp_path):
    """復原 global **不動 self**:dst 既有 self 列在 restore 後仍在(extend=upsert,非 replace_all)。"""
    # 寫一份 global-only 匯出檔。
    src = _canon(FakeD1())
    src.extend([_rec("Ethiopia", rid="g1"), _rec("Kenya", rid="g2")])
    out = tmp_path / "g.jsonl"
    export_global(canonical=src, path=out)

    # dst 已有一筆 self(alice)+ 一筆舊 global。
    dst_fake = FakeD1()
    dst = _canon(dst_fake)
    dst.extend([_rec("Old", user_id="alice", rid="a1"), _rec("OldG", rid="g1")])

    n = restore_global(canonical=dst, path=out)
    assert n == 2
    back = {r.id: r for r in dst.iter_records()}
    assert "a1" in back and back["a1"].user_id == "alice"   # self 未被清
    assert back["g1"].bean.origin == "Ethiopia"             # 舊 global 被 upsert 覆寫
    assert "g2" in back


def test_restore_global_rejects_non_global(tmp_path):
    """檔內混入非 global 列 → 拒絕(避免把錯誤命名空間灌進真相)。"""
    bad = tmp_path / "bad.jsonl"
    from cie.portability import export_jsonl
    export_jsonl([_rec("X", rid="g"), _rec("Y", user_id="alice", rid="s")], bad)
    with pytest.raises(ValueError):
        restore_global(canonical=_canon(FakeD1()), path=bad)


def test_round_trip_global_d1_to_export_to_fresh_d1(tmp_path):
    """round-trip:D1-A(global+self)→ export → restore 進 **fresh** D1-B → global 一致、self 不外洩。"""
    a = _canon(FakeD1())
    a.extend([
        _rec("Ethiopia", rid="g1"), _rec("Kenya", rid="g2"),
        _rec("Secret", user_id="alice", rid="s1"),     # self,不該出現在 B
    ])
    out = tmp_path / "g.jsonl"
    assert export_global(canonical=a, path=out) == 2

    b = _canon(FakeD1())                               # 全新(模擬 D1 被誤刪後重建)
    assert restore_global(canonical=b, path=out) == 2
    b_recs = {r.id: r for r in b.iter_records()}
    assert set(b_recs) == {"g1", "g2"}                 # 只 global 還原
    assert all(r.user_id == GLOBAL_USER_ID for r in b_recs.values())
    assert "s1" not in b_recs                          # self 個資未外洩到快照/復原

    # 對 D1 的 INSERT OR REPLACE → 再 restore 一次冪等(不重複)。
    assert restore_global(canonical=b, path=out) == 2
    assert set(r.id for r in b.iter_records()) == {"g1", "g2"}


def test_restore_global_into_local_jsonl(tmp_path):
    """後端無關:LocalJsonlCanonical 也能被 export/restore(離線開發路徑)。"""
    src = LocalJsonlCanonical(path=str(tmp_path / "canon.jsonl"))
    src.extend([_rec("Ethiopia", rid="g1"), _rec("Self", user_id="alice", rid="s1")])
    out = tmp_path / "g.jsonl"
    assert export_global(canonical=src, path=out) == 1
    dst = LocalJsonlCanonical(path=str(tmp_path / "dst.jsonl"))
    assert restore_global(canonical=dst, path=out) == 1
    assert [r.id for r in dst.iter_records()] == ["g1"]


# ────────────────────────────── §A 晉升綁定:snapshot_reminder ──────────────────────────────

def test_promote_returns_snapshot_reminder(tmp_path):
    """晉升成功 → 回傳帶 `snapshot_reminder`(提醒跑 cie.snapshot);工具本身不碰 git。"""
    cfg = _cfg()
    fake = FakeD1()
    eng = Engine(store=VectorStore(cfg), canonical=_canon(fake, cfg))
    # member alice 寫一筆 self → owner 晉升為 global。
    logged = do_log_calibration(eng, make_member_principal("member:alice", "alice"),
                                brew_mechanism="percolation", grade="C",
                                origin="Ethiopia", process="washed", roast_agtron=74,
                                method="V60", grind_um=650, acidity=7.4, user_id="self")
    rid = logged["id"]
    out = do_promote_customization(eng, LOCAL_PRINCIPAL, record_id=rid,
                                   grade="B", protocol="")
    assert out["ok"] is True
    assert "snapshot_reminder" in out and "cie.snapshot" in out["snapshot_reminder"]


def test_promote_via_member_has_no_reminder_and_is_blocked():
    """非 owner 走晉升 → 被擋(無 snapshot_reminder)。"""
    cfg = _cfg()
    eng = Engine(store=VectorStore(cfg), canonical=_canon(FakeD1(), cfg))
    out = do_promote_customization(eng, make_member_principal("member:bob", "bob"),
                                   record_id="whatever", grade="B")
    assert out["ok"] is False
    assert "snapshot_reminder" not in out


# ────────────────────────────── §B export_all(全量私密備份) ──────────────────────────────

def test_export_all_includes_self_and_global(tmp_path):
    """全量 dump 含 global + 各 self;依 (user_id,id) 排序(命名空間分群、確定性)。"""
    fake = FakeD1()
    canon = _canon(fake)
    canon.extend([
        _rec("Ethiopia", user_id=GLOBAL_USER_ID, rid="g2"),
        _rec("A", user_id="alice", rid="a1"),
        _rec("B", user_id="bob", rid="b1"),
        _rec("Brazil", user_id=GLOBAL_USER_ID, rid="g1"),
        _rec("A2", user_id="alice", rid="a0"),
    ])
    out = tmp_path / "full.jsonl"
    n = export_all(canonical=canon, path=out)
    assert n == 5
    recs = read_jsonl(out)
    assert [(r.user_id, r.id) for r in recs] == [
        ("alice", "a0"), ("alice", "a1"), ("bob", "b1"),
        (GLOBAL_USER_ID, "g1"), (GLOBAL_USER_ID, "g2"),
    ]
    assert {r.user_id for r in recs} == {GLOBAL_USER_ID, "alice", "bob"}


def test_export_all_is_read_only_and_deterministic(tmp_path):
    fake = FakeD1()
    canon = _canon(fake)
    canon.extend([_rec("Ethiopia"), _rec("Self", user_id="alice", rid="s1")])
    fake.calls.clear()
    out = tmp_path / "full.jsonl"
    export_all(canonical=canon, path=out)
    assert fake.writes() == []                       # 唯讀
    first = out.read_bytes()
    export_all(canonical=canon, path=out)
    assert out.read_bytes() == first                 # 確定性


def test_all_sorted_helper_groups_by_user_then_id():
    recs = [_rec("x", user_id="bob", rid="1"), _rec("y", rid="9"),
            _rec("z", user_id="alice", rid="5"), _rec("w", rid="2")]
    assert [(r.user_id, r.id) for r in _all_sorted(recs)] == [
        ("alice", "5"), ("bob", "1"), (GLOBAL_USER_ID, "2"), (GLOBAL_USER_ID, "9")]


# ────────────────────────────── 預設路徑常數 sanity ──────────────────────────────

def test_git_commit_snapshot_noop_outside_repo(tmp_path):
    """git 副作用防呆:非 git 工作區 → 回 False、不拋例外(匯出仍已落檔,可手動 commit)。"""
    f = tmp_path / "g.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    assert git_commit_snapshot(path=f, count=1, cwd=tmp_path) is False


def test_export_paths_are_where_docs_say():
    assert GLOBAL_EXPORT_PATH.name == "global.export.jsonl"
    assert GLOBAL_EXPORT_PATH.parent.name == "corpus"   # 公開 git
    from cie.export import DEFAULT_EXPORT_PATH
    assert DEFAULT_EXPORT_PATH.parent.name == "backups"  # gitignored 私密
