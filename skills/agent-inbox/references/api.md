# agent-inbox 的 API 細節

只有要改 `scripts/inbox.py` 時才需要讀。controller 的完整 API 在本 repo 的 `controller/backend/README.md`。

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `FLOW_CONTROLLER_URL` | `http://127.0.0.1:18090` | controller 後端 |
| `FLOW_CONTROLLER_TOKEN_<AGENT>` | —（必填） | 員工 agent 自己的 controller token（agent id 轉大寫、`-` 換 `_`） |
| `AGENT_INBOX_PAUSED` | （空） | 逗號分隔的 agent id，或 `all`：這些 agent 暫停代為確認 |
| `OPENCLAW_STATE_DIR` | `~/.openclaw/state` | 狀態檔根目錄 |
| `FREEFORJOY_ENV_FILE` | `~/.openclaw/secrets/freeforjoy.env` | 環境變數沒設時從這個檔補讀（不覆蓋既有變數） |

controller 端要先準備好：

```bash
uv run flow-controller upsert-agent gobta --role employee
uv run flow-controller issue-agent-token gobta            # 存成 FLOW_CONTROLLER_TOKEN_GOBTA
uv run flow-controller import-identities identities.json  # 每位員工的 agent_id 要填，收件匣才讀得到
```

## 流程

0. `GET /api/inbox/confirm-mode`（Bearer 員工 token）：這位確認者目前由誰確認。回 `{"confirm_mode": "human"}`
   （admin 在前端切成真人確認，課堂 demo 由講師確認）就 `outcome=paused`、什麼都不做；`agent` 照常處理；
   舊版 controller 沒有這個端點（404 `not_found`）時照舊處理。`human` 時 controller 本來就不給 agent 確認連結
   （`link=null`、`link_hidden=true`），所以這一步查不到也不會誤代確認。
1. `GET /api/inbox?limit=100`（Bearer 員工 token）：controller 依 token 找 `Identity.agent_id == <agent>` 的收件匣。
   員工 token 看得到自己扮演那個人的確認連結（`link`）。
2. 只看 `kind == "approval_request"`；同一個 `approval_id` 有多則（重送）時只用最新那則。
3. `GET /api/approvals/{id}?token=<連結裡的 token>`：確認頁內容
   `{flow_title, step_title, question, options:[{value,label}], context, expires_at}`。
   - 410（`token_used`／`token_expired`／`approval_closed`）：已結束，記下來不再處理
   - 403（`invalid_token`）：舊連結（重送過），記下這則通知 id，等新通知
4. 分類（依序判斷）：
   - `step_title`／`question` 含「決定退款／退款決定」，或選項有 `escalate`：退款決定（流程 ② `decide_refund`）
   - 含「確認回覆／回覆客戶」：回覆確認（流程 ② `review_reply`）
   - 含「到職／新人」是到職清單（一律選 `ack` 確認收到）
   - 含「報帳／交通費／里程費／費用」是報帳，含「派車／公務車／用車／車輛」是派車，其他是其他。
   - 退款決定三選一：客服主管 80% `approve`（不帶 `data.amount_twd`，controller 用推估金額）、10% `reject`、10% `escalate`；
     其他角色走「其他」規則。回覆確認：客服主管 90% `approve`、10% `revise`。
   - `kind == "task"` 的通知（IT 任務）不處理，只列在輸出的 `tasks`。
5. 金額（只有報帳需要）：`context` 裡的 `amount`／`amount_twd`／`total`／`金額`……，
   找不到就從問題文字抓 `NT$1,250`、`820 元`。
6. 擲骰、挑選項、寫理由，**先寫進狀態檔**，再
   `POST /api/approvals/{id}/decision` `{"token","decision","comment"}`。
   - 200：完成（controller 會自動把相關通知標成已讀）
   - 410：別人先處理了
   - 連線失敗、5xx：保留決定，下次重跑沿用

一次性 token 用過就失效，所以送出決定的 POST 只在「連線根本沒建立」時重試。

## 選項對應

| 要做的事 | 選項 value | 找不到時用 label |
|---|---|---|
| 核准 | `approve`、`approved`、`confirm`、`accept`、`yes`、`ok`、`ack` | 含「核准／確認／同意／收到／可以」且不含「不／退／拒」 |
| 退回 | `reject`、`rejected`、`deny`、`decline`、`no`、`return`、`revise` | 含「退回／拒絕／不核准／不同意／駁回」 |
| 轉人工（只有退款決定） | `escalate`、`handoff`、`human` | 含「轉人工／專人」 |

要退回卻沒有退回類選項（例如只有「確認收到」）就改成核准；兩種都找不到就用第一個選項。

## 狀態檔

`~/.openclaw/state/agent-inbox/<agent>/handled.json`：

```json
{
  "approvals": {
    "<approval_id>": {
      "status": "decided", "flow_id": "…", "flow_title": "…", "step_title": "…",
      "category": "dispatch", "amount": null, "roll": 0.42, "rule": "sales_dispatch_90",
      "decision": "approve", "approve": true, "comment": "…", "context": {"時段": "…"},
      "first_seen": "2026-10-12", "decided_at": "…"
    }
  },
  "skipped_notifications": {"<notification id>": "2026-10-12"}
}
```

`status`：`pending`（決定了但還沒送成功）、`decided`、`closed`。超過 30 天的紀錄會自動清掉。
`context` 給 `agent-crm report-trip` 判斷拜訪日期用。
