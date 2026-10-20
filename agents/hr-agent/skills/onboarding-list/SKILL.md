---
name: onboarding-list
description: 流程 ③（新人到職）步驟 1：查 HRM 下週一到週日到職的新人，每位用 flow-start 啟動 onboarding 工作流。cron job 叫你做「新人到職掃描」「查下週到職的新人」「啟動到職流程」時一定要用這個 skill。只用於排程或內部作業。
---

# 新人到職掃描（流程 ③ 步驟 1）

每週一早上，cron 叫你查 HRM **下週一到下週日**到職的員工（以主要 Employment 的到職日為準、在職狀態），
每位交給工作流 controller 啟動一條 `onboarding` 流程（之後的筆電、會議室、到職清單、主管確認都由 controller 安排）。

HRM 查詢、組流程輸入、呼叫 `flow-start` 全都寫在 `scripts/onboarding_list.py` 裡。**不要自己組 API 請求，
也不要自己呼叫 flow-start。**

## 怎麼用

```bash
python3 ~/.openclaw/workspace-hr-agent/skills/onboarding-list/scripts/onboarding_list.py --agent hr-agent
```

腳本只吐一行 JSON。看兩個欄位就夠了：

- **`announce`** — `true` 才需要說明要人處理的事；`false` 就安靜結束
- **`outcome`** — 發生了什麼事（見下表）

`detail` 是一句給人看的說明，你的最終輸出照抄或改寫它就好。

## 什麼時候不出聲

| `outcome` | 意思 | `announce` |
|---|---|---|
| `no_new_hires` | 下週沒有新人到職 | `false` |
| `flows_started` | 有新人，已啟動流程（`started` 列出每位的 flow_id） | `false` |
| `already_started` | 有新人，但流程之前就啟動過了（重跑，controller 去重回 `created=false`） | `false` |
| `needs_attention` | 有新人被跳過（`skipped`：HRM 沒設部門等）或啟動失敗（`failed`） | **`true`** |
| `config_error` | 缺 `HRM_API_TOKEN`、找不到 flow-start skill 等設定問題 | **`true`** |
| `hrm_401`／`hrm_403` | HRM token 無效、過期，或 scope 不對 | **`true`** |
| `would_scan` | `--dry-run`：只顯示要查的期間，沒有連網 | `false` |

`announce=false` 時**不要發任何訊息**，最終輸出一句話交代即可（例如「10/19～10/25 有 1 位新人到職，已啟動 1 條流程」）。

`announce=true` 時，最終輸出列出每一位被跳過或失敗的新人（`employee_number`、`name`、`reason` 或 `error`），
請人資到 HRM 補齊資料後再手動重跑一次（重跑不會重複啟動已啟動的流程）。
排程是 `--no-deliver`，這段輸出會留在 cron 的執行紀錄。

連不上 HRM 這類基礎設施問題 `announce` 是 `false`，exit 1 讓 cron 記成 error 就好。

## 規則

- **期間：** 下週一到下週日（含邊界）。週一跑查 7 天後的那一週；週日跑查隔天開始的那一週。國定假日不在這裡判斷。
- **只帶流程需要的欄位：** `employee_id`、`name`、`employee_number`、`department_id`、`department_name`、`start_date`
  （HRM token 本來就不回電話、地址、email；職稱 HRM 沒有回，所以不帶 `job_title`）。
- **沒有部門的新人：** 跳過並回報（controller 要靠部門找主管），不猜部門。
- **啟動：** 每位呼叫共用 skill `flow-start --type onboarding --input-file - --dedupe-key hrm:employee:<id>`，
  input 欄位照 `controller/backend/docs/flows/onboarding.md`「啟動」。去重交給 controller
  （去重鍵固定 `hrm:employee:<id>`），所以重跑、補跑都安全。
- **部門沒有主管**（目前資訊部、管理部、人資部）不在這裡判斷：照常啟動，controller 會在 `resolve_parties` 停下並通知人資。

## demo 與除錯

```bash
onboarding_list.py --agent hr-agent --dry-run                       # 不連網：顯示會查哪個期間
onboarding_list.py --agent hr-agent --week-of 2026-10-21            # 改查指定日期所在那一週（補跑、demo）
onboarding_list.py --agent hr-agent --now 2026-10-12T09:00:00+08:00 --dry-run   # 假裝是週一早上
```

環境變數（放在 `~/.openclaw/secrets/freeforjoy.env`）：`HRM_API_TOKEN`、`HRM_API_BASE`（選填，預設
`http://localhost:18000/api`）、`FLOW_CONTROLLER_URL`、`FLOW_CONTROLLER_TOKEN_HR_AGENT`（由 flow-start 使用）、
`FLOW_SKILLS_DIR`（選填，共用 skill 的位置，預設 `~/.openclaw/skills`）。
