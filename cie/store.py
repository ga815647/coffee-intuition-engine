"""向量庫 — 可插拔後端:Vectorize(Cloudflare)/ Qdrant / 記憶體。

選擇(見 config.store_backend):
  - 有 CF 金鑰 + Vectorize index → VectorizeStore(雲端持久化)
  - 有 CIE_QDRANT_URL            → VectorStore(Qdrant Cloud)
  - 皆無                         → VectorStore(":memory:",開發預設,離線)

混合記錄(設計 §4.1):
  vector  = 情境文字嵌入(模糊召回)
  payload/metadata = 結構化欄位(機制硬過濾 + 物理距離 + 分級加權)

鐵則:
  - brew_mechanism 為**硬分區鍵**,召回必過濾,永不跨機制混合。
  - 分級加權與物理距離在召回後於 retrieval 程式層計算(後端只負責過濾 + kNN)。
  - 不同嵌入模型向量不可混用;canonical 為 JSONL 真相,向量為衍生物(見 portability)。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterator, List, Optional, Protocol

from .config import CONFIG
from .embedding import LocalHashEmbedder, get_embedder
from .schema import BrewMechanism, Record

logger = logging.getLogger("cie.store")


# ────────────────────────────── 共用 payload 建構 ──────────────────────────────

def record_to_payload(r: Record) -> Dict[str, Any]:
    """把 Record 攤平成結構化 payload(機制過濾 + 物理距離 + 分級加權用)。

    後端無關的精簡欄位;Qdrant 另存 `_canonical` 全量、Vectorize 另作 metadata 淨化。
    """
    return {
        "brew_mechanism": r.params.brew_mechanism.value,
        "method": r.params.method,
        "process": r.bean.process.value,
        "roast_band": r.bean.roast_band(),
        "roast_agtron": r.bean.roast_agtron,
        "origin": r.bean.origin,
        # 參數數值(供物理距離 / 收縮)
        "water_temp_c": r.params.water_temp_c,
        "brew_ratio": r.params.brew_ratio,
        "grind_um": r.params.grind_um,
        "contact_time_s": r.params.contact_time_s,
        "tds_pct": r.params.tds_pct,
        "ey_pct": r.params.ey_pct,
        # 風味軸
        **{f"flavor_{k}": v for k, v in r.flavor.axis_vector().items()},
        "flavor_notes": r.flavor.flavor_notes,
        "defects": r.flavor.defects,
        # 來源
        "grade": r.grade.value,
        "confidence": r.confidence,
        "user_id": r.user_id,
        "protocol": r.protocol,
        "timestamp": r.timestamp,
    }


class StoreBackend(Protocol):
    """所有向量庫後端的共同介面(鴨子型別,可插拔)。"""
    model_id: str
    def upsert(self, record: Record) -> str: ...
    def upsert_many(self, records: List[Record]) -> int: ...
    def search(self, query_text: str, mechanism: BrewMechanism, top_k: int = 20,
               process: Optional[str] = None, roast_band: Optional[str] = None,
               exclude_predictions: bool = True,
               user_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]: ...
    def count(self) -> int: ...


# ────────────────────────────── Qdrant / 記憶體後端 ──────────────────────────────

class VectorStore:
    """Qdrant 後端(url 留空 = 記憶體模式,離線開發預設)。

    同時作為向後相容的型別:既有程式 `VectorStore()` 仍取得 Qdrant/記憶體後端。
    """

    def __init__(self, config=CONFIG, embedder=None):
        from qdrant_client import QdrantClient  # 延遲匯入:Vectorize-only 部署可不裝
        from qdrant_client.http import models as qm

        self._qm = qm
        self.config = config
        # 可注入嵌入器(如 CachingEmbedder),供 eval 跨折共用快取;留空則依設定建立。
        self.embedder = embedder or get_embedder(config)
        self.model_id = self.embedder.model_id
        self.collection = config.collection
        if config.use_memory_store:
            self.client = QdrantClient(":memory:")
        else:
            self.client = QdrantClient(url=config.qdrant_url, api_key=config.qdrant_api_key)
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        qm = self._qm
        existing = {c.name for c in self.client.get_collections().collections}
        if self.collection not in existing:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=qm.VectorParams(
                    size=self.embedder.dim, distance=qm.Distance.COSINE
                ),
            )

    @staticmethod
    def _payload(r: Record) -> Dict[str, Any]:
        # 含 `_canonical` 全量 JSON,供無損 iter_records / 匯出。
        return {**record_to_payload(r), "_canonical": r.model_dump_json()}

    # ── 寫入 ──
    def upsert(self, record: Record) -> str:
        qm = self._qm
        vec = self.embedder.embed(record.build_embedding_text())
        self.client.upsert(
            collection_name=self.collection,
            points=[qm.PointStruct(id=record.id, vector=vec, payload=self._payload(record))],
        )
        return record.id

    def upsert_many(self, records: List[Record]) -> int:
        qm = self._qm
        if not records:
            return 0
        vecs = self.embedder.embed_batch([r.build_embedding_text() for r in records])
        points = [
            qm.PointStruct(id=r.id, vector=v, payload=self._payload(r))
            for r, v in zip(records, vecs)
        ]
        self.client.upsert(collection_name=self.collection, points=points)
        return len(points)

    # ── 檢索:機制硬過濾 + 語意召回 ──
    def search(
        self,
        query_text: str,
        mechanism: BrewMechanism,
        top_k: int = 20,
        process: Optional[str] = None,
        roast_band: Optional[str] = None,
        exclude_predictions: bool = True,
        user_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        qm = self._qm
        must = [qm.FieldCondition(key="brew_mechanism", match=qm.MatchValue(value=mechanism.value))]
        if process:
            must.append(qm.FieldCondition(key="process", match=qm.MatchValue(value=process)))
        if roast_band:
            must.append(qm.FieldCondition(key="roast_band", match=qm.MatchValue(value=roast_band)))
        if user_ids:  # 多租戶讀範圍(§16.3):只納入這些 user_id(如 global + 自己)
            must.append(qm.FieldCondition(key="user_id", match=qm.MatchAny(any=list(user_ids))))
        must_not = []
        if exclude_predictions:
            must_not.append(qm.FieldCondition(key="grade", match=qm.MatchValue(value="prediction")))

        flt = qm.Filter(must=must, must_not=must_not or None)
        vec = self.embedder.embed(query_text)
        if hasattr(self.client, "query_points"):
            resp = self.client.query_points(
                collection_name=self.collection, query=vec, query_filter=flt,
                limit=top_k, with_payload=True,
            )
            hits = resp.points
        else:  # pragma: no cover - 舊版相容
            hits = self.client.search(
                collection_name=self.collection, query_vector=vec, query_filter=flt,
                limit=top_k, with_payload=True,
            )
        return [{"id": h.id, "score": h.score, "payload": h.payload} for h in hits]

    def count(self) -> int:
        return self.client.count(collection_name=self.collection, exact=True).count

    # ── 全量列舉(匯出 / 重建用) ──
    def iter_records(self) -> Iterator[Record]:
        offset = None
        while True:
            points, offset = self.client.scroll(
                collection_name=self.collection, limit=256,
                with_payload=True, with_vectors=False, offset=offset,
            )
            for p in points:
                canonical = (p.payload or {}).get("_canonical")
                if canonical:
                    yield Record.model_validate_json(canonical)
                else:  # pragma: no cover - 舊資料無 canonical
                    logger.warning("記錄 %s 無 _canonical,匯出跳過。", p.id)
            if offset is None:
                break


# ────────────────────────────── Cloudflare Vectorize 後端 ──────────────────────────────

# 機制硬分區 + 結構化硬過濾用到的 metadata 欄(需先建 metadata index 才可過濾)。
# user_id 供多租戶讀範圍過濾(§16.3);注意:metadata index 須在寫入前建立,
# 既有向量不會回溯索引(見 ensure_index 註記)。
VECTORIZE_FILTER_FIELDS = ("brew_mechanism", "process", "roast_band", "grade", "user_id")


def _sanitize_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Vectorize metadata 淨化:丟掉 None、list 轉逗號字串(僅標量可被索引/過濾)。"""
    out: Dict[str, Any] = {}
    for k, v in payload.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            if v:
                out[k] = ",".join(str(x) for x in v)
        elif isinstance(v, bool):
            out[k] = v
        elif isinstance(v, (int, float, str)):
            out[k] = v
        else:  # pragma: no cover - 防禦
            out[k] = str(v)
    return out


class VectorizeStore:
    """Cloudflare Vectorize v2 後端(REST)。

    機制硬分區用 metadata 等值過濾;分級加權與物理距離在 retrieval 程式層算。
    注意:寫入為**最終一致**(async),測試/demo 寫後立即讀可能尚未可查(見 README)。
    canonical 不存於索引內(metadata 僅留精簡過濾欄);全量真相在 R2/D1 的 JSONL。
    """

    # returnMetadata="all" 時 topK 上限 50。
    _TOPK_CAP = 50
    _UPSERT_BATCH = 1000

    def __init__(self, config=CONFIG, client=None, embedder=None):
        from .cfapi import CloudflareClient
        self.config = config
        self.index = config.vectorize_index
        # 可注入嵌入器(對稱於 VectorStore);留空則依設定建立。
        self.embedder = embedder or get_embedder(config)
        self.model_id = self.embedder.model_id
        self.client = client or CloudflareClient(
            config.cf_account_id, config.cf_api_token,
            config.cf_timeout_s, config.cf_max_retries,
        )
        if isinstance(self.embedder, LocalHashEmbedder):
            logger.warning(
                "Vectorize 後端搭配 local 雜湊嵌入(dim=%d);請確認索引維度相符,"
                "雲端建議 CIE_EMBEDDING_PROVIDER=workers_ai(bge-m3=1024)。",
                self.embedder.dim,
            )

    # ── 寫入 ──
    def upsert(self, record: Record) -> str:
        self.upsert_many([record])
        return record.id

    def upsert_many(self, records: List[Record]) -> int:
        if not records:
            return 0
        vecs = self.embedder.embed_batch([r.build_embedding_text() for r in records])
        lines = [
            {"id": r.id, "values": v, "metadata": _sanitize_metadata(record_to_payload(r))}
            for r, v in zip(records, vecs)
        ]
        for i in range(0, len(lines), self._UPSERT_BATCH):
            self.client.vectorize_upsert(self.index, lines[i:i + self._UPSERT_BATCH])
        return len(lines)

    # ── 檢索:機制硬過濾 + 語意召回 ──
    def search(
        self,
        query_text: str,
        mechanism: BrewMechanism,
        top_k: int = 20,
        process: Optional[str] = None,
        roast_band: Optional[str] = None,
        exclude_predictions: bool = True,
        user_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        flt: Dict[str, Any] = {"brew_mechanism": mechanism.value}  # 硬分區鍵
        if process:
            flt["process"] = process
        if roast_band:
            flt["roast_band"] = roast_band
        if exclude_predictions:
            flt["grade"] = {"$ne": "prediction"}
        if user_ids:  # 多租戶讀範圍(§16.3):user_id ∈ 白名單(如 global + 自己)
            flt["user_id"] = {"$in": list(user_ids)}

        vec = self.embedder.embed(query_text)
        body = {
            "vector": vec,
            "topK": min(top_k, self._TOPK_CAP),
            "returnMetadata": "all",
            "returnValues": False,
            "filter": flt,
        }
        result = self.client.vectorize_query(self.index, body)
        matches = (result or {}).get("matches", []) if isinstance(result, dict) else []
        return [
            {"id": m.get("id"), "score": m.get("score", 0.0), "payload": m.get("metadata") or {}}
            for m in matches
        ]

    def count(self) -> int:
        """最終一致的近似筆數(來自 index info);失敗回 -1。"""
        try:
            info = self.client.vectorize_info(self.index)
        except Exception as e:  # pragma: no cover - 需網路
            logger.warning("Vectorize info 取得失敗:%s", e)
            return -1
        return int(info.get("vectorCount", info.get("vectorsCount", -1)))

    def ensure_index(self) -> None:  # pragma: no cover - 需金鑰,一次性設定
        """建立機制/過濾欄的 metadata index(冪等;已存在的錯誤忽略)。

        注意:metadata index 須在寫入**之前**建立,既有向量不會回溯索引。
        正式設定建議用 wrangler(見 README);此方法為程式化便利。
        """
        from .cfapi import CloudflareError
        for field in VECTORIZE_FILTER_FIELDS:
            try:
                self.client.vectorize_create_metadata_index(self.index, field, "string")
            except CloudflareError as e:
                logger.info("metadata index %s 略過(可能已存在):%s", field, e)


# ────────────────────────────── 工廠 ──────────────────────────────

def get_store(config=CONFIG) -> StoreBackend:
    """依設定選後端:vectorize → VectorizeStore;否則 VectorStore(qdrant/記憶體)。"""
    if config.store_backend == "vectorize":
        return VectorizeStore(config)
    return VectorStore(config)
