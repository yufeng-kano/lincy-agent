# GUI Computer Use（AX-first 架構）

本文件定義 GUI 任務子系統的架構、context 管理策略與 vendor 規範。2026-07 起取代舊的「視覺 worker 猜座標」範式。

## 背景與動機

舊架構（三層）：brain → `gui_task` → GUIManager（全盲，靠文字轉述）→ GUIWorker（Gemini 視覺，截圖給 bbox）→ pyautogui 前景點擊。弱點不在視覺模型的指位準度（實測 opus 4.8 / gpt-5.5 / gemini flash 指位全數命中），而在架構本身：

- manager 看不到畫面，靠 worker 文字二手轉述
- 每步重新視覺搜尋元素，無語義結構、看不到捲軸外內容
- pyautogui 佔用真實游標、剪貼簿（`type_text` 走 pbcopy+Cmd+V）與前景焦點

新架構仿照官方 Codex computer use（AX-first）：以 accessibility tree 為主要觀察與定位手段、截圖為輔、事件走背景投遞（`CGEventPostToPid` 定向到目標 app，不動真游標）。本機驗證：opus 4.8 當 brain 的端到端任務 3/3 成功（計算機運算、TextEdit 建檔打字含 discard 對話框、系統設定跨 pane 導航查值，含 app 中途重啟與 tool error 的自主恢復）。

## 架構

```
brain --gui_task--> GUIManager (opus 4.8, AX-first CU loop)
                       |-- MCP stdio --> OpenComputerUse server (Swift, 本地)
                       |                   AX tree + 視窗截圖 + 背景輸入
                       `-- 本地工具: wait / done / fail / report_problem

brain --screenshot_by_subagent--> GUIWorker (Gemini 視覺describe，僅此用途)
```

- **兩層不變**：CU loop 隔離在 `gui_task` 子 loop，不把 9 個 AX 工具掛到 brain。理由：每步 tree+截圖是高流量暫時性 context，掛 brain 會炸 `soft_max_prompt_tokens`、破壞 brain cache 準則，且做完後殘留對話歷史。
- **manager 直接看圖**：tool result 內含截圖（claude_code provider 支援 tool result image parts），不再有視覺轉述層。
- **gui_worker 僅存於** `screenshot_by_subagent`；GUI 任務 loop 不再依賴。
- `gui_task` 對 brain 的介面（intent / session_id / app_prompt、背景執行、GUI lock）完全不變。

## 工具面（9 MCP + 4 本地）

MCP 工具全帶 `app` 參數（英文名或 bundle id；本地化名稱解析不到）：
`list_apps`、`get_app_state`、`click`（element_index 或 x/y fallback、右鍵、雙擊）、`drag`、`scroll`、`type_text`、`press_key`、`set_value`、`perform_secondary_action`。

本地工具：`wait`（可用 `allow_wait_tool` 關閉）、`done`、`fail`、`report_problem`（terminal 語義與舊版相同）。

工具定義為靜態（`gui/manager.py` 的 `MCP_TOOL_DEFS`），與 pin 的 server schema 對齊；升級 pin commit 時須核對 schema。

### 關鍵行為（實測）

- **Element index 會隨 UI 變化重排**，只對最新快照有效。動作工具會自帶回新狀態，prompt 已要求逐步重新定位、禁止跨快照批次點擊。
- `get_app_state` 0.2-0.8s/次；未執行的 app 會自動啟動。
- 樹品質分四級（2026-07 本機實測）：原生 app 優（Calculator 每鍵有穩定 ID、顯示值可直讀）；Electron/Gecko 極佳（Obsidian 757 元素、Firefox 654）；Qt 結構在內容盲（LINE 33 元素，rows 無標籤但列表可捲、composer settable）——用「截圖認位置、index 點擊、set_value 輸入」混合模式；自繪 GPU UI 荒漠（Zed 14 元素）——只能座標 fallback，精度不足時 report_problem。

## Context 管理（CU loop 內）

每步 tree（0.7k-27k chars）+ 截圖若全量保留，14 步任務累計計費 input 可達 350k tokens（無 cache 裸跑實測值）。策略：

1. **舊快照折疊**：`_collapse_stale_states()` 只保留最近 `keep_full_states`（預設 2）份完整多模態 tool result；更舊的**去圖、文字截到 `stale_text_max_chars`（預設 2000）**——token 大頭（截圖、巨樹尾部）砍掉，但已觀察到的內容保留給讀取/回報型任務。每輪只有「剛過期的那一份」變動，已折疊前綴保持 byte-stable，對 prompt cache 友善。
2. **樹上限**：`ax.max_tree_nodes` / `ax.max_tree_depth` 傳入 `get_app_state`（預設 null = server 預設）。
3. **Prompt cache（對齊 brain）**：`agents.gui_manager.cache` 啟用後，組裝層用 `resolve_breakpoint_cache_ttl` 夾 TTL（claude_code / anthropic / openrouter 上限 `1h`），system message 掛 `cache_control`；每次 tool-loop request 前呼叫共用的 `advance_cache_breakpoint`，把 conversation-tier breakpoint 推到最新合格 message（與 brain responder / worker 同路徑）。

## Vendor 與供應鏈

- 來源：[iFurySt/open-codex-computer-use](https://github.com/iFurySt/open-codex-computer-use)（MIT），**pin commit** 於 `gui/ax_runtime.py` 的 `DEFAULT_COMMIT`。
- 建置：`chat-supervisor start` 的 `ax-server-build` oneshot（`python -m lincy.gui.ax_runtime`）→ shallow fetch pin commit（`GIT_LFS_SKIP_SMUDGE=1`，upstream LFS 額度已爆且 LFS 物件僅為逆向資產）→ `swift build -c release` → 快取到 `~/.cache/lincy/ocu/<commit12>/`。已快取則秒過。cli 組裝時也會 `ensure_binary()` 兜底。
- 稽核（pin commit 當下）：Kit 與主程式零網路 API（無 URLSession/socket）、零外部 Swift 依賴。升級 pin 時重跑此稽核。
- 前置需求：swift toolchain（缺失時 oneshot 早停並給出安裝指令）、Accessibility + Screen Recording 權限（授予宿主進程；`OpenComputerUse doctor` 可檢查）。**重編譯/更新 binary 後 TCC 權限可能要重新授予**。
- 設定覆寫：`gui_manager.ax.repo/commit/binary_path`（預設 null = 用 pin 值）。

## 設定（cfgs/agent.yaml）

`gui_manager.ax` 是 AX 後端的完整設定面。兩條消費路徑都讀同一份設定、行為一致：

- **cli 組裝**：`cli/app.py` 把來源覆寫傳給 `ensure_binary()`、行為調校傳給 `GUIManager`
- **supervisor oneshot**（`ax-server-build`）：`ax_runtime.resolve_build_params()` 讀 `cfgs/agent.yaml`；`gui_manager.enabled: false` 時直接跳過建置

```yaml
agents:
  gui_manager:
    cache:
      enabled: true
      ttl: "1h"   # breakpoint providers clamp to 1h
    ax:
      # --- 來源覆寫（不設 = 用 gui/ax_runtime.py 的 DEFAULT_REPO / DEFAULT_COMMIT pin 值）---
      repo: null         # 改用 fork 時填 git URL
      commit: null       # 40 字元 sha；改了等於換 cache 目錄，該機需 Swift >= 6.2 自建
      binary_path: null  # 直接指定現成 binary，優先於一切、跳過 clone+build

      # --- 行為調校 ---
      keep_full_states: 2        # loop 保留幾份完整 tree+截圖
      stale_text_max_chars: 2000 # 折疊後每份舊狀態保留的文字上限（去圖、截文）
      max_tree_nodes: null       # 單次 get_app_state 樹節點上限（null = server 預設）
      max_tree_depth: null       # 樹深度上限（null = server 預設）
      tool_timeout: 90           # 單一 MCP 工具呼叫逾時（秒）
```

升 pin（改 `commit`）時依「Vendor 與供應鏈」規範重跑網路 API 稽核，並核對 9 工具 schema——`gui/manager.py` 的工具定義（`MCP_TOOL_DEFS`）是靜態對齊 pin commit 的。x86 / 舊 Swift 主機見下一節，或用 `binary_path` 跳過自建。

已移除：`allow_direct_screenshot`（manager 恆為直接看圖）。`gui_worker` 段保留（screenshot_by_subagent 用）。

## 舊 Swift 主機部署（如 Intel Sonoma VM）

upstream `Package.swift` 要求 `swift-tools-version: 6.2`；macOS 14（Sonoma）的 CLT 上限為 Swift 6.0.x，**無法自建**（oneshot 會報 tools version 錯誤）。`platforms` 為 `.macOS(.v14)`、程式無更高 API floor，因此 **binary 本身相容 Sonoma 與 x86_64**——只是要在別台編。流程：

```bash
# 在任一 Swift >= 6.2 機器（arm64 Mac 可直接交叉編譯 x86_64）
swift build -c release --product OpenComputerUse --arch x86_64
scp "$(swift build -c release --product OpenComputerUse --arch x86_64 --show-bin-path)/OpenComputerUse" \
    <host>:.cache/lincy/ocu/<commit12>/OpenComputerUse
```

`ensure_binary()` 看到 cache 就不會要求 swift toolchain。或改設 `gui_manager.ax.binary_path` 指向任意路徑。注意：

- 未簽章 binary **首次執行**會被 Gatekeeper/XProtect 掃描拖慢（舊 Intel 機可達分鐘級、看似掛死），一次性，之後正常
- `doctor` 與 TCC 權限檢查須在 GUI session 內執行（純 ssh 無 window server 會卡住）；Accessibility + 螢幕錄製權限也要在 VM 的 GUI 裡授一次
- pin commit 升級後需重新交叉編譯部署，直到該主機有 Swift >= 6.2

## 已知限制與後續

- LINE 混合模式尚未跑過真實聊天任務回歸（建議用測試帳號驗證後再開放 LINE 相關 skill）。
- 舊 `actions.py` 保留：`activate_app`（resume 重啟 app）、`take_screenshot`（brain 的 `screenshot` 工具）、其餘座標函式已無 caller，待清理。
- server 端虛擬游標動畫使單次 click ~2s，可研究 server 設定調速。
- GUI session resume 格式沿用；舊 session 的步驟紀錄（ask_worker/bbox 工具名）對新 loop 僅為歷史文字，無相容問題。
