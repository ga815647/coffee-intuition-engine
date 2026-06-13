# 咖啡直覺引擎 — 語意記憶資料庫設計規格 v0.2

> **v0.2 變更摘要**(第二輪深度研究後):確認 AUDIT 四項硬傷皆成立並給出實作修訂——萃取機制三軌分立(§12.1)、水質層含關鍵科學爭議(§12.2)、信心區間改用 conformal/CQR(§12.3)、少樣本用貝氏收縮 + 冷啟動策略(§12.4)、爬取合法性與資料來源(§12.5)、並用既有咖啡 ML 研究校準期待值(§12.6)。新增內容見文末 §12,原 §1–§11 為 v0.1 骨架仍有效,差異處以 §12 為準。

> 目標:打造一個「AI 咖啡大佬」。凡是人類咖啡大佬會的——生豆判讀、烘焙理解、杯測、沖煮調參、方法移植、風味診斷、把模糊感官語言翻成可執行參數——AI 大佬都要會。本文件是可照著建的技術規格,並含對抗式 AUDIT。
>
> 部署:雲端託管向量庫｜交付:Markdown 設計文件｜研究深度:深度(含外部佐證)

---

## 0. 一頁摘要(TL;DR)

「直覺」不是玄學,而是一個**映射函數**:給定〔豆/焙條件 + 沖煮參數〕,輸出〔風味〕;反向則是診斷與配方設計。我們用三件事把它建出來:

1. **把所有東西投影到物理軸**,而不是記泡法名稱——這樣才能「沒喝過也推得出味道」、能換泡法遷移推理。
2. **語意向量庫**存每一筆校準經驗(豆況＋參數＋風味＋來源分級),模糊比對撈最近鄰、加權內插出起手參數。
3. **資料分級加權**:杯測協定下產生的高品質標籤(A 級)定方向與錨點,海量社群配方(C 級)只壓雜訊、補密度。

**誠實的邊界**:模型沒有味覺受器,無法「嚐」。它複製的是大佬的**知識、推理與決策邏輯**,不是大佬的舌頭。因此它是「**你的感官的高保真放大器**」——統計先驗它完勝任何單一人類,最終「這杯到底對不對」的裁決仍需少量人類校準回饋。期待設在這,系統很強;設成「它自己會品鑑」,會持續失望。

---

## 1. 設計目標與非目標

**目標**
- 覆蓋人類咖啡大佬的完整能力盤(見 §2 能力對映表)。
- 跨泡法遷移推理:V60↔愛樂壓↔法壓↔義式,味道位移可預測。
- 越用越準:每次校準寫回,個人化收斂。
- 可解釋:每個建議能回溯到「依據哪幾筆經驗、哪條物理規律」。

**非目標(明確排除,避免幻覺)**
- 不宣稱模型「有味覺」或能取代品鑑。
- 不在空庫/資料稀疏時硬給精確數值(改給物理先驗 + 信心區間)。
- 不把數值講成絕對真值——延續既有鐵則:方向可信、量保守。

---

## 2. 核心架構:三層空間 + 一個映射

```
       ┌─────────────────────┐
       │  L1 豆/焙條件層       │  產地·品種·處理法·焙度·養豆天數
       │  (起始物料,移動映射) │
       └──────────┬──────────┘
                  │ 條件化
                  ▼
   ┌──────────────────────────┐        映射函數 = 「直覺」
   │  L2 參數空間(物理軸)    │  ───────────────────────────►  ┌────────────────────────┐
   │  溫度·粉水比·研磨·時間·   │   以萃取物理為骨幹             │  L3 風味空間(杯測量化)  │
   │  擾動·均勻度·浸泡vs滴濾   │   (TDS / 萃取率為樞紐)        │  酸甜苦·body·餘韻·乾淨度 │
   └──────────────────────────┘  ◄───────────────────────────  └────────────────────────┘
                                    反向 = 診斷 / 配方設計
```

### 2.1 L1 — 豆/焙條件層(起始物料)

不是參數,而是**決定映射落在哪**的條件變數。同一組沖煮參數,淺焙日曬與深焙水洗會落到完全不同的風味點。欄位:

| 欄位 | 型別 | 說明 |
|---|---|---|
| origin / variety | 類別 | 產地、品種(影響風味潛勢) |
| process | 類別 | 水洗 / 日曬 / 蜜處理 / 厭氧(影響甜感、發酵調、body) |
| roast_level | 數值 | Agtron 數值或淺/中/深(連續化) |
| development (DTR) | 數值 | 發展時間比,影響可溶性與酸的轉化 |
| days_off_roast | 數值 | 養豆天數,影響排氣/通道效應/悶蒸 |
| density / moisture | 數值(選填) | 生豆密度、含水率 |

### 2.2 L2 — 參數空間(物理軸)

關鍵設計:**把每種泡法拆解投影到同一組底層物理量**,泡法名稱只是這些軸的一組座標。這就是跨方法遷移推理的基礎。

| 物理軸 | 量綱 | 對萃取的作用 |
|---|---|---|
| water_temp | °C | 萃取速率↑(非揮發物);80–99°C 區間對浸泡平衡常數不敏感* |
| brew_ratio | 水:粉 | **最重要**;TDS≈與粉水比成反比* |
| grind_size | µm / 刻度 | 表面積→速率;對浸泡平衡常數不敏感但對滴濾極敏感* |
| contact_time | s | 總接觸時間→萃取率 |
| agitation / turbulence | 等級 | 擾動→均勻度與速率 |
| extraction_uniformity | 指標 | 通道效應的反指標 |
| immersion_vs_percolation | 軸 | 滴濾萃取速率 > 浸泡* |
| **派生:TDS** | % | 濃度(strength) |
| **派生:extraction_yield (EY)** | % | 萃取率;浸泡下與粉水比無關* |

\* 來源:Nature《equilibrium desorption model》(見 §11)。

**樞紐**:TDS 與 EY 是連接 L2→L3 的物理橋樑(SCA Brewing Control Chart 的兩軸)。理想盒約 TDS 1.15–1.35%、EY 18–22%,但「理想」最終由 L3 個人校準定義,不是固定框。

### 2.3 L3 — 風味空間(杯測量化軸)

**為什麼一定要量化**:「明亮/平衡/醇厚」每人指不同的東西。要讓 A 級高品質標籤不被語意噪音糊掉,風味端必須投影到固定數值軸。採 SCA 杯測表 + WCR 風味詞典(2016 版,110 個帶實體參照標準的描述詞)。

| 風味軸 | 範圍 | 備註 |
|---|---|---|
| acidity_intensity | 0–10 | 並記**酸型**:檸檬酸 / 蘋果酸 / 醋酸(WCR 區分) |
| sweetness | 0–10 | 消費者喜好主驅動之一 |
| bitterness | 0–10 | 與高 TDS 正相關 |
| body / mouthfeel | 0–10 | |
| aftertaste | 0–10 | |
| balance | 0–10 | |
| cleanliness / clarity | 0–10 | 通道效應與缺陷的反指標 |
| flavor_notes[] | 標籤 | 對映風味輪節點(花/果/堅果/焦糖…) |
| defects[] | 標籤 | 過萃苦澀、萃取不足尖酸、發酵過度等 |

**已知物理→風味先驗(來自萃取研究,作為映射的初始骨架)**:
- 高 TDS → 苦、煙燻、烘烤調↑
- 高 TDS + 低 EY → 尖酸、柑橘酸↑
- 低 TDS → 甜感、茶感、花香↑

### 2.4 映射函數 = 「直覺」本體

直覺 = 從〔L1 + L2〕到〔L3〕的擬合映射。三種運作方向:

- **正向(預測)**:給豆/焙 + 參數 → 預測風味。「沒喝過也推得出大概」。
- **反向(診斷)**:給「實際風味偏差」(太酸/悶) → 推「往哪個物理軸調」。
- **設計(配方)**:給「目標風味」 → 反解一組參數,並標明物理紅線。

換泡法 = **在 L2 空間平移座標**,經映射得 L3 位移。例:V60→愛樂壓 = 接觸時間↑、紊流↑、均勻度↑ → 預測酸度鈍化、body 增厚、甜感前移。本質是**機制內插**,不是查表。

### 2.5 為什麼烘焙與杯測是關鍵(回應「焙/杯也要懂」)

- **烘焙**:是 L1 的核心條件,且**動態改變 L2→L3 映射**。深焙=可溶性↑、萃取快→需收(降溫/變粗/縮時);淺焙=需更積極萃取以避尖酸。養豆天數影響排氣與悶蒸穩定度。不懂焙,等於不知道映射被搬到哪。
- **杯測**:是讓 A 級標籤**可跨人比較的協定**。杯測之所以固定比例、研磨、水溫、評分表,正是為了消除「每個人舌頭是不同的尺」。所以——**杯測協定遵循度本身就是分級依據**(見 §3),不是看「誰測的」,而是看「在多標準化的協定下測的」。AI 大佬要「懂杯測」= 懂這套協定如何把主觀感官轉成可比數值,以及如何據此給資料加權與識別缺陷。

### 2.6 能力對映表(大佬會的,AI 大佬如何覆蓋)

| 人類大佬能力 | AI 大佬如何覆蓋 | 機器極限 / 需人補 | 主要資料層 |
|---|---|---|---|
| 生豆/產地判讀 | 知識檢索 + 條件化映射 | 視覺挑豆需影像 | L1 |
| 烘焙理解 | 焙度→萃取/風味的因果推理 | 不能實體烘、不能嚐烘焙樣 | L1↔映射 |
| 杯測 | 懂協定、據以加權、識別缺陷描述 | **實際感官評分需人類舌頭** | L3 + 分級 |
| 沖煮調參 | 核心引擎,正向預測 | — (AI 完勝統計先驗) | L2→L3 |
| 風味診斷 | 反向映射,問題歸因 | 需可信的風味回饋輸入 | 映射反向 |
| 方法移植/配方設計 | L2 空間內插 + 物理紅線 | — | L2/映射 |
| 風味語言溝通 | 模糊描述→參數的翻譯器 | 受輸入精度上限約束 | L3 詞典 |
| 感官(品鑑本身) | ✗ 無法複製 | **完全需人**,系統放大而非取代 | 校準回饋 |

一句話:**知識與推理類能力 AI 完勝;感官裁決類能力需你少量校準。** 後者因先驗已強,所需杯數很少即可收斂。

---

## 3. 資料分級與加權(校準品質,不是名氣)

加權的對象是**標籤的校準品質**,不是「人紅不紅」。

| 級別 | 定義 | 用途 | 權重 |
|---|---|---|---|
| **A** | 閉環、可複現、標準化協定下產生(SCA 杯測、競賽配方、Hoffmann/Kasuya 等具明確方法的來源、你自己的精確校正) | **定映射方向與錨點**(因果規律) | 高 |
| **B** | 有對照、描述具體但單人主觀、協定部分遵循 | 補充、區域微調 | 中 |
| **C** | 論壇/社群海量配方,標籤不一致、開環 | **只壓隨機雜訊、估量級/密度** | 低 |

**核心風險**:百萬筆 C 級會稀釋幾十筆 A 級的訊號。對策:加權檢索時 A 級的有效權重須能壓過大量 C 級的票數(見 §5 加權公式),且映射「方向」只允許 A/B 級定義,C 級不得改方向、只能調量級。

> 延續既有鐵則:數值是官方來源回歸的平均傾向,非真值;方向可信、量保守。

---

## 4. 向量庫 Schema(雲端託管)

**選型建議:Qdrant Cloud**(最佳性價比,1M 向量約 $25–45/月,開源、無鎖定;若要零維運可選 Pinecone Serverless,操作更簡單但成長期成本較高)。嵌入模型建議 Voyage 或 OpenAI text-embedding-3。

### 4.1 關鍵設計決策:混合記錄(避免「用詞像但情境不像」)

這是這類系統最常翻車的點:把所有東西塞成一段文字去嵌入,撈回來的「相似」只是**用詞相似**,不是**情境相似**。對策是**混合記錄**:

- **語意向量**:只嵌入「情境的標準化文字描述」(豆況 + 風味敘述),負責模糊召回。
- **結構化 payload**:參數軸、TDS/EY、焙度、分級等**數值**獨立存為可過濾、可加權欄位——不靠嵌入去理解數字(數值嵌入易失真:同數值不同語意)。數值先做正規化(z-score;右偏特徵用 power-law)。
- **檢索**:語意召回 + 結構化過濾 + 物理距離項 + 分級加權,四者結合(見 §5)。

### 4.2 Collection schema(Qdrant point 範例)

```jsonc
{
  "id": "uuid",
  "vector": [/* 情境文字的嵌入,dim 視模型 */],
  "payload": {
    // --- L1 豆/焙 ---
    "origin": "Ethiopia Yirgacheffe",
    "variety": "Heirloom",
    "process": "washed",
    "roast_level_agtron": 72,        // 正規化後另存 roast_z
    "dtr": 0.21,
    "days_off_roast": 12,

    // --- L2 參數(原值 + 正規化) ---
    "water_temp_c": 92,
    "brew_ratio": 16.0,              // 水:粉
    "grind_um": 650,
    "contact_time_s": 150,
    "agitation_level": 2,
    "method": "V60",                 // 僅標籤;推理走物理軸
    "tds_pct": 1.38,
    "ey_pct": 20.4,

    // --- L3 風味(杯測量化) ---
    "acidity": 7.5, "acidity_type": "citric",
    "sweetness": 7.0, "bitterness": 3.0,
    "body": 5.5, "aftertaste": 6.5, "balance": 7.0, "clarity": 8.0,
    "flavor_notes": ["bergamot", "white_floral", "stone_fruit"],
    "defects": [],

    // --- 校準與來源 ---
    "grade": "A",                    // A/B/C
    "protocol": "SCA_cupping",       // 標籤產生的協定
    "source": "competition_2025 / user_calibration / forum",
    "confidence": 0.9,
    "user_id": "self",               // 區分個人偏好層 vs 客觀因果層
    "timestamp": "2026-06-13",
    "embedding_text": "淺焙 衣索比亞水洗,柑橘酸明亮、白花、乾淨..."  // 可重建嵌入
  }
}
```

### 4.3 記憶分兩層(避免混淆)

- **客觀因果層**(`user_id=global`):研磨↔萃取率這類物理規律,跨人通用。
- **個人偏好層**(`user_id=self`):你愛的酸甜平衡點,會個人化收斂。
檢索時兩層分別貢獻:因果層定「會怎樣」,偏好層定「你要哪樣」。**不可混合平均**。

---

## 5. 檢索 / 推理邏輯

### 5.1 起手參數推薦(正向)

```
輸入:新豆/焙條件 (+ 可選目標風味)
1. 結構化過濾:同 process、roast 近鄰、days_off 合理區間(硬條件)
2. 語意召回:情境文字嵌入 → top-K 最近鄰(模糊比對,跨豆相似情境)
3. 混合分數:dense 相似 + BM25(風味詞) 經 RRF 融合      // 召回+15~30%
4. 分級加權:score_i *= w(grade_i) * confidence_i        // A>>C
   並施加「方向鎖」:映射方向只由 A/B 級鄰居投票
5. 物理內插:在 L2 軸上對加權鄰居做內插 → 起手參數
6. cross-encoder rerank(選配,精度再+)
7. 輸出:參數 + 信心區間 + 依據鄰居清單(可解釋)
```

加權公式(示意):`final_i = sim_i × grade_weight[grade_i] × confidence_i × recency_decay`,其中 `grade_weight = {A:1.0, B:0.4, C:0.1}`,且設下限要求 top 結果中 A 級累積權重佔比 ≥ 閾值,否則降級為「物理先驗 + 低信心」輸出(防 C 級洗票)。

### 5.2 換泡法預測(遷移)

```
1. 取當前 (參數, 風味) 為原點
2. 目標泡法 → 在 L2 軸上的座標位移 Δ(時間/紊流/均勻度/滴濾vs浸泡)
3. 經映射(物理先驗 + 鄰居校正)推 L3 位移
4. 輸出:預測風味變化 + 不確定度(離 A 級錨點越遠,區間越寬)
```

### 5.3 診斷(反向)

```
輸入:實際風味偏差(例「尖酸、收尾水」)
1. 對映到 L3 缺陷向量
2. 反查映射:哪些 L2 軸位移最可能造成此偏差(萃取不足→尖酸)
3. 輸出:排序的調整建議 + 每項的預期效果與信心
```

---

## 6. MCP 工具規格

先做 3 支即可,圖譜密度夠了再擴充。每支都回傳「依據」以維持可解釋與防幻覺。

### 6.1 `query_flavor_map` — 查相似情境、出建議
```jsonc
// input
{ "bean": {...L1...}, "params": {...L2 可選...},
  "target_flavor": {...L3 可選...}, "mode": "recommend|predict|diagnose" }
// output
{ "suggested_params": {...}, "predicted_flavor": {...},
  "confidence": 0.0-1.0, "evidence": [{id, grade, why}...],
  "physics_note": "依萃取模型...", "warnings": ["離錨點較遠,量保守"] }
```

### 6.2 `log_calibration` — 寫回校準
```jsonc
// input:一筆完整 (L1+L2+L3+grade+protocol);自動正規化、嵌入、寫入向量庫 + 同步 Notion
{ "record": {...schema §4.2...} }
// output
{ "id": "uuid", "neighbors_updated": n, "drift_alert": false }
```

### 6.3 `predict_method_swap` — 換泡法推味道
```jsonc
// input
{ "from": {method, params}, "to_method": "AeroPress", "bean": {...} }
// output
{ "param_translation": {...}, "predicted_flavor_delta": {...},
  "uncertainty": "low|med|high", "evidence": [...] }
```

---

## 7. 與現有 Notion + Aiden MCP 接法

- **Notion**:當「人看的前台 + 真相來源(raw)」。回饋 DB 維持現狀,新增分級/協定/TDS/EY 等欄位。
- **向量庫**:當「機器用的後台」,存嵌入 + 正規化數值,供模糊檢索。
- **同步**:`log_calibration` 一次寫兩邊(Notion 可讀、向量庫可檢索);夜間批次校驗一致性。
- **Aiden brew MCP**:現有 `validate_aiden_profile` / `create_aiden_brew_link` 維持;新引擎的輸出參數通過 validate 後再生連結,確保不破物理紅線。
- **角色分工不變**:沖煮看規則頁、回饋看回饋頁;新引擎屬「進階配方」層,**永遠與基準並列,不覆蓋基準**。日常產 basic profile 時,引擎視為不存在(延續鐵則)。

---

## 8. 冷啟動策略(防空庫幻覺)

資料稀疏期最危險——空庫做最近鄰只會生幻覺。分階段:

1. **Phase 0 純物理先驗**:庫空時,只用萃取模型 + SCA 控制圖給「起手 + 寬信心區間」,明說「尚無經驗資料」。
2. **Phase 1 A 級種子**:先灌競賽配方、Hoffmann/Kasuya 等具明確方法的 A 級錨點(數十~數百筆),映射方向先立起來。
3. **Phase 2 個人校準**:你每次校正寫回,個人偏好層開始收斂。
4. **Phase 3 C 級補密度**:最後才大量灌社群配方,且只允許其調量級、不准改方向。

**信心閘**:當某查詢的 A/B 級鄰居不足,一律退回物理先驗 + 標註低信心,不硬給精確數字。

---

## 9. 落地步驟(分階段)

| 階段 | 產出 | 重點 |
|---|---|---|
| 1 | 確定 schema + 正規化規則 | L1/L2/L3 欄位與量綱凍結 |
| 2 | 開 Qdrant Cloud + 嵌入管線 | 混合記錄、payload 過濾驗證 |
| 3 | 灌 A 級種子 + 物理先驗 | 映射骨架可跑 |
| 4 | 實作 3 支 MCP 工具 | 先 query + log,再 swap |
| 5 | 接 Notion 雙寫同步 | 一致性校驗 |
| 6 | 加權檢索 + 方向鎖調校 | 防 C 級洗票 |
| 7 | 個人校準迴圈上線 | 偏好層收斂 |
| 8 | rerank / 圖譜擴充 | 密度足後再上 |

---

## 10. AUDIT — 對抗式審查

我用「想讓這個系統失敗」的角度,逐項攻擊上面的設計。標 🔴 為會動搖可行性的硬傷、🟡 為須處理但有解、🟢 為已涵蓋僅提醒。

### 10.1 風險清單

| # | 風險 | 嚴重 | 說明 | 緩解 |
|---|---|---|---|---|
| A1 | **物理先驗用錯了萃取機制** | 🔴 | §2.2 引的平衡常數「對研磨/焙度/溫度不敏感」是**全浸泡(full immersion)專屬**結論。滴濾(V60)下 EY 對研磨、流速、擾動**極度敏感**。把浸泡物理當通用先驗套到 V60,是分類錯誤。 | 映射先驗**按萃取機制分軌**(浸泡 / 滴濾 / 加壓各一組),不可共用。schema 已有 `immersion_vs_percolation` 軸,需升級為「機制條件化映射」。 |
| A2 | **水質完全沒進模型** | 🔴 | 整份設計漏了**水化學(礦物質/硬度/鹼度)**——這是萃取與風味的一級變數,SCA 有專門水質標準。同參數不同水,味道天差地別。 | L1/L2 之間補一層 `water_profile`(GH/KH/TDS_water)。個人用戶至少記錄固定水,作為「控制變數」而非自由變數。 |
| A3 | **回寫迴圈污染(model collapse)** | 🔴 | 若把引擎**自己的預測**當校準寫回庫,沒有獨立感官真值,系統會自我增強偏差,越用越自信但越錯。 | `log_calibration` 的 A 級寫入**強制要求人類感官標籤**;AI 預測只能存為 `prediction`,不得標 A 級、不得進方向投票。 |
| A4 | **C 級發表偏差是系統性的,量壓不掉** | 🟡 | 我在正文說「量能壓雜訊」——對隨機雜訊成立,但**發表偏差(難喝的沒人貼)是系統性偏移**,平均一百萬個有偏樣本仍有偏。 | C 級**只准用於最粗的量級先驗**,且不得參與風味方向與絕對值;明確標記其為「社群傾向」而非真值。 |
| A5 | **A 級錨點本身缺量化風味標籤** | 🟡 | Hoffmann/Kasuya 等方法的風味描述是**散文,不是杯測數值**,且方法是 method-specific。直接當 L3 錨點會帶失真。 | 移植時依既有鐵則「明列失真點、限適用帶」;錨點先過一道**人工杯測量化**或僅作「方向錨」不作「數值錨」。 |
| A6 | **研磨用單一數值,丟失分佈** | 🟡 | 「650µm」掩蓋了**細粉佔比與雙峰分佈**,而細粉主導通道效應與苦澀。ZP6 一致性好,但單數值軸仍有損。 | 記錄研磨**刻度 + 磨豆機型號**(ZP6 已固定→可當常數);未來可加「細粉指標」選填欄。 |
| A7 | **遷移推理的有效範圍被高估** | 🟡 | 粗物理軸對「濾沖之間」遷移尚可,但跨到**義式(加壓)**時,壓力/預浸/粉餅動力學不在軸上,預測會不可靠。 | 明定**遷移信封**:同機制內高信心、跨機制(濾↔義式)標「僅定性、高不確定」。 |
| A8 | **信心數值是假精確** | 🟡 | 輸出一個 `confidence:0.9` 很容易,但要**校準良好**很難,否則誤導比沒有更糟。 | 用 conformal prediction / 留出集驗證產生**有覆蓋保證的區間**,而非拍腦袋的單一信心值。 |
| A9 | **人類校準者本身會漂移** | 🟡 | 你的舌頭隨時間/疲勞/健康漂移,「真值」不穩定;這正是杯測要標準化的原因。 | 校準記錄**當日狀態 + 用配對/相對判斷**(A vs B 哪個酸)而非絕對打分;偏好層加時間衰減。 |
| A10 | **文字距離與數值距離尺度不一致** | 🟡 | §4.1 要把語意 cosine 相似和正規化數值距離結合,兩者尺度不同,直接相加會被某一邊主導。 | 用**兩階段**:結構化硬過濾 + 語意召回候選 → 再用學習到的權重對數值距離 rerank,避免裸相加。 |
| A11 | **「百萬杯」的現實落差與合法性** | 🟡 | 個人用戶實際資料是數千筆,撐不起百萬;百萬靠爬取,涉及**網站 ToS/版權**,且爬來的配方多半**缺設備脈絡**,作為 C 級價值低於預期。 | 不追求百萬;以「**少量高品質 A/B + 中量結構化社群**」為務實目標。爬取前確認來源授權。雲端庫對數千筆是過度配置——可先用 Qdrant 免費層/本機,規模到了再上雲。 |
| A12 | **沒有引擎自身的評測迴圈** | 🔴 | 最致命的盲點:**怎麼知道它變準了還是在漂?** 沒有 held-out 盲測,無法分辨改善與退化。 | 建**盲測評測集**:對庫中沒有的豆,引擎先預測 → 你盲沖盲評 → 量化誤差(MAE on L3 軸)。每次重大更新跑一次,當回歸測試。 |

### 10.2 對設計的必要修正(已反映於上表,需回填正文)

1. **映射按萃取機制分軌**(A1)——這是正確性層級的修正,不分軌會系統性出錯。
2. **新增水質層**(A2)——目前最大的單一遺漏變數。
3. **回寫須人類感官真值,AI 預測不得自我標記為校準**(A3)——防 model collapse 的硬規則。
4. **新增盲測評測集**(A12)——沒有它,整個「越用越準」的宣稱無法驗證。

### 10.3 經得起攻擊的部分(確認可行)

- **物理軸投影 + 機制內插**作為遷移基礎:成立,只要分軌(A1)。
- **A/B/C 校準品質加權 + 方向鎖**:成立,且正確地對抗了 C 級洗票。
- **混合記錄(語意向量 + 結構化 payload)**:成立,正確避開數值嵌入失真。
- **冷啟動的物理先驗退路 + 信心閘**:成立,正確防空庫幻覺。
- **「放大器不是味覺」的定位**:這是全案最重要的誠實,守住它,期待就不會崩。

### 10.4 裁決

**可行,且架構健全——但有三個會「無聲出錯」的硬傷必須先補,否則系統會自信地給你錯答案:**(1) 萃取機制分軌、(2) 水質變數、(3) 防自我回寫污染 + 盲測評測。補上這四項(連同 A12),這套設計就站得住。

核心定位不變:**它是你感官的高保真放大器**。統計先驗它完勝任何單一人類;最終裁決需要你少量、誠實、標準化的校準。把它建成「會推理的學徒」,不是「有舌頭的大佬」——這樣它會非常強,且不會騙你。

---

## 11. 參考來源

- Nature《An equilibrium desorption model for the strength and extraction yield of full immersion brewed coffee》— https://www.nature.com/articles/s41598-021-85787-1
- SCA Brewing Fundamentals / Brewing Control Chart — https://sca.coffee/brewing-research
- ScienceDirect《Coffee extraction: parameters and influence on flavour》— https://www.sciencedirect.com/science/article/abs/pii/S0924224419305692
- SCA / WCR Coffee Taster's Flavor Wheel & Sensory Lexicon — https://sca.coffee/research/coffee-tasters-flavor-wheel
- RAGFlow《From RAG to Context: 2025 review》(hybrid retrieval, RRF, rerank) — https://ragflow.io/blog/rag-review-2025-from-rag-to-context
- Gorishniy et al.《On Embeddings for Numerical Features》(NeurIPS 2022) — https://arxiv.org/pdf/2203.05556
- Qdrant vs Pinecone 2026 成本/選型 — https://www.kalviumlabs.ai/blog/vector-databases-compared-pgvector-pinecone-qdrant-weaviate/
- MCP + 向量記憶模式(Milvus / sqlite-vec / LanceDB) — https://milvus.io/docs/milvus_and_mcp.md

---

## 12. v0.2 研究增補與設計修訂(第二輪深度研究)

四路平行研究 + 交叉驗證後的可用結論。每節先給「已驗證事實(含信心)」,再給「對設計的具體修訂」。**最大教訓:幾條通俗咖啡知識被同儕審查證據推翻,系統不能照搬網路常識當因果。**

### 12.1 萃取機制必須三軌分立(確認 AUDIT A1 成立)

**已驗證事實:**
- 全浸泡會趨近**熱力學平衡**:平衡萃取率 E 與粉水比無關(3–25 範圍恆約 21%),且對水溫(80–99°C)、研磨(579–1311µm)、攪拌**不敏感**——這些只改變「達到平衡的速度」,不改終點。〔高信心,UC Davis/Nature〕
- 滴濾/注水是**流動非平衡系統**:新鮮水持續維持高濃度梯度,永不達平衡,E 由流體傳輸(研磨、流速、床均勻性)主控,對研磨與流速**極度敏感**。〔高信心,Hoffmann/Coffee ad Astra/Scott Rao〕
- 義式**自成一類**:約 9 bar 加壓滲濾、20–30 秒、粉餅阻力主導;E 對研磨呈**非單調(峰值)**關係——磨太細誘發通道效應反而降低 E 並破壞重現性(Matter 2020 實測,推翻「越細萃取越多」直覺)。〔高信心〕
- 關鍵「假矛盾」釐清:入門教材說「溫度↑/攪拌↑/磨細→萃取↑」描述的是**未達平衡的動力學**;UC Davis 說「不影響終點」描述的是**達平衡的終點**。兩者不矛盾,是「速率 vs 終點」之別。

**設計修訂:**
- 映射函數**按機制分三套**:`immersion` / `percolation` / `pressure`,各有獨立的物理先驗與敏感度權重,**不可共用、不可跨機制平均**。
- schema 的 `immersion_vs_percolation` 升級為必填類別欄 `brew_mechanism ∈ {immersion, percolation, pressure}`,作為 L2 的**硬分區鍵**(檢索先按此硬過濾)。
- 跨機制遷移(§5.2)標為**高不確定、僅定性**;同機制內才給數值級建議。

### 12.2 新增水質層 + 一個重大科學爭議(確認 AUDIT A2 成立)

**已驗證事實(數字):**
- SCA/SCAA 2009 標準:總硬度目標 **68 mg/L CaCO₃(範圍 17–85)**、鹼度目標 **~40 mg/L**、TDS 目標 **150(範圍 75–250)**、pH **7(6.5–7.5)**。〔高信心〕
- ⚠️ **網路盛傳的「總硬度 50–175 ppm」是以訛傳訛**(混入 TDS 上限/舊 grain 換算),應採 68(17–85)。
- 同樣 GH/KH 數字下,**陽離子組成(Ca:Mg 比)不同會明顯改變風味**——光記 GH/KH/TDS 三個數字不足以鎖定水。〔中信心,Barista Hustle〕

**已驗證事實(爭議——這是本輪最重要的發現):**
- 通俗口訣「鎂=明亮果酸、鈣=醇厚 body」(源自 Hendon《Water for Coffee》)被兩路證據挑戰:
  - **同儕審查 GC-MS/NMR 研究(Bratthäll et al.)**:鎂/鈣陽離子**並未促進有機酸萃取**;酸的萃取「獨立於水組成」。陽離子「更可能影響知覺,而非萃取本身」。〔高信心,方法學最嚴謹〕
  - **Royal Coffee 雙盲杯測**:實測是**鈣→強調酸質/明亮**、**鎂→甜感/花香/body**——與通俗口訣相反;且**蒸餾水(0 礦物)在一場雙盲中拿最高分**。〔中信心,小樣本〕
- 高鹼度(碳酸氫鹽)會緩衝壓抑酸度、使咖啡平淡 chalky。〔高信心〕

**設計修訂:**
- 新增 `water_profile` 欄組:`GH`、`KH/alkalinity`、`TDS_water`、`pH`、**`Ca_Mg_ratio`**、`recipe_name`。
- **水質設為「固定控制變數 / 批次標籤」,不是每杯自由變數**——一致性比命中精確數字重要;漂移的水會掩蓋豆/研磨/比例的訊號,使 dial-in 與診斷失效。
- **鐵則(防偽因果)**:系統**不得**把「鎂→明亮」這類通俗水-風味因果寫死進映射。水對風味的因果證據互斥且未定論,故水欄位只作**分群/控制變數**,不進風味方向投票。若要研究水,開獨立「水實驗」維度,一次只動水。
- 「分析測得的酸含量」≠「杯中風味」(陽離子螯合會干擾儀器讀數)——延續鐵則:數值非真值。

### 12.3 信心區間改用 Conformal Prediction(確認 AUDIT A8 成立)

**已驗證事實:**
- Conformal prediction 對**任意底層模型**提供**有限樣本、分佈無關**的覆蓋保證 P(Y∈C(X))≥1−α,唯一前提是**可交換性**(比 i.i.d. 弱)。〔高信心〕
- 少樣本下方法仍**有效**,但退化形式是「實現覆蓋的變異變大」(某些校準分割會低於目標),非偏差變大;可用 **Small Sample Beta Correction (SSBC)** 修正。〔A5 高信心 / A6 SSBC 中信心,單一近期 preprint〕
- **CQR(Conformalized Quantile Regression)**讓區間隨輸入自適應寬窄(異方差友善),通常更短同時保覆蓋。〔高信心〕
- ⚠️ 可交換性在**漂移**下失效——這正對應人類味覺漂移(§12.4),純 conformal 保證會打折,需加權修正。

**設計修訂:**
- §6 工具輸出的 `confidence: 0.0-1.0` 單一數字**廢除**,改回傳 **CQR 預測區間**(每條 L3 風味軸給 [下界, 上界] @ 90% 名目覆蓋)。
- 小資料期套用 **SSBC** 調整顯著水準;明示「覆蓋為平均意義,非每次保證」。
- 因味覺漂移破壞可交換性,採**加權 conformal**(近期校準權重高),並把區間覆蓋當作 §12.6 評測指標之一(實測覆蓋 vs 名目)。

### 12.4 少樣本用貝氏收縮 + 冷啟動策略

**已驗證事實:**
- **貝氏收縮 / empirical Bayes**(James-Stein 等)在樣本少時把估計往先驗(全體平均)拉,降低過度自信;d≥3 維時 James-Stein 在平方誤差下**處處優於**樣本均值。〔高信心,經典定理〕但對「異常個體」過度收縮會傷害,需限制收縮量。〔中信心〕
- **Content-based 先驗**(用豆/焙/參數特徵)能解冷啟動,因不需互動記錄即可對新情境打分;**混合式**(content 注入協同模型)通常最佳。〔高信心〕
- **k-NN/檢索式在稀疏時的失效模式**:相似度由極少共同樣本算出而不可靠(極端時 1 個共評項就給相似度=1)。緩解:設「最少鄰居數」門檻、剔除稀有共現、改用不依賴共現的相似度。〔高信心〕
- **Contextual bandit(LinUCB/Thompson)**可把冷啟動轉為探索-利用,從首次互動就平衡探索。〔中-高信心〕

**設計修訂:**
- §5.1 的加權內插升級為**層級貝氏收縮**:某豆況鄰居少時,估計自動往「同機制 + 同處理法 + 同焙度帶」的群組先驗收縮,鄰居越多收縮越弱。取代原本裸的加權平均(也解決 AUDIT A10 的尺度問題:收縮在統一機率尺度上做)。
- **k-NN 防稀疏**:檢索設 `min_neighbors` 與 `min_shared_context` 門檻,不足則退回群組先驗 + 寬區間(銜接 §8 信心閘)。
- **主動校準(進階)**:把「下一杯該試什麼參數最能學到東西」當 contextual bandit——系統可建議**資訊增益最大**的實驗點,加速收斂。列為 v0.3 選配。

### 12.5 資料來源與爬取合法性(確認 AUDIT A11,非法律意見)

**已驗證事實(管轄區差異大):**
- 美國:爬取**公開、免登入**資料一般不違反 CFAA(hiQ v. LinkedIn;Van Buren 收窄「逾越授權」)。〔高信心〕但 **CFAA 合法 ≠ 免責**——hiQ 最終因加州普通法「動產侵害/不當挪用」負 50 萬美元。〔高信心〕
- **沖煮參數是事實資料,不受美國著作權保護**(Feist:事實不可著作權化、拒絕「額頭流汗」原則);但配方附帶的**敘事文字/品飲筆記受保護**。〔高信心〕
- **歐盟另有資料庫 sui generis 權利**(對「實質投資」的資料庫提供保護,即使內容是事實),美國無此權利。〔中-高信心〕
- 若資料含可識別個人(論壇帳號等)可能觸發 **GDPR**;純參數通常不觸發。〔中信心〕
- robots.txt 多數轄區非法律契約,但德國法院承認其可執行性;遵守是降風險最佳實務。〔高信心〕

**設計修訂(C 級資料蒐集規範):**
- **優先**用已公開資料集(如 Kaggle 上的 **CQI 咖啡品質資料集 ~1,340 筆**)與官方 API,而非自行爬取。
- 若爬取:尊重 robots.txt 與速率限制、**只取事實參數不複製敘事文字**、**不要登入帳號後再爬**(避開 clickwrap 契約拘束)、避開個人資料;涉及 EU 來源時注意資料庫權利與 GDPR。
- 呼應 AUDIT A11:不追求「百萬杯」;C 級的價值本就有限(缺設備/水質脈絡),合法且乾淨的中量 > 海量但有風險。

### 12.6 用既有咖啡 ML 研究校準期待值(新增,務實降溫)

**已驗證事實:**
- 用**客觀製程/沖煮變數**預測 SCA 杯測總分,真實難度高:Ferraz et al.(2026, RF/XGBoost)最佳 **R²≈0.53、MAE≈0.80**(中等預測力)。〔中-高信心〕
- 用**感官子項**預測總分可達 R²≈0.82,但有資訊洩漏疑慮(子項與總分高度相關),不可與客觀變數預測相提並論。〔中信心〕
- 感測器路線(e-nose/NIR/高光譜)對**烘焙度/強度分類**可達 88–98%,但那是**較窄的分類任務**,不代表能預測細緻感官品質。〔中-高信心〕
- 共同限制(跨多篇一致):**資料量小**(CQI 僅約 1,340 筆、~44 特徵)、**標籤主觀**(受偏好/疲勞/環境影響)、**跨資料集遷移困難**(CQI 偏泰國產/水洗)。〔高信心〕

**設計修訂(期待值管理):**
- 把引擎定位為**方向與排序**工具,不是「精準預測杯測分數」——後者連專門研究都只到 R²≈0.53。對外/對自己的宣稱都收斂到此。
- 風味預測輸出**相對比較與排序**(「A 參數比 B 更酸/更甜」)的信心,高於**絕對數值**;這也與 §12.3 區間、AUDIT A9「用配對判斷」一致。
- 警惕**資訊洩漏**:評測時嚴禁用「事後感官子項」回推總分;§12.6 的 R²≈0.82 陷阱要在盲測協定中明文排除。

### 12.7 更新裁決(v0.2)

第二輪研究的結論:**原架構方向全部站得住,且 AUDIT 抓的四項硬傷經外部證據確認為真、現已給出具體實作修訂。** 額外收穫是兩個「反常識」校正——(a) 水-風味的通俗因果不可信,水只當控制變數;(b) 預測精度的天花板比想像低(R²~0.5 級),系統價值在方向/排序而非絕對分數。

定位再次收斂、且更穩固:**它是一個「會分機制推理、給校準過的不確定區間、誠實知道自己預測上限」的學徒**,放大你的感官、不偽裝成味覺,也不照搬網路偽因果。這版可以開始落地。

**v0.2 來源(第二輪)**
- 萃取機制:https://www.nature.com/articles/s41598-021-85787-1 · https://www.cell.com/matter/fulltext/S2590-2385(19)30410-2 · https://coffeeadastra.com/2019/01/29/the-dynamics-of-coffee-extraction/
- 水質:https://www.baristahustle.com/diy-water-recipes-redux/ · https://dailycoffeenews.com/2018/08/29/a-practical-water-guide-for-coffee-professionals-part-ii-the-sensory-data/ · https://www.ncbi.nlm.nih.gov/pmc/articles/PMC10907646/
- 信心校準:https://arxiv.org/pdf/2107.07511 · https://arxiv.org/abs/1905.03222 · https://arxiv.org/html/2509.15349v1
- 冷啟動/收縮:https://www2.stat.duke.edu/~pdh10/Teaching/732/Notes/shrinkage.pdf · https://arxiv.org/pdf/1706.05730 · https://arxiv.org/pdf/1405.7544
- 爬取合法性:https://en.wikipedia.org/wiki/HiQ_Labs_v._LinkedIn · https://www.eff.org/cases/van-buren-v-united-states · https://supreme.justia.com/cases/federal/us/499/340/
- 咖啡 ML:https://ift.onlinelibrary.wiley.com/doi/10.1111/1750-3841.70946 · https://arxiv.org/abs/2509.18124 · https://www.kaggle.com/datasets/fatihb/coffee-quality-data-cqi

---

## 13. MCP / Worker 整合模式(參照 fellow-aiden-mcp)

把 CIE 接成「能被 claude.ai / Claude Code 呼叫的 MCP」時,沿用既有 `fellow-aiden-mcp` 的接線慣例,讓兩個 MCP 在你的生態裡一致。參照專案重點:

**fellow-aiden-mcp 的形態**:Cloudflare Workers 上的 **remote MCP(Streamable HTTP)**,技術棧 `agents` 的 `McpAgent`(Durable Object,SQLite-backed)+ `@modelcontextprotocol/sdk` 的 `McpServer` + `zod`。可重用的關鍵模式:

1. **工具註冊**:`server.registerTool(name, {description, inputSchema: zodShape}, handler)`。description 寫滿約束(讓 JSON Schema 把每個範圍/列舉文件化給模型);跨欄位規則(如陣列長度=數量)per-field shape 表達不了,於 handler 內再跑一次 `objectSchema.safeParse(input)` 補驗。
2. **結果形狀**:`{content:[{type:"text",text}], isError?}`;用 `textResult` / `errorResult` 小助手統一。
3. **邊緣 HTTP**:`/mcp` 端點 + `/health` 根路由;CORS 鎖 `*.claude.ai`。
4. **認證(load-bearing)**:共享密鑰 `MCP_AUTH_TOKEN`,**同時**接受 `Authorization: Bearer <token>` 與 `?token=` query param——因為 claude.ai 網頁連接器 UI 無法送自訂 header,只有 query 這條讓網頁版能用。未設密鑰則 fail-closed;比對用常數時間。
5. **外部呼叫「worker」邏輯獨立成模組**(該專案的 `fellow.ts`):typed error class、`401` 自動 re-login + 單次重試、防禦式解析、錯誤帶上游 body(截斷)。
6. **密鑰**:`.dev.vars`(本地 `wrangler dev`,gitignored)/ `wrangler secret put`(prod);`.dev.vars.example` 入庫。

### CIE 的落地抉擇(重要)

CIE 核心是 **Python + 重 ML 依賴**(qdrant-client、numpy、conformal),**跑不進 Cloudflare Workers**。兩條路:

- **選項 A(建議):薄 TS Worker 邊緣 + Python ML 後端。** Worker 完全照 fellow-aiden-mcp 模式(MCP 協定、token 認證、claude.ai CORS、工具 schema),但工具 handler 不自己算,而是 `fetch` 呼叫 Python 後端的 HTTP API(CIE 的 recommend/predict/diagnose/swap/log)。Python 後端即「worker」,類比 `fellow.ts` 那層的角色。好處:對 claude.ai 的接法與 Aiden 完全一致;Python 保有完整 ML 生態。
- **選項 B:Python 原生 remote MCP。** 用 Python MCP SDK 的 Streamable HTTP transport,自行實作同樣的邊緣慣例(Bearer/?token 認證、claude.ai CORS、/mcp + /health、rich tool description、typed error)。少一層、但要在 Python 端複刻 Worker 那套認證/CORS,且需自備可長跑的主機。

兩者都必須保留 §13.4 的**雙重 token 認證**(否則 claude.ai 網頁連接器無法掛上)。現有 `mcp_server.py`(FastMCP/stdio)留作本地開發與 Claude Code 直連;remote 形態另立,不互斥。

### 與 Aiden 的關係

CIE 完成後是獨立 MCP;Aiden 仍是 Aiden(日常沖煮/brew.link)。兩者透過 claude.ai 同時掛載即可協作:Aiden 產生/驗證 brew profile,CIE 提供直覺層的起手參數與診斷。CIE 不改 Aiden 基準(延續鐵則:進階配方與基準並列、不覆蓋)。

---

## 14. 託管選型更新:改用 Cloudflare 原生(取代 Qdrant)

**本節更新 §4 的「Qdrant Cloud 建議」與 §13 的落地抉擇。** 因為使用者已在 Cloudflare 上跑 Aiden,且 Qdrant Cloud 要錢,改用 Cloudflare 原生服務,整套留在同一生態、個人規模基本免費。

### 14.1 元件對映

| 角色 | 原方案 | 改用 Cloudflare |
|---|---|---|
| 向量庫 | Qdrant Cloud($) | **Vectorize**(免費額度覆蓋個人規模) |
| 嵌入 | Voyage/OpenAI($) | **Workers AI `@cf/baai/bge-m3`**(多語,適合中文風味筆記) |
| canonical 記錄 | — | **R2 或 D1** 存 JSONL(真相來源,可重建索引) |
| 邊緣/認證 | Worker(§13) | 同 §13,不變 |

### 14.2 額度與成本(2026 現況)

- Vectorize 免費:每月 500 萬 stored dimensions、3000 萬 queried dimensions;索引上限 1000 萬向量、最高 1536 維。
- Workers AI 免費:每天 10,000 neurons;`bge-m3` 約 1075 neurons/百萬 input tokens($0.012/百萬 tokens)。
- 維度:bge-m3=1024、bge-base-en=768、bge-small-en=384。
- 估算:5,000 筆 × 768 維(bge-base)≈ 384 萬 stored dims,**穩在免費內**;用 bge-m3(1024)≈ 512 萬,貼線、超出也僅幾分錢($0.05/億 stored)。個人查詢量遠在免費 queried dims 內。
- 結論:**個人規模實際帳單約 $0**。

### 14.3 架構簡化:純 Cloudflare 變可行

原 §13 因 Qdrant + Python ML 依賴,需「TS Worker 邊緣 + Python 後端」。若向量庫(Vectorize)與嵌入(Workers AI)都在 Cloudflare,而收縮 / conformal 那層數學夠輕(TS 可實作),則可做成**純 Cloudflare Worker**:

```
claude.ai ─► Worker(MCP + 認證,§13) ─► Vectorize(召回) + Workers AI(嵌入)
                                       └► R2/D1(canonical JSONL,可重建索引)
```

Python 版(`mcp_server.py` + `cie/` + 離線雜湊嵌入 + 記憶體/本地向量)**保留為本地開發與離線後備**;兩形態共用同一份 JSONL 記錄格式(`seeds/anchors.jsonl` 的放大版),確保雲端↔本地可攜(呼應 §12.4 / 可攜性討論)。

### 14.4 介面影響(最小改動)

- `cie/store.py` 的 `VectorStore` 介面已可插拔:新增 **Vectorize 後端**(Worker binding;若從 Python 走 **Vectorize REST API**,需 `CIE_CF_ACCOUNT_ID` / `CIE_CF_API_TOKEN` / `CIE_VECTORIZE_INDEX`)。記憶體 / Qdrant 後端保留為替代。
- `cie/embedding.py` 的 `Embedder` 介面:新增 **Workers AI 後端**(預設 `@cf/baai/bge-m3`);`LocalHashEmbedder` 留作離線後備。
- **鐵則不變**:嵌入器一致性(同一模型才可直接搬向量;否則從 JSONL 的 `embedding_text` 重嵌入)、機制硬分區、分級加權、防 model collapse——全部與後端無關,照舊。

### 14.5 注意

- 切換嵌入模型 = 全庫向量需**重建**(維度/語意空間不同)。因此 canonical 一律存文字(`embedding_text`)+ 結構化欄位,向量視為可重生的衍生物。
- Vectorize 的 metadata 過濾用於機制硬分區(`brew_mechanism`)——確認其 metadata filtering 支援等值過濾(目前支援);分級加權與物理距離在召回後於程式層計算。

### 14.6 P0 實作狀態(已落地)

本輪只做 **Python 端可選 Cloudflare 後端 + 可攜性**,不做純 Worker 改寫(那是後續)。schema 未動。

- **嵌入**:`cie/embedding.py` 新增 `WorkersAIEmbedder`(REST,`result.data[i]`,batch 分塊、可選 pooling);`get_embedder()` 依 `CIE_EMBEDDING_PROVIDER` 選 `workers_ai|local|openai|voyage`,缺金鑰一律退回 `LocalHashEmbedder`。維度由模型決定(bge-m3=1024),非寫死。
- **向量庫**:`cie/store.py` 新增 `VectorizeStore`(REST:NDJSON upsert / query / `vectorize_info` 計數);機制硬分區與 `grade≠prediction` 用 Vectorize metadata filter;`get_store()` 工廠依設定選 `memory|qdrant|vectorize`。記憶體後備保留 `_canonical` payload 供無損匯出。
- **REST 基建**:`cie/cfapi.py`(Cloudflare v4 envelope 解包、typed `CloudflareError`)+ `cie/_http.py`(stdlib urllib,429/5xx 指數退避重試),**零新 pip 依賴**。
- **可攜性**:`cie/portability.py`(`export_jsonl` / `read_jsonl` / `import_jsonl` 重嵌 / `export_store`)。canonical JSONL 為真相,換模型/雲↔地一律重建。Vectorize 後端不支援 `export_store`(真相放 R2/D1 JSONL)。
- **測試/demo**:`tests/test_cloud_backends.py`(假用戶端,離線驗解析/硬過濾/淨化/退回)、`tests/test_portability.py`(無損匯出、跨維度重建);`cie/demo.py` 端到端跑四模式。需金鑰的真實整合測試以 `skipif` 標記。

---

## 15. Canonical 真相持久層 + 盲測評測協定

本節記錄兩塊「安全網 / 證據」基建:**canonical 真相層**(讓 Vectorize 部署不再無源)與**盲測評測集**(讓「越用越準」可被量化驗證)。兩者皆全離線可跑,不違反任何鐵則。

### 15.1 Canonical 真相持久層(取代「Vectorize 無源」風險)

**問題:** Vectorize 後端只存**精簡 metadata**(機制/處理法/焙度帶/grade 等過濾欄),不存 `_canonical` 全量、也不支援全庫掃描。一旦只用 Vectorize,就**無法重建/匯出全量**;而換嵌入模型(維度/語意空間不同,**必須重嵌**)時又**無源可重建** → 違反 §14.5「向量是可重生的衍生物」的前提。記憶體 / Qdrant 後端因把 `_canonical` 塞進 payload(`store.iter_records` 可無損列舉)沒這問題,Vectorize 有。

**設計:** 新增獨立的 **canonical sink**(`cie/canonical.py`),與向量庫**雙寫**——canonical 是真相、向量是衍生物。

| 角色 | 介面 / 實作 |
|---|---|
| 介面 | `CanonicalStore`:`append(record)` / `extend(records)` / `iter_records()` |
| 本地 | `LocalJsonlCanonical`:append-only JSONL(`CIE_CANONICAL_PATH`,預設 `./data/canonical.jsonl`) |
| 雲端(選配) | `R2Canonical`:R2 物件存整份 JSONL;append 採 **read-modify-write**(缺金鑰不啟用) |
| 工廠 | `get_canonical(config)`:有 CF 金鑰 + `CIE_R2_BUCKET` → R2;否則 Local |
| sink 選擇 | `maybe_get_canonical(store)`:後端**無 `iter_records`(=Vectorize)才回 sink**,記憶體/Qdrant 回 `None`(避免重複寫與測試副作用) |

**接線(雙寫):**
- **bootstrap(初始來源):** `cie/bootstrap.py`(`python -m cie.bootstrap`)把**策展語料 `corpus/global.jsonl`(446 筆,`tools/qa_merge.py` 由 `corpus/raw/` provenance 重生)**整批灌入 canonical sink。這是 canonical 的初始真相,**不是**空的 `./data/canonical.jsonl`、也不是 6 筆 `seeds/anchors.jsonl`。一次性;`--force` 用 `canonical.replace_all` 整份覆寫(re-init/災後重建)。canonical = 此策展語料(初始) + 之後 `log_calibration` 累積的回饋。
- `engine.log_calibration`:`store.upsert` 成功後,若有 canonical sink **且 `grade ≠ prediction`** → `canonical.append(record)`。
- `seed`:CLI(`python -m cie.seed`)對需要 sink 的後端同步 `canonical.extend(records)`;**僅 6 筆冷啟動錨點 demo**,正式載入請走 bootstrap。
- **`prediction` 級不入 canonical**:它本身是衍生物,既不該被當真相、也不該被 rebuild「復活」(呼應防 model collapse 鐵則)。

**重建(`cie/rebuild.py` / `python -m cie.rebuild`):** 讀 canonical 全量 → 用**當前**嵌入器重嵌 → upsert(`portability.import_records`)。**一律重嵌、不搬舊向量**(嵌入器一致性鐵則)。這就是 Vectorize 部署的還原點:canonical(R2/本地)是源,索引隨時可重生、可換模型。

**R2 注意:** R2 物件無原生 append,`R2Canonical` 以 read-modify-write 覆寫整份物件——**非並發安全**(個人單寫者足夠;高並發應改每筆獨立物件 + list,或改 D1)。REST 走既有 `cie/_http.py`(新增 `request_text` 原始文字傳輸)+ `cfapi.py` 的 `r2_get_object`(404→`None`)/`r2_put_object`,**零新 pip 依賴**;金鑰只進 `.env`。

### 15.2 盲測評測集(證明「越用越準」)

**目的:** 對**庫裡沒有的豆**先 `predict` → 比對人工真值,量化 L3 各軸誤差、區間覆蓋與方向排序,當回歸測試。沒有它就無法證明系統有變準(§12.6「期待值管理」的落地)。

**留出集設計(關鍵升級):** 留出集**改用 `corpus/global.jsonl` 的 A/B 級記錄、按機制分層的 k-fold 交叉驗證**(`run_cv_eval`,`python -m eval.run` 預設;k=5)。每筆 A/B 記錄輪流當一次 holdout(out-of-fold),統計效力最大——取代撐不起結論的 5 筆合成 holdout。
- **C 級永不當 holdout 真值**(開環、標籤不一致;只能留在召回庫壓量級,鐵則 §3)。eligible = 語料中 grade∈{A,B} 的記錄。
- **分層**:`_stratified_folds` 各機制各自(依內容指紋,**不依隨機 uuid**)round-robin 切 k 折 → 各折機制比例一致、確定性可重現。
- 合成 `eval/dataset.jsonl`(5 筆,涵蓋三機制)**降級為洩漏偵測器回歸**用(`run_eval` 路徑),證明守衛非虛設。

**結構(`eval/`):**
- `eval/run.py` / `python -m eval.run`:每筆留出記錄 → 去 flavor → `engine.predict` → 比對:
  - **(a) MAE / RMSE**:各 L3 軸、overall,**且分機制**。
  - **(b) 覆蓋率**:真值落在預測 conformal 區間 `[lower, upper]` 的比例 vs 名目(~90%);各軸、overall、**分機制**。
  - **(c) 方向 / 排序**:**同機制**配對裡,預測高低排序與真值一致的比例(pairwise accuracy)——呼應「方向 > 絕對值」與 AUDIT A9「用配對判斷」。**跨機制不配對**(鐵則 §12.1);各軸與**分機制**皆報。
- **分機制報告**(n / MAE / RMSE / 覆蓋 / 方向)是本次升級重點:三軌物理範式不同,合併平均會掩蓋機制間差異。
- 輸出:JSON 報告(`eval/report.json`,gitignored)+ 印表格。

**防洩漏(關鍵,三道 + C 級守衛):**
1. **留出豆絕不進召回庫**:每折用獨立記憶體 store 灌入**策展語料 `corpus/global.jsonl`(446 筆)並按內容指紋扣除該折 holdout**(豆+機制+泡法+核心參數;因語料不帶穩定 id,**不能靠 id 扣除**),**不是只灌 6 筆 seeds**。CV 路徑的 holdout 即語料記錄、同一份記憶體載入,故執行期 `holdout_ids` 與庫 id 互斥檢查**非虛設**(uuid 一致可對撞);任何一筆 `evidence` 都不得是留出豆。指紋扣除同時涵蓋**完全重複**的記錄(同 sig 一起排除)。
2. **結構性無子項回推**(§12.6 的 R²≈0.82 陷阱):`predict()` 只吃 `bean + params`,**完全不碰任何真值風味軸**——這是設計層保證(測試以「竄改真值風味、預測不變」驗證),非靠自律。
3. **預測不寫回**:評測一律不呼叫 `log_calibration`(`grade=prediction` 不進方向投票);報告明列「庫筆數前後不變」。
4. **C 級未當 holdout 真值**:報告 `leakage_checks.c_grade_never_holdout`;eligible 僅 A/B,結構上 C 不入 holdout 池。

**期待值(務實):** 離線雜湊嵌入本就不準,harness **不對 MAE 下硬門檻**;測試只斷言「協定可跑、每筆 A/B 留出一次、留出豆確被排除、覆蓋率與方向算得出、同機制配對、分機制統計成立、C 不當真值、無洩漏、無寫回」。真實準度數字待接 `workers_ai` 嵌入 + 真實資料後,用**同一 harness** 直接複用。
