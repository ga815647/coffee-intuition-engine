"""從 canonical 真相層重建向量索引(用『當前』嵌入器重嵌)。

    python -m cie.rebuild

何時用:換嵌入模型(維度/語意空間不同,必須全庫重嵌)、換後端、或災後重建。
流程:讀 canonical 全量 → 以當前 `Embedder` 重新嵌入 → upsert 到目標向量庫。

鐵則(§14.5):**一律重嵌、不搬舊向量**(不同模型向量不可混用);
canonical 為真相、向量為衍生物。對 Vectorize 這種「無法自存 canonical」的後端,
本路徑就是它的還原點(canonical 來自 R2 或本地 JSONL)。

記憶體 / Qdrant 後端自帶 `_canonical`,亦可改走 `portability.export_store` + `import_jsonl`。
"""
from __future__ import annotations

import sys
from typing import Optional

from .canonical import CanonicalStore, get_canonical
from .config import CONFIG
from .portability import import_records
from .store import StoreBackend, get_store


def rebuild(store: Optional[StoreBackend] = None,
            canonical: Optional[CanonicalStore] = None,
            config=CONFIG) -> int:
    """讀 canonical → 重嵌 → upsert 到 store。回傳寫入筆數。"""
    store = store or get_store(config)
    canonical = canonical if canonical is not None else get_canonical(config)
    records = list(canonical.iter_records())
    return import_records(records, store)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows 主控台 UTF-8
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    store = get_store()
    canonical = get_canonical()
    n = rebuild(store=store, canonical=canonical)
    print(f"已從 canonical 重建 {n} 筆;向量庫現有 {store.count()} 筆。嵌入器: {store.model_id}")


if __name__ == "__main__":
    main()
