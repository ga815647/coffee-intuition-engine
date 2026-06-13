# 研究 → 分級錨點資料(RESEARCH_ANCHOR_PROMPT)

> 用途:把咖啡研究轉成 CIE 客觀因果層(`user_id="global"`)的分級錨點 JSONL。
> 每個研究 session / subagent 開頭:「讀 docs/RESEARCH_ANCHOR_PROMPT.md,範圍=<填>」。
> 動工前先讀 `cie/schema.py` 與 `seeds/anchors.jsonl` 對齊格式。

## 一、輸出契約

- 格式:**JSONL**,每行一筆,完全符合 `Record` schema 與列舉值。不要陣列、不要註解、不要 markdown。
- 位置:**寫到自己的檔** `corpus/raw/<scope>.jsonl`(scope 用小寫連字號,如 `ethiopia-washed`)。**絕不寫別人的檔、絕不共寫同一個檔**(防 race,見 §五)。orchestrator 之後跑 `python tools/qa_merge.py` 把 `corpus/raw/` 併成 curated `corpus/global.jsonl`。
- `user_id` 一律 `"global"`;`timestamp` 留空(系統補)。

## 二、機制硬分區(填錯會污染整個分區)

`params.brew_mechanism` 必填,三選一:
- `percolation`:手沖 / V60 / Kalita / Chemex / 注水法
- `immersion`:法壓 / 聰明杯(Clever)/ 杯測(cupping)/ 浸泡式
- `pressure`:義式 espresso / moka pot / 加壓

## 三、欄位重點

- `bean`:`origin` / `variety` / `process`(washed|natural|honey|anaerobic|other)/ `roast_agtron`(淺~75、中~62、深~50;**僅在來源有講或可合理推估時填,否則 null**)。
- `params`:`method`(泡法名,僅標籤)、`water_temp_c`(70–100)、`brew_ratio`(水:粉,如 16)、`grind_um`、`contact_time_s`、`pressure_bar`(義式)、`tds_pct`(0–20)、`ey_pct`(0–30)。
- `flavor`:`acidity`/`sweetness`/`bitterness`/`body`/`aftertaste`/`balance`/`clarity` 為 **0–10**;`acidity_type` ∈ citric|malic|acetic|lactic|mixed|none;`flavor_notes` 用一致的英文風味輪詞(bergamot, stone_fruit, cocoa, jasmine…);`defects` 同理。
- `water`:**只當控制變數**。除非來源明確給水值,否則整個留 null。**絕不寫入任何「水→風味」因果或自編礦物數字**(通俗「鎂=明亮」有同儕審查反證)。
- `source`:放 URL 或方法名,方便日後查核。

## 四、分級規則(最重要,寧低勿高)

- **A 級**:閉環、標準化協定下的真值——SCA 杯測分數、競賽配方、具明確參數的名人方法(Hoffmann/Kasuya/Tetsu 等),**且風味是真有杯測或明確描述**。`protocol` 必填(如 `SCA_cupping` / `competition_recipe` / `method:Hoffmann_V60`)。`confidence` 0.8–0.9。
- **B 級**:有對照、具體但單人主觀;或風味只是文字描述、需你保守量化。`confidence` 0.5–0.7。
- **C 級**:社群 / 聚合、標籤不一致——**只壓量級、不准定方向**。風味數值保守、寧可 null。`confidence` 0.1–0.3。
- 禁用 `grade="prediction"`(那是引擎自己用的)。

## 五、誠實鐵則(違反就是污染資料)

1. 只填來源真的有的參數;沒有的留 null,**不要編**。
2. 來源只給文字風味、沒有杯測分數 → **最多 B 級**,保守量化,別假裝是 A 級杯測分數。
3. 不寫任何水→風味因果。
4. 方向 > 絕對值:數值是傾向、要保守,別硬湊到小數點。
5. 每筆盡量附 `source`。
6. **A 級佔比天然稀少**:若你整批幾乎都 A 級,八成是把描述當分數湊——自己先降級。

## 六、並行與防 race(orchestrator + subagent 都讀)

- **唯一輸出檔**:一個 scope 一個 subagent 一個 `corpus/raw/<scope>.jsonl`。檔名互斥 = 無寫入衝突。
- **subagent 內不要 `git commit`、不要 `pip install`、不要灌庫**(`cie.seed`/`rebuild`)。這些有共享狀態(git index、site-packages、向量庫),並行會互踩。全部留給 orchestrator 序列化做。
- **不需要 git worktree**:因為各 subagent 只「新增」獨立檔、不改共享程式碼、不 commit。worktree 是為「並行改同一份程式碼 / 並行 commit」設計的;這裡用不到,加了反而增開銷與合併成本。
  - 例外:若某 subagent 需要改程式(不該發生於資料蒐集),才給它獨立 worktree + 分支。
- **合併與灌庫一律單執行緒**:orchestrator 等所有 subagent 完成後,序列做 validate → 去重 → QA 抽查 → 合併 → (稍後) rebuild。

## 七、收尾自檢(subagent 回報)

1. 逐行 `json.loads` + `Record.model_validate` 能過(跑 `python -c` 驗)。
2. 列出各級筆數、涵蓋機制、3 筆範例、每筆 source。
3. **先別灌庫**:等 workers_ai 嵌入就緒,由 orchestrator 一次 `python -m cie.rebuild` 重嵌灌入。
