# Tools

此檔只記錄連線方式與憑證引用，不保存秘密。

## 財務辦法（知識庫）

- **來源：** `${FINANCE_DOCS_PATH}`（報帳辦法、差旅費辦法、核決權限表、會計科目表）

## 會計系統

目前**沒有會計系統**；報帳草稿交給 controller 保存（flow state），業務主管核准後由 controller 通知財會人員手動入帳。
因此報帳與請款進度、部門預算、應收帳款帳齡目前都**無法查詢**，被問到時回覆「尚未連線」，不可捏造數字。

## 交通費費率

- **費率：** `${MILEAGE_RATE_TWD_PER_KM}`（NT$/km，由財會提供，不寫死在任何檔案）
- **是否為 demo 假費率：** `${MILEAGE_RATE_IS_PLACEHOLDER}`（預設 `true`）
- 目前 demo 使用**假費率 5**，草稿與回報一律標明「demo 假費率」；財會提供正式費率後更新 secrets 檔並把 `MILEAGE_RATE_IS_PLACEHOLDER` 設為 `false`。
- 費率未設定時，交通費草稿回報 `needs_human`，不得猜。
- 使用方式：`skills/mileage-claim-draft`（流程 ① 步驟 6）。

## CRM API（唯讀，案件內部備註除外）

- **Base URL:** `${CRM_API_BASE}`（gateway 主機：`http://localhost:18081/api`，和 `skills/agent-crm` 同一個變數；端點在 `/v1/` 底下）
- **Credential reference:** `${CRM_API_TOKEN_FINANCE}`（service token，`Authorization: Bearer`；credential 名稱 `OpenClaw finance-agent`）
- **Scopes：** `customer:read`、`interaction:read`、`case:read`、`case_note:read`、`case_note:write`（CRM C4 之後由
  `seed_demo --issue-credentials --rotate` 重發的 token 才有後兩個）
- **用途（流程 ②）：**
  - `refund-assessment`（`assess_refund`）：讀客戶完整資料（`GET /v1/customers/{id}/context/`）、案件（`GET /v1/cases/{id}/`）、
    過去案件（`GET /v1/cases/?customer=<id>`），把評估寫進案件內部備註（`POST /v1/cases/{id}/notes/`，kind=assessment）。
  - `refund-decision-note`（`record_decision`）：把客服主管的決定寫進案件內部備註（kind=decision）。
- **案件內部備註（C4）：** 只能新增、不能修改或刪除；不會出現在客戶 context、互動紀錄或案件內容。
  寫入一律帶 controller 給的 `Idempotency-Key`（缺 key 回 400、同鍵換內容回 409）。讀取 `GET /v1/cases/{id}/notes/?kind=`。
- 除內部備註外不得寫入 CRM：不新增或修改客戶、互動、案件、待辦跟進，也不結案（`case:transition` 不發給任何 agent）。
- CRM 沒有訂單資料；退款評估的訂單金額只能從案件內容、互動紀錄或價目表推估。

## 價目表與退款政策（流程 ②）

- **位置：** `${REFUND_POLICY_FILE}`，沒設定時用 `skills/refund-assessment/references/refund-policy.json`
- 目前是 **demo 假價格、demo 退款政策**（`is_placeholder: true`）：退款類全額、補償類 50%，評估依據一律標明；
  財會提供正式價目表與退款辦法後另存一份 JSON、設定 `REFUND_POLICY_FILE`，並把 `is_placeholder` 設成 `false`。

## Workflow Controller

- **Base URL:** `${FLOW_CONTROLLER_URL}`（gateway 主機：`http://127.0.0.1:18090`，API 在 `/api` 底下）
- **Credential reference:** `${FLOW_CONTROLLER_TOKEN_FINANCE_AGENT}`
- 只用 `flow-start`、`flow-report` 兩個 skill 呼叫，不得直接修改流程狀態。
- 例外（唯讀）：寫入 CRM 前，`refund-assessment`、`refund-decision-note` 用這把 token `GET /api/flows/{flow_id}` 核對
  步驟確實派給 finance-agent。

## 使用限制

- 本 Agent 只有唯讀權限（CRM 案件內部備註除外），沒有付款、過帳或修改帳戶的能力，也不得透過其他方式取得。
- 認證資訊只能由環境變數或 secret manager 注入（gateway 主機：`~/.openclaw/secrets/freeforjoy.env`），不得寫入 workspace。
- 不得使用 shell、瀏覽器或其他 Agent 繞過上述限制。
- 工具未設定時明確回報「尚未連線」，不可捏造數字。

## 尚待設定

- 財務辦法文件位置與版本
- 正式交通費費率與報帳規則（計畫 D4，由財會提供；提供前用 demo 假費率）
- 會計系統（目前沒有）
- 正式價目表與退款辦法（由財會提供；提供前用 demo 假資料）
- 核決權限表與 finance owner
