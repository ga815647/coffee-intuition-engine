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
    """讀 canonical → 重嵌 → upsert 到 store。回傳寫入筆數。

    對 Vectorize 後端,**先建 metadata index 再寫向量**(`ensure_index`):機制硬分區
    與多租戶 `user_id` 讀過濾都靠 metadata 過濾,而 Vectorize 的索引**不回溯**——
    既有向量不會被新建索引涵蓋。rebuild 是部署/換模型的寫入點,在此確保 user_id 等
    過濾欄已索引,避免 §16.3 self 隔離在 Vectorize 上「過濾失效、靜默 fail-open」。
    """
    store = store or get_store(config)
    canonical = canonical if canonical is not None else get_canonical(config)
    ensure = getattr(store, "ensure_index", None)
    if callable(ensure):
        ensure()  # 冪等;記憶體/Qdrant 無此方法 → 略過。
    records = list(canonical.iter_records())
    return import_records(records, store)


def prime_serving_index(engine, config=CONFIG) -> Optional[int]:
    """冷啟動載入:把共用 canonical(D1 / R2)重建進 `engine` 的 in-memory 索引。回傳重建筆數。

    僅在**生產自幹 index 組合**(記憶體後端 + 外部 canonical:d1 或 r2)觸發。in-memory
    索引在 Cloud Run scale-to-zero / 重啟後丟失,D1/R2 是單一共用真相,故每個容器冷啟動時
    一次性從 canonical 重嵌重建即可(同實例後續請求重用)。owner 本機 stdio 同樣用此載入,
    讀得到 global 全量、可審查晉升。

    非該組合(離線開發、Qdrant、Vectorize 等)回 `None`——由呼叫端決定是否改灌冷啟動種子。
    用 duck-typed `engine`(只取 `engine.store` / `engine.canonical`),不綁 Engine 型別。
    """
    if (config.store_backend == "memory"
            and config.canonical_backend in ("r2", "d1")
            and getattr(engine, "canonical", None) is not None):
        return rebuild(store=engine.store, canonical=engine.canonical, config=config)
    return None


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
