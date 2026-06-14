# CLAUDE.md — Coffee Intuition Engine (CIE)

> 給 Claude Code 的專案指南。每個 session 自動讀本檔。**動工前先讀 `docs/DESIGN_v0.2.md`(權威設計,含完整研究與 AUDIT)。**

## 任務

打造「AI 咖啡大佬」的語意記憶 / 直覺引擎。把〔豆/焙條件 + 沖煮參數〕映射到〔杯測量化風味〕,支援四種推理:`recommend`(起手參數)、`predict`(預測風味)、`diagnose`(問題歸因)、`method_swap`(換泡法)。以校準品質分級加權的語意檢索累積長久記憶,越用越準。

定位鐵則:**它是使用者感官的高保真放大器,不是味覺本體。** 統計先驗它強過任何單一人類;最終風味裁決需要人類校準回饋。不偽裝成會品鑑,不照搬網路偽因果。

## 不可違反的鐵則(改動程式時務必守住)

1. **機制三軌硬隔離** — `immersion` / `percolation` / `pressure` 各有獨立物理先驗,**永不跨機制平均**。它是 `store.search` 的硬分區鍵(必過濾)。理由:浸泡趨平衡(E 對研磨/溫度不敏感)、滴濾非平衡流動(E 對研磨/流速極敏感)、義式加壓(研磨→E 非單調)。三者是不同物理範式。
2. **水只當控制變數** — `WaterProfile` 只作分群/批次標籤,**絕不把水→風味因果寫死**(如「鎂=明亮」)。同儕審查證據(Bratthäll et al.)顯示陽離子不影響有機酸萃取、只影響知覺;通俗口訣不可信。
3. **方向 > 絕對值** — 客觀變數預測杯測分數天花板約 R²≈0.5。輸出以方向與排序的信心為主,不吹精準分數。
4. **誠實的不確定** — 用 conformal 預測區間,不給假精確的單一信心數字。鄰居越少區間越寬。
5. **防 model collapse** — 引擎自身預測一律存 `grade=prediction`,**禁止進方向投票**;`A` 級寫入必須附 `protocol`(人類感官真值來源,如 SCA_cupping),否則 `log_calibration` 拒收。
6. **冷啟動防幻覺** — 鄰居不足時退回物理先驗 + 寬區間 + 警告,**不硬給精確數值**。
7. **個人偏好層 vs 客觀因果層** — `user_id=self`(會收斂的口味)與 `user_id=global`(跨人物理規律)分開,檢索時別混合平均。

## 架構

```
L1 豆/焙(BeanRoast)─┐
水質(WaterProfile,控制變數)
L2 物理參數(BrewParams, brew_mechanism=硬分區鍵)─┼─► 映射(機制分軌)─► L3 杯測風味(FlavorProfile)
                                                語意向量庫(分級加權 kNN + 貝氏收縮 + conformal)
```

| 檔案 | 職責 |
|---|---|
| `cie/schema.py` | Pydantic 模型 + 列舉(BrewMechanism/Grade/Process/AcidityType);`FLAVOR_AXES` 為模組級常數 |
| `cie/physics.py` | 三軌物理先驗 `PRIORS` + `flavor_prior_from_extraction` + `diagnose_prior` |
| `cie/embedding.py` | `Embedder` 介面;`LocalHashEmbedder`(離線預設)+ `WorkersAIEmbedder`(bge-m3,生產) |
| `cie/store.py` | `VectorStore`(記憶體自幹索引,生產主力)/ Qdrant / `VectorizeStore`(選項);機制硬過濾召回 |
| `cie/canonical.py` | canonical 真相層:`D1Canonical`(生產)/ `R2Canonical` / `LocalJsonlCanonical`;`maybe_get_canonical`(d1/r2 強掛 sink) |
| `cie/rebuild.py` | 從 canonical 重嵌重建索引;`prime_serving_index`(冷啟動 memory←D1)|
| `cie/retrieval.py` | `GRADE_WEIGHT`、`weighted_estimate`(收縮+區間)、`assess`(A級權重佔比→信心) |
| `cie/engine.py` | `Engine`:recommend/predict/diagnose/method_swap/log_calibration(寫:先 D1 canonical 後記憶體) |
| `cie/seed.py` | 載入 `seeds/anchors.jsonl` A 級種子 |
| `mcp_server.py` | FastMCP stdio = **owner 門**:全 6 工具(query_flavor_map / log_calibration / delete_calibration / predict_method_swap + 晉升 list_customizations / promote_customization);與 HTTP 共用 `cie/mcp_tools.py` |
| `server_http.py` | remote MCP(streamable-http)= **網路面**:member 受限寫 + 刪(`log_calibration` / `delete_calibration`,皆只落 / 只刪自有 self);身分 / 寫入閘在 `cie/mcp_principal.py`(三層 + 晉升,§16) |
| `tests/test_smoke.py` | 9 項端到端測試 |

## 現況

骨架 v0.2 可跑,144 測試全綠(+1 skip)。開發預設全離線(記憶體向量庫 + 雜湊嵌入,免金鑰)。收縮與 conformal 為可運作的簡化版,標 `TODO(prod)`。

**生產上線形態(已上線 ✅ Cloud Run,2026-06 asia-east1 `https://cie-mcp-936606065390.asia-east1.run.app`;見 `docs/DESIGN_v0.2.md` §14.7):** Cloud Run($0 scale-to-zero)+ **記憶體自幹索引**(`CIE_STORE_BACKEND=memory`,冷啟動從 D1 重嵌重建,**不用 Vectorize**)+ **D1 共用 canonical**(`CIE_CANONICAL_BACKEND=d1` 或 CF 金鑰 + `CIE_D1_DATABASE_ID` 自動 `d1`;owner 本機與 member HTTP 寫同一 D1)+ **Workers AI bge-m3 嵌入**。member 寫入**先落 D1 再更新記憶體**(durability:撐過 scale-to-zero);`max-instances=1`(守記憶體 serving 索引最終一致;D1 逐筆 INSERT OR REPLACE 並發安全,不再為寫一致性所必需)、`CIE_MCP_STATELESS=1`(隔離命門)。**公開部署須關閉 FastMCP 內建 DNS-rebinding allowlist**(否則雲端 Host → 421;`build_app` 顯式 `transport_security` 已處理,`CIE_MCP_ALLOWED_HOSTS` 可硬化)。冷啟動載入在 `rebuild.prime_serving_index`,`server_http` / `mcp_server` 啟動時呼叫。測試 `tests/test_serving_index.py` / `tests/test_mcp_http.py`;線上驗證 `tools/smoke_remote.py`。

## 慣例

- Python 3.10、pydantic v2、型別註記齊全。註解用繁中可。
- **每次改動保持測試綠,並為新功能加測試。** 改 schema 要同步更新 `docs/DESIGN_v0.2.md` 與測試。
- 介面可插拔(Embedder / VectorStore),不要硬綁單一供應商。
- 機密只進 `.env`(見 `.env.example`)/ `config.py`,不入庫。
- 保持 PR/commit 小而可審。

## 執行與測試

```bash
pip install -r requirements.txt   # 本沙箱需加 --break-system-packages
python -m cie.bootstrap           # 把策展語料 corpus/global.jsonl(446)灌入 canonical 真相層
python -m cie.rebuild             # 從 canonical 用當前嵌入器重嵌、灌入向量庫(≈446)
python -m cie.seed                # (替代)只灌 6 筆 seeds/anchors.jsonl,空庫冷啟動 demo 用
python -m pytest -q               # 應全綠
python mcp_server.py              # 啟動 MCP(stdio)
```

> **bootstrap vs seed(別搞混):** 正式載入走 `cie.bootstrap`(corpus/global.jsonl → canonical,446 筆)再 `cie.rebuild`;`cie.seed` 只是 6 筆冷啟動錨點。canonical = 策展語料(初始) + 之後 `log_calibration` 累積的回饋。

## 環境地雷(重要)

- 本沙箱 `pip install` 需 `--break-system-packages`。
- 此版 `qdrant-client` 用 `query_points`(非 `search`);`store.py` 已兼容兩者。
- 記憶體向量庫**不跨行程持久化**;要持久化設 `CIE_QDRANT_URL`/`CIE_QDRANT_API_KEY`。
- 已知:連接資料夾偶發**大檔寫入截斷**。若遇 SyntaxError/檔案被砍尾,用 shell 重寫並 `wc -l`/`md5sum` 核對;`python -c "import ast; ast.parse(open(f).read())"` 驗語法。

## 路線圖(依序接手;每項完成須測試綠 + 不破鐵則)

**P0 — 讓召回真的有語意 + 改用 Cloudflare 原生託管(省錢)**
- 接真嵌入:預設改 **Cloudflare Workers AI `@cf/baai/bge-m3`(多語,適合中文風味筆記)**;`embedding.py` 的 `Embedder` 介面加 Workers AI 後端(Worker binding 或 REST),`local` 雜湊版留作離線後備,Voyage/OpenAI 為選配。驗收:同義/中文豆況召回明顯優於雜湊版(加召回品質測試)。
- **持久化(生產定案改走「記憶體自幹 index + D1 共用 canonical」,Vectorize 降為選項)**:`store.py` 的 `VectorStore` 介面已含 **Vectorize 後端**(REST,保留可用),但**上線不用**——個人規模(446 筆)記憶體 cosine kNN 即毫秒級,免外部向量 DB。生產 `CIE_STORE_BACKEND=memory`,canonical 存 **D1**(SQLite-over-HTTP,單一共用真相;逐筆 INSERT OR REPLACE 無 R2 整檔 race、免綁卡),冷啟動 `rebuild.prime_serving_index` 從 D1 重嵌重建記憶體索引。驗收(已過 ✅):重啟(Cloud Run scale-to-zero / 新 revision)後資料還在 + member 寫入撐過冷啟動(`tests/test_serving_index.py` + 線上 `tools/smoke_remote.py --verify-persistence`)。詳見 §14.7。
- 維度注意:bge-m3=1024、bge-base=768。(Vectorize 選項:免費 500 萬 stored dims;記憶體後端不受此限。)
- ✅ **canonical 真相層(完成)**:`cie/canonical.py`(`LocalJsonlCanonical` / `R2Canonical`,雙寫)+ `cie/rebuild.py`(`python -m cie.rebuild`,從 canonical 用當前嵌入器重嵌重建)。`engine.log_calibration` / `seed` 對 Vectorize 等無法自存的後端走 canonical sink;`prediction` 級不入真相。Vectorize **不再無源**。詳見 `docs/DESIGN_v0.2.md` §15.1。
- ✅ **canonical bootstrap 來源 = `corpus/global.jsonl`(完成)**:`cie/bootstrap.py`(`python -m cie.bootstrap`,一次性把 446 筆策展語料灌入 canonical sink;`--force` 整份覆寫)→ 再 `python -m cie.rebuild`。**驗收已過**:rebuild 後向量庫筆數 = 446(**不是** 6 筆 seeds)。bootstrap≠seed:後者只是冷啟動錨點。`canonical.replace_all` 為 force/災後重建提供冪等覆寫。

**P1 — 把「直覺」做扎實**
- 收縮升級:`retrieval.weighted_estimate` 由 `n/(n+k)` 近似改成層級貝氏(群組先驗=同機制+同處理法+同焙度帶)。驗收:少樣本時估計明顯往群組先驗收斂的單元測試。
- Conformal/CQR:用 MAPIE 或自實作 split-conformal + 小樣本 Beta 修正(SSBC),取代經驗分位 stub;對味覺漂移用加權 conformal。驗收:留出集實測覆蓋 ≈ 名目(±)。
- 🚧 **盲測評測集(關鍵,進行中)**:建 `eval/`,對庫中沒有的豆先 `predict` → 人工盲評 → 算 L3 各軸 MAE/區間覆蓋,當回歸測試。沒有它就無法證明「越用越準」。**已落地**:`python -m eval.run` 預設跑**按機制分層的 k-fold 交叉驗證**(`run_cv_eval`,k=5):留出集 = `corpus/global.jsonl` 的 **A/B 級**記錄、按機制分層,每筆 A/B 輪流當一次 holdout(取代撐不起結論的 5 筆合成 holdout);**C 級永不當 holdout 真值**(只留召回庫壓量級)。算 MAE/RMSE、conformal 區間覆蓋、**同機制**方向排序準確率,**且分機制報告 n/MAE/覆蓋/方向**。**召回庫 = `corpus/global.jsonl`(446 筆策展真相)每折扣除該折 holdout**(按豆+機制+參數的**內容指紋**扣除,因語料不帶穩定 id)。含三道防洩漏(留出豆排除 + 結構性無子項回推 + 預測不寫回)+ C 級守衛。合成 `eval/dataset.jsonl` 降級為**洩漏偵測器回歸**(`run_eval` 路徑)。離線雜湊嵌入**不下 MAE 門檻**;真實準度待接 `workers_ai` 嵌入 + 真實資料(同一 harness 複用)。詳見 §15.2。

**P2 — 資料與整合**
- Notion 雙寫:`log_calibration` 同步寫 Notion 回饋 DB(讀 `CIE_NOTION_*`)+ 向量庫;夜間一致性校驗。
- 匯入公開資料:把 Kaggle CQI 資料集當 **C 級**載入(只壓量級、不進方向);尊重授權,只取事實參數。
- 同機制內 `method_swap` 量化(目前跨機制僅定性)。

**P3 — 進階**
- 主動校準:用 contextual bandit(LinUCB/Thompson)建議「下一杯試什麼最能學到東西」。
- 研磨分佈/細粉指標、義式壓力軸細化。

## 完成定義(每個改動)

測試綠;未違反任何鐵則;若動 schema 同步更新設計文件與 seeds;新增功能附測試與簡短 docstring。

## MCP / Worker 整合(接 claude.ai 時)

> ✅ **remote MCP 已落地(選項 B,Python 原生 streamable-http;「三層 + 人工晉升」)。** 入口 `server_http.py`(`uvicorn server_http:app` 或 `python server_http.py`);與 stdio(`mcp_server.py`)**共用** `cie/mcp_tools.py` 一份工具邏輯,**未重寫任何引擎邏輯**。三層身分:**member**(HTTP token→命名空間,能寫但**強制只落自有 self 層**、`grade≤B`、寫不到 global / 讀不到他人 self)、**reader**(HTTP 無命名空間 token,只讀 global)、**owner**(本機 stdio,唯一能寫 global / 唯一晉升)。寫入隔離命門靠三道結構保證(命名空間 confinement + grade 上限 + 讀範圍加性過濾,§16.2/§16.3);晉升工具(`list_customizations` / `promote_customization`)**只在 stdio owner 門**,HTTP 不掛 → 網路無寫 global 路徑。雙 token 認證(Bearer + `?token=`)、CORS 鎖 `*.claude.ai`、`/mcp`+`/health`、`CIE_MCP_STATELESS=1` 啟動硬檢查(否則 member principal 退回 owner 預設,瓦解隔離)皆生效並有測試(`tests/test_mcp_{gate,http}.py`)+ 端到端 smoke(`tools/smoke_http.py`:member 寫自有 ns / global 不被污染 / self 讀隔離)+ `Dockerfile`。設定見 `.env.example` 的 `CIE_MCP_*`;細節見 `docs/DESIGN_v0.2.md` §13.6 / §16.2 / §16.3、`README.md`「Remote MCP」。**TODO**:訪客 member token self-serve 簽發、實際部署平台、claude.ai 連接器掛載(需公開 HTTPS host)、選項 A(純 Cloudflare Worker 邊緣)若日後要邊緣化再做。

接 remote MCP 時**照既有 `fellow-aiden-mcp` 的慣例**(設計見 `docs/DESIGN_v0.2.md` §13)。該參照專案是 Cloudflare Workers remote MCP(`agents` McpAgent + `@modelcontextprotocol/sdk` + zod)。必守重點:

- **雙重 token 認證(load-bearing)**:`MCP_AUTH_TOKEN` 同時接受 `Authorization: Bearer` 與 `?token=` query param;claude.ai 網頁連接器只能用 query 那條,別拿掉。未設密鑰 fail-closed。
- **CORS 鎖 `*.claude.ai`**;`/mcp` + `/health` 路由。
- **工具註冊**:rich description 寫滿約束;跨欄位規則在 handler 內 `safeParse` 補驗。結果用 `{content:[{type:text}], isError?}`。
- **外部呼叫獨立模組**:typed error、401 re-login+retry、防禦式解析。
- **落地抉擇**:CIE 重 ML 依賴跑不進 Workers。**本輪採選項 B**(Python 原生 remote MCP:`server_http.py`,薄傳輸+認證+寫入閘,引擎邏輯全留 `cie.*`)。**選項 A**=薄 TS Worker 邊緣(照 Aiden 模式)+ Python ML 後端 HTTP API 當 worker,留作日後要純 Cloudflare 邊緣時的路徑;兩者不互斥。`mcp_server.py`(stdio)續作本地/Claude Code 直連。
- 參照原始碼路徑(若已連接上層資料夾):`../fellow-aiden-mcp/src/{index,fellow,profile}.ts`。
