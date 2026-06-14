# 分享 / 多租戶 onboarding — guest runbook

把 CIE remote MCP 分享給一個 guest 的完整步驟。權威設計見 `docs/DESIGN_v0.2.md` §16.3
(共享與讀隔離)、§16.2 / §16.6(寫入 / 刪除隔離);token 產生工具見 `tools/add_guest.py`。

## TL;DR

一個 guest 需要三樣:**專屬 member token** + **連接器 URL** + **coffee-intuition skill**。
owner 用 `python -m tools.add_guest` 產 token + 驗證 + 拿指令,自己套用到 Secret Manager 並重部署,
再把 URL + token + skill 交給 guest。

## 隔離保證(§16.3,為什麼這樣安全)

每個 guest 是一個 **member**,落在自己的 `self` 命名空間:

- **寫 / 刪**:只落自有 `self`,`grade≤B`;**寫不到 global、刪不到他人**。指定他人 / global 的
  record_id 也動不了(命名空間 confinement,儲存層強制)。
- **讀**:`[global, 自己的 self]`;**讀不到他人的 self**(隱私命門)。
- **global 客觀層**:跨人共享、只讀;只有 **owner(本機 stdio)** 能寫 / 晉升 → 共享真相不被遠端污染。
- **唯一性守衛**:任兩個 guest 的 `user_id` 絕不可相同(否則共用同一 self = 跨 guest 混入);撞了
  伺服器**啟動 fail-closed 拒絕**(`GuestTokenConfigError`)。`add_guest` 在產出前就先以同一守衛擋下。

token 是唯一憑證:外洩最壞情況 = 該 guest 自己 `self` 層被人讀 / 寫 / 刪光,**global 與他人 self 毫髮無傷**。

## Owner 步驟(發一個 guest)

1. **產 token + 驗證 + 拿指令**(不碰 live):
   ```bash
   python -m tools.add_guest --user-id <唯一命名空間>      # 或 --name "顯示名"(自動轉 slug)
   ```
   - 預設**遮罩** token;要實際複製進 secret / URL 時加 `--show`。
   - 想把完整 token 存到安全處:加 `--save` → 落進 gitignored `secrets/guest-<user_id>.env`。
   - realistic 流程:先把 Secret Manager 現值拉下來當基準餵進去
     `--existing @current-secret.json`(預設則讀 `.env` 的 `CIE_MCP_GUEST_TOKENS`)。**現值若非空但無法
     解析(含 BOM / 語法錯)會 fail-closed 拒絕產出**——避免把併好的 JSON 縮成只剩新 token、貼上去反而
     撤銷其他 guest。
   - `user_id` 須為小寫 slug `[a-z0-9-]`(`--name` 會自動轉);含其他字元會被擋。
   - 非預設環境可用 `--public-url` / `--secret-name` / `--service` / `--region` 覆寫
     (預設 `cie-mcp` / `asia-east1` / `cie-mcp-guest-tokens`)。
   - 撞 user_id / 撞 primary token / 認領保留字 `global`/`self` → **不產出**、報哪裡撞、exit≠0。

   輸出四樣:① 新 token ② 併好的 `CIE_MCP_GUEST_TOKENS` JSON ③ gcloud 指令範本 ④ 連接器 URL。

2. **更新 Secret Manager**(把 ② 的完整 JSON 寫進暫存檔,再套用 ③ 印出的 gcloud 範本;需 `--show` 取 ② 完整值):
   - 把 add_guest 印出的【② 併好的 JSON】寫成 **utf-8 無 BOM** 暫存檔(PowerShell pipe 會注 BOM;用 Python 寫)。
   - `gcloud secrets versions add cie-mcp-guest-tokens --data-file=guest-tokens.json`(加新版本、可回滾)。
   - 用後刪暫存檔(token 不留在工作目錄)。
   - Windows 上 gcloud 可能不在 PATH;完整路徑 / SA / 區域見 memory `gcloud-deploy-ops`。

3. **讓新 token 生效 —— 必須冷啟動 / 重部署**:
   > ⚠ **暖實例讀的是舊 secret**。新 token **不會即時生效**:要嘛重部署
   > (`gcloud run deploy cie-mcp --source . --region asia-east1 --update-secrets
   > CIE_MCP_GUEST_TOKENS=cie-mcp-guest-tokens:latest --max-instances=1 --min-instances=0`),
   > 要嘛等 scale-to-zero(閒置約 15 分)後下次冷啟動。重部署需人工授權。

4. **把三樣交給 guest**:連接器 URL(`https://<host>/mcp?token=<token>`)+ token + 請 guest 裝
   coffee-intuition skill。token 走安全管道,別貼進公開 / 會被索引的地方。

## Guest 步驟(claude.ai)

1. **方案**:需支援**自訂連接器**(Pro / Team / Enterprise);免費方案通常不行。
2. **加自訂連接器**:Settings → Connectors → 加自訂連接器 → 貼上 owner 給的 URL
   (`https://<host>/mcp?token=<token>`)。URL 已內含 token(claude.ai 網頁連接器走 `?token=` query)。
3. **裝 skill**:Settings → Capabilities → 安裝 **coffee-intuition** skill(行為規範:繁中、參數導向、
   機制硬分區、方向>絕對值、誠實標不確定)。
4. **建議用 Opus**:推理 / 多步驟工具編排品質較好。
5. 連上後可用 `query_flavor_map`(recommend / predict / diagnose)、`predict_method_swap`、
   `log_calibration`(沖+嚐後記一筆,「越用越準」的燃料)、`delete_calibration`(只能刪自己的)。
   **晉升 / 寫 global 不在網路面**(owner 本機 stdio 專屬)。

## 撤銷 / 輪替

1. 從 `CIE_MCP_GUEST_TOKENS` 移除該 token(輪替 = 移除舊的 + `add_guest` 產新的)。
2. 更新 Secret Manager(同步驟 2,加新版本)。
3. **重部署 / 等冷啟動**(同步驟 3;暖實例仍認舊 token 直到冷啟動)。
4. 移除後該 token 一律 401(無共用 fallback)。被撤的 guest 的 `self` 資料仍在 D1;要清除得由
   owner(本機 stdio)用 `delete_calibration` 針對其命名空間刪。

## 疑難

- **guest 連上但 401**:多半是還沒冷啟動 / 重部署(暖實例讀舊 secret),或 token 沒進 secret。
- **伺服器起不來(`GuestTokenConfigError`)**:guest 設定有破口(重複 user_id / 撞 primary /
  認領保留字)。先在本機跑 `add_guest` 或檢查 JSON;§16.3 守衛是啟動 fail-closed,刻意不讓帶病設定上線。
- **guest 看到別人的資料?**:不會。讀範圍加性過濾 `[global, 自己]`;若真發生,檢查
  `CIE_MCP_STATELESS=1`(命門:有狀態模式會讓 principal 退回 owner 預設,見 §16.3)。
