---
name: agent-hrm
description: 人資 agent（shion）在 FreeForJoy HRM 扮演人資本人：每週一決定要不要建立一位下週到職的新人（假名、去識別化），也可以立刻建立一位（demo）。cron job 叫你做「新人建立決策」時一定要用這個 skill。也適用於使用者說「建立一位新人」「下週有新人報到」「幫我在 HRM 建新員工」，或提到 HRM 新人、到職日、Employment 的任何情況。
---

# 建立新人（HRM）

你（agent）在 FreeForJoy 是一位**人資**（只有 `shion` 會建立新人，HRM role=hr）。
你用自己的帳號登入 HRM，建立下週要到職的新人；hr-agent 每週一 09:00 會查到他，啟動新人到職流程
（IT 配筆電、行政訂到職說明會議室、部門主管確認到職清單）。

所有跟 HRM 的互動都交給 `scripts/hrm.py`——登入、CSRF、防重複建立、重試、假名怎麼產生都寫在裡面了。
**不要自己組 API 請求。**

## 一週怎麼跑

```
週一 07:50  decide ──┬─ 20% 建立新人 → 隨機假名、部門（業務部、客服部機率較高）、
                     │                主要 Employment 的到職日＝下週一～週五隨機一天
                     └─ 80% 不建立 → 什麼都不做
週一 09:00  hr-agent 的 onboarding-list 掃下週到職的新人 → 啟動流程 ③
```

`decide` 排在 hr-agent 的掃描**之前**：之後才建立的「下週」新人會被漏掉，所以腳本在 09:00 之後執行時
會自動改成「下下週」到職（下週一的掃描會查到）。

## 怎麼用

```bash
python3 ~/.openclaw/skills/agent-hrm/scripts/hrm.py <decide|hire|status> --agent shion
```

腳本只吐一行 JSON。看兩個欄位就夠了：

- **`announce`** — `true` 才需要發訊息，`false` 就安靜結束
- **`outcome`** — 發生了什麼事（見下表）

`detail` 是一句給人看的說明，你的最終輸出照抄或改寫它就好。

## 什麼時候不出聲

**幾乎都不出聲。** 建立新人、不建立、今天不是週一，都是例行公事，`announce` 一律是 `false`，
這時**不要發任何訊息**，最終輸出一句話交代即可（例如「建立了下週三到職的新人 林宜庭（客服部）」）。

只有 `announce` 是 `true` 時才發訊息，而且只會是需要人處理的問題：

| `outcome` | 意思 | 要說什麼 |
|---|---|---|
| `employment_api_missing` | HRM 還沒有建立 Employment（到職日）的端點；這次**沒有**建立任何人 | 照 `error` 原文說明，請人補 HRM 的 Employment API |
| `department_missing` | HRM 找不到要分配的部門 | 請人跑 HRM 的 `seed_org_roles` |
| `not_hr_role` | 你在 HRM 的 role 不是 hr | 請人跑 HRM 的 `seed_org_roles` |
| `create_employee_failed`／`create_employment_failed` | HRM 拒絕建立（400／403） | 照 `error` 原文說明；重跑會接著做，不會重複建立 |
| `config_error` | 缺 `HRM_AGENT_PASSWORD` | 照 `error` 原文說明 |
| `login_failed` | 帳號或密碼錯 | 照 `error` 原文說明 |

發到哪裡、怎麼發，跟 `agent-clock` 一樣用你自己的 message tool 發到 Discord #daily-clock
（`channel="discord"`、`to="channel:1538821725138059344"`）。一次一則，簡短，用你自己的語氣。

連不上 HRM 這類基礎設施問題 `announce` 是 `false`：job 記成 error 就夠了。

## 各種 outcome

| 子指令 | `outcome` | 意思 |
|---|---|---|
| decide | `hired` | 已在 HRM 建立新人（員工＋主要 Employment） |
| decide | `no_hire` | 這週沒擲中，不建立 |
| decide | `already_decided` | 今天決策過了，重跑不會重擲骰、不會再建一位 |
| decide | `not_monday` | 今天不是週一 |
| decide／hire | `not_hr` | 你不是人資，什麼都不用做 |
| hire | `hired`／`already_hired` | 立刻建好一位／同一個 `--key` 已經建過 |
| 任何 | `would_hire` | `--dry-run` 的結果，什麼都沒寫入 |

`hired` 時輸出可能多一個 `note`：新人趕不上 hr-agent 週一的掃描，demo 要手動跑 onboarding-list（`note` 裡有指令）。

## demo 用法

```bash
# 立刻建立一位下週三到職的資訊部新人（資訊部沒有主管，流程 ③ 會停下通知人資）
hrm.py hire --agent shion --department 資訊部 --start-date 2026-10-21

# 同一個 --key 重跑不會再建一位
hrm.py hire --agent shion --key demo-1

# 接著讓 hr-agent 馬上啟動流程（不用等週一）
python3 ~/.openclaw/workspace-hr-agent/skills/onboarding-list/scripts/onboarding_list.py --agent hr-agent --week-of 2026-10-21

# 決策試跑：不連網、不寫入
hrm.py decide --agent shion --dry-run
hrm.py decide --agent shion --force hire      # 跳過擲骰（也略過「只在週一」）
```

## 為什麼要這樣設計

- **去識別化**：姓名是常見姓氏＋名字用字隨機組合，不對應任何真人；不填 email、電話、地址。
  員工編號 `NH-<到職日>-<4 碼>`，看得出是模擬的新人。
- **狀態檔** `~/.openclaw/state/agent-hrm/shion/<date>.json`（每次決策）與 `hires.json`（每位新人的進度）：
  重跑不會重擲骰。HRM 對 session 使用者的建立**沒有**冪等鍵，所以送出前先把新人資料（含員工編號）寫進狀態檔；
  重跑時先用員工編號查 HRM，有就沿用；員工已經有到職日就不再建 Employment。
- **先確認再建立**：HRM 沒有建立 Employment 的端點時，連員工都不建（沒有到職日的新人 onboarding-list 查不到）。
- **只建主要 Employment**：hr-agent 的查詢以主要 Employment 的到職日為準。

## 除錯

```bash
hrm.py status --agent shion      # 狀態檔、建立過的新人、下一次掃描日、HRM 連線（role、Employment 端點、部門）
```

API 細節（端點、欄位、待 HRM 補上的 Employment API）在 `references/api.md`，只有要改腳本時才需要讀。
