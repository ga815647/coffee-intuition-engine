"""種子載入:A 級錨點 bootstrap(設計 §8 Phase 1)。

用法:
    python -m cie.seed              # 載入 seeds/anchors.jsonl 到設定的向量庫
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from .canonical import CanonicalStore, maybe_get_canonical
from .schema import Record
from .store import StoreBackend, get_store

SEED_PATH = Path(__file__).resolve().parent.parent / "seeds" / "anchors.jsonl"


def load_records(path: Path = SEED_PATH) -> List[Record]:
    records: List[Record] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(Record.model_validate(json.loads(line)))
    return records


def seed(store: StoreBackend | None = None, path: Path = SEED_PATH,
         canonical: Optional[CanonicalStore] = None) -> int:
    """灌種子到向量庫;若提供 canonical sink,同步 append 一份真相。

    注意:再次 seed 會於 canonical 重複 append(JSONL 無依 id 去重);種子通常只灌一次。
    """
    store = store or get_store()
    records = load_records(path)
    n = store.upsert_many(records)
    if canonical is not None:
        canonical.extend(records)
    return n


if __name__ == "__main__":
    store = get_store()
    # Vectorize 等無法自存 canonical 的後端需獨立 sink;記憶體/Qdrant 回 None。
    canonical = maybe_get_canonical(store)
    n = seed(store, canonical=canonical)
    where = "" if canonical is None else "(已雙寫 canonical 真相層)"
    print(f"已載入 {n} 筆 A 級種子;向量庫現有 {store.count()} 筆。{where}")
