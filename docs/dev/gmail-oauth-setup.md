# Gmail OAuth2 設定教學

讓 Gmail Adapter 能存取 agent 的 Gmail 信箱。整個流程大約 5 分鐘。

---

## Step 1：啟用 Gmail API

> 這步最容易漏掉，沒啟用會一直 403 Forbidden。

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立新專案（或選既有專案）
3. 左側選 **APIs & Services → Library**
4. 搜尋 `Gmail API` → 點進去 → 點 **Enable**
5. 確認頁面變成 **Manage**（代表已啟用）

## Step 2：設定 OAuth 同意畫面

1. 左側選 **APIs & Services → OAuth consent screen**（或 **Google Auth Platform**）
2. 選 **External**
3. 填基本資料（App name 隨意，email 填你的）
4. 進入 **資料存取權**（Scopes）頁面
   - 點 **新增或移除範圍**
   - 搜尋或手動輸入 `https://mail.google.com/`
   - 它會出現在「受限制範圍」下的「Gmail 範圍」
   - **Save**
5. 進入 **目標對象**（Test users）頁面
   - 加入 agent 的 Gmail 帳號（例如 `hibikikafuu@gmail.com`）

## Step 3：建立 OAuth Credentials

1. 左側選 **用戶端**（Credentials）
2. **Create Credentials → OAuth client ID**
3. Application type 選 **電腦版應用程式（Desktop app）**
   - 不要選 Web application（那個需要 redirect URI 和授權網域）
4. Name 隨意
5. 建立後記下 **Client ID** 和 **Client Secret**

## Step 4：取得 Refresh Token

在終端機跑：

```bash
uv run python scripts/gmail_auth.py
```

依提示操作：
1. 貼上 Client ID
2. 貼上 Client Secret
3. 瀏覽器會自動開啟 → 用 **agent 的 Gmail 帳號** 登入並同意
4. 授權完成後瀏覽器會顯示「Authorization successful!」，腳本自動接收授權碼
5. Script 會印出三個值

## Step 5：填入 .env

把 Script 輸出的三行貼到專案根目錄的 `.env`：

```
GMAIL_CLIENT_ID=你的-client-id
GMAIL_CLIENT_SECRET=你的-client-secret
GMAIL_REFRESH_TOKEN=你的-refresh-token
```

## Step 6：啟動驗證

確認 `.env` 已設定 `CHAT_AGENT_USER=your-username`，然後啟動：

```bash
uv run lincy start
```

正常的話不會有錯誤訊息。debug 模式下會顯示 `Gmail adapter registered`。

---

## 設定調整

`cfgs/agent.yaml`：

```yaml
channels:
  gmail:
    enabled: true           # false 暫停 adapter（不需刪 .env）
    poll_interval: 45       # 幾秒 poll 一次 inbox，最低 10
```

不寫這個區塊也能跑，預設 enabled + 45 秒。

---

## 長期使用

OAuth consent screen 預設是 **Testing** 狀態，有兩個限制：

| 限制 | 影響 |
|------|------|
| 只有 Test users 能授權 | 已在 Step 2 處理 |
| Refresh token **7 天過期** | 過期後要重跑 Step 4 |

要解除 7 天限制 → 把 app **Publish**：
- Google Auth Platform → **目標對象** → 發布應用程式
- 個人用途不需要 Google 審核，上方的驗證警告可忽略
- Publish 後 refresh token 永久有效

---

## 故障排除

| 錯誤 | 原因 | 解法 |
|------|------|------|
| `403 Forbidden` | Gmail API 沒啟用 | 回 Step 1 確認 |
| `403 Forbidden`（API 已啟用） | Scope 沒設 | 回 Step 2 確認有 `mail.google.com`，然後重跑 Step 4 |
| `invalid_grant` | Refresh token 過期 | 重跑 Step 4，或 Publish app 解除 7 天限制 |
| `invalid_request` (400) | OOB 流程已停用 | 確認使用最新版 `scripts/gmail_auth.py`（localhost 流程） |
| `access_denied` | Agent Gmail 不在 Test users | 回 Step 2 加 |
| `invalid_client` | Client ID/Secret 打錯 | 對照 Step 3 的值 |
| Adapter 沒啟動 | `.env` 缺值 | 確認三個 `GMAIL_*` 都有 |
| `網域無效：須為頂層私人網域` | OAuth consent screen 填了授權網域 | 那個欄位留空，Desktop app 不需要 |
