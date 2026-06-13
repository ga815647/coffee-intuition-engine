"""可攜性:canonical JSONL 匯出 / 匯入。

設計鐵則(§14.5):**canonical 文字記錄是真相,向量是可重生的衍生物。**
切換嵌入模型 / 換機 / 雲端↔本地搬遷時,一律用『重新嵌入』而非搬運舊向量
——不同模型的向量空間不可混用(嵌入器一致性鐵則)。因此匯入流程是
『讀 JSONL → 用當前嵌入器重嵌 → upsert』,跨模型 / 跨後端都不壞。

JSONL 格式與 `seeds/anchors.jsonl` 相同:每行一筆 Record 的 JSON。
這也是 Cloudflare 託管下 R2/D1 canonical 的格式,可用來重建 Vectorize 索引。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List, Union

from .schema import Record

PathLike = Union[str, Path]


def export_jsonl(records: Iterable[Record], path: PathLike) -> int:
    """把 Records 全量寫成 JSONL(canonical 真相格式)。回傳寫入筆數。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(r.model_dump_json())
            f.write("\n")
            n += 1
    return n


def read_jsonl(path: PathLike) -> List[Record]:
    """讀 JSONL → Record 清單(不觸碰向量庫)。"""
    records: List[Record] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(Record.model_validate(json.loads(line)))
    return records


def import_records(records: List[Record], store) -> int:
    """用 store 的『當前嵌入器』重新嵌入 records → upsert。回傳寫入筆數。

    換模型 / 換後端 / 重建索引的核心:**一律重嵌、不搬舊向量**(嵌入器一致性鐵則)。
    來源可以是 JSONL 路徑(`import_jsonl`)或 canonical 真相層(`cie.rebuild`)。
    """
    return store.upsert_many(records)


def import_jsonl(path: PathLike, store) -> int:
    """讀 JSONL → 用 store 的『當前嵌入器』重新嵌入 → upsert。回傳寫入筆數。

    這是換模型 / 換後端 / 重建索引的標準路徑:canonical 不變,向量重生。
    """
    return import_records(read_jsonl(path), store)


def export_store(store, path: PathLike) -> int:
    """從支援全量列舉的後端(記憶體 / Qdrant)匯出 canonical JSONL。

    Vectorize 後端不支援全庫掃描(canonical 設計上存於 R2/D1,非索引內);
    其匯出請直接讀 R2/D1 的 JSONL,或對 import 來源 JSONL 留存版本。
    """
    if not hasattr(store, "iter_records"):
        raise NotImplementedError(
            f"{type(store).__name__} 不支援全量列舉;"
            "canonical 請從 R2/D1 的 JSONL 匯出(向量為衍生物,從 JSONL 重建)。"
        )
    return export_jsonl(store.iter_records(), path)
