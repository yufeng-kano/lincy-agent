# GUI 後端改為自寫真實輸入（取代 OpenComputerUse）

用 Python + pyobjc 自寫 GUI computer-use 後端：AX tree + 截圖 + vision 觀察，真實鍵盤滑鼠事件操作，取代 pin 住的 OpenComputerUse Swift MCP server。

## 狀態：進行中（1-7 完成，8 待 lincy 主機實機回歸）

## 背景

2026-10-06 Google 表單填寫任務兩輪各 50 步全失敗（log 已整理在 `~/Desktop/lincy_gui_log_20261006`，GUI session `20261006_175901_475dde`、`20261006_181338_fd81c6`）。根因：

1. **可觀測性為零**：gui_manager 的 LLM client 沒接 session debug（`src/lincy/agent/build.py:579` 沒傳 `session_debug_label`），session 檔每步只存結果第一行（`src/lincy/gui/manager.py:577` 的 `_first_text_line`）。模型自述「工具輸出被截斷」無法驗證。
2. **Loop 不強制 prompt 的硬規則**：一回應連發多個 index 動作、同一座標重複點 14 次、盲按 11 次 Up，`manager.py:536` 的 tool_calls 迴圈照單全收。
3. **OpenComputerUse 架構邊界**：事件走 `CGEvent.postToPid`、AX 樹只讀該 app 的 focused window。macOS 開檔面板跑在 `com.apple.appkit.xpc.openAndSavePanelService` 另一個程序，點擊與 Escape 永遠到不了，Cmd+Shift+G 被 Chrome 當成「找上一個」。upstream issue #82（Firefox 同問題，2026-09-24 開，零回覆）、#83（多視窗綁錯、set_value 回成功但沒改值、date 欄位填不進去，零回覆）。KeyMapping 缺 `slash`、`period`、`comma`，main 分支亦未補。
4. **upstream 維護方向**：pin `c8a7758`（2026-07-17）落後 39 commits，新 commit 集中在 JS REPL、DeepSeek Harness、release 打包、Linux/Windows；macOS 核心修補幾乎全來自外部貢獻者，open issue 無維護者回覆。
5. **Resume 機制存在但沒被用**：`gui_task(session_id=)` 可續跑任意 status 的 session，但 worker prompt 只說 blocked 才續、步數用完回 `[GUI FAILED]`，resume context 只有步驟流水帳（`src/lincy/gui/session.py:97`），situation report 存了卻沒注入。

歷史 17 個 GUI session 中 12 個成功或正確 BLOCKED，失敗集中在原生對話框、長流程原生 app 導航。

## 設計決策

### 捨棄 OpenComputerUse，自寫 Python 後端

- **選擇**：pyobjc 直接呼叫 ApplicationServices（AX）、Quartz（CGEvent）、`screencapture` 或 ScreenCaptureKit，不再有 Swift vendor、MCP stdio、pin commit、supply-chain 稽核、Sonoma 交叉編譯。
- **原因**：今天的失敗全在 vendor 內且 upstream 無修復跡象；OCU Kit 9400 行 Swift 中 2400 行是虛擬游標動畫（每次點擊約 2 秒）；核心 API 只有三組，pyobjc 全部可用（需新增 `pyobjc-framework-ApplicationServices`）。
- **替代方案**：fork OCU 改 Swift（仍要維護 Swift toolchain 與稽核）、只做 Python 替補處理對話框（兩條輸入路徑，決定不要）。

### 真實輸入，獨佔機器

- **選擇**：所有滑鼠鍵盤事件走 `CGEventPost` 到 HID tap（`kCGHIDEventTap`），動真游標、切前景。不做虛擬游標、不做 postToPid。
- **原因**：專機只給 agent 用；真實輸入「打到螢幕上是什麼就是什麼」，面板、選單、跨程序對話框全部不用管 pid。既有 `gui_lock` 已序列化 GUI 任務。
- **代價**：任務期間該機不可用；螢幕必須解鎖且不睡眠（`caffeinate`、關螢幕保護，寫進部署文件）。

### 工具面：只給「人坐在鍵盤前能做的事」

- **選擇**：`get_state`、`click`（座標或 index；左右鍵、單雙擊）、`drag`、`scroll`、`type_text`、`press_key`、`set_input_source`、`wait`、`done`、`fail`、`report_problem`。
- **拿掉**：`set_value`（人做不到，也是 OCU「回成功沒改值」的來源）、`perform_secondary_action`、存截圖到路徑、任何 shell。gui_manager 不改 command 權限，與 worker/brain 的 `excluded_tools` 設計一致。
- **原因**：一致性。每個動作都是真實輸入，模型看到的狀態就是真的狀態，失敗模式只剩點錯位置或打錯字。填欄位一律「點、Cmd+A、打字」，步數預算要重估。
- **配套**：視窗 raise 與放大是 runtime 在任務開始自動做，不是工具。

### 觀察：截圖 + AX tree（帶 bbox）+ vision

- 每個元素帶 bbox 進樹，格式如 `12 button "瀏覽" [x,y,w,h]`。
- 單一座標空間：screen points、左上原點（AX 與 CGEvent 原生即此），不碰 NSScreen 左下原點。截圖依 backing scale 縮到 1 pixel = 1 point。
- 視窗選擇：每次 state 從 systemwide `AXFocusedUIElement` 往上找所屬視窗。焦點在對話框就讀對話框、跨程序亦可；焦點視窗變更要在 state 明講。任務開始選定目標視窗、raise、設 `AXPosition`/`AXSize` 填滿可見螢幕（不用 macOS 全螢幕 Space）。
- 樹的過濾規則參考 OCU `AccessibilitySnapshot.swift`（Chrome 匿名圖示控件、連結邊界、`showing X-Y of N`），照搬邏輯不重新發明。
- 可選：index 畫在截圖上（Set-of-Marks），做成開關。
- `type_text` 用 `CGEventKeyboardSetUnicodeString` 直塞字元；`press_key` 用 `UCKeyTranslate` 從目前鍵盤佈局反查 keycode，不查寫死的表。

### 輸入法

- `get_state` 回報目前 input source id（如 `com.apple.keylayout.ABC`、`com.apple.inputmethod.TCIM.Zhuyin`）。
- `set_input_source` 工具，HIToolbox `TISCopyCurrentKeyboardInputSource` / `TISSelectInputSource`，pyobjc 沒包，用 ctypes 載 Carbon。
- 任務開始預設切 ABC。

### TCC、Secure Input、虛擬 HID：不做

- **決定**：不處理 TCC 授權窗與 Gatekeeper 對話框，不做 Karabiner-DriverKit-VirtualHIDDevice，不走 Screen Sharing/VNC 注入。遇到系統保護的對話框一律 `report_problem`，由人透過螢幕共享按。
- **原因**：Apple 自 Mojave 起過濾打到受保護 UI 的合成事件（`Sender is prohibited from synthesizing events`），只有 Apple 白名單例外。虛擬 HID 需第三方 dext、root daemon、root C++ client、滑鼠為相對位移且過加速曲線、鍵盤為 keycode 無 Unicode，為一年幾次的對話框不划算。
- **Secure Input** 擋的是 CGEventTap（讀），不擋 CGEventPost（寫），一般密碼欄位不需特別處理。
- **替代做法**：專機上權限一次給齊，之後不會再問（見權限檢查）。

### Loop 治理寫進程式，不靠 prompt

- 同一回應只執行第一個 index 動作，其餘回錯誤。
- 連續相同 `(tool, args)` 且 state 未變，強制終止並 report。
- 步數用完 status 為 `paused`（非 `failed`），回 `[GUI PAUSED]`；`max_steps` 語意改為「每次呼叫的步數」，總上限由 worker attempt 數管。
- Resume 注入上一輪 situation report + 最後截圖 + 最後完整 state，不注入步驟流水帳。

### 可觀測性

- gui_manager client 接 `wrap_llm_client_with_session_debug`，requests/responses 進 session debug log。
- session 檔每步存完整工具文字（去圖）。

### 權限檢查

- `lincy init`：盡力檢查 Accessibility（`AXIsProcessTrustedWithOptions`，**不帶** prompt：從 Terminal 跑會授權給 Terminal 而非服務）、Screen Recording（`CGPreflightScreenCaptureAccess` / `CGRequestScreenCaptureAccess`）、鍵盤導覽（`defaults read -g AppleKeyboardUIMode`）、Secure Input 持有者、目前輸入法。文字提示，並註明：TCC 以「負責程序」計，Terminal 下跑查到的是 Terminal 的權限，正式以 validate 階段為準。
- validate 階段（launchd 下、`docs/dev/host-runtime.md`）：正式檢查，缺就早停並寫 log（validate 失敗時沒有 channel 可通知）。記錄授權時的 Python 執行檔**解析後**路徑（`os.path.realpath(sys.executable)`，venv 的 symlink 重建後路徑不變，TCC 看的是真正的 binary），之後比對，路徑變（venv 重建、升 Python）直接提示要重授。

### 呼叫契約（不只 worker prompt）

GUI 的能力範圍與結果語意散在多處，要一起改，否則模型會拿到互相矛盾的描述：

| 位置 | 要改什麼 |
|------|---------|
| `kernel/agents/worker/prompts/system.md` | GUI agent 能力範圍：只有看與鍵鼠，無 shell、不能存檔、不能貼路徑；準備工作（檔案放桌面、算好值）與事後驗證歸 worker。`[GUI PAUSED]` 預設帶同一 `session_id` 續跑，除非 report 指出方向錯誤。attempt 上限改為「總步數」概念 |
| `kernel/agents/brain/prompts/system.md` | `worker` 工具說明與「瀏覽器、桌面 UI」段落（約 524、606 行）對 GUI 升級鏈的描述，與新能力範圍對齊 |
| `kernel/agents/gui_manager/prompts/system.md` | 全部重寫：新工具面、真實輸入、焦點視窗語意、輸入法、系統保護對話框一律 report_problem |
| `src/lincy/gui/tool_adapter.py` | `gui_task` 工具描述（範例與 intent 撰寫規則）、`session_id` 說明、`_format_result` 新增 `[GUI PAUSED]` |
| `kernel/builtin-skills/skill-installer/SKILL.md` | 提到 `gui_task` 的段落措辭對齊 |
| `docs/dev/brain-worker-delegation.md`、`host-runtime.md`、`web-dashboard.md` | GUI 升級鏈、validate 階段 OCU build、dashboard 顯示的 GUI 狀態等引用更新 |

以上 kernel 檔案變更一律附 migration。

## 套件與依賴

已完成（本 session）：`uv add pyobjc-framework-ApplicationServices pyobjc-framework-Quartz pyobjc-framework-Cocoa`、`uv remove pyautogui`。AX 函式在 `ApplicationServices`（`AXUIElementCreateSystemWide`、`AXUIElementCopyAttributeValue`、`AXIsProcessTrustedWithOptions`），CGEvent 在 `Quartz`。HIToolbox 的 TIS 與 `IsSecureEventInputEnabled` pyobjc 沒包，用 `ctypes` 載 `/System/Library/Frameworks/Carbon.framework/Carbon`。

## 檔案結構

```
src/lincy/gui/
├── __init__.py        # exports（GUIManager、GUITaskResult、DesktopBackend、Snapshot、session、tool_adapter、worker）
├── ax.py              # AX 樹快照：ElementRecord/Snapshot 建構、過濾規則、視窗 raise/maximize
├── capture.py         # take_screenshot(max_width, quality, region) -> ContentPart；save_screenshot(part, path)
├── input.py           # CGEvent HID tap：move/click/drag/scroll/type_text/press_key/parse_key
├── input_source.py    # TIS via ctypes：current_input_source()/list_input_sources()/select_input_source(id)
├── permissions.py     # Accessibility / Screen Recording / 鍵盤導覽 / Secure Input 檢查與執行檔路徑紀錄
├── desktop.py         # DesktopBackend：把 ax/capture/input/input_source 組成 manager 用的動作介面
├── manager.py         # GUIManager loop（重寫，不再有 MCP）
├── session.py         # GUISessionStore：paused 狀態、append-only 步驟檔、最後 state 與截圖
├── tool_adapter.py    # gui_task（新增 app 參數、PAUSED）、screenshot 工具
├── worker.py          # vision describer（只改 take_screenshot 的 import 來源）
└── smoke.py           # `uv run python -m lincy.gui.smoke`，lincy 主機上的手動檢查，測試不跑
刪除：actions.py、ax_runtime.py、mcp_client.py 及其測試
```

其他：`core/schema.py`（`AXServerConfig` 改 `DesktopBackendConfig`）、`cfgs/agent.yaml`、`agent/build.py`、`host/check.py`、`host/stages.py`、`host/init.py`、`agent/ui_event_console.py`（`print_gui_step` 去掉 `worker_timing`）、`worker/runner.py`（forced report 要求帶出 GUI session）、prompts、migration、docs。

## 技術設計

### 座標與狀態模型（`ax.py`）

```python
class ElementRecord(BaseModel):
    index: int
    role: str          # 短角色名：button, textfield, checkbox, radio, link, statictext, menuitem, ...
    label: str         # title / description / placeholder，套 text_limit
    value: str         # 欄位值、checkbox on/off、slider 值
    bbox: tuple[int, int, int, int]  # screen points，左上原點 (x, y, w, h)
    focused: bool
    enabled: bool

class Snapshot(BaseModel):
    snapshot_id: int               # backend 內單調遞增，index 只對同一 snapshot_id 有效
    app_name: str
    bundle_id: str
    pid: int
    window_title: str
    window_bbox: tuple[int, int, int, int]
    window_changed: bool           # 與上一份 snapshot 的 focused window 不同
    input_source: str              # 例 com.apple.keylayout.ABC
    elements: list[ElementRecord]
    truncated: bool                # 撞到 max_tree_nodes
    screenshot: ContentPart | None # JPEG，已縮到 1 pixel = 1 point，再套 screenshot_max_width
    def render(self) -> str        # 標頭 + 樹 + focused 摘要（純文字，不含圖）
```

- 樹的根：systemwide `AXFocusedApplication` 的 `AXFocusedWindow`；沒有則 `NSWorkspace.frontmostApplication` 的第一個視窗。若 systemwide `AXFocusedUIElement` 不在該視窗內（選單、popover、另一程序的面板），另附該元素所屬 top-level 元素的子樹。這就是開檔面板能被看到的機制。
- 每行格式：`<index> <role> "<label>"[ = "<value>"][ (focused)][ (disabled)] [x,y,w,h]`。只有可互動或帶文字的節點給 index；純容器攤平。過濾規則移植 OCU `AccessibilitySnapshot.swift`（本機快取 `~/.cache/lincy/ocu/src-c8a7758d50e1/packages/OpenComputerUseKit/Sources/OpenComputerUseKit/`）：Chrome 無名 AXGroup/AXUnknown 若帶 AXPress 保留為 button、連結保留獨立 index、列表顯示 `showing X-Y of N`。
- 預設 `max_tree_nodes: 800`、`text_limit: 200`。超過 cap 時 `truncated=True`，render 末尾加一行說明。
- bbox 來源 `AXPosition`/`AXSize`，已是 screen points 左上原點，不經 NSScreen 轉換。
- 視窗操作：`raise_window(pid)`（`AXRaise` + `NSRunningApplication.activate`）、`maximize_window(window)`（設 `AXPosition`/`AXSize` 為 `NSScreen.mainScreen.visibleFrame` 轉成左上原點座標；不用全螢幕 Space）。
- `set_marks: true` 時用 Pillow 把 index 畫在截圖上，預設關。

### 截圖（`capture.py`）

- `take_screenshot(*, max_width: int | None = None, quality: int = 80, region: tuple[int,int,int,int] | None = None) -> ContentPart`：簽名與現有 `actions.take_screenshot` 相同，`tool_adapter.py` 與 `worker.py` 只改 import。實作 `CGWindowListCreateImage` 全螢幕（`kCGWindowListOptionOnScreenOnly`），除以 `backingScaleFactor` 縮到 points，region 以 points 計，再套 `max_width`，JPEG。
- `save_screenshot(part: ContentPart, path: str) -> None`。

### 輸入（`input.py`）

- 事件來源 `CGEventSourceCreate(kCGEventSourceStateHIDSystemState)`，一律 `CGEventPost(kCGHIDEventTap, ...)`。
- `move(x, y)`、`click(x, y, button="left"|"right", count=1)`：mouseMoved → down → up，`kCGMouseEventClickState` 帶 count，事件間 0.03s。
- `drag(x1, y1, x2, y2)`：down、分段 move、up。
- `scroll(x, y, direction, amount=3)`：move 後 `CGEventCreateScrollWheelEvent`，line 單位。
- `type_text(text)`：以 `CGEventKeyboardSetUnicodeString` 每塊至多 20 個 UTF-16 unit 送 keyDown/keyUp；`\n` 改送 Return。
- `press_key(spec)`：`parse_key("Command+Shift+G")` → 修飾鍵（command/cmd、shift、option/alt、control/ctrl）+ 主鍵。主鍵解析順序：(1) 具名鍵表：return/enter、tab、space、escape/esc、delete/backspace、forwarddelete、up/down/left/right、home、end、pageup、pagedown、f1–f12；(2) 單一可印字元：用 `UCKeyTranslate` 走目前鍵盤佈局（`TISCopyCurrentKeyboardLayoutInputSource` 的 `kTISPropertyUnicodeKeyLayoutData`）在 keycode 0–127 反查，大寫字母自動加 shift；(3) 查不到就報 `unsupported key`。具名鍵表只放不可印鍵，不再為 `/` `.` `,` 這類字元維護表。

### 輸入法（`input_source.py`）

ctypes 載 Carbon：`TISCopyCurrentKeyboardInputSource`、`TISGetInputSourceProperty(kTISPropertyInputSourceID)`、`TISCreateInputSourceList`、`TISSelectInputSource`。`current_input_source() -> str`、`list_input_sources() -> list[str]`、`select_input_source(source_id: str) -> None`（找不到 raise ValueError）。

### 權限（`permissions.py`）

```python
def accessibility_granted(*, prompt: bool = False) -> bool       # AXIsProcessTrustedWithOptions
def screen_recording_granted(*, request: bool = False) -> bool  # CGPreflightScreenCaptureAccess / CGRequestScreenCaptureAccess
def keyboard_navigation_enabled() -> bool                        # defaults read -g AppleKeyboardUIMode in (2, 3)
def secure_input_enabled() -> bool                               # IsSecureEventInputEnabled via ctypes
def check_gui_permissions(state_dir: Path) -> list[str]
```

`check_gui_permissions` 回傳問題清單（空 = 通過）。全部通過時把 `os.path.realpath(sys.executable)` 寫入 `state_dir/gui_permissions.json`；缺權限且紀錄中的執行檔與現在不同時，訊息加註「上次授權的是 <舊路徑>，venv 重建或升級 Python 後 TCC 視為新程式，要重新授權」。每條訊息附系統設定路徑（隱私權與安全性 → 輔助使用 / 螢幕錄製）與「從 Terminal 執行時 TCC 看到的是 Terminal 的權限，正式以 launchd 服務為準」。

- validate 階段（`host/check.py`）：`gui_manager.enabled` 時呼叫，清單非空 → `EnvironmentCheckFailed`，訊息就是清單本身。這是硬性早停：沒權限的 GUI agent 會全盲，寧可不啟動。
- `lincy init`（`host/init.py`）：印一段「GUI permissions」與要授權的 binary 路徑，不觸發系統提示，非致命。鍵盤導覽與 Secure Input 只提示不阻擋。validate 另檢查 `default_input_source` 是已啟用的輸入法。

### DesktopBackend（`desktop.py`）

```python
class StaleIndexError(Exception): ...

class DesktopBackend:
    def __init__(self, *, screenshot_max_width: int | None, screenshot_quality: int,
                 max_tree_nodes: int, text_limit: int, set_marks: bool,
                 default_input_source: str, settle_seconds: float) -> None
    def get_state(self) -> Snapshot
    def prepare(self, app: str | None) -> Snapshot       # 任務開始：open_app(app) 或取 frontmost；maximize；切 default_input_source
    def open_app(self, name: str) -> Snapshot            # 依英文名或 bundle id 啟動/啟用、raise、maximize
    def click(self, *, index: int | None = None, x: int | None = None, y: int | None = None,
              button: str = "left", count: int = 1) -> Snapshot
    def drag(self, x1: int, y1: int, x2: int, y2: int) -> Snapshot
    def scroll(self, *, index: int | None = None, x: int | None = None, y: int | None = None,
               direction: str = "down", amount: int = 3) -> Snapshot
    def type_text(self, text: str) -> Snapshot
    def press_key(self, key: str) -> Snapshot
    def set_input_source(self, source_id: str) -> Snapshot
    @property
    def latest(self) -> Snapshot | None
```

- 每個動作先做、等 `settle_seconds`、再 `get_state()` 回傳新快照。
- `index` 只對 `latest.snapshot_id` 有效：模型傳的 index 對應不到最新快照的元素 → `StaleIndexError`，manager 轉成工具錯誤「index N 不在最新狀態裡，先看回傳的新狀態再動作」。點擊位置為 bbox 中心。
- backend 不做 AXPress、不做 set value，沒有第二條路。

### Manager loop（`manager.py`）

工具定義（全部靜態，在 manager.py）：

| 工具 | 參數 |
|------|------|
| `get_state` | 無 |
| `open_app` | `name` |
| `click` | `index` 或 `x`,`y`；`button`（left/right）；`count`（1/2） |
| `drag` | `x1`,`y1`,`x2`,`y2` |
| `scroll` | `index` 或 `x`,`y`；`direction`（up/down/left/right）；`amount` |
| `type_text` | `text` |
| `press_key` | `key` |
| `set_input_source` | `source_id` |
| `wait` | `seconds`（`allow_wait_tool` 可關） |
| `done` / `fail` / `report_problem` | 同現行 |

治理（寫在 loop，不靠 prompt）：

- 一個回應只執行**第一個**非終止工具；同回應其餘工具一律回 `Error: one tool per response; read the returned state before the next action`，不計步。
- 連續 `repeat_limit`（預設 3）次相同 `(tool, args)` 且回傳快照的 `render()` 文字相同 → 停止，status `paused`，summary 說明重複動作無效，並要 situation report。
- 步數用完 → status `paused` + situation report。`max_steps` 是每次呼叫的步數。
- `done` → `success`；`fail` → `failed`；`report_problem` → `blocked`；取消 → `failed`。
- 快照回傳給模型的 tool result 為 `[text: snapshot.render(), image: screenshot]`；舊快照折疊沿用 `_collapse_stale_states`（`keep_full_states`、`stale_text_max_chars`）。
- 任務開始：runtime 先 `backend.prepare(app)`，把結果當第一則 user 內容（文字 + 圖）附在 intent 後面，模型不必為此花一步。

```python
class GUITaskResult(BaseModel):
    status: Literal["success", "failed", "blocked", "paused"]
    summary: str
    report: str = ""
    session_id: str
    steps_used: int
    elapsed_sec: float
    screenshot_path: str = ""     # session 專屬路徑，不是共用暫存檔
```

### Session（`session.py`）

```python
class GUISessionData(BaseModel):
    session_id: str
    intent: str
    app: str | None = None
    status: Literal["active", "completed", "failed", "blocked", "paused"] = "active"
    summary: str = ""
    report: str = ""
    steps_used: int = 0
    last_state_text: str = ""        # 最後一份 Snapshot.render()
    last_screenshot_path: str = ""   # <gui_sessions_dir>/<session_id>.jpg
    created_at: datetime
    updated_at: datetime
```

- 步驟改 append-only：`<gui_sessions_dir>/<session_id>.steps.jsonl`，每行 `{"tool", "args", "result"}`，`result` 是完整工具文字（去圖）。`append_step` 只 append 一行並更新 `steps_used`，不再整檔重寫。`load_steps(session_id) -> list[GUIStepRecord]`。
- `finalize(session_id, *, status, summary, report, last_state_text, screenshot: ContentPart | None)` 寫入最後 state 與截圖檔。
- Resume（`gui_task` 帶 `session_id`）：system 之後第一則 user 內容為 `[text, image]`：`Resuming GUI session <id>.\nPrevious report:\n<report>\n\nLast observed state:\n<last_state_text>\n\nNew instruction: <intent>` 加最後截圖；接著 runtime 照常 `prepare(app)` 附上目前狀態。不再注入步驟流水帳。步數從 0 起算。
- `format_steps_as_context` 刪除。

### `gui_task`（`tool_adapter.py`）

- 參數：`intent`（必填）、`app`（選填，英文名或 bundle id，runtime 開始前帶到前景並放大）、`session_id`（選填，續跑）、`app_prompt`（同現行）。
- 描述寫明：GUI agent 只有看與鍵鼠（AX 樹 + 截圖 + 真實滑鼠鍵盤），沒有 shell、不能存檔、不能貼路徑、不能讀 worker 的檔案；intent 模板固定五欄：目標、成功條件、要填的值（逐項）、已準備好的東西（檔案在哪、頁面是否已開）、禁止事項；不寫操作步驟。
- 回傳：`[GUI SUCCESS|FAILED|BLOCKED|PAUSED] (steps: N, time: Ns, session: <id>)` + summary + `Screenshot: <path>` + `Report:` 段；BLOCKED/PAUSED 末尾加 `Call gui_task again with session_id=<id> and a new instruction to continue.`

### 設定（`core/schema.py`、`cfgs/agent.yaml`）

`AgentConfig.ax: AXServerConfig` 改為 `AgentConfig.desktop: DesktopBackendConfig`：

```yaml
  gui_manager:
    max_steps: 50            # 每次 gui_task 呼叫的步數上限，用完回 PAUSED
    desktop:
      keep_full_states: 2
      stale_text_max_chars: 2000
      max_tree_nodes: 800
      text_limit: 200
      set_marks: false
      default_input_source: com.apple.keylayout.ABC
      settle_seconds: 0.4
      repeat_limit: 3
```

`screenshot_max_width` / `screenshot_quality` 留在 `gui_manager` 原位。`BuildInputs.ax_binary`、`ValidatedEnv.ax_binary` 刪除；`build.py` 以 `_build_subagent_client("gui_manager", gm_config, session_debug_label="gui_manager")` 建 client，直接組 `DesktopBackend` 與 `GUIManager`。

### Worker / brain 契約落點

- `worker/runner.py` 的 `_FORCED_REPORT_PROMPT` 加一句：有未完成的 gui_task session 時，報告必須帶 `session_id` 與狀態。
- brain prompt 結果處理加 PAUSED 分支：原任務單加「續跑 GUI session <id>」重新委派。
- 其餘見「呼叫契約」表。

### Smoke（`smoke.py`）

`uv run python -m lincy.gui.smoke [--app Calculator] [--click INDEX] [--type TEXT]`：印權限狀態、輸入法、一份快照的前 40 行、把截圖存到 `/tmp/lincy_gui_smoke.jpg`。不帶 `--click`/`--type` 時不送任何事件。只在 lincy 主機手動跑，測試與 CI 不執行。

### 測試邊界

- 單元測試只測純邏輯：樹渲染與過濾（餵假的 element 資料）、`parse_key`、截圖縮放數學、`check_gui_permissions` 的紀錄與訊息（monkeypatch 檢查函式）、`DesktopBackend` 以假的 ax/input 模組注入、manager loop 以 FakeBackend、session 檔案格式、`gui_task` 格式。
- 禁止在測試或開發機上送任何 CGEvent、禁止帶 prompt 呼叫 Accessibility 檢查。
- 真機回歸（步驟 8）在 lincy 主機跑 smoke 與回歸清單，本 session 不做。

## 步驟

1. 可觀測性：gui_manager 接 session debug；步驟存完整文字。
2. Loop 治理：單 index 動作、重複偵測、`paused` 狀態、resume 注入 report + 截圖 + state。
3. 呼叫契約：worker/brain/gui_manager 三份 prompt、`gui_task` 工具描述與結果格式、skill-installer、相關 docs；kernel migration。
4. 新後端：capture、ax tree with bbox、input（click/drag/scroll/type/key）、input_source、視窗自動 raise/放大。
5. 新 gui_manager prompt 與工具定義；移除 OCU 相關程式、config、文件。
6. 權限檢查：init 提示、validate 正式檢查與路徑比對。
7. 文件：重寫 `docs/dev/gui-computer-use.md`，部署文件加 caffeinate/螢幕保護/權限路徑。
8. 回歸：Calculator、TextEdit、系統設定導航、Google 表單含檔案上傳、兩個 Chrome 視窗同時開。

## 驗證

- Google 表單含檔案上傳一次成功，開檔面板可操作。
- 兩個 Chrome 視窗時 state 綁到前景視窗。
- 注音開啟時任務開始自動切 ABC，`type_text` 中英文正確。
- 步數用完回 `[GUI PAUSED]`，worker 續跑同一 session。
- session debug log 可找到 gui_manager 的 requests/responses。

## 完成條件

- [x] OCU 相關程式與 config 全移除（`ax_runtime.py`、`mcp_client.py`、`actions.py`、`AXServerConfig`、validate 的 `ensure_binary`、pyautogui 依賴）
- [x] 新後端與 loop 治理單元測試通過（`uv run pytest -q`：1986 tests，shell_task 的 PTY 測試在高負載下會 flake，乾淨 HEAD 一樣；其餘全過；ruff clean）
- [x] 文件、prompt、migration m0179（kernel 0.79.0）更新
- [ ] 步驟 8 真機回歸（lincy 主機）：`uv run python -m lincy.gui.smoke`，再跑回歸清單（見 `docs/dev/gui-computer-use.md`）

## 實作與規格的差異（已確認接受）

- `press_key` 的字元反查用 `TISCopyCurrentASCIICapableKeyboardLayoutInputSource`，不是 current layout：注音啟用時 current layout 把 `a` 對到ㄇ，ASCII-capable layout 才是快捷鍵走的那套。
- `Snapshot` 多了 `outside_focus` / `outside_focus_count`：鍵盤焦點在視窗外（選單、他程序面板）時，該子樹排在最前面，截斷不會吃掉它；以及 `screenshot_scale`，截圖被 `screenshot_max_width` 縮小時標頭多一行比例。
- 每份快照對 application 元素寫 `AXManualAccessibility` / `AXEnhancedUserInterface` = true（Chromium/Firefox 不設就不吐網頁內容；OCU 亦同）。`maximize_window` 期間暫關 `AXEnhancedUserInterface`。除此之外只寫 `AXPosition`/`AXSize`/`AXRaise`，無 AXPress、無 set value。
- 一回應只執行第一個工具，包含 `done`/`fail` 排在動作後面也不接受（模型沒看到動作結果就宣告完成）。
- `wait` 回傳新 state 而非文字；repeat key 包含結果文字，連續三次相同錯誤也會 paused。
- `check_gui_permissions` 只擋 Accessibility 與 Screen Recording；鍵盤導覽、Secure Input 只在 init 提示（鎖屏時 Secure Input 為 on，擋它會讓服務起不來）。
- `session/cleanup.py` 連同 `<id>.steps.jsonl`、`<id>.jpg` 一起清。
- code review 後修正：`click`/`scroll` 帶 index 須附 `state`（每份快照從 1 重新編號，舊 index 會靜默對到別的元素）；畫面外的點拒絕；截圖、比例、maximize、畫面外檢查統一用主螢幕；重複偵測加截圖粗指紋；動作已送出但讀狀態失敗回 `StateReadError` 專屬訊息；loop 例外以 `paused` 收尾並保存最後 state；steps.jsonl 純 append、session JSON 只在 finalize 寫；`lincy init` 不再觸發 TCC 提示；validate 檢查預設輸入法；刪除依賴 pyautogui 的 `scripts/click_test.py`、`scripts/scroll_test.py`。

## 尚未在真機驗證

- 開檔面板透過 outside_focus 子樹可見（開發機上 systemwide focus 查詢對 Firefox 回 -25204，只有假資料測過）。
- `TISSelectInputSource` 從背景執行緒呼叫；scroll 方向與「自然捲動」設定；`maximize_window` 對會夾尺寸的 app；`activateWithOptions_` 在新版 macOS 是否真的帶到前景；Chrome 大頁面的樹品質與耗時。
- 任何真實點擊、拖曳、捲動、打字、按鍵都還沒送過。
- lincy 主機目前跑 `0df2f60`（main），本工作在 `host-runtime-refactor`；主機的 `cfgs/agent.override.yaml` 沒有 `gui_manager.ax` 區塊，不會被 strict config 擋。主機 `AppleKeyboardUIMode` 未設（鍵盤導覽關），init 會提示。
