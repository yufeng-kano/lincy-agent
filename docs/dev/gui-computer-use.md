# GUI Computer Use（原生真實輸入後端）

本文件定義 GUI 任務子系統的架構、觀察格式、loop 治理、設定與部署規範。2026-10 起以 Python + pyobjc 自寫的原生後端取代舊的 Swift vendor 後端，設計緣由見「決策紀錄」，完整任務規格見 [task/gui-native-input-backend.md](task/gui-native-input-backend.md)。

## 架構

```
brain --worker--> worker --gui_task (同步)--> GUIManager (LLM loop)
                                                 `-- DesktopBackend
                                                       |-- ax.py            AX 樹快照（systemwide 焦點視窗）
                                                       |-- capture.py       全螢幕截圖
                                                       |-- input.py         CGEventPost 到 HID tap
                                                       `-- input_source.py  TIS 輸入法（ctypes 載 Carbon）

brain --screenshot_by_subagent--> GUIWorker (vision describe，僅此用途)
```

- **呼叫者是 worker**：brain 以 `excluded_tools` 排除 `gui_task`，見 [brain-worker-delegation.md](brain-worker-delegation.md)「GUI 升級鏈」。只有一個同步 `gui_task`，註冊在共用 registry；並行 GUI 任務由 `gui_lock` 序列化，等待超過 `agents.gui_manager.lock_wait_seconds` 回 `[GUI BUSY]`。
- **兩層隔離不變**：CU loop 留在 `gui_task` 子 loop，不把 GUI 工具掛到 brain 或 worker。每步的樹與截圖是高流量暫時性 context，掛在 brain 會撐爆 `soft_max_prompt_tokens` 並破壞 brain 的 cache 準則。
- **真實輸入、獨佔機器**：所有滑鼠鍵盤事件以 `CGEventPost(kCGHIDEventTap, ...)` 送出（事件來源 `kCGEventSourceStateHIDSystemState`），會動真游標、切前景。不做虛擬游標、不做 per-pid 投遞。打到螢幕上是什麼就是什麼，選單、面板、跨程序對話框都不需要知道 pid。代價是任務期間這台機器不能給人用。
- **焦點視窗選擇**：每次取 state 都從 systemwide `AXFocusedApplication` 的 `AXFocusedWindow` 開始；沒有則取 `NSWorkspace.frontmostApplication` 的第一個視窗。若 systemwide `AXFocusedUIElement` 不在該視窗內（選單、popover、另一程序的面板，例如 `com.apple.appkit.xpc.openAndSavePanelService` 跑的開檔面板），另附該元素所屬 top-level 元素的子樹。焦點視窗與上一份不同時，state 標 `window_changed`。
- **任務開始**：runtime 先 `backend.prepare(app)`：有 `app` 就 `open_app(app)`，否則取前景 app；選定視窗後 `AXRaise` + activate，設 `AXPosition`/`AXSize` 填滿主螢幕 `visibleFrame`（不用 macOS 全螢幕 Space）；切到 `default_input_source`。結果當第一則 user 內容（文字 + 圖）附在 intent 後面，模型不必為此花一步。
- **manager 直接看圖**：tool result 是 `[text: "State N" + snapshot.render(), image: screenshot]`。`gui_worker` 只留給 brain 的 `screenshot_by_subagent`。
- **可觀測性**：gui_manager 的 LLM client 以 `wrap_llm_client_with_session_debug` 包裝（label `gui_manager`），requests / responses 進 session debug log（見 [session-debug-logs.md](session-debug-logs.md)）；session 步驟檔存完整工具文字。

## 工具面

只給「人坐在鍵盤前能做的事」。工具定義全部靜態，寫在 `gui/manager.py`。

| 工具 | 參數 | 說明 |
|------|------|------|
| `get_state` | 無 | 回傳焦點視窗的 state + 截圖 |
| `open_app` | `name` | 英文名或 bundle id；啟動或啟用、raise、放大 |
| `click` | `index` 或 `x`,`y`；`button`（left/right）；`count`（1/2） | index 點 bbox 中心 |
| `drag` | `x1`,`y1`,`x2`,`y2` | down、分段 move、up |
| `scroll` | `index` 或 `x`,`y`；`direction`（up/down/left/right）；`amount` | line 單位 |
| `type_text` | `text` | `CGEventKeyboardSetUnicodeString` 直塞字元，每塊至多 20 個 UTF-16 unit；`\n` 送 Return |
| `press_key` | `key` | 修飾鍵 + 主鍵，見下 |
| `set_input_source` | `source_id` | 例 `com.apple.keylayout.ABC` |
| `wait` | `seconds` | 0.1-10 秒；`allow_wait_tool: false` 時不提供 |
| `done` / `fail` / `report_problem` | `summary` / `reason` / `problem`，皆可帶 `report` | 終止工具 |

- **刻意不提供**：直接設欄位值（人做不到，也是舊後端「回成功但沒改值」的來源）、元素的次要動作、存截圖到路徑、任何 shell。填欄位一律「點、`Command+A`、打字」。
- **`press_key` 解析**：修飾鍵 `command`/`cmd`、`shift`、`option`/`alt`、`control`/`ctrl`。主鍵依序：(1) 具名鍵表，只放不可印鍵：return/enter、tab、space、escape/esc、delete/backspace、forwarddelete、up/down/left/right、home、end、pageup、pagedown、f1-f12；(2) 單一可印字元，用 `UCKeyTranslate` 走目前鍵盤佈局在 keycode 0-127 反查，大寫字母自動加 shift；(3) 查不到回 `unsupported key`。不為 `/` `.` `,` 這類字元維護寫死的表。
- **輸入法**：`get_state` 回報目前 input source id；`set_input_source` 走 HIToolbox `TISSelectInputSource`（ctypes），找不到 id 回錯。任務開始預設切 ABC；`type_text` 以 Unicode 直塞，ABC 下中英文都正確。
- **視窗 raise 與放大**是 runtime 在任務開始與 `open_app` 時自動做，不是工具。
- **系統保護對話框**（TCC 授權窗、Gatekeeper「從網路下載」對話框）合成事件無效，prompt 要求立即 `report_problem`。

## 觀察：state 格式

`Snapshot.render()` 輸出純文字（標頭 + 樹 + focused 摘要），截圖另以 image part 附上。manager 交給模型時在最前面加一行 `State N`（`snapshot_id`）；`render()` 本身不含它，重複偵測才比得出「沒變」。

- **標頭**：app 名稱、視窗標題、`window_changed`、`input_source`；樹撞到 `max_tree_nodes` 時末尾加一行截斷說明（`truncated`）。
- **每行元素**：

  ```
  <index> <role> "<label>"[ = "<value>"][ (focused)][ (disabled)] [x,y,w,h]
  ```

  `role` 為短角色名（button、textfield、checkbox、radio、link、statictext、menuitem 等）；`label` 取 title / description / placeholder，`label` 與 `value` 套 `text_limit`。只有可互動或帶文字的節點給 index，純容器攤平。
- **過濾規則**移植舊後端的 `AccessibilitySnapshot.swift`：Chrome 無名 AXGroup/AXUnknown 若帶 AXPress 保留為 button、連結保留獨立 index、列表顯示 `showing X-Y of N`。
- **index 生命週期**：每份快照都從 1 重新編號，同一個 index 在不同快照是不同元素。因此 `click` / `scroll` 帶 index 時必須同時帶 `state`（讀取該 index 的 `State N`），不等於最新 `snapshot_id` 就 `StaleIndexError`，轉成工具錯誤且不送任何點擊。只檢查 index 是否在範圍內不夠：舊 index 會靜默對到新快照的另一個元素。
- **畫面外的點**：index 的 bbox 中心或給定的 x/y 不在主螢幕範圍內就拒絕並要求先捲動。真游標會被夾到螢幕邊緣，否則會點到 Dock 或選單列。Chrome / Electron 會回報整份 DOM，畫面外元素很常見。
- **單一螢幕**：截圖、`screenshot_scale`、`maximize_window`、畫面外檢查都用主螢幕（`NSScreen.screens()[0]`，原點 (0,0)），不用 `mainScreen`（key window 所在螢幕）。多螢幕時目標視窗會被移到主螢幕，模型永遠看得到它在操作的那塊螢幕。
- `set_marks: true` 時用 Pillow 把 index 畫在截圖上（Set-of-Marks），預設關。

## 座標系統

- **單一座標空間**：screen points、螢幕左上為原點。AX 的 `AXPosition`/`AXSize` 與 CGEvent 原生就是這個空間，不經 NSScreen 的左下原點轉換。bbox、`click(x, y)`、`drag`、`scroll(x, y)` 全部用它。
- **截圖**：`CGWindowListCreateImage` 全螢幕，依 `backingScaleFactor` 縮到 1 pixel = 1 point，`region` 以 points 計，之後再套 `screenshot_max_width`（目前 1000）。螢幕寬度超過上限時截圖會再縮小，與 points 不再 1:1；此時 state 標頭多一行 `Screenshot: WxH, scaled Sx ...` 給出比例（`Snapshot.screenshot_scale`），螢幕座標 = 圖片像素 / S。prompt 規定座標以樹上的 bbox 為準，截圖用來確認畫面、辨識無標籤元素；只有目標不在樹上時才參考鄰近 bbox 從截圖估點。

## Context 管理（CU loop 內）

每步樹 + 截圖若全量保留，長任務的累計 input 會快速膨脹。策略沿用：

1. **舊快照折疊**：`_collapse_stale_states()` 只保留最近 `keep_full_states`（預設 2）份完整多模態 tool result；更舊的去圖、文字截到 `stale_text_max_chars`（預設 2000）。每輪只有剛過期的那一份會變動，已折疊前綴保持 byte-stable，對 prompt cache 友善。
2. **樹上限**：`max_tree_nodes`（預設 800），超過時 `truncated=True`。
3. **Prompt cache**：`agents.gui_manager.cache` 啟用後，組裝層用 `resolve_breakpoint_cache_ttl` 夾 TTL（kano_proxy / anthropic / openrouter 上限 `1h`），system message 掛 `cache_control`；每次 tool-loop request 前呼叫共用的 `advance_cache_breakpoint`（與 brain responder / worker 同路徑）。

## Loop 治理

治理寫在 loop 程式裡，不靠 prompt；prompt 只負責告訴模型這些規則存在。

- **一回應一工具**：一個回應只執行第一個非終止工具；同回應其餘工具一律回 `Error: one tool per response; read the returned state before the next action`，不計步。
- **重複偵測**：連續 `repeat_limit`（預設 3）次相同 `(tool, args)`，且回傳快照的 `render()` 文字與截圖粗指紋（`capture.coarse_fingerprint`：32x18、16 階灰階，游標閃爍與選單列時鐘會被抹掉）都相同 → 停止，status `paused`，並要求 situation report。只比 AX 文字會把 canvas / 無 AX 的 UI 上有效的捲動誤判為沒變。
- **動作已送出但讀狀態失敗**：`DesktopBackend._settle` 把 `get_state` 的例外包成 `StateReadError`，manager 回給模型「動作已執行，只是讀新狀態失敗，先 get_state，不要重做」。避免把已送出的提交或打字當成失敗再做一次。
- **loop 例外**：LLM 或 runtime 例外（非取消）以 `paused` 收尾，存下最後 state 與截圖，worker 可帶同一 session_id 續跑；不會留下 `active` 且空白的 session。
- **步數**：`max_steps` 是**每次 `gui_task` 呼叫**的步數。用完 → status `paused` + situation report，回 `[GUI PAUSED]`。總上限由 worker 管（同 session 續跑不限次，新 session 每任務合計至多 2 個，含第一個）。
- **終止語意**：`done` → `success`；`fail` → `failed`；`report_problem` → `blocked`；取消 → `failed`。
- **每個動作**：先做、等 `settle_seconds`、再 `get_state()`，回傳新快照。

### 結果格式（`gui_task`）

```
[GUI SUCCESS|FAILED|BLOCKED|PAUSED] (steps: N, time: Ns, session: <id>)
<summary>
Screenshot: <path>
Report:
<report>
```

BLOCKED / PAUSED 末尾加 `Call gui_task again with session_id=<id> and a new instruction to continue.`

`gui_task` 參數：`intent`（必填）、`app`（選填，英文名或 bundle id，runtime 開始前帶到前景並放大）、`session_id`（選填，續跑）、`app_prompt`（選填，workspace 相對路徑的 app 專屬指南，附加到 system prompt）。

### Session 與 resume

Session 檔放在 `<agent_os_dir>/session/gui/`：

| 檔案 | 內容 |
|------|------|
| `<session_id>.json` | `GUISessionData`：intent、app、status（`active`/`completed`/`failed`/`blocked`/`paused`）、summary、report、`steps_used`、`last_state_text`、`last_screenshot_path`、時間戳 |
| `<session_id>.steps.jsonl` | append-only debug log，每行 `{"tool", "args", "result"}`，`result` 是完整工具文字（去圖）。runtime 不讀它；`<session_id>.json` 只在 finalize 寫一次，`steps_used` 在那時累加 |
| `<session_id>.jpg` | 最後一張截圖；`gui_task` 回傳的 `Screenshot:` 指向它，不是共用暫存檔 |

Resume（`gui_task` 帶 `session_id`）：system 之後第一則 user 內容為 `Resuming GUI session <id>.`、上一輪 report、最後完整 state（`last_state_text`）、`New instruction: <intent>`，加上最後截圖；接著 runtime 照常 `prepare(app)` 附上目前狀態。不注入步驟流水帳，步數從 0 起算。

## 設定（cfgs/agent.yaml）

```yaml
agents:
  gui_manager:
    max_steps: 50            # 每次 gui_task 呼叫的步數上限，用完回 PAUSED
    allow_wait_tool: false   # false = 不提供 wait
    lock_wait_seconds: 300   # 等待 gui_lock 的上限，逾時回 [GUI BUSY]
    screenshot_max_width: 1000
    screenshot_quality: 70
    desktop:
      keep_full_states: 2
      stale_text_max_chars: 2000
      max_tree_nodes: 800
      text_limit: 200
      set_marks: false
      default_input_source: com.apple.keylayout.ABC
      settle_seconds: 0.4
      repeat_limit: 3
    cache:
      enabled: true
      ttl: "1h"
```

- `gui_manager.desktop`（`DesktopBackendConfig`）取代舊的 `gui_manager.ax`。設定是 pydantic `extra="forbid"`：`cfgs/agent.override.yaml` 若還留著 `gui_manager.ax` 底下的鍵，validate 會直接報錯，要手動刪掉或改到 `desktop`。
- `screenshot_max_width` / `screenshot_quality` 留在 `gui_manager` 原位，也供 brain 的 `screenshot` 工具使用。
- `agent/build.py` 以 `_build_subagent_client("gui_manager", gm_config, session_debug_label="gui_manager")` 建 client，直接組 `DesktopBackend` 與 `GUIManager`。

## 權限與部署

### 權限

GUI 後端需要兩項 TCC 權限：**輔助使用（Accessibility）** 與 **螢幕錄製（Screen Recording）**。

- **授權對象**：TCC 以「負責程序」計。正式環境由 launchd 跑 `<repo>/.venv/bin/python -m lincy start`（見 [host-runtime.md](host-runtime.md)「launchd」），權限要給這支 Python 執行檔解析後的真正 binary（例如 `~/.local/share/uv/python/cpython-3.12.x-macos-aarch64-none/bin/python3.12`），系統設定裡拖進去的是這個路徑，不是 `.venv/bin/python` symlink。從 Terminal 執行時 TCC 看到的是 Terminal 的權限，Terminal 有權限不代表服務有，正式以 launchd 下的 validate 為準。
- **路徑紀錄**：`check_gui_permissions` 全部通過時把解析後的 Python 執行檔路徑（`os.path.realpath(sys.executable)`，venv 的 symlink 重建後路徑不變，TCC 看的是真正的 binary）寫入 `state/gui_permissions.json`。之後缺權限且紀錄中的執行檔與現在不同，訊息會加註上次授權的路徑：venv 重建或升級 Python 後 TCC 視為新程式，要重新授權。
- **`lincy init`**：只檢查並印出「GUI permissions」段與要授權的 Python binary 解析後路徑，**不**觸發系統授權提示。TCC 把請求算在負責程序上，從 Terminal 跑 init 會變成授權給 Terminal，服務的 Python 仍然沒有。非致命。
- **預設輸入法**：validate 同時檢查 `gui_manager.desktop.default_input_source` 是已啟用的輸入法，不是就 `EnvironmentCheckFailed` 並列出可用的 id。每個 GUI 任務開頭都會切到它，錯的 id 不該拖到任務執行時才失敗。prompt 不寫死 ABC，任務訊息會告訴模型預設輸入法是哪個。
- **validate 階段**：`agents.gui_manager.enabled` 時由 `host/check.py` 呼叫 `check_gui_permissions`，清單非空 → `EnvironmentCheckFailed`，訊息就是清單本身，硬性早停。沒權限的 GUI agent 會全盲，寧可不啟動。每條訊息附系統設定路徑（隱私權與安全性 → 輔助使用 / 螢幕錄製）。
- **鍵盤導覽**：系統設定 → 鍵盤 →「鍵盤導覽」打開（`defaults read -g AppleKeyboardUIMode` 為 2 或 3），對話框按鈕才能用 Tab 切換。只提示不阻擋。
- **Secure Input**：擋的是 CGEventTap（讀取事件），不擋 CGEventPost（送出事件），一般密碼欄位不需特別處理。檢查只提示持有者，不阻擋。

### 主機狀態

- **螢幕必須解鎖且不睡眠**：關閉螢幕保護程式與自動鎖定，並讓服務期間保持喚醒（以 `caffeinate` 常駐，或在系統設定把顯示器睡眠設為永不）。
- **專機獨佔**：任務期間真游標會被搶走，不要在 GUI 任務進行中操作這台機器。
- **系統保護對話框不可操作**：Apple 自 Mojave 起過濾打到受保護 UI 的合成事件（`Sender is prohibited from synthesizing events`）。TCC 授權窗、Gatekeeper 對話框出現時 GUI agent 會 `report_problem`，由人透過螢幕共享處理。權限在專機上一次給齊，之後不會再問。

### 手動檢查（smoke）

```bash
uv run python -m lincy.gui.smoke [--app Calculator] [--click INDEX] [--type TEXT]
```

印權限狀態、輸入法、一份快照的前 40 行，把截圖存到 `/tmp/lincy_gui_smoke.jpg`。不帶 `--click` / `--type` 時不送任何事件。只在 lincy 主機手動跑，測試與 CI 不執行。注意從 ssh / Terminal 跑時權限看的是該終端程序，不等於 launchd 服務。

## 測試邊界

- 單元測試只測純邏輯：樹渲染與過濾（假 element 資料）、`parse_key`、截圖縮放數學、`check_gui_permissions` 的紀錄與訊息（monkeypatch 檢查函式）、`DesktopBackend`（注入假的 ax/input 模組）、manager loop（FakeBackend）、session 檔案格式、`gui_task` 結果格式。
- 禁止在測試或開發機上送任何 CGEvent，禁止帶 prompt 呼叫 Accessibility 檢查。

## 真機回歸清單（待在 lincy 主機執行）

以下尚未執行，需在 lincy 主機上以 launchd 服務跑：

- [ ] **第一步，最便宜也最關鍵**：Chrome 前景按 Cmd+O 開啟開檔面板，跑 `uv run python -m lincy.gui.smoke`，輸出必須有 `--- Keyboard focus is outside the window` 標頭且列出面板的取消／打開按鈕。沒有這行，後面 Google 表單回歸一定在同一個地方失敗，先修 `ax.build_snapshot` 的 outside-focus 路徑再往下
- [ ] smoke：權限、輸入法、快照、截圖輸出正常
- [ ] Calculator：按鍵運算並讀出結果
- [ ] TextEdit：建檔、打字（中英文）、關閉時處理儲存 / 不儲存對話框
- [ ] 系統設定：跨 pane 導航並讀值
- [ ] Google 表單含檔案上傳一次成功，開檔面板可操作（worker 先把檔案放到桌面）
- [ ] 兩個 Chrome 視窗同時開時，state 綁到前景視窗
- [ ] 注音開啟時任務開始自動切 ABC，`type_text` 中英文正確
- [ ] 步數用完回 `[GUI PAUSED]`，worker 續跑同一 session
- [ ] session debug log 可找到 gui_manager 的 requests / responses

## 決策紀錄

### 為何捨棄舊的 Swift vendor 後端

舊後端是 pin 在固定 commit 的開源 Swift computer-use server，經 stdio 協定呼叫，事件以 per-pid 投遞、只讀目標 app 的焦點視窗。2026-10-06 Google 表單任務兩輪各 50 步全失敗，根因（細節與 upstream issue 編號見 [task/gui-native-input-backend.md](task/gui-native-input-backend.md)「背景」）：

- **架構邊界**：per-pid 投遞到不了另一程序的開檔面板，AX 樹也看不到它；直接設值的工具回成功但沒改值；按鍵對照表缺 `/` `.` `,`。
- **upstream 方向**：pin 落後數十個 commit，新 commit 集中在其他平台與周邊功能，macOS 核心問題的 issue 無維護者回覆。
- **成本**：Swift toolchain、交叉編譯舊 macOS、supply-chain 稽核、每次點擊約 2 秒的虛擬游標動畫；而核心 API 只有 AX、CGEvent、截圖三組，pyobjc 全部可用。

替代方案（fork 後自改 Swift、只用 Python 補處理對話框形成兩條輸入路徑）都沒選。

### 刻意不做

- **TCC / Gatekeeper 對話框**：不嘗試自動化，一律 `report_problem` 交給人。
- **虛擬 HID**（Karabiner-DriverKit-VirtualHIDDevice）：需第三方 dext、root daemon、root client，滑鼠為相對位移且過加速曲線、鍵盤只有 keycode 沒有 Unicode，為一年幾次的對話框不划算。
- **Screen Sharing / VNC 注入**：同上，不走。
- **直接設值、AXPress 等第二條操作路徑**：backend 只有真實輸入，模型看到的狀態就是真的狀態，失敗模式只剩點錯位置或打錯字。
