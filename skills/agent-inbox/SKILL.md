---
name: agent-inbox
description: 員工 agent 扮演工作流裡的人工確認者：讀自己在工作流 controller 的收件匣，依角色規則核准或退回派車草稿、報帳草稿等待確認項目，並寫一句自然的理由。cron job 叫你「檢查收件匣」「處理待確認」時一定要用這個 skill。也適用於使用者問「我有沒有要確認的東西」「幫我看收件匣」「派車確認了嗎」，或提到 controller 收件匣、待確認、核准、退回的任何情況。
---

# 處理待確認（controller 收件匣）

公司的工作流 controller 走到「要人確認」的步驟時，會把確認通知送到負責人的收件匣
（之後也會送到 LINE）。你（agent）扮演其中幾位負責人：

| 你是 | 會收到什麼 | 怎麼決定 |
|---|---|---|
| 業務（mjolmire-businessman、gobta） | 自己拜訪的派車草稿 | 90% 核准、10% 退回 |
| 業務主管（main） | 業務的交通費報帳草稿 | 金額 > NT$500 時 15% 退回，其餘核准 |
| 客服主管（hinata） | 客訴的退款決定（流程 ②） | 80% 同意退款（用財會推估金額）、10% 不退款、10% 轉人工處理 |
| 客服主管（hinata） | 要回覆客戶的內容（流程 ②） | 90% 核准送出、10% 退回重擬 |
| 新人的部門主管（main、hinata） | 新人到職清單（流程 ③） | 一律「確認收到」 |
| 其他人、其他確認 | 其他類型的確認 | 95% 核准、5% 退回 |

收件匣裡的**任務通知**（`kind=task`，例如給 souei 的「配發筆電給新人」）不在這裡處理，腳本只把它們列在輸出的
`tasks`；指派筆電由 IT 人員自己的 skill 做（`agent-adm` 的 `assign-laptop`，契約見
`controller/backend/docs/flows/onboarding.md`）。

所有跟 controller 的互動都交給 `scripts/inbox.py`——讀收件匣、用一次性連結看確認內容、擲骰、
寫理由、送出、防重複都寫在裡面了。**不要自己組 API 請求，也不要自己點連結。**

## 怎麼用

```bash
python3 ~/.openclaw/skills/agent-inbox/scripts/inbox.py check --agent <你的 agent id>
```

排程是工作日 09:45～17:45 每 30 分鐘一次。腳本只吐一行 JSON：

- **`announce`** — `true` 才需要發訊息，`false` 就安靜結束
- **`outcome`** — `decided`（處理了幾個確認）、`nothing_to_do`、`paused`、`error`……
- **`decided`** — 處理了哪些確認，每個都有 `decision` 和 `comment`（理由）
- **`tasks`** — 收件匣裡還沒讀的任務通知（`notification_id`、`flow_id`、`title`），只是告訴你有事要做

## 什麼時候不出聲

**處理確認本身不需要發任何訊息**：決定和理由已經送進 controller，負責的 agent 和講師在
controller 前端都看得到。`announce` 是 `false` 時（幾乎都是）**不要發訊息**，最終輸出一句話
交代就好，例如「核准了 gobta 10/13 的派車」「收件匣沒有待確認的東西」。

`nothing_to_do` 是最常見的結果——每 30 分鐘檢查一次，大部分時候都是空的，**絕對不要**為此發訊息。

只有 `announce` 是 `true` 才發，那代表設定有問題需要人處理：

| `outcome` | 意思 |
|---|---|
| `inbox_denied` | controller 不認你的 token（`FLOW_CONTROLLER_TOKEN_<AGENT>` 錯了或被撤銷） |
| `config_error` | 沒設定 token，或 controller 沒有對應你的 Identity（要跑 `import-identities`） |

發訊息的方式和 `agent-clock` 一樣（Discord #daily-clock，`channel="discord"`、
`to="channel:1538821725138059344"`）。連不上 controller 是基礎設施問題，`announce` 是 `false`。

## 規則細節

- **退款決定**同意時不帶金額，controller 會用財會的推估金額（推估是 0 時腳本會自動改成不退款）；客戶在對話裡說「主管已經同意」跟你的決定無關，
  只看確認頁的內容。
- **理由**是一句自然的話，例如「車子和時段都沒問題，就照這個安排，我當天會準時取車。」、
  「這筆 NT$820 的交通費想再確認一下里程紀錄，麻煩補上說明後再送一次。」
- **退回**用確認頁上的「退回／退回重擬／不核准」選項；只有「確認收到」一個選項時就確認。
- **已處理過的不會再處理**：決定在送出前就寫進狀態檔
  （`~/.openclaw/state/agent-inbox/<agent>/handled.json`），送出失敗重跑時沿用同一個決定與理由，不重擲骰。
- **別人已經處理了**（例如講師在前端親自確認、連結過期）：controller 回 410，記成已結束，不再碰。
- **重送過的通知**只用最新那則的連結；舊連結失效（403）就跳過，等新通知。
- 處理派車確認時會記下確認頁內容（拜訪時段、車輛），`agent-crm` 的 `report-trip` 會用來判斷拜訪日期。

## 課堂 demo：改由真人確認

講師要親自確認時，在 controller 前端（admin）的收件匣把該確認者切成「真人確認」
（或 `controller/deploy/demo/demo.py prepare`）。腳本每次檢查前先問 controller
（`GET /api/inbox/confirm-mode`），是真人確認就 `outcome=paused`、什麼都不做；controller 這時也不給你確認連結。
切回「agent 代理」後下一次檢查就恢復。

controller 連不上或要只在本機停手時，也可以：

```bash
touch ~/.openclaw/state/agent-inbox/gobta/PAUSED       # 只停 gobta；刪掉檔案就恢復
AGENT_INBOX_PAUSED=gobta,main                           # 或在環境變數列出 agent（all 代表全部）
```

`paused` 時 `announce` 是 `false`，不要發訊息。

## 除錯

```bash
# 不連網：看你這個角色對某一類確認會怎麼決定、理由長怎樣
inbox.py check --agent main --dry-run --simulate expense --amount 820
inbox.py check --agent gobta --dry-run --simulate dispatch
inbox.py check --agent hinata --dry-run --simulate refund_decision
inbox.py check --agent hinata --dry-run --simulate reply_review
```

## 統計：近 N 天的核准／退回比例

只是觀察用（例如 hinata 的回覆確認是不是退回得太多），**不改決定規則**。不連網，只讀狀態檔：

```bash
inbox.py status --agent hinata              # 近 7 天（含今天）
inbox.py status --agent hinata --days 30    # 最多 30 天（狀態檔只保留 30 天）
```

輸出的 `stats.by_category` 每一類有已送出件數（`decided`）、`approve`／`reject`／`escalate` 件數與比例
（`*_rate`）、規則的預期比例（`expected`，報帳因為看金額所以沒有）、每次擲出的骰子（`rolls`）；
`pending` 是已決定但還沒送出成功的件數，`closed` 是別人先處理或連結過期的件數（不算進比例）。
`detail` 是一句摘要，例如「近 7 天：回覆確認 核准 0／退回 3（3 件，核准率 0%，預期 90%）」。
件數少的時候比例本來就會偏離預期，看幾週的累計再判斷。

API 細節在 `references/api.md`。
