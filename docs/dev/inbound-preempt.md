# Inbound Preempt：同一對話的新訊息併入進行中的 turn

brain 正在跑 tool loop 時，同一個對話又來了新訊息，系統不等這輪跑完：取消還沒執行的 tool call，把新訊息接在對話後面，brain 在同一輪直接接著處理。所有 channel 走同一條路，沒有 channel 特判。

## 流程

1. 新訊息經 adapter 進入 queue（Discord 要先過 debounce，cli 與 Gmail 直接進）。
2. responder 在**每個** tool call 執行前呼叫 `check_preempt`（`agent/responder.py`），查 queue 裡有沒有同一對話的 ready inbound。
3. 命中時，這一輪剩下的 tool call 全部填入 `Error: preempted — new message arrived; action cancelled.` 的 tool result，該輪 assistant 的草稿文字清掉，已完成的 tool result 保留。
4. `on_preempt`（`AgentCore._make_preempt_taker`）用 `take_ready_pending_inbound` 把同一對話的訊息從 queue 拿出來，逐則 `print_inbound`、加成 user message（帶 channel / sender / timestamp / timing metadata）、ack receipt。
5. responder 重建 messages 再呼叫模型，迴圈繼續。拿不到訊息（例如中途被 `schedule_action` 移除）就退回舊行為：結束本輪，下一輪再處理。

正在執行中的那一個 tool call 不會被殺掉，取消的是它後面的。模型呼叫同樣等它回來。

brain prompt 的「同一輪被新訊息打斷」一節告訴模型：被取消的呼叫沒有執行，先讀新訊息再決定要不要重做，不為被打斷道歉。

## 對話範圍

比對鍵是 `scope.py` 算出的 `scope_id`，由 `AgentCore.enqueue` 寫進 `metadata["scope_id"]`：

| channel | scope |
|---------|-------|
| Discord DM | `discord:dm:{user_id}` |
| Discord guild 頻道 | `discord:channel:{channel_id}`（頻道內任何人發言都算） |
| Gmail | `gmail:thread:{thread_id}`，缺 thread 時 `gmail:sender:{email}` |
| cli | 無 scope，退回比對整個 channel |
| system / worker | 永不觸發 |

`PersistentPriorityQueue.has_ready_pending_inbound` 與 `take_ready_pending_inbound` 都只看 in-memory ready queue：delayed 訊息、其他 channel、system 訊息不算。

## 上限

`tools.max_preempts_per_turn`（`cfgs/agent.yaml`，預設 3）限制一輪最多併入幾次，避免連發訊息把一輪拉得無限長。超過上限後 tool call 照常執行，後續訊息等下一輪。`tools.max_tool_iterations` 仍然照常累計。

## 與 cli 的關係

`ConsoleAdapter.submit()` 不再在上一輪未結束時回 `AgentBusy`；訊息直接進 queue，由進行中的 turn 併入。`AgentBusy("Agent is still starting.")` 仍保留（`mark_ready()` 之前）。

## 與 Discord debounce 的關係

Discord 的輸入偵測不變：DM 等 `dm_debounce_seconds`（現為 8 秒）靜默，打字中另有 `dm_typing_quiet_seconds`。訊息進 queue 之前 brain 看不到它，先前「訊息還在 debounce buffer 就先通知 preempt」的 `has_buffered_inbound` 訊號已移除，統一以進入 queue 為準。

## Worker 的對應能力

brain 自己被打斷時，不會自動動到已派出的 worker。要改 worker 的方向用 `worker_append`，要停用 `worker_cancel`，見 [brain-worker-delegation.md](brain-worker-delegation.md) 的「中途控制」一節。
