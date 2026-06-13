"""種子載入:A 級錨點 bootstrap(設計 §8 Phase 1)。

用法:
    python -m cie.seed              # 載入 seeds/anchors.jsonl 到設定的向量庫
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

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


def seed(store: StoreBackend | None = None, path: Path = SEED_PATH) -> int:
    store = store or get_store()
    records = load_records(path)
    n = store.upsert_many(records)
    return n


if __name__ == "__main__":
    store = get_store()
    n = seed(store)
    print(f"已載入 {n} 筆 A 級種子;向量庫現有 {store.count()} 筆。")
