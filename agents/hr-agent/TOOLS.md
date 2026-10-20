# Tools

此檔只記錄連線方式與憑證引用，不保存秘密。

## 人事規章（知識庫）

- **來源：** `docs/hr-policy.md`（示範用假規章；換成真實員工手冊前先依 `docs/deidentify-mapping.md` 去識別化）
- **用途：** 規章問答的唯一依據
- **讀取方式：** 用 `read` 工具讀 workspace 內的檔案

## 人事系統 API（HRM）

- **Base URL:** `${HRM_API_BASE}`（gateway 主機：`http://localhost:18000/api`）
- **Credential reference:** `${HRM_API_TOKEN}`（service token，`Authorization: Bearer`；不需要 session、CSRF、Idempotency-Key）
- **Scopes：** `employee:read`、`department:read`（只能讀；任何寫入都回 403）。資料範圍固定為全公司。
- **契約文件：** HRM repo 的 `docs/agent-service-api.md`

| 端點 | scope | 用途 |
|---|---|---|
| `GET /api/employees/?start_date_after=&start_date_before=&status=active` | `employee:read` | 流程 ③ 步驟 1：查下週到職的新人（含邊界，以主要 Employment 的到職日為準；有分頁） |
| `GET /api/employees/{id}/` | `employee:read` | 單一員工 |
| `GET /api/departments/`、`GET /api/departments/{id}/` | `department:read` | 部門清單與名稱 |
| `GET /api/departments/{id}/managers/` | `department:read` | 流程 ③ 步驟 5：部門主管（不分頁；沒有主管回 `[]`） |

- token 回傳的員工欄位只有：`id`、`employee_number`、`full_name`、`department`、`department_name`、`start_date`、
  `employment_type`；不含電話、email、地址。主管端點會回 `email`，**不要放進到職清單**。
- 401：token 錯誤、停用或過期；403：寫入或沒有 scope；404：其他公司的資料或不存在。
- 每次呼叫都會寫進 HRM 的稽核紀錄（讀取員工資料屬於個資存取）。
- 使用方式：`skills/onboarding-list`、`skills/onboarding-notice`（API 細節都在腳本裡，不要自己組請求）。
- 尚未提供：出勤、假別餘額、申請狀態、教育訓練紀錄的查詢，以及任何申請草稿的建立——被問到時回覆「尚未連線」。

## Workflow Controller

- **Base URL:** `${FLOW_CONTROLLER_URL}`
- **Credential reference:** `${FLOW_CONTROLLER_TOKEN_HR_AGENT}`
- 只用 `flow-start`、`flow-report` 兩個 skill 呼叫（`~/.openclaw/skills/`），不得直接修改流程狀態。

## 使用限制

- 只呼叫已核准的 API；禁止直連資料庫或使用共享管理員帳號。
- 認證資訊只能由環境變數或 secret manager 注入，不得寫入 workspace。
- API 未提供的能力視為不可用，不得用 shell、瀏覽器或其他 Agent 繞過。
- 工具未設定時明確回報「尚未連線」，不可捏造結果。

## 尚待設定

- 真實規章文件與版本（目前為示範假規章 v0.1）
- HRM 出勤、假別、申請、教育訓練的查詢端點（目前 token 只開員工與部門的唯讀）
- 核准角色與 escalation contact
