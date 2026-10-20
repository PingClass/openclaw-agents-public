# Tools

此檔只記錄連線方式與憑證引用，不保存秘密。憑證值只放在部署機的 `~/.openclaw/secrets/freeforjoy.env`
（權限 600），由環境變數注入；skill 腳本在環境變數沒設時會從這個檔補讀（不覆蓋既有變數）。

## FAQ 知識庫（RAG）

- **來源：** `${CS_KB_PATH}`（產品說明、運送與退換貨政策、付款方式、常見問題）
- **用途：** 回答客戶問題的唯一依據
- **維護人：** 客服主管（待設定）

## CRM API（FreeForJoy CRM）

- **Base URL:** `${CRM_API_BASE}`（預設 `http://localhost:18081/api`；端點都在 `/api/v1/` 底下）
- **Credential reference:** `${CRM_API_TOKEN}`（cs-agent 專用的 service credential，綁定一個 workspace）
- **Header:** `Authorization: Bearer ${CRM_API_TOKEN}`；所有 POST／PUT／PATCH 都要帶 `Idempotency-Key`
  （≤128 字元；重試沿用同一個鍵與同樣內容會重播第一次的結果；同一個鍵用在不同端點或不同內容回 409）
- **scope（目前發給 cs-agent 的）：** `customer:read/write`、`interaction:read/write`、`case:read/write`、
  `followup:read/write`、`followup:complete`、`case_note:read`
  - `case_note:read`：CRM C4 上線後用 `seed_demo --issue-credentials --rotate` 補發（舊 token 立刻失效，要換 secrets 檔的值）；
    還沒補發時讀備註回 403，`crm-reply-draft` 會略過對照、照 controller 的指令擬稿
  - **沒有** `case_note:write`：退款評估、主管決定的備註由 finance-agent 寫入
  - `case:transition`（案件開始處理、解決、結案）**不發給 agent**，由人在 CRM 操作

| 用途 | 端點 | scope |
|---|---|---|
| 找客戶 | `GET /api/v1/customers/?search=`（比對名稱、email、電話、`external_customer_id`） | customer:read |
| 新增客戶 | `POST /api/v1/customers/`（`name`、選填 `external_customer_id`、`email`、`phone`、`tags`、`notes`） | customer:write |
| 客戶完整資料 | `GET /api/v1/customers/{id}/context/`（客戶、最近 20 筆互動、未結案件、未完成待辦） | customer:read |
| 記互動 | `POST /api/v1/interactions/`（`customer`、`summary`、選填 `external_message_id`；只能新增） | interaction:write |
| 查互動 | `GET /api/v1/interactions/?customer=` | interaction:read |
| 開案件 | `POST /api/v1/cases/`（`customer`、`subject`、`description`、`category`、`priority`） | case:write |
| 查案件 | `GET /api/v1/cases/?customer=&status=&category=&priority=`（可組合；category／priority 不合法回 400）、`GET /api/v1/cases/{id}/` | case:read |
| 讀案件內部備註 | `GET /api/v1/cases/{id}/notes/?kind=assessment\|decision\|internal`（舊到新，`{count, results}`） | case_note:read |
| 建待辦跟進 | `POST /api/v1/follow-ups/`（`customer`、`title`、`due_at`、選填 `case`、`kind`＝general／visit） | followup:write |
| 查待辦跟進 | `GET /api/v1/follow-ups/?kind=&status=&due_after=&due_before=&assignee_hrm_employee_id=` | followup:read |
| 完成待辦跟進 | `POST /api/v1/follow-ups/{id}/complete/` | followup:complete |

- 案件 `category`：CRM C3 的選項 `general`／`refund`／`compensation`／`complaint`／`inquiry`，其他值回 400
  （見 `AGENTS.md`「案件格式」）；`refund`、`compensation` 由 `crm-intake` 啟動流程 ②。`priority`：`low`／`normal`／`high`／`urgent`。
- 案件內部備註（CaseNote，C4）：退款評估（`kind=assessment`）、主管決定（`kind=decision`）。只能新增、不能修改；
  **不會**出現在 `/customers/{id}/context/`、互動紀錄或案件內容。cs-agent 只讀，內容不得寫進互動紀錄或客戶回覆。
- 拜訪紀錄的互動摘要格式依 CRM 文件的 C5 建議（第一行 `[拜訪]`），由 `crm-visit-close` skill 產生。
- service credential 不能設定客戶的 `status`／`owner`，也不能設定案件、待辦的 `assignee`。

## 客服案件與轉真人（CRM cases、follow-ups）

原本規劃的「客服工單系統」改用 CRM：

- **轉真人：** 開 CRM 案件（`POST /api/v1/cases/`），附對話摘要；需要人在特定時間處理的，另建待辦跟進。
- **指派：** agent 不能指派負責人，由客服主管在 CRM 指派。
- **通知：** 客服主管透過 CRM 與 Workflow Controller 的收件匣看到案件與流程；LINE 通知待設定。

公司**沒有訂單／物流系統**可以查詢；客戶問訂單或物流時，記互動、開案件（`category=inquiry`）轉真人。

## Workflow Controller

- **Base URL:** `${FLOW_CONTROLLER_URL}`（gateway 主機 `http://127.0.0.1:18090`，API 在 `/api` 底下）
- **Credential reference:** `${FLOW_CONTROLLER_TOKEN_CS_AGENT}`
- 只用 `flow-start`、`flow-report` 兩個共用 skill 呼叫（`~/.openclaw/skills/`），不得直接修改流程狀態。
- cs-agent 可以啟動 `visit`（流程 ①）與 `refund`（流程 ②，只由 `crm-intake` 依案件分類啟動，去重鍵 `crm:case:<case_id>`）。
- 模擬客戶訊息由 controller `/api/sim/messages` 轉來（milim 扮客戶），標示「模擬客戶、external_customer_id=…」，
  用 `crm-intake` 處理；cs-agent 不呼叫 `/api/sim/*`。

## 使用限制

- 認證資訊只能由環境變數或 secret manager 注入，不得寫入 workspace。
- 不得使用 shell、瀏覽器或其他 Agent 繞過上述限制；CRM 寫入只用本檔列出的端點。
- 工具未設定時明確回報「尚未連線」，不可捏造結果。

## 尚待設定

- FAQ 知識庫位置與更新流程
- 通路（LINE）客戶識別與 CRM `external_customer_id` 的對應規則
- 轉真人的即時通知方式（LINE 官方帳號，計畫 P6）
