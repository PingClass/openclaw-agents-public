---
name: agent-adm
description: 員工 agent 在 FreeForJoy ADM 行政系統扮演員工本人：每天決定要不要預約會議室（行政財會另外會預約公務車），時段衝突就換時段；IT 人員（souei）另外處理新人到職流程的「配發筆電」任務（assign-laptop）。cron job 叫你做「行政預約決策」或「處理 IT 任務／配發筆電」時一定要用這個 skill。也適用於使用者說「幫我訂會議室」「訂一間明天下午的會議室」「我訂了哪些會議室／公務車」「把筆電指派給新人」，或提到 ADM、會議室預約、公務車預約、筆電配發的任何情況。
---

# 會議室與公務車預約（ADM）

你（agent）在 FreeForJoy 是一位員工。跟真人一樣，偶爾要訂會議室開會；
行政財會（`luminous`）偶爾還要借公務車外出辦事（自己用，不是替別人訂）。
你用自己的帳號登入 ADM 預約，讓公司的會議室和車輛有正常的使用量，也會跟別人搶時段。

所有跟 ADM 的互動都交給 `scripts/adm.py`——登入、CSRF、Idempotency-Key、時段衝突換時段、
重試都寫在裡面了。**不要自己組 API 請求。**

## 一天怎麼跑

```
工作日早上  decide ──┬─ 10% 預約會議室（今天或之後 2 個工作天，10:00～17:30 之間的時段）
                     ├─ luminous 另外 5% 預約公務車（行政外出：銀行、國稅局、郵局……）
                     └─ 其他 → 什麼都不做
```

時段被別人搶走（ADM 回 409）或查不到空房／空車，就換一個時段，**最多再試 2 次**；
還是不行就放棄，今天不訂。預約是立即生效的，不用另外排 cron。

## 怎麼用

```bash
python3 ~/.openclaw/skills/agent-adm/scripts/adm.py <decide|book-room|assign-laptop|status> --agent <你的 agent id>
```

腳本只吐一行 JSON。看 `announce` 和 `outcome` 就夠了，`detail` 是一句給人看的說明。

## 什麼時候不出聲

**預約會議室、借公務車、沒擲中、時段都滿了，全部都不出聲。** 這是日常行政，沒有人想在頻道
看到「我訂了 3 樓會議室」。`announce` 是 `false` 的時候**不要發任何訊息**，最終輸出一句話交代
就好（例如「今天不用訂會議室」「訂好 10/13 14:00 大會議室」）。

只有 `announce` 是 `true` 才發訊息，而且只會是需要人處理的問題：

| `outcome` | 意思 |
|---|---|
| `config_error` | 沒設定 `ADM_AGENT_PASSWORD`，或 agent id 不對 |
| `login_failed` | 帳號或密碼錯（ADM 帳號還沒建？） |
| `error`（announce=true） | ADM 拒絕預約（400／403），照 `error` 原文說明 |

發訊息的方式和 `agent-clock` 一樣（Discord #daily-clock，`channel="discord"`、
`to="channel:1538821725138059344"`），一次一則，用你自己的語氣。
連不上 ADM、ADM 回 5xx 是基礎設施問題，`announce` 是 `false`，不要發。

## 各種 outcome

| 子指令 | `outcome` | 意思 |
|---|---|---|
| decide | `booked` | 已預約（看 `bookings` 欄位） |
| decide | `no_booking` | 今天沒擲中 |
| decide | `already_decided` | 今天已經預約過了，重跑不會再訂 |
| decide | `unavailable` | 試了 3 個時段都沒有空位，今天不訂 |
| decide | `not_working_day` | 週末 |
| book-room | `room_booked`／`already_booked`／`room_unavailable` | 同上，針對單次預約 |
| assign-laptop | `assigned`／`nothing_to_do`／`needs_human`／`not_it` | 見下一節 |
| 任何 | `would_*` | `--dry-run` 的結果，什麼都沒寫入 |

## IT 人員的配發筆電任務（assign-laptop，souei 用）

新人到職流程（③）會在 IT 人員的 controller 收件匣放一則**任務通知**（`kind=task`，標題「IT 任務：配發筆電給新人 …」），
裡面有 it-agent 產生的配發建議（建議的資產編號＋備選）。你是 IT 人員，**指派筆電要用你自己的 ADM 帳號**
（session 登入、staff），不是任何 agent 的 token——ADM 的指派 API 對 token 一律拒絕。

```bash
python3 ~/.openclaw/skills/agent-adm/scripts/adm.py assign-laptop --agent souei
```

腳本做的事（契約：`controller/backend/docs/flows/onboarding.md` 的 await_laptop）：

1. 讀自己收件匣裡未讀的 IT 任務（或用 `--flow <flow_id>` 指定一條流程）。
2. 查流程：目前步驟要是 `await_laptop`，取建議的筆電（`state.steps.draft_laptop.asset_code`＋`alternatives`）與新人 HRM ID。
3. 用你的 ADM session 指派：新人名下已經有筆電（包含先前在網頁上指派過）就**視為成功、不再指派第二台**；
   建議的那台已被別人指派、停用或不存在就改用備選。指派時一併帶新人姓名與員工編號（流程的 `state.input.name`／
   `employee_number`）：新人還沒有 ADM 帳號時，ADM 會自動建立**停用、不可登入**的保管人帳號（`hrm-<員工編號>`）再完成指派，
   之後新人帳號啟用時沿用同一個帳號。
4. 向 controller 送 `laptop_assigned` 事件（實際指派的資產編號；換成備選、或保管人是自動建立的佔位帳號時附說明），把任務標成已讀。

| `outcome` | 意思 | 要不要出聲 |
|---|---|---|
| `assigned` | 已指派並回報（看 `assigned`；`placeholder_custodian` 表示新人的保管人帳號是 ADM 自動建立、尚未啟用的佔位帳號） | 不出聲，最終輸出一句話交代 |
| `nothing_to_do` | 沒有待處理的 IT 任務，或都處理過了 | 不出聲 |
| `needs_human` | 建議與備選都不能用、流程沒有新人姓名、新人的 HRM ID 連到停用帳號……（看 `needs_human[].reason`） | 第一次 `announce=true`，之後同一條流程不再重複；任務保留未讀，人處理好之後下一輪自動完成 |
| `error` | controller 拒絕事件（例如 403 `not_reporter`：你不是這條流程的 IT 人員） | `announce=true` |
| `forbidden` | ADM 不允許你指派（帳號不是 staff） | `announce=true` |
| `not_it` | 你不是 IT 人員（`--flow` 也一樣），不做任何事 | 不出聲 |

- 流程已結束（取消、失敗、逾期）時一律不指派，只把任務標成已讀（`skipped[].reason = flow_<狀態>`）。
- 「新人名下已有筆電」只算啟用中的筆電；停用的舊筆電不算。

重跑是安全的：指派給同一個人 ADM 直接回 200；事件回應遺失時，重跑會查流程確認 controller 已收下，不會重複回報。

## demo 用法

```bash
# 明天 14:00 訂一間 6 人會議室開 60 分鐘（衝突會自動換同一天的其他時段）
adm.py book-room --agent hinata --on 2026-10-13 --start 14:00 --duration 60 --capacity 6 --purpose 客訴案例檢討

# 跳過擲骰
adm.py decide --agent luminous --force both      # room／vehicle／both／none

# 試跑：不連網、不寫入
adm.py decide --agent souei --dry-run
```

同樣的 `book-room` 參數同一天重跑，只會拿回同一筆預約；要再訂一間就換參數。

## 為什麼要這樣設計

- **決策寫進狀態檔**（`~/.openclaw/state/agent-adm/<agent>/<date>.json`）：重跑不會重擲骰。
- **每次嘗試先記下再送出**：時段、會議室、Idempotency-Key 都先寫進狀態檔。網路斷掉重跑時，
  用同一個鍵、同一份內容重送，ADM 會回原本那筆，不會訂到兩間。
- **換時段最多 2 次**：跟真人一樣，搶不到就算了，不要整天卡著重試。

## 除錯

```bash
adm.py status --agent luminous   # 狀態檔＋自己還沒結束的會議室／公務車預約
adm.py assign-laptop --agent souei --dry-run       # 不連網：列出 assign-laptop 的狀態檔
adm.py assign-laptop --agent souei --flow fl_xxx   # 只處理指定流程（收件匣沒有任務通知時也可以）
```

API 細節在 `references/api.md`，只有要改腳本時才需要讀。
