---
name: openclaw-cron
description: 建立、修改、除錯 OpenClaw 排程（automations／cron）。觸發：cron、排程、定時任務、automation、job 失敗、沒跑、model not allowed。
---

# OpenClaw Cron（Automations）

Gateway 內建的排程器：時間到就叫醒 agent 執行，結果可選擇送到聊天頻道。
**gateway 沒開，排程就不會觸發。**

> 本檔對齊 OpenClaw 2026.10.9（2026-11-03 以 `--help` 與實際 job 查證）。
> 版本更新後先跑 `openclaw cron add --help` 對照；文件：https://docs.openclaw.ai/cli/cron

## 指令名稱

`openclaw cron` 與 `openclaw automations` 是同一組指令（互為別名）；`add` 與 `create` 也是別名。
Homebrew 安裝，一律用裸的 `openclaw`（`/opt/homebrew/bin/openclaw`），資料目錄 `~/.openclaw`。

## ⚠️ 不要直接改檔案

排程與執行紀錄已遷移到 SQLite（`~/.openclaw/state/openclaw.sqlite`，有 `cron-authority.lock`）。
`~/.openclaw/cron/jobs.json`、`cron/runs/*.jsonl` 都已改名為 `*.migrated`，**改它們沒有任何效果**。
所有修改一律用 `openclaw cron edit`；也不要直接寫 sqlite。

## 常用指令

| 指令 | 用途 |
|---|---|
| `openclaw cron list` | 列出所有 job |
| `openclaw cron status` | 排程器狀態 |
| `openclaw cron show <id>` / `get <id>` | 看單一 job（`get` 輸出 JSON） |
| `openclaw cron add ...` | 建立 |
| `openclaw cron edit <id> ...` | 修改（只改有帶的欄位） |
| `openclaw cron run <id> [--wait]` | 立刻跑一次（`--due` 只在到期時跑） |
| `openclaw cron runs <id> [--limit N] [--status ...] [--query ...]` | 執行紀錄；`--all` 看全部 job |
| `openclaw cron enable/disable <id>` | 啟用／停用 |
| `openclaw cron rm <id>` | 刪除 |
| `openclaw cron scratch <id>` | 讀寫該 job 的私有暫存區 |

## 建立 job

### 標準流程：先停用建立 → 手動 run → 確認後再啟用

```bash
openclaw cron add "0 8 * * 1-5" "用 morning-check skill 執行今天的早班巡檢" \
  --name "早班巡檢" --tz "Asia/Taipei" \
  --session isolated --no-deliver --disabled
openclaw cron run <id> --wait
openclaw cron runs <id> --limit 3
openclaw cron enable <id>
```

- 排程與 prompt 可用位置參數（`"<schedule>" "<prompt>"`），schedule 可為 cron 式、`every 1h`、`20m`、ISO 時間。
  也可用具名參數：`--cron` / `--every <duration>` / `--at <when>` ＋ `--message`。
- **一定帶 `--tz`**，不帶就用 gateway 主機時區。

### 長 prompt 怎麼辦

`--message` 沒有讀檔參數。做法：
1. **建議**：把規則與步驟寫進 skill，cron 的 prompt 只寫「用 X skill 做 Y」。prompt 可審、可在聊天裡手動測。
2. 用 shell 讀檔：`--message "$(cat prompt.md)"`。注意存進 job 後與原檔脫鉤，改檔要再 `edit`。

### 一次性提醒（main session）

```bash
openclaw cron add --name "Reminder" --at "2026-12-01T09:00:00+08:00" \
  --session main --system-event "提醒：檢查電池"
```
一次性 job **成功後預設自動刪除**；要保留加 `--keep-after-run`。

### 定時報告並送到頻道（isolated）

```bash
openclaw cron add --name "Morning brief" --cron "0 7 * * *" --tz "Asia/Taipei" \
  --session isolated --message "Summarize updates." \
  --announce --channel discord --to "<channel-id>"
```

## Payload（每個 job 只能有一種）

| 類型 | 參數 | 說明 |
|---|---|---|
| systemEvent | `--system-event <text>` | 插入 main session 的一則事件 |
| agentTurn | `--message <text>` 或位置參數 | 跑一輪 agent（main／isolated 都可） |
| command | `--command <shell>` / `--command-argv <json>` | 在 gateway 主機跑指令，不呼叫模型 |
| script | `--script <file\|->` | headless script，僅 main／isolated |

## Session 目標

`--session main|isolated|current|session:<id>`
- `main`：在 agent 主對話裡跑，帶既有 context。
- `isolated`：每次全新 session（key `cron:<jobId>`），適合報告與背景工作。
- `current`：綁定建立當下的對話；`session:<id>`：固定的持久 session。

## 常用參數

| 參數 | 說明 |
|---|---|
| `--name` / `--description` / `--display-name` | 名稱、說明、顯示名 |
| `--agent <id>` | 綁定 agent（edit 用 `--clear-agent` 解除） |
| `--disabled` | 建立時先停用（edit 用 `--enable` / `--disable`） |
| `--model` / `--fallbacks` / `--thinking` | 模型、備援、思考等級（edit 有 `--clear-*`） |
| `--tools <list>` | 工具白名單；`""` 關閉所有工具（存成 `payload.toolsAllow`） |
| `--light-context` | 精簡 bootstrap context |
| `--timeout-seconds <n>` | agent／command 逾時 |
| `--exact` / `--stagger <dur>` | 關閉／設定 cron 錯開時間 |
| `--announce` `--channel` `--to` `--thread-id` `--account` | 結果送到頻道；`--channel` 為 `last` 或任一 channel plugin id |
| `--no-deliver` | 不送出（isolated 預設會 announce，不想對外就加這個） |
| `--best-effort-deliver` | 送不出去不算 job 失敗 |
| `--webhook <url>` | 完成後 POST 結果 |
| `--trigger-script <file>` / `--trigger-once` | 條件觸發 |
| `--keep-after-run` / `--delete-after-run` | 一次性 job 跑完是否保留 |

已棄用：`--deliver`（改 `--announce`）、`--wake now|next-heartbeat`（仍接受但不再需要）。

### 失敗通知（edit 專用）

```bash
openclaw cron edit <id> --failure-alert --failure-alert-after 3 \
  --failure-alert-channel discord --failure-alert-to "<channel-id>" --failure-alert-cooldown 1h
```

## 除錯

1. `openclaw cron runs <id> --limit 5`（或 `--status error`、`--query "<關鍵字>"`）看 status／error。
2. `openclaw cron show <id>` 看 `state.lastStatus`、`consecutiveErrors`、`lastDeliveryStatus`。
3. 手動重現：`openclaw cron run <id> --wait`。
4. Log：`~/.openclaw/logs/gateway.log`、`gateway.err.log`；每日 log 在 `/tmp/openclaw/openclaw-YYYY-MM-DD.log`。
5. isolated 的 session 紀錄：`~/.openclaw/agents/<agentId>/sessions/`。

常見錯誤：
- `model not allowed: <model>` → `openclaw cron edit <id> --clear-model` 或換允許的模型。
- `429 / RESOURCE_EXHAUSTED` → 限流，檢查 `--fallbacks`。
- CLI `gateway timeout` → CLI 預設等 30s，job 可能仍在背景跑；用 `runs` 確認，或 `run --wait`。
- 有跑但沒收到訊息 → 看 `lastDeliveryStatus`；`delivery.mode: none` 代表根本沒設定送出。
- 版本升級後怪怪的 → `openclaw doctor --fix` 會正規化舊版 automation 儲存。

JSON 結構見 `references/job-schema.md`。
