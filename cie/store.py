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
import uuid
from typing import Any, Dict, Iterator, List, Optional, Protocol

from .config import CONFIG
from .embedding import LocalHashEmbedder, get_embedder
from .schema import BrewMechanism, Record

logger = logging.getLogger("cie.store")

# Qdrant 點 id **必為 UUID(或無號整數)**。Record.id 多為 uuid4(合法),但 owner 策展條目
# 可有刻意固定的可讀 id(如 contested-acidity-direction-ucdavis,為 INSERT OR REPLACE 冪等與
# 穩定 snapshot diff 而固定)。對非 UUID 的 id 用 uuid5 **決定性**映射成合法點 id;canonical
# 真相 id **不變**(evidence / delete / promote 一律走真實 id)。否則單一可讀 id 會讓 qdrant
# 的 all-or-nothing upsert(prime_serving_index)整批崩潰 → 冷啟動 serving 索引全空。
_POINT_ID_NAMESPACE = uuid.UUID("b1e9c0de-c0ff-ee00-1234-c1e500000001")


def point_id(record_id: str) -> str:
    """把 Record.id 映成合法的 qdrant 點 id。已是 UUID → 正規化原樣;否則 uuid5 決定性雜湊。

    決定性保證:同一 record_id 永遠映到同一點 id,故 upsert 冪等、delete 可精準命中。
    """
    s = str(record_id)
    try:
        return str(uuid.UUID(s))
    except (ValueError, AttributeError, TypeError):
        return str(uuid.uuid5(_POINT_ID_NAMESPACE, s))


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
        "variety": r.bean.variety,  # §3.2 bean_match 同豆閘需要(origin+variety+process)
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
    def upsert_many(self, records: List[Record], skip_errors: bool = False) -> int: ...
    def search(self, query_text: str, mechanism: BrewMechanism, top_k: int = 20,
               process: Optional[str] = None, roast_band: Optional[str] = None,
               exclude_predictions: bool = True,
               user_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]: ...
    def count(self) -> int: ...
    def delete(self, record_id: str, user_id: Optional[str] = None) -> int: ...


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
        # `_id` 保留**真實** record id(點 id 可能被 point_id 正規化/雜湊);evidence/刪除用它。
        # `_canonical` 全量 JSON,供無損 iter_records / 匯出。
        return {"_id": r.id, **record_to_payload(r), "_canonical": r.model_dump_json()}

    # ── 寫入 ──
    def upsert(self, record: Record) -> str:
        qm = self._qm
        vec = self.embedder.embed(record.build_embedding_text())
        self.client.upsert(
            collection_name=self.collection,
            points=[qm.PointStruct(id=point_id(record.id), vector=vec,
                                   payload=self._payload(record))],
        )
        return record.id

    def upsert_many(self, records: List[Record], skip_errors: bool = False) -> int:
        # 契約:呼叫端須先對 id 去重(見 portability.import_records)。本法不保證 batch 內
        # 同 id 的勝出者——避免依賴後端 batch upsert 的隱性語意(換後端不致靜默回退)。
        # 全有全無:整批一次 upsert。`skip_errors=True`(僅冷啟動 prime 傳)時,批次失敗後**降級
        # 逐筆隔離**——壞記錄 log WARNING + skip、好記錄照進,避免單一壞記錄讓整個 serving 索引
        # 歸零(PR6:防「靜默空 index」)。預設 False:正常寫入(member log_calibration)仍 fail loud。
        qm = self._qm
        if not records:
            return 0
        try:
            vecs = self.embedder.embed_batch([r.build_embedding_text() for r in records])
            points = [
                qm.PointStruct(id=point_id(r.id), vector=v, payload=self._payload(r))
                for r, v in zip(records, vecs)
            ]
            self.client.upsert(collection_name=self.collection, points=points)
            return len(points)
        except Exception:
            if not skip_errors:
                raise
            return self._upsert_isolated(records)

    def _upsert_isolated(self, records: List[Record]) -> int:
        """批次 upsert 失敗後的最後手段:逐筆重嵌 + upsert,隔離壞記錄。回傳成功載入筆數。

        紀律(PR6):skip 是 last resort、**絕不靜默**——每筆失敗都 log WARNING(印 id + 原因),
        並在結尾彙總 (loaded, skipped)。跳掉幾筆的後果由冷啟動完整性門檻(prime 的 assert)把關。
        """
        qm = self._qm
        loaded = skipped = 0
        for r in records:
            try:
                vec = self.embedder.embed(r.build_embedding_text())
                self.client.upsert(
                    collection_name=self.collection,
                    points=[qm.PointStruct(id=point_id(r.id), vector=vec,
                                           payload=self._payload(r))],
                )
                loaded += 1
            except Exception as e:
                skipped += 1
                logger.warning("upsert 跳過壞記錄 id=%s:%s", getattr(r, "id", "?"), e)
        if skipped:
            logger.warning(
                "upsert_many 逐筆隔離:載入 %d、跳過 %d(批次 upsert 曾失敗,skip_errors=True)。",
                loaded, skipped)
        return loaded

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
        # 回傳**真實** record id(payload._id;點 id 可能被 point_id 正規化/雜湊),
        # 讓 evidence / delete_calibration / promote 用得到的 id 對得上 canonical 真相。
        return [
            {"id": (h.payload or {}).get("_id", h.id), "score": h.score, "payload": h.payload}
            for h in hits
        ]

    def count(self) -> int:
        return self.client.count(collection_name=self.collection, exact=True).count

    # ── 刪除(member 只刪自有 self;owner 不限) ──
    def delete(self, record_id: str, user_id: Optional[str] = None) -> int:
        """刪一筆。`user_id` 給定時先驗該點命名空間 == user_id 才刪(member confinement,
        不誤刪他人);None=不限(owner)。先 retrieve 驗存在 + 命名空間 → 回精準刪除數(0/1)。"""
        qm = self._qm
        pid = point_id(record_id)  # 真實 id → 點 id(與 upsert 對稱),非 UUID 的 id 也能精準命中
        got = self.client.retrieve(collection_name=self.collection, ids=[pid],
                                   with_payload=True, with_vectors=False)
        if not got:
            return 0
        if user_id is not None and (got[0].payload or {}).get("user_id") != user_id:
            return 0
        self.client.delete(collection_name=self.collection,
                           points_selector=qm.PointIdsList(points=[pid]))
        return 1

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

    def upsert_many(self, records: List[Record], skip_errors: bool = False) -> int:
        if not records:
            return 0
        vecs = self.embedder.embed_batch([r.build_embedding_text() for r in records])
        lines = [
            {"id": r.id, "values": v, "metadata": _sanitize_metadata(record_to_payload(r))}
            for r, v in zip(records, vecs)
        ]
        loaded = 0
        for i in range(0, len(lines), self._UPSERT_BATCH):
            chunk = lines[i:i + self._UPSERT_BATCH]
            try:
                self.client.vectorize_upsert(self.index, chunk)
                loaded += len(chunk)
            except Exception as e:
                if not skip_errors:  # 預設 fail loud;skip_errors=True(冷啟動)才隔離壞批次
                    raise
                logger.warning("Vectorize upsert 跳過批次 %d 筆:%s", len(chunk), e)
        return loaded

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

    def delete(self, record_id: str, user_id: Optional[str] = None) -> int:  # pragma: no cover - 需網路
        """刪一筆(get_by_ids 驗命名空間 → delete_by_ids)。`user_id` 給定時只刪自有。
        Vectorize 最終一致且本上線不用(prod 走 memory + D1);保留以維持後端介面一致。"""
        got = self.client.vectorize_get_by_ids(self.index, [record_id])
        if not got:
            return 0
        md = (got[0] or {}).get("metadata") or {}
        if user_id is not None and md.get("user_id") != user_id:
            return 0
        self.client.vectorize_delete_by_ids(self.index, [record_id])
        return 1

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
