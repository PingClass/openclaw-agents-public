# Tools

此檔只記錄連線方式與憑證引用，不保存秘密。

## IT 規範與排除手冊（知識庫）

- **來源：** `${IT_DOCS_PATH}`（IT 使用規範、常見問題排除手冊、帳號申請流程）

## IT 工單系統

- 尚未連線（佔位）：目前沒有工單系統，工單相關要求只整理問題描述、轉 IT 人員，不可捏造工單編號。
- **Base URL:** `${ITSM_API_BASE_URL}`
- **Credential reference:** `${ITSM_API_TOKEN}`
- 預期能力：建立工單、查詢本人工單狀態、附加排除紀錄

## 資產與授權清冊：ADM 行政系統 API（唯讀）

- **Base URL:** `${ADM_API_BASE}`（gateway 主機：`http://localhost:18001/api/v1`，不經過 Cloudflare Access 網域）
- **Credential reference:** `${ADM_API_TOKEN_IT}`（`adm_...`，it-agent 的 ADM agent token；`Authorization: Bearer`）
- **Scope:** 只有 `assets:read`
- 預期能力：

| 能力 | 端點 | scope |
|---|---|---|
| 查資產（依編號） | `GET /assets/?code=<資產編號>` | assets:read |
| 查未指派的筆電（新人配發建議） | `GET /assets/?category=laptop&unassigned=true&is_active=true` | assets:read |
| 查某台資產的保管人 | 回應的 `custodian_username`、`custodian_hrm_employee_id` | assets:read |

- **不能指派或變更保管人**：ADM 的 `POST /assets/{code}/assign/` 僅限 IT 人員以 session 登入（staff）操作，
  任何 agent token 一律 403。新人配發只透過 skill `laptop-allocation-draft` 產生建議。
- 軟體授權與到期日：ADM 沒有這類資料，尚未連線。
- API 文件：FreeForJoy ADM repo 的 `docs/agent-api.md`（「篩選資產（A3）」「指派保管人（A4，僅限人工）」）。

## Workflow Controller

- **Base URL:** `${FLOW_CONTROLLER_URL}`（gateway 主機：`http://127.0.0.1:18090`，API 在 `/api` 底下）
- **Credential reference:** `${FLOW_CONTROLLER_TOKEN_IT_AGENT}`
- 只用 `flow-start`、`flow-report` 兩個 skill 呼叫（`~/.openclaw/skills/`），不得直接修改流程狀態。
- it-agent 只回報步驟結果（`flow-report`），不啟動流程。

## 使用限制

- 本 Agent 沒有帳號管理、權限變更或遠端控制的權限，也不得透過其他方式取得。
- 認證資訊只能由環境變數或 secret manager 注入，不得寫入 workspace（gateway 主機：`~/.openclaw/secrets/freeforjoy.env`，skill 腳本會自動補讀）。
- 不得使用 shell、瀏覽器或其他 Agent 繞過上述限制。
- 工具未設定時明確回報「尚未連線」，不可捏造結果。

## 尚待設定

- IT 規範文件位置
- 工單系統連線
- 資安事件聯絡人與通報方式
