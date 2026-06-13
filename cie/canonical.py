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

    def iter_records(self) -> Iterator[Record]:
        yield from _jsonl_to_records(self._read_text())


# ────────────────────────────── 工廠 ──────────────────────────────

def get_canonical(config=CONFIG) -> CanonicalStore:
    """依設定選 canonical 後端:有 CF 金鑰 + R2 bucket → R2;否則本地 JSONL。"""
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
    if config.canonical_backend == "r2":
        return get_canonical(config)
    if hasattr(store, "iter_records"):
        return None
    return get_canonical(config)
