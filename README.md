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
| `cie/mcp_tools.py` | **單一工具註冊點**:`do_*` 邏輯 + `register_tools`;stdio 與 HTTP 共用一份 |
| `cie/mcp_principal.py` | 身分解析(reader / owner)+ 寫入信任閘(§16.2);transport 無關 |
| `mcp_server.py` | **stdio = 私有門**(本機 / Claude Code 直連):owner 唯一寫入,掛全部工具 |
| `server_http.py` | **HTTP = 公開門**(streamable-http):**唯讀**、雙 token 認證 + claude.ai CORS + `/health` |

## 快速開始

```bash
pip install -r requirements.txt   # 雲端後端零新依賴(走 stdlib urllib)

# 開發模式:記憶體向量庫 + 離線雜湊嵌入,免任何金鑰
python -m cie.seed          # 灌種子
python -m pytest -q         # 測試(雲端後端用假用戶端,離線可全綠)
python -m cie.demo          # 端到端跑 recommend / predict / diagnose / method_swap
python -m eval.run          # 盲測評測:留出豆先測再比真值,算 L3 MAE / 區間覆蓋 / 方向準確度

# 啟動 MCP server(本地 / Claude Code 直連,stdio)
python mcp_server.py
```

## Remote MCP(接 claude.ai)— 兩扇門

把引擎接成 claude.ai / Claude Code 可掛載的 **remote MCP**,採**兩扇門**模型:

- **公開門(HTTP,`server_http.py`)= 唯讀。** 你日常用、分享給別人用,都走這。只掛讀工具(query/recommend、predict、diagnose、method_swap),**`log_calibration` 根本不在 HTTP 暴露 → 網路上沒有任何寫入路徑**;所有 HTTP token 一律唯讀。token 外洩最壞只是被讀。
- **私有門(本機 stdio,`mcp_server.py`)= owner 唯一寫入。** 校準回饋只在你自己機器上寫,靠「跑在你的機器上」授權,不需任何網路 token。

HTTP 層是**薄傳輸 + 認證**——檢索 / 收縮 / conformal / 機制三軌 / 物理先驗全留在 `cie.*`,與 stdio 共用同一份工具(`cie/mcp_tools.py`,HTTP 傳 `include_writes=False`)。設計見 `docs/DESIGN_v0.2.md` §13.6 / §16.2 / §16.3。

```bash
# 1. 設唯讀 token(fail-closed:未設 → 所有 /mcp 回 401。stdio 私有門不受影響)
python -c "import secrets; print(secrets.token_urlsafe(32))"
#    把值填進 .env 的 CIE_MCP_AUTH_TOKEN(主要唯讀 token;見 .env.example)
#    Windows 注意:用編輯器貼上,別用 PowerShell pipe(會混入 BOM)。

# 2. 本地起 HTTP server(公開門,唯讀)
uvicorn server_http:app --host 0.0.0.0 --port 8000
#    或  python server_http.py   (讀 CIE_MCP_HOST / CIE_MCP_PORT)

# 3. 健康檢查(public,免 token)
curl http://127.0.0.1:8000/health      # Windows + SChannel:curl --ssl-no-revoke ...

# 4. 端到端 smoke(真起 server + 真 MCP client:認證 / 機制分區 / **無寫入路徑**)
python tools/smoke_http.py
```

**端點**:`POST /mcp`(streamable-http)、`GET /health`、`GET /`(public 狀態)。

**雙 token 認證**(對齊 fellow-aiden-mcp):`Authorization: Bearer <token>` **與** `?token=<token>` 皆可——claude.ai 網頁連接器只能送 query param,故 `?token=` 不可少。

**claude.ai 自訂連接器**:新增連接器 → URL 填(**唯讀** token)
```
https://<你的 host>/mcp?token=<CIE_MCP_AUTH_TOKEN>
```

**token(公開門一切唯讀,§16):**

| token | 角色 | 寫入 | 讀範圍 |
|---|---|---|---|
| `CIE_MCP_AUTH_TOKEN` | 主要唯讀(日常 + 分享) | **無**(HTTP 無寫入路徑) | 共享真相(global 客觀層 + owner 校準) |
| `CIE_MCP_GUEST_TOKENS`(JSON `{token:label}` 或 `[token]`) | 額外唯讀(個別發放 / 撤銷) | **無** | 同上 |
| 本機 stdio(`LOCAL_PRINCIPAL`) | **owner,唯一寫入** | A(須 protocol)/ B / C;`self` 或 `global` | 全部(不過濾) |

寫入只在**本機 Claude Code stdio**:`A` 級須附 `protocol`(人類感官真值來源),`grade=prediction` 為內部保留級、即便 owner 自己注入也拒收(防 model collapse)。每位訪客各自寫 `self` 層 + 硬隔離彼此 =「未來如需再加」(加性讀過濾機制已就緒,§16.3)。

### 部署(host-agnostic 容器)

`Dockerfile` 不綁單一供應商(Fly / Railway / Render / Cloud Run / VPS 皆可)。runtime 輕量(CF 後端時免重 ML)。

```bash
docker build -t cie-mcp .
docker run --rm -p 8000:8000 --env-file .env cie-mcp
```

埠:`CIE_MCP_PORT` 優先,否則採平台注入的 `$PORT`,再否則 8000。正式部署請設 `CIE_STORE_BACKEND=vectorize` + `CIE_EMBEDDING_PROVIDER=workers_ai`(別在公開實例用 local 雜湊嵌入),並先 `python -m cie.bootstrap` + `python -m cie.rebuild` 灌策展語料。

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
npx wrangler vectorize create-metadata-index cie-records --property-name=user_id        --type=string
```

> **索引不回溯**:Vectorize 的 metadata index 只涵蓋「建立索引之後」寫入的向量,既有向量不被追溯涵蓋。故**先建齊所有過濾欄(含 `user_id`)再寫向量**——`user_id` 是 §16.3「未來如需再加」per-tenant self 隔離的硬過濾鍵,**先建好供未來沿用**(漏建會讓日後啟用的隔離「過濾失效、靜默 fail-open」)。`python -m cie.rebuild` 已在寫入前冪等呼叫 `store.ensure_index()` 補建這些索引;首次部署仍建議手動跑上面指令確認。
>
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
