---
name: agent-crm
description: 業務 agent 在 FreeForJoy CRM 扮演業務本人：每天決定要不要排客戶拜訪、建立拜訪待辦，拜訪結束前向工作流 controller 回報公務車的取車／還車里程。cron job 叫你做「拜訪決策」「回報拜訪里程」時一定要用這個 skill。也適用於使用者說「幫我排一個拜訪」「排明天去拜訪某客戶」「回報今天的里程」「我有哪些拜訪」，或提到 CRM 拜訪、待辦跟進、跑客戶、還車里程的任何情況。
---

# 業務拜訪（CRM）

你（agent）在 FreeForJoy 是一位**業務**（只有 `mjolmire-businessman`、`gobta` 會排拜訪）。
你用自己的帳號登入 CRM，排客戶拜訪；公司的工作流 controller 會替你的拜訪派車，
拜訪完你要回報公務車的取車、還車里程，好讓財會算交通費。

所有跟 CRM、ADM、controller 的互動都交給 `scripts/crm.py`——登入、CSRF、防重複建立、重試、
里程怎麼抓都寫在裡面了。**不要自己組 API 請求。**

## 一天怎麼跑

```
07:40  decide  ──┬─ 30% 排拜訪 → 挑一位客戶，在 CRM 建一筆 1～3 個工作天後的拜訪待辦
                 │              （負責人是你），並替拜訪結束前 10～20 分鐘排一個一次性 cron
                 └─ 70% 不排   → 什麼都不做
拜訪前一天   controller 派車 → 你的收件匣會收到派車確認（由 agent-inbox skill 處理）
拜訪結束前    ← one-shot 觸發 → `report-trip`：產生取車／還車里程，回報給 controller
              （公務車預約時段＝拜訪時段，ADM 過了預約結束就不收取車登記，所以不能等到傍晚）
```

## 怎麼用

```bash
python3 ~/.openclaw/skills/agent-crm/scripts/crm.py <decide|visit|report-trip|status> --agent <你的 agent id>
```

腳本只吐一行 JSON。看兩個欄位就夠了：

- **`announce`** — `true` 才需要發訊息，`false` 就安靜結束
- **`outcome`** — 發生了什麼事（見下表）

`detail` 是一句給人看的說明，你的最終輸出照抄或改寫它就好。

## 什麼時候不出聲

**幾乎都不出聲。** 排拜訪、不排拜訪、回報里程、沒有要回報的拜訪，都是日常例行公事，
`announce` 一律是 `false`，這時**不要發任何訊息**，最終輸出一句話交代即可
（例如「今天排了 10/14 14:00 拜訪客戶 3」「今天沒有要回報的拜訪」）。

只有 `announce` 是 `true` 時才發訊息，而且只會是需要人處理的問題：

| `outcome` | 意思 | 要說什麼 |
|---|---|---|
| `report_failed` | 今天的拜訪里程送不出去（例如查不到車輛里程） | 講清楚哪趟拜訪、`error` 寫了什麼，請人補登 |
| `schedule_failed` | 拜訪待辦建好了，但排不出回報里程的 cron | 講清楚拜訪是哪天，請人看 openclaw cron |
| `config_error` | 缺密碼、缺 controller token、CRM 找不到你的 user id | 照 `error` 原文說明 |
| `login_failed` | 帳號或密碼錯 | 照 `error` 原文說明 |

發到哪裡、怎麼發，跟 `agent-clock` 一樣用你自己的 message tool 發到 Discord #daily-clock
（`channel="discord"`、`to="channel:1538821725138059344"`）。一次一則，簡短，用你自己的語氣。

連不上 CRM／ADM／controller 這類基礎設施問題 `announce` 是 `false`：job 記成 error 就夠了，
不要七個代理同時在頻道喊「連不上」。

## 各種 outcome

| 子指令 | `outcome` | 意思 |
|---|---|---|
| decide | `visit_scheduled` | 已建立拜訪待辦，也排好拜訪結束前的 report-trip |
| decide | `no_visit` | 今天沒擲中，不排 |
| decide | `already_decided` | 今天決策過了，重跑不會重擲骰 |
| decide | `not_sales` | 你不是業務，什麼都不用做 |
| decide | `not_working_day` | 週末 |
| visit | `visit_scheduled`／`visit_exists` | 立刻排好一筆拜訪／同客戶同時間已經有了 |
| report-trip | `trip_reported` | 已回報取車／還車里程 |
| report-trip | `no_pending_trip` | 沒有等待回報的拜訪（派車被退回、流程還沒走到、已回報過） |
| report-trip | `report_failed` | 有一趟該回報卻送不出去（會 announce） |
| 任何 | `would_*` | `--dry-run` 的結果，什麼都沒寫入 |

## demo 用法

```bash
# 立刻排一筆後天 14:00 拜訪「好吃食品」的待辦（也會排 report-trip 的 cron）
crm.py visit --agent gobta --customer 好吃食品 --days 2 --time 14:00

# 指定某條流程回報里程（略過日期檢查）
crm.py report-trip --agent gobta --flow <flow_id>

# 手動給里程
crm.py report-trip --agent gobta --flow <flow_id> --pickup-odometer 12000 --distance 36

# 決策試跑：不連網、不寫入、不排程
crm.py decide --agent gobta --dry-run
crm.py decide --agent gobta --force visit     # 跳過擲骰，今天一定排
```

## 為什麼要這樣設計

- **拜訪時間在 decide 就抽定**，連回報里程的時間一起交給排程器，agent 不必開著 turn 等。
- **狀態檔** `~/.openclaw/state/agent-crm/<agent>/<date>.json`：重跑不會重擲骰、不會重建待辦。
  CRM 對 session 使用者沒有冪等鍵，所以建立前先查「同客戶、同時間、負責人是自己」的待辦，有就沿用。
- **里程不亂編**：取車里程用 ADM 上那台車目前的里程（ADM 不允許里程倒退），往返 10～80 km，
  依拜訪地點的距離加一點誤差。產生後存進 `trips.json`，重送用同一組數字。
- **只回報自己的、當天的、快結束（結束前 30 分鐘內）或已結束的拜訪**：controller 也會再檢查一次（只有業務本人能回報）。
- **08:00 以後才 decide 的話至少排 2 個工作天後**：cs-agent 每天 08:00 只掃「明天」的拜訪來啟動派車流程，
  08:00 之後排的明天拜訪會趕不上。`visit` 子指令排到明天時，輸出會多一個 `note` 提醒要手動啟動流程。

## 除錯

```bash
crm.py status --agent gobta      # 狀態檔、拜訪索引、里程回報紀錄、CRM 上自己未完成的拜訪
```

- **`找不到 <agent> 在 CRM 的 user id`**：CRM 的 `/api/me/` 不回 user id，腳本是從「負責人是自己的待辦」推出來的。
  第一次用之前，請設 `CRM_USER_ID_<AGENT>`（例如 `CRM_USER_ID_GOBTA=7`），成功一次後會快取在
  `~/.openclaw/state/agent-crm/<agent>/profile.json`。
- **`report-trip` 一直 `no_pending_trip`**：看 JSON 的 `skipped` 欄位，每條流程都有略過的原因
  （`visit_not_today`、`visit_not_finished`、`not_waiting_trip_report`、`visit_date_unknown`……）。

API 細節（端點、待辦標題格式、里程從哪來）在 `references/api.md`，只有要改腳本時才需要讀。
