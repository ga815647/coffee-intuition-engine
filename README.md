# Coffee Intuition Engine (CIE)

一個「AI 咖啡大佬」的語意記憶 / 直覺引擎。把〔豆/焙條件 + 沖煮參數〕映射到〔杯測量化風味〕,支援正向預測、反向診斷、跨泡法遷移,並以校準品質分級加權的語意檢索累積長久記憶。

設計依據:`coffee_intuition_engine_design.md`(v0.2)。本倉庫是該規格的實作骨架。

## 核心原則(鐵則)

1. **不是味覺,是放大器**——模型沒有味覺受器。它放大你的感官,最終裁決需要你的校準回饋。
2. **機制三軌分立**——`immersion` / `percolation` / `pressure` 各有獨立物理先驗,**不可跨機制平均**。這是檢索的硬分區鍵。
3. **水只當控制變數**——水-風味的通俗因果(如「鎂=明亮」)有同儕審查反證,故水欄位只作分群,**不進風味因果**。
4. **方向 > 絕對值**——客觀變數預測杯測分數的天花板約 R²≈0.5,系統定位在方向與排序,不吹精準分數。
5. **不確定要誠實**——用 conformal 預測區間,不給假精確的單一信心數字。

## 架構

```
L1 豆/焙條件  ─┐
L2 物理參數    ├─►  映射(機制分軌)  ─►  L3 杯測量化風味
water(控制變數)┘        ▲
                  語意向量庫(分級加權 kNN + 貝氏收縮 + conformal)
```

模組:

| 檔案 | 職責 |
|---|---|
| `cie/schema.py` | L1/L2/L3 + water + grade 的 Pydantic 模型 |
| `cie/physics.py` | 三軌物理先驗 + TDS/EY→風味相關 |
| `cie/embedding.py` | 可插拔嵌入:`local`(離線雜湊)/ `workers_ai`(bge-m3)/ `openai` / `voyage`;`get_embedder()` 工廠,缺金鑰自動退回 local |
| `cie/store.py` | 可插拔向量庫:記憶體 / Qdrant / Vectorize;`get_store()` 工廠;機制硬過濾召回 |
| `cie/canonical.py` | canonical 真相層:`LocalJsonlCanonical` / `R2Canonical`;`log_calibration`/`seed` 雙寫(Vectorize 不再無源) |
| `cie/rebuild.py` | `python -m cie.rebuild`:讀 canonical → 當前嵌入器重嵌 → 重建向量索引 |
| `cie/portability.py` | canonical JSONL 匯出/匯入;換模型或雲↔地重建索引(向量是衍生物) |
| `cie/cfapi.py` | Cloudflare REST 用戶端(Workers AI run + Vectorize upsert/query) |
| `cie/_http.py` | stdlib urllib HTTP(重試/逾時),零新依賴 |
| `cie/retrieval.py` | 機制硬過濾 kNN + 收縮 + conformal 區間 |
| `cie/engine.py` | recommend / predict / diagnose / method_swap |
| `cie/seed.py` | A 級錨點 + 物理先驗 bootstrap |
| `cie/demo.py` | `python -m cie.demo`:端到端跑四種推理 |
| `mcp_server.py` | FastMCP:`query_flavor_map` / `log_calibration` / `predict_method_swap` |

## 快速開始

```bash
pip install -r requirements.txt   # 雲端後端零新依賴(走 stdlib urllib)

# 開發模式:記憶體向量庫 + 離線雜湊嵌入,免任何金鑰
python -m cie.seed          # 灌種子
python -m pytest -q         # 測試(雲端後端用假用戶端,離線可全綠)
python -m cie.demo          # 端到端跑 recommend / predict / diagnose / method_swap
python -m eval.run          # 盲測評測:留出豆先測再比真值,算 L3 MAE / 區間覆蓋 / 方向準確度

# 啟動 MCP server
python mcp_server.py
```

## 設定(.env,見 .env.example)

開發預設全部離線可跑(記憶體向量庫 + local 雜湊嵌入)。後端與嵌入各自由環境變數選擇,缺金鑰一律自動退回離線:

- **向量庫**`CIE_STORE_BACKEND`:留空=自動(有 CF 金鑰+Vectorize→`vectorize`;有 Qdrant URL→`qdrant`;皆無→`memory`)。可強制 `memory|qdrant|vectorize`。
- **嵌入**`CIE_EMBEDDING_PROVIDER`:`local`(預設)|`workers_ai`|`openai`|`voyage`。
- Qdrant(替代):`CIE_QDRANT_URL` / `CIE_QDRANT_API_KEY`。

### Cloudflare 原生(建議,省錢:個人規模多在免費額度內)

向量庫用 **Vectorize**、嵌入用 **Workers AI `@cf/baai/bge-m3`**(1024 維、多語,適合中文風味筆記)。

**1. 建 Vectorize 索引 + metadata 索引**(機制硬分區與防 model collapse 的過濾鍵都需建 metadata 索引才可用):

```bash
npx wrangler vectorize create cie-records --dimensions=1024 --metric=cosine
npx wrangler vectorize create-metadata-index cie-records --property-name=brew_mechanism --type=string
npx wrangler vectorize create-metadata-index cie-records --property-name=process        --type=string
npx wrangler vectorize create-metadata-index cie-records --property-name=roast_band     --type=string
npx wrangler vectorize create-metadata-index cie-records --property-name=grade          --type=string
```

> 維度必須對齊嵌入模型:bge-m3=1024、bge-base=768。換模型要新建索引並從 JSONL 重嵌(見下「可攜性」)。

**2. 設環境變數**(`.env`):

```
CIE_STORE_BACKEND=vectorize
CIE_EMBEDDING_PROVIDER=workers_ai
CIE_CF_ACCOUNT_ID=<你的 account id>
CIE_CF_API_TOKEN=<API token:需 Workers AI Run + Vectorize Edit 權限>
CIE_VECTORIZE_INDEX=cie-records
CIE_WORKERS_AI_EMBED_MODEL=@cf/baai/bge-m3
```

**3. 灌種子並驗證**:`python -m cie.seed`(Vectorize 為最終一致,upsert 後查詢可能有秒級延遲)。

### 可攜性(canonical 是真相,向量是衍生物)

```python
from cie.portability import export_store, import_jsonl
from cie.store import get_store

export_store(get_store(), "backup.jsonl")     # 全量匯出(需記憶體/Qdrant 後端)
import_jsonl("backup.jsonl", get_store())      # 用「當前」嵌入器重嵌並寫入
```

切換嵌入模型、換機器、雲↔地遷移,都從 JSONL 重建索引——絕不直接搬舊向量(不同模型的向量不可混用)。`seeds/anchors.jsonl` 本身就是 canonical 格式。

**Vectorize 後端的真相來源**:Vectorize 只存 sanitized metadata、無法自存全量 canonical,故 `log_calibration` / `seed` 會**雙寫 canonical sink**(`cie/canonical.py`:本地 `./data/canonical.jsonl`,或設 `CIE_R2_BUCKET` 改存 R2)。重建走:

```bash
python -m cie.rebuild   # 讀 canonical → 當前嵌入器重嵌 → 重建向量索引
```

記憶體 / Qdrant 後端自帶 `_canonical`,不另寫此檔(`maybe_get_canonical` 偵測後略過)。

## 狀態

骨架(v0.2)。收縮與 conformal 為可運作的簡化實作,標 `TODO(prod)` 處待換生產級(MAPIE/CQR、層級貝氏)。資料量到位前,引擎在鄰居不足時自動退回物理先驗 + 寬區間(防空庫幻覺)。
