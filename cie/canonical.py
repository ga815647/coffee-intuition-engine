"""Canonical 真相層 — 累積寫入的全量記錄(JSONL),向量庫為其衍生物。

設計鐵則(§14.5 / §15):**canonical 文字記錄是真相,向量可重生。**
記憶體 / Qdrant 後端把 `_canonical` 全量 JSON 塞進 payload,本身即可無損列舉
(見 `store.iter_records`);但 **Vectorize 後端只存精簡 metadata、無法回放全量**,
因此一旦只靠 Vectorize 就「無源」——換嵌入模型(必須重嵌)時無從重建。

本模組補上獨立的 canonical sink:寫向量庫的同時 append 一份真相,
之後可用「當前」嵌入器從 canonical 重嵌、重建索引(見 `cie/rebuild.py`)。

格式與 `seeds/anchors.jsonl`、`portability` 的匯出完全一致:每行一筆 Record 的 JSON。

後端:
  - `LocalJsonlCanonical`:本地 JSONL(預設 `CIE_CANONICAL_PATH`)。
  - `R2Canonical`:Cloudflare R2 物件(選配;缺金鑰不啟用)。read-modify-write
    append(個人規模足夠;非並發安全,見下方註記)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Protocol, runtime_checkable

from .config import CONFIG
from .schema import Record


@runtime_checkable
class CanonicalStore(Protocol):
    """canonical 真相層介面(可插拔)。"""
    def append(self, record: Record) -> None: ...
    def extend(self, records: Iterable[Record]) -> int: ...
    def iter_records(self) -> Iterator[Record]: ...
    def replace_all(self, records: Iterable[Record]) -> int: ...
    def delete(self, record_id: str, user_id: Optional[str] = None) -> int: ...


def _records_to_jsonl(records: Iterable[Record]) -> str:
    """Records → JSONL 文字(每行一筆,尾隨換行)。空輸入回空字串。"""
    lines = [r.model_dump_json() for r in records]
    return ("\n".join(lines) + "\n") if lines else ""


def _jsonl_to_records(text: Optional[str]) -> List[Record]:
    """JSONL 文字 → Records;空 / None 回空清單。"""
    out: List[Record] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            out.append(Record.model_validate_json(line))
    return out


# ────────────────────────────── 本地 JSONL ──────────────────────────────

class LocalJsonlCanonical:
    """本地 append-only JSONL(離線預設)。

    append 直接以 `a` 模式寫一行;iter_records 串流讀回。檔案不存在時視為空。
    """

    def __init__(self, path: str = "", config=CONFIG):
        self.path = Path(path or config.canonical_path)

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: Record) -> None:
        self._ensure_parent()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(record.model_dump_json())
            f.write("\n")

    def extend(self, records: Iterable[Record]) -> int:
        self._ensure_parent()
        n = 0
        with open(self.path, "a", encoding="utf-8") as f:
            for r in records:
                f.write(r.model_dump_json())
                f.write("\n")
                n += 1
        return n

    def replace_all(self, records: Iterable[Record]) -> int:
        """清空後整份重寫(re-init / bootstrap --force / 災後重建)。回傳寫入筆數。

        ⚠️ 會覆蓋累積的校準回饋。bootstrap 是一次性初始化;之後請只用 append/extend。
        """
        self._ensure_parent()
        text = _records_to_jsonl(records)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)
        return text.count("\n")

    def delete(self, record_id: str, user_id: Optional[str] = None) -> int:
        """刪一筆(讀全部 → 濾掉相符 → 整份重寫)。`user_id` 給定時只刪該命名空間自有
        (member confinement:即便 id 猜中,非自有命名空間也刪不掉);None=不限(owner)。
        回傳實際刪除筆數。"""
        if not self.path.exists():
            return 0
        kept: List[Record] = []
        removed = 0
        for r in self.iter_records():
            if r.id == record_id and (user_id is None or r.user_id == user_id):
                removed += 1
                continue
            kept.append(r)
        if removed:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(_records_to_jsonl(kept))
        return removed

    def iter_records(self) -> Iterator[Record]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield Record.model_validate_json(line)


# ────────────────────────────── Cloudflare R2 ──────────────────────────────

class R2Canonical:
    """R2 物件 canonical(選配)。整份 JSONL 存於單一物件。

    R2 物件不支援原生 append,故 append/extend 採 **read-modify-write**:
    取回現有 JSONL → 接上新行 → 整份覆寫。
    ⚠️ 非並發安全:多寫者同時 append 可能互相覆蓋。個人單寫者規模足夠;
    高並發場景應改用每筆獨立物件 + list,或 D1。canonical 仍為真相,最壞情況
    重跑 rebuild 即可從現存物件重建。
    """

    def __init__(self, bucket: str = "", key: str = "", client=None, config=CONFIG):
        from .cfapi import CloudflareClient
        self.bucket = bucket or config.r2_bucket
        self.key = key or config.r2_canonical_key
        self.client = client or CloudflareClient(
            config.cf_account_id, config.cf_api_token,
            config.cf_timeout_s, config.cf_max_retries,
        )

    def _read_text(self) -> Optional[str]:
        return self.client.r2_get_object(self.bucket, self.key)

    def append(self, record: Record) -> None:
        self.extend([record])

    def extend(self, records: Iterable[Record]) -> int:
        new_text = _records_to_jsonl(records)
        if not new_text:
            return 0
        existing = self._read_text() or ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        self.client.r2_put_object(self.bucket, self.key, existing + new_text)
        return new_text.count("\n")

    def replace_all(self, records: Iterable[Record]) -> int:
        """整份覆寫 R2 物件(re-init / bootstrap --force)。回傳寫入筆數。

        ⚠️ 會覆蓋累積的校準回饋。bootstrap 是一次性初始化;之後請只用 append/extend。
        """
        text = _records_to_jsonl(records)
        self.client.r2_put_object(self.bucket, self.key, text)
        return text.count("\n")

    def delete(self, record_id: str, user_id: Optional[str] = None) -> int:
        """刪一筆(read-modify-write:讀整份 → 濾掉相符 → 整份覆寫)。`user_id` 給定時只刪自有。
        ⚠️ 同 append 非並發安全(個人單寫者足夠)。回傳實際刪除筆數。"""
        records = list(self.iter_records())
        kept = [r for r in records
                if not (r.id == record_id and (user_id is None or r.user_id == user_id))]
        removed = len(records) - len(kept)
        if removed:
            self.client.r2_put_object(self.bucket, self.key, _records_to_jsonl(kept))
        return removed

    def iter_records(self) -> Iterator[Record]:
        yield from _jsonl_to_records(self._read_text())


# ────────────────────────────── Cloudflare D1 ──────────────────────────────

class D1Canonical:
    """Cloudflare D1(SQLite-over-HTTP)canonical(生產定案後端)。每筆 Record 一列。

    `payload` 欄存完整 Record JSON(真相);`user_id / grade / mechanism` 去正規化出來供
    WHERE 過濾(如 list_customizations = SELECT WHERE user_id)。寫入用 **INSERT OR REPLACE**
    (id 為主鍵)→ 同 id 後寫者勝(晉升 / 修正天然冪等、不需事後去重),且**逐筆寫、無
    R2 單物件 read-modify-write 的並發 race**:多寫者各寫各列不互相覆蓋。需 token 權限 D1:Edit。

    schema 採**惰性確保**(首次讀寫才 CREATE TABLE/INDEX IF NOT EXISTS):建構不觸網路,
    與 R2Canonical 一致(工廠 / isinstance 測試離線可跑)。
    """

    _COLS = ("id", "user_id", "grade", "mechanism", "payload", "ts")
    # D1 /query 的綁定變數硬上限 = 100(SQLITE_ERROR 7500「too many SQL variables」;
    # 非 SQLite 預設 999)。對真庫實測:100 OK、101 拒。批次插入依此分頁(見 extend)。
    _D1_MAX_VARS = 100

    def __init__(self, database_id: str = "", table: str = "records",
                 client=None, config=CONFIG):
        from .cfapi import CloudflareClient
        self.database_id = database_id or config.d1_database_id
        if not self.database_id:
            raise ValueError("D1Canonical 需 database_id(設 CIE_D1_DATABASE_ID 或傳入)。")
        self.table = table
        self.client = client or CloudflareClient(
            config.cf_account_id, config.cf_api_token,
            config.cf_timeout_s, config.cf_max_retries,
        )
        self._schema_ready = False

    # ── schema(惰性、冪等) ──
    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        self.client.d1_query(
            self.database_id,
            f"CREATE TABLE IF NOT EXISTS {self.table} ("
            "id TEXT PRIMARY KEY, user_id TEXT, grade TEXT, mechanism TEXT, "
            "payload TEXT NOT NULL, ts TEXT)")
        self.client.d1_query(
            self.database_id,
            f"CREATE INDEX IF NOT EXISTS idx_{self.table}_user ON {self.table}(user_id)")
        self._schema_ready = True

    @classmethod
    def _row_params(cls, r: Record) -> List[object]:
        """Record → 一列的位置參數(順序須與 _COLS 一致)。payload 為完整 JSON 真相。"""
        return [r.id, r.user_id, r.grade.value, r.params.brew_mechanism.value,
                r.model_dump_json(), r.timestamp]

    @staticmethod
    def _rows(result) -> List[dict]:
        """從 d1_query 的 result 陣列取第一語句的列(SELECT 用)。"""
        if result and isinstance(result, list):
            return (result[0] or {}).get("results") or []
        return []

    @staticmethod
    def _changes(result) -> int:
        """從 d1_query result 取受影響列數(DELETE/UPDATE 的 meta.changes)。"""
        if result and isinstance(result, list):
            meta = (result[0] or {}).get("meta") or {}
            try:
                return int(meta.get("changes", 0) or 0)
            except (TypeError, ValueError):  # pragma: no cover - 防禦
                return 0
        return 0

    # ── 寫 ──
    def append(self, record: Record) -> None:
        self.extend([record])

    def extend(self, records: Iterable[Record]) -> int:
        rows = list(records)
        if not rows:
            return 0
        self._ensure_schema()
        cols = ", ".join(self._COLS)
        # D1 綁定變數上限 100(見 _D1_MAX_VARS);依欄數分批避免超限。6 欄 → 每批 16 列(96 變數)。
        per = max(1, self._D1_MAX_VARS // len(self._COLS))
        one = "(" + ", ".join(["?"] * len(self._COLS)) + ")"
        n = 0
        for i in range(0, len(rows), per):
            batch = rows[i:i + per]
            placeholders = ", ".join([one] * len(batch))
            sql = f"INSERT OR REPLACE INTO {self.table} ({cols}) VALUES {placeholders}"
            params: List[object] = []
            for r in batch:
                params.extend(self._row_params(r))
            self.client.d1_query(self.database_id, sql, params)
            n += len(batch)
        return n

    def replace_all(self, records: Iterable[Record]) -> int:
        """整份覆寫(re-init / bootstrap --force):清表再批次插入。回傳寫入筆數。

        ⚠️ 會清掉累積的校準回饋。bootstrap 是一次性初始化;之後只用 append/extend。
        """
        self._ensure_schema()
        self.client.d1_query(self.database_id, f"DELETE FROM {self.table}")
        return self.extend(records)

    def delete(self, record_id: str, user_id: Optional[str] = None) -> int:
        """刪一筆。`user_id` 給定時 SQL 加 `AND user_id = ?`——**member 刪除隔離命門**:
        即便 id 猜中,非自有命名空間的列也刪不掉(WHERE 不匹配 → changes=0);
        None=不限(owner 可刪任一,清理語料用)。回傳實際刪除列數(0/1)。"""
        self._ensure_schema()
        if user_id is None:
            result = self.client.d1_query(
                self.database_id, f"DELETE FROM {self.table} WHERE id = ?", [record_id])
        else:
            result = self.client.d1_query(
                self.database_id,
                f"DELETE FROM {self.table} WHERE id = ? AND user_id = ?",
                [record_id, user_id])
        return self._changes(result)

    # ── 讀 ──
    def iter_records(self) -> Iterator[Record]:
        self._ensure_schema()
        result = self.client.d1_query(
            self.database_id, f"SELECT payload FROM {self.table} ORDER BY rowid")
        for row in self._rows(result):
            payload = row.get("payload")
            if payload:
                yield Record.model_validate_json(payload)

    def select_by_user(self, user_id: str) -> List[Record]:
        """SELECT WHERE user_id(供「列某人 self 客製層」之類按租戶過濾)。"""
        self._ensure_schema()
        result = self.client.d1_query(
            self.database_id,
            f"SELECT payload FROM {self.table} WHERE user_id = ? ORDER BY rowid",
            [user_id])
        return [Record.model_validate_json(row["payload"])
                for row in self._rows(result) if row.get("payload")]


# ────────────────────────────── 工廠 ──────────────────────────────

def get_canonical(config=CONFIG) -> CanonicalStore:
    """依設定選 canonical 後端:d1(金鑰+db_id)> r2(金鑰+bucket)> 本地 JSONL。"""
    if config.canonical_backend == "d1":
        return D1Canonical(config=config)
    if config.canonical_backend == "r2":
        return R2Canonical(config=config)
    return LocalJsonlCanonical(config=config)


def maybe_get_canonical(store, config=CONFIG) -> Optional[CanonicalStore]:
    """回傳該後端需要的獨立 canonical sink,否則 None。

    兩種情況需要 sink:
      1. **外部持久 canonical(R2)已設定** → 一律掛上,即便後端有 `iter_records`。
         理由:生產自幹 index 用記憶體後端(`CIE_STORE_BACKEND=memory`),其 `_canonical`
         payload **不跨行程持久化**(Cloud Run scale-to-zero / 重啟即丟)。R2 是單一共用
         真相,owner 本機 stdio 寫 global、member 經 HTTP 寫自有 self 都落同一個 R2 bucket,
         冷啟動再從 R2 重建(見 `rebuild.prime_serving_index`)。漏掉這條,member 的寫入會
         在 scale-to-zero 前丟失。
      2. **後端無法自存 canonical(Vectorize,無 `iter_records`)** → 必須有 sink 才不「無源」。

    其餘(離線開發:記憶體 / Qdrant + 本地 canonical)後端自存 `_canonical`,回 None
    避免重複寫與測試副作用(不會憑空寫出 `./data/canonical.jsonl`)。
    """
    if config.canonical_backend in ("r2", "d1"):   # 外部持久共用真相 → 一律掛 sink
        return get_canonical(config)
    if hasattr(store, "iter_records"):
        return None
    return get_canonical(config)
