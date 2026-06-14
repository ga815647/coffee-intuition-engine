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
from .schema import Grade
from .store import StoreBackend, get_store


class ServingIndexIntegrityError(RuntimeError):
    """冷啟動後 serving 索引筆數遠低於 canonical 應載入量 → fail-closed 拒啟動。

    與 `validate_guest_token_config` 同款 boot-time fail-closed:寧可整個 revision 起不來
    (Cloud Run 不切流量、續用舊健康版),也不要一個殘缺 / 空索引在 `/health` 回 200 的偽裝下
    把所有查詢靜默退回物理先驗(PR6:防「靜默空 / 短 index」)。
    """


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


def prime_serving_index(engine, config=CONFIG, *,
                        assert_integrity: bool = True,
                        min_ratio: float = 0.9) -> Optional[int]:
    """冷啟動載入:把共用 canonical(D1 / R2)重建進 `engine` 的 in-memory 索引。回傳重建筆數。

    僅在**生產自幹 index 組合**(記憶體後端 + 外部 canonical:d1 或 r2)觸發。in-memory
    索引在 Cloud Run scale-to-zero / 重啟後丟失,D1/R2 是單一共用真相,故每個容器冷啟動時
    一次性從 canonical 重嵌重建即可(同實例後續請求重用)。owner 本機 stdio 同樣用此載入,
    讀得到 global 全量、可審查晉升。

    非該組合(離線開發、Qdrant、Vectorize 等)回 `None`——由呼叫端決定是否改灌冷啟動種子。
    用 duck-typed `engine`(只取 `engine.store` / `engine.canonical`),不綁 Engine 型別。

    PR6 完整性護欄:
      - 用 `skip_errors=True` 載入 → 單一壞記錄不會讓整批歸零(`store.upsert_many` 降級逐筆隔離)。
      - 載入後比對 `store.count()` vs canonical **非-prediction 去重**應載入量;落差超過門檻
        (`served < min_ratio × expected`)→ 丟 `ServingIndexIntegrityError` **fail-closed 拒啟動**。
        以「落差比例」而非絕對值觸發:容忍少量 skip,但攔住「空 / 嚴重短缺」索引切到流量。
        空-canonical(expected=0)合法:`0 < min_ratio×0` 不成立 → 不誤殺全新 / 未 bootstrap 部署。
      - 把 expected 暫存到 `engine.serving_canonical_count`,讓 `/health` 隨時可見落差
        (即便沒觸發 fail-closed)。
    """
    if not (config.store_backend == "memory"
            and config.canonical_backend in ("r2", "d1")
            and getattr(engine, "canonical", None) is not None):
        return None

    records = list(engine.canonical.iter_records())
    loaded = import_records(records, engine.store, skip_errors=True)
    # 對齊基準:非-prediction(鐵則5:預測不入真相 / 不該占 serving 索引)+ 同 id 去重(避免
    # append-only 重複 id / 晉升雙版造成假落差)。canonical 照鐵則本就無 prediction,過濾為防禦。
    expected = len({r.id for r in records if r.grade != Grade.PREDICTION})
    served = engine.store.count()
    try:  # 供 /health 顯示(duck-typed:engine 可能非 Engine,失敗忽略)
        engine.serving_canonical_count = expected
    except Exception:  # pragma: no cover - 防禦
        pass

    if assert_integrity and served < min_ratio * expected:
        raise ServingIndexIntegrityError(
            f"冷啟動 serving 索引完整性不足:索引 {served} 筆 < {min_ratio:.0%} × canonical "
            f"應載入 {expected} 筆(import 回報 {loaded})。疑似大量壞記錄被跳過或 prime 部分失敗;"
            f"fail-closed 拒啟動,避免殘缺索引把所有查詢靜默退回物理先驗(舊健康 revision 續服務)。"
        )
    return loaded


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
