# macOS 原生個人資料工具

這批工具直接操作 macOS 原生 app 的真實資料：

- `calendar_tool`
- `reminders_tool`
- `notes_tool`
- `photos_tool`
- `mail_tool`

目標不是做最小 CRUD，而是提供足夠強的搜尋、分類探索、讀寫能力，讓 agent 可以先理解使用者目前的分類結構，再決定資料應該放哪裡。

## 原則

1. 這些能力放在 `tools.apple_apps`，不是 `features.*`
2. 預設啟用，但只在 `sys.platform == "darwin"` 註冊
3. 每個 app 採「單一強工具 + 多個 action」設計，不拆成大量零碎 tool
4. 寫入前，若目標分類不明確，先跑 `catalog` 或 `search`
5. `notes_tool` 建立新筆記時，必須指定 `folder_id` 或 `folder_path`，避免直接丟進預設資料夾
6. `photos_tool` 先負責搜尋、分範圍、匯出；後續 OCR、摘要、整理流程由其他既有工具處理
7. `notes_tool` 對 LLM 預設回傳可讀文字，不直接暴露巨大 `body_html`

## 設定

`cfgs/agent.yaml`

```yaml
tools:
  apple_apps:
    enabled: true
    timeout_seconds: 30
    max_search_results: 25
    photos_export_dir: "tmp/photos-exports"
    mail_export_dir: "tmp/mail-attachments"
```

- `timeout_seconds`：仍走 JXA / AppleScript 的呼叫（Reminders `flagged` 寫入、Notes `get` 與寫入、Mail `trash`）的 timeout；另外也是 Reminders fetch 與 Photos 每個匯出檔案等待 completion handler 的上限
- `max_search_results`：`search` 預設上限
- `photos_export_dir`：`photos_tool(action="export")` 未指定 `destination_dir` 時的預設匯出根目錄，會建立在 `agent_os_dir` 下
- `mail_export_dir`：`mail_tool(action="export_attachment")` 未指定 `destination_dir` 時的預設匯出根目錄，會建立在 `agent_os_dir` 下

這批工具現在只在 LLM 明確判斷需要時才呼叫，不會自動同步到 `agent_note`，也不會自動建立排程或背景摘要。

## 權限

這批工具需要的 TCC 權限：

| 權限 | 用途 |
|------|------|
| 行事曆：完整取用 | `calendar_tool`（EventKit） |
| 提醒事項：完整取用 | `reminders_tool`（EventKit） |
| 照片：完整取用 | `photos_tool`（PhotoKit） |
| 完整磁碟取用權限 | 唯讀讀取 Reminders、Notes、Photos、Mail 的資料庫（`flagged`、筆記、照片文字 metadata、信件） |
| 自動化：Reminders / Notes / Mail | Reminders `flagged` 寫入、Notes `get` 與寫入、Mail `trash` |

`tools.apple_apps.enabled` 時，validate 階段（`host/check.py`）會呼叫各工具的 `*_permission_problems()`，任一項缺少就 `EnvironmentCheckFailed`，不啟動。理由同 GUI 權限：少了權限每次呼叫都會失敗，寧可啟動時就停。

TCC 以負責程序計。正式環境 lincy 跑在 Terminal 裡的 tmux，權限要給 Terminal；改成 launchd 啟動時要給 `.venv/bin/python` 解析後的真正 binary（見 [gui-computer-use.md](gui-computer-use.md)「權限與部署」）。

### 權限預熱

```bash
uv run permissions-warmup
```

這個指令會對行事曆、提醒事項送出 EventKit 完整取用請求、對照片送出 PhotoKit 請求，再對 Reminders、Notes、Mail 跑一次唯讀 JXA 讓 macOS 跳出自動化授權，最後列出 validate 階段會擋的剩餘問題。完整磁碟取用權限沒有請求 API，只能手動到系統設定加入。它不建立、不更新、不刪除任何資料，也不寄信。

若只想確認會觸發哪些 app：

```bash
uv run permissions-warmup --list
```

授權是 macOS 對「執行此指令的 app」發放；若從不同終端、Codex app 或其他 runner 啟動，macOS 可能會再次詢問。

Runtime cache：
- `cache/apple_notes`：Apple Notes 衍生快取，保存 `content_markdown` 與搜尋摘要
- `cache/vision`：所有 vision request 共用的圖片快取；只要圖片內容、prompt、vision 模型指紋相同，就直接回舊結果

## Tool 設計

### `calendar_tool`

用途：
- 列出 calendars
- 依 calendar / 日期區間 / 關鍵字搜尋 events
- 檢查候選時段是否撞期
- 讀取單一 event
- 建立 event
- 更新 event

actions：
- `catalog`
- `search`
- `conflicts`
- `get`
- `create`
- `update`

重要參數：
- `calendar`
- `calendars`
- `event_uid`
- `exclude_event_uid`
- `query`
- `start`
- `end`
- `title`
- `notes`
- `location`
- `url`
- `all_day`
- `sort_by`

備註：
- `create` 必須指定 `calendar`
- `update` 用 `event_uid`
- `start` / `end` 使用本地時間 ISO 格式，例如 `2026-04-20T14:00`
- Calendar 讀回的 `start` / `end` 會以 app timezone 輸出並帶 offset，例如 `2026-04-20T14:00:00+08:00`
- `conflicts` 適合先檢查某個時段是否已有重疊事件，更新既有 event 時可帶 `exclude_event_uid`
- `catalog` 會回傳 `writable`、`color`、`path`、`source`，讓 agent 不只知道名字，也知道哪個 calendar 可寫；`description` 一律為 null（EventKit 不提供）
- `calendar` / `calendars` 可用名稱或 `source/title` 路徑；同名 calendar（例如 iCloud 與 Google 都有「工作」）在 search 會一起比對，`create` 會報錯並列出可用路徑
- `uid` 是 EventKit `calendarItemIdentifier`，與舊 JXA 的 `uid` 相同，先前存下的 uid 仍可用
- search / conflicts 回傳週期事件的各個 occurrence（帶 `recurring`）；`get` 回 series master。`update` 改標題、備註、地點、URL 會套用到整個 series；改 start / end / all_day 會被拒絕，避免拿某一次 occurrence 的時間把整個 series 搬走
- search 未帶 start / end 時查 EventKit 上限的 4 年區間；明確給的區間超過 4 年時，切成不超過 4 年的連續視窗逐段查詢再合併（以 uid + 開始時間去重），避免 EventKit 靜默截斷
- `conflicts` 用嚴格重疊判斷，頭尾相接不算撞期
- Calendar.app 的虛擬日曆「已排程的提醒事項」與「Siri 建議」不在 EventKit 內，`catalog` 不會出現；提醒事項一律用 `reminders_tool`

### `reminders_tool`

用途：
- 列出 reminder lists
- 搜尋 reminders
- 讀取單一 reminder
- 建立 reminder
- 更新 reminder
- 標記完成 / 取消完成

actions：
- `catalog`
- `search`
- `get`
- `create`
- `update`
- `complete`

重要參數：
- `list_id`
- `list_name`
- `list_path`
- `reminder_id`
- `query`
- `title`
- `notes`
- `due`
- `due_start`
- `due_end`
- `priority`
- `priority_min`
- `priority_max`
- `flagged`
- `completed`
- `sort_by`

備註：
- 沒有 `list_id` 時可用 `list_name` 或 `list_path`
- 若 agent 不知道要放哪個 list，應先 `catalog`
- `search` 可直接篩完成狀態、旗標、priority 範圍、到期區間
- `due` / `due_start` / `due_end` 使用本地時間 ISO 格式，例如 `2026-04-20T09:00`；帶 offset 的輸入（`+08:00`、`Z`）也會被正確換算
- 讀回的 `due` 會以 app timezone 輸出並帶 offset，例如 `2026-04-20T09:00:00+08:00`，與 Calendar 行為一致
- `id` / `reminder_id` 是 EventKit `calendarItemIdentifier`（裸 UUID）；輸入帶 `x-apple-reminder://` 前綴也接受
- `list_path` 格式是 `<帳號>/<清單>`，例如 `iCloud/提醒事項`，與舊版相同
- 寫入 `due` 時同時寫一個絕對時間的 alarm，與 Reminders.app 透過 JXA 寫 `dueDate` 的結果一致；改 `due` 只取代時間等於舊到期時間的那個絕對 alarm，相對 alarm（例如提前一天）、地點 alarm 與其他自訂時間的 alarm 都保留
- EventKit 沒有 `flagged`：讀取與 `flagged` 篩選改從 Reminders 資料庫（`~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/Data-*.sqlite`，表 `ZREMCDREMINDER`，`ZIDENTIFIER` 對 `calendarItemIdentifier`、`ZFLAGGED`）唯讀讀取；寫入 `flagged` 用一次 JXA `reminders.byId(...)`，約 1.2 秒
- create / update 先由 EventKit 存檔再寫 `flagged`；JXA 寫 `flagged` 失敗時仍回 `ok: true` 與已存檔的 reminder，另帶 `warning` 說明旗標未套用與原因，避免 agent 重試造成重複建立
- search 回傳 `limit`、`truncated`、`warning`；指定不存在的清單會報錯

### `notes_tool`

用途：
- 列出 Notes account / folder tree
- 搜尋筆記
- 讀取單一筆記
- 在指定 folder 建筆記
- 更新或 append 既有筆記
- 將筆記搬到另一個 folder

actions：
- `catalog`
- `search`
- `get`
- `create`
- `update`
- `move`

重要參數：
- `account`
- `folder_id`
- `folder_path`
- `target_folder_id`
- `target_folder_path`
- `note_id`
- `query`
- `created_after`
- `created_before`
- `modified_after`
- `modified_before`
- `title`
- `body`
- `template_markdown`
- `variables`
- `images`
- `append`
- `sort_by`
- `offset`

備註：
- `create` 必須指定 `folder_id` 或 `folder_path`
- `folder_path` 來自 `catalog`，格式如 `iCloud/待讀`
- `title` 是 Notes 真正的筆記名稱；如果同時用了 `template_markdown`，tool 會確保 `title` 成為第一個可見區塊
- `title` 與正文是兩層不同概念：`title` 管 Notes 真正的筆記名稱，`template_markdown` 管正文排版
- `body` 會轉成簡單 HTML，保留換行
- `template_markdown` 走受控 Markdown 子集，適合讓 agent 控制 note 區塊順序與格式
- `variables` 與 `images` 的 key 可以自由新增，不限固定欄位；模板裡寫 `{paper_title}`、`{quote_1}`、`{image_cover}` 都可以
- 圖片 placeholder 支援兩種：
  - `{image_cover}`
  - `![封面](image_cover)`
- `images.image_cover` 這種值必須是可讀的本機圖片路徑
- `template_markdown` 目前支援：
  - `#` / `##` / `###`
  - 段落與空行
  - `**粗體**` / `*斜體*` / `` `inline code` ``
  - 無序清單 / 編號清單
  - 連結 `[文字](url)` 與裸 `https://...` URL
  - 簡單表格 `|...|`
  - 圖片 placeholder
- Notes 寫回時可能把 HTML `<a href>` 正規化成底線文字；最穩的是直接保留完整 URL，不要假設自訂連結文字一定還能點
- `template_markdown` 不支援完整自由排版；重點是控制區塊順序，不是做 Word 等級排版
- Notes 友善映射目前固定為：
  - `#` -> `h1` + `15.0pt bold`
  - `##` -> `h2` + `13.5pt bold`
  - `###` -> `h3` + `12pt bold`
  - 無序清單 / 編號清單 -> 純文字行，不強制轉成 HTML list
- 若已傳 `title`，正文預設不要再用同樣文字寫一個 `# 主標題`；否則會變成「筆記名稱一份，正文大標又一份」
- 正文若已傳 `title`，建議直接從 `##` 開始
- `#` / `##` / `###` 後面建議空一行，版面比較自然
- `search` 預設回傳摘要結果，不回 `body_html` 或全文；欄位包含 `summary`、`content_kind`、`has_images`、`source_url`、`content_chars`
- `get` 預設回傳單一 `content_markdown`
- `notes_tool` 讀取筆記時，會先把 HTML 轉成 Markdown；若遇到 `data:image/...` 內嵌圖片，會先走 vision，再把圖片替換成文字摘要
- `search` 的摘要建立在 `get` 產出的 `content_markdown` 上，並快取到 `cache/apple_notes`
- `move` 讓 agent 可以先暫存到某個 folder，之後再整理到正確分類
- `search` 支援 `offset` 分頁；`limit` 預設 5，最大受 `tools.apple_apps.max_search_results` 限制
- 讀取來源是 `~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite`：entity 編號查 `Z_PRIMARYKEY`，store UUID 取 `Z_METADATA.Z_UUID`；純文字是 `ZICNOTEDATA.ZDATA` 的 gzip protobuf，欄位路徑 2 → 3 → 2（不含表格附件內的文字）
- `note_id` 格式 `x-coredata://<Z_UUID>/ICNote/p<Z_PK>`，與舊 JXA id 相同
- `search` 完全不呼叫 Notes.app：快取新鮮（`modified_stamp` 相符）時用快取的摘要，否則用資料庫純文字做摘要；結果帶 `summary_source`（`cache` / `plain_text` / `none`）。純文字摘要另存在快取的 `plain_summary`，不會讓完整內容的快取看起來是新的
- `get` 用一次 JXA `notes.byId(id).body()` 取完整 HTML 並更新快取，之後的 search 會變成 `summary_source: "cache"`
- Notes.app 偶爾會在單次 `body()` 卡住 8 到 85 秒，這是 search 不呼叫 Notes.app 的原因
- 「最近刪除」資料夾與其中筆記一律排除；上鎖筆記回 `password_protected: true`、`content_kind: "locked_note"`，不讀內容
- 本文尚未同步（`ZDATA` 為 NULL，例如未下載的共享筆記）或無法解碼的筆記只影響自己：search 回 `content_kind: "unavailable_note"`、`summary_source: "none"`；缺少建立或修改時間的筆記回 `created_at` / `modified_at` 為 null，日期篩選不會命中、排序排在最後，也不使用快取；`get` 照常向 Notes.app 取 body
- `catalog` 回傳真實巢狀路徑與 `note_count`；存在 NoteStore.sqlite 之外的舊 IMAP 帳號（例如空的 `Google/Notes`）不會出現
- `search` 指定不存在的 folder 會報錯

### `photos_tool`

用途：
- 列出 Photos albums / folders
- 依 album / album path / folder / folder path / 日期 / 關鍵字 / favorite 搜尋照片
- 讀取單一 album
- 讀取指定 media item metadata
- 匯出 media items 成檔案
- 建立 album
- 把照片加入 album

actions：
- `catalog`
- `search`
- `get_album`
- `get_media`
- `export`
- `create_album`
- `add_to_album`

重要參數：
- `album_id`
- `album_name`
- `album_path`
- `folder_id`
- `folder_path`
- `parent_folder_id`
- `parent_folder_path`
- `query`
- `start`
- `end`
- `favorite`
- `sort_by`
- `media_ids`
- `destination_dir`
- `use_originals`

備註：
- `search` 回傳 `media item id`
- `search` 也會回傳 `description`、`width`、`height`、`size`、`location`
- `get_album` 可用 `album_id`、`album_name`、`album_path`
- `create_album` 可用 `parent_folder_id` 或 `parent_folder_path`
- `export` 要帶 `media_ids`
- `destination_dir` 必須落在 `allowed_paths` 內；未指定時會自動匯出到 `tools.apple_apps.photos_export_dir`
- `sort_by=filename_asc` 必須先縮小範圍；日期排序在 PhotoKit fetch 內完成，不受限制
- 未帶 `sort_by` 時預設新到舊
- 讀回的 `date` 以 app timezone 輸出並帶 offset
- id 是 PhotoKit `localIdentifier`（例如 `<UUID>/L0/001`），與舊 JXA id 相同
- `album_path` / `folder_path` 格式不變：`資料夾/子資料夾/相簿`，頂層相簿只有名稱；album 列另有 `path`、`parent_folder_id`、`parent_folder_name`
- `export` 回傳 `failed: [{id, error}]`；全部失敗或 id 不存在會報錯；iCloud 最佳化儲存的原檔會下載
- PhotoKit 已回傳但 Photos.sqlite 還沒有對應列的 asset（例如匯入或同步中），`title`、`filename`、`description`、`size` 回 null、`keywords` 回空陣列

### `mail_tool`

用途：
- 列出 Mail.app 統一 scope 摘要
- 在指定 scope 內掃描有限數量信件
- 讀取單封信
- 匯出單封信附件
- 將明確指定的信件移到垃圾桶

actions：
- `catalog`
- `search`
- `get`
- `export_attachment`
- `trash`

重要參數：
- `scope`
- `message_ref`
- `message_refs`
- `attachment_ids`
- `query`
- `search_body`
- `date_after`
- `date_before`
- `unread`
- `flagged`
- `has_attachments`
- `scan_limit`
- `limit`
- `offset`
- `destination_dir`
- `dry_run`

備註：
- 不提供 `account` 與 `mailbox_path`；Mail.app 已經負責集中多帳號信件
- `scope` 可用 `inbox`、`sent`、`drafts`、`trash`、`junk`、`outbox`、`all`，預設是 `inbox`
- `search` 一律用 `scan_limit` 控制最多檢查幾封信，預設 300，上限 2000；檢查範圍是 scope 與日期區間內最新的 N 封，unread / flagged / 附件 / query 篩選在這個範圍內套用
- `limit` 只控制回傳幾筆結果，不控制 Mail.app 實際掃描量
- `date_after` / `date_before` 以本地時間解析；日期格式 `YYYY-MM-DD` 會被視為本地整天，避免 UTC 造成日期少抓或多抓
- `query` 預設只查寄件者與主旨；若要查正文才設 `search_body=true`
- `search` 回傳 `message_ref`（格式 `mailmsg:<ROWID>`，輸入也接受裸數字 ROWID，與舊 JXA 相容；ROWID 即 Envelope Index `messages.ROWID`，也等於 Mail AppleScript 的 message `id`），後續 `get`、`export_attachment`、`trash` 都用它，不讓 agent 自行拼 Mail.app 內部位置
- `attachment_ids` 是 IMAP part 編號（例如 `2`、`1.2`），與 Envelope Index `attachments` 表一致；未下載到本機的附件匯出時會報錯
- 正文未下載到本機時，`get` 的 `content` 為 null，`content_error` 說明需在 Mail.app 開啟該信
- `trash` 執行前會先比對每封信的 Message-ID，已在垃圾桶或找不到的信直接拒絕；`dry_run=true` 也會向 Mail.app 確認每封信可定位
- inbox 未讀數以資料庫為準，可能和 Mail.app 的 `unreadCount` 不同
- `trash` 只接受 `message_ref` / `message_refs`，不接受 `query`；預設 `dry_run=true`
- `trash(dry_run=false)` 單次最多 20 封，只移到 Trash，不做永久刪除
- `export_attachment` 的 `destination_dir` 必須落在 `allowed_paths` 內；未指定時會自動匯出到 `tools.apple_apps.mail_export_dir`

## 使用規則

### 1. 先查分類，再寫入

錯的做法：
- 直接把文章存進 Notes 預設資料夾
- 直接把會議建立到預設 calendar

對的做法：
1. `notes_tool(action="catalog")`
2. 看有沒有 `待讀`、`工作`、`講座` 等 folder
3. 再 `notes_tool(action="create", folder_path="iCloud/待讀", ...)`

### `template_markdown` 例子

```json
{
  "action": "create",
  "folder_path": "iCloud/待讀",
  "title": "多目標追蹤模型",
  "template_markdown": "來源：{url}\n\n## 原圖\n{image_cover}\n\n## 簡介\n{summary}\n\n## 重點\n- {point_1}\n- {point_2}",
  "variables": {
    "url": "https://x.com/...",
    "summary": "Roboflow 把多目標追蹤完整開源。",
    "point_1": "支援任意檢測器即插即用",
    "point_2": "CLI 一行命令可追蹤影片"
  },
  "images": {
    "image_cover": "/absolute/path/to/clip.png"
  }
}
```

說明：
- agent 可以自己新增變數名，不必侷限在 `title`、`summary`
- 如果要控制 Notes 實際顯示的筆記名稱，應另外傳 `title`，不要只把標題塞在模板變數裡
- 模板出現順序，就是最後 note 的區塊順序
- 圖片要在上面，就把 `{image_cover}` 放在上面

乾淨版型：

```json
{
  "action": "create",
  "folder_path": "iCloud/待讀",
  "title": "多目標追蹤模型",
  "template_markdown": "來源：{url}\n\n## 簡介\n\n{summary}\n\n## 原文\n\n- {point_1}\n- {point_2}"
}
```

不要這樣：

```json
{
  "action": "create",
  "folder_path": "iCloud/待讀",
  "title": "多目標追蹤模型",
  "template_markdown": "# 多目標追蹤模型\n來源：{url}\n\n## 簡介\n\n{summary}"
}
```

原因：
- `title` 已經會控制 Notes 真正的筆記名稱
- 正文再放同樣的 `# 多目標追蹤模型`，畫面就會重複一份大標

### 2. 複合流程先拿資料，再處理

例子：整理今天的講座照片成文字

1. `photos_tool(action="search", query="講座", start="2026-04-11T00:00", end="2026-04-11T23:59")`
2. `photos_tool(action="export", media_ids=[...])`
3. 對匯出的圖片跑 `read_image`
4. 把整理結果寫進 `notes_tool`

例子：先檢查會議是否撞期，再建立事件

1. `calendar_tool(action="conflicts", calendars=["工作", "居家"], start="2026-04-15T14:00", end="2026-04-15T15:00")`
2. 若 `count=0`，再 `calendar_tool(action="create", calendar="工作", ...)`

例子：把已整理過的筆記從 `待讀` 搬到 `已讀`

1. `notes_tool(action="search", folder_path="iCloud/待讀", query="某篇文章")`
2. `notes_tool(action="move", note_id="...", target_folder_path="iCloud/已讀")`

### 3. 危險操作要保守

目前這批工具刻意不提供大量刪除能力。

原因：
- 這些都是使用者真實資料
- 第一版先把探索、搜尋、建立、更新做穩
- 刪除或搬移大量資料時，後續要另外設計保護機制
- `mail_tool` 只提供 `trash`，先預覽、再把明確指定信件移到垃圾桶；不提供永久刪除

## 實作細節

### 為什麼不用 JXA 讀資料

2026-10 在 macOS 26.7.1 實測，每個 Apple Event 約 1 秒。舊實作逐筆、逐欄位讀資料，54 筆提醒的 search 必定超過 30 秒，Calendar 一週 search 13 到 29 秒，Photos 一週 search 與 Mail 掃 300 封都超過 90 秒。改成下列方式後，讀取都在 1 秒內：

| 工具 | 讀取 | 寫入 |
|------|------|------|
| `calendar_tool` | EventKit | EventKit |
| `reminders_tool` | EventKit；`flagged` 讀 Reminders 資料庫 | EventKit；`flagged` 用一次 JXA `byId` |
| `photos_tool` | PhotoKit；標題、描述、關鍵字、檔名讀 Photos.sqlite | PhotoKit `performChangesAndWait` |
| `notes_tool` | NoteStore.sqlite；完整 HTML 用一次 JXA `byId` | AppleScript，以 id 直接指定 note / folder |
| `mail_tool` | Envelope Index + `.emlx` | `trash` 用 JXA 直接指定單封信 |

規則：
- 不准回頭用 `whose` 或逐筆掃描整個集合；JXA / AppleScript 只能以 id 直接指定單一物件
- 資料庫一律以 `file:...?mode=ro` 唯讀開啟；這些資料庫都是 WAL，唯讀連線看得到最新寫入
- Core Data 的 entity 編號在不同 macOS 版本、不同 store 檔會變，一律從 `Z_PRIMARYKEY` 查，不寫死
- 不提供退回 JXA 讀取的旁路或旗標；權限或 schema 不符就明確報錯

### EventKit

- 每個 entity type 一個 process-wide `EKEventStore`，每次使用前 `reset()`（約 2ms）。實測每次新建 store 在同一 process 內約 9 次查詢後 `calendarItemWithIdentifier_` 會回 None；長壽 store 又看不到其他 process 的修改，`refreshSourcesIfNecessary()` 也無效，只有 `reset()` 有效（`macos_apps/eventkit.py`）
- host 是單一 process，brain 與 worker 會從不同 thread 同時呼叫工具，所以 `open_store` 是 context manager：每個 entity type 一把 `RLock`，從 `reset()` 到最後一次 EventKit 存取或存檔都持有，避免另一個呼叫的 `reset()` 讓手上的物件失效；同一操作內巢狀呼叫不會 deadlock，也不會再 `reset()`
- Reminders fetch 是非同步 API，用 `threading.Event` 等，上限 `timeout_seconds`
- 不在工具內呼叫 requestAccess；授權交給 `permissions-warmup` 與 validate 階段

### 時間

- 所有日期時間輸入一律先在 Python 端用設定的 app timezone 轉成帶 offset 的時間，再交給 EventKit / PhotoKit / JXA；輸出一律轉成 app timezone 帶 offset 的 ISO 字串
- 不透過年月日時分秒逐項組日期，因為那會依賴子行程的時區解釋（歷史案例：Reminders due date 在 UTC+8 下偏移 8 小時）
- AppleScript 寫入的文字欄位不直接用 `system attribute` 傳內容，會先寫成 UTF-8 暫存檔再讀回，避開 `osascript` 在非 ASCII 文字上的亂碼問題
- 若 JXA / AppleScript 呼叫過慢或超時，log 會記下 `operation`、`elapsed` 與隱私安全的參數摘要
- `photos_tool(action="export")` 與 `mail_tool(action="export_attachment")` 會先驗證 `destination_dir` 是否在 `allowed_paths` 內

## 測試範圍

這批工具至少要有：

- 資料庫讀取測試：用只含真實欄位名稱的合成 sqlite（Reminders、NoteStore、Photos、Envelope Index）與合成 `.emlx`
- 權限缺少時的錯誤訊息與 validate 阻擋

- registry wiring 測試：macOS 時會註冊，非 macOS 不註冊
- action 參數驗證測試：缺必要參數時要回 `Error: ...`
- export 路徑限制測試：不允許匯出到未授權路徑
- Mail 時間範圍測試：日期輸入要轉成本地整天，輸出 UTC 時間要轉回 app local time
- Reminders due 時間測試：輸入本地時間要轉成帶 app offset 的 ISO 再交給 JXA，輸出 UTC 時間要轉回 app local time

真正讀寫 Calendar / Reminders / Notes / Photos / Mail 的整合測試不放進自動化測試，因為會碰到本機資料與系統權限。實機驗證在 lincy 主機以正式權限情境（Terminal 裡的 tmux 新視窗）執行；寫入只碰自己建立的 `[lincy-test]` 項目並在結束時刪除。從 ssh 直接跑的結果不準，因為 TCC 對 ssh 的授權與 Terminal 不同。
