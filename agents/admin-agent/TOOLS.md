# Tools

此檔只記錄連線方式與憑證引用，不保存秘密。

## 行政辦法（知識庫）

- **來源：** `${ADMIN_DOCS_PATH}`（行政管理辦法、核決權限表、表單範本）

## ADM 行政系統 API（會議室、公務車、資產）

- **Base URL:** `${ADM_API_BASE}`（gateway 主機：`http://localhost:18001/api/v1`，不經過 Cloudflare Access 網域）
- **Credential reference:** `${ADM_API_TOKEN}`（`adm_...`，admin-agent 的 ADM agent token；`Authorization: Bearer`）
- **Scopes:** `rooms:read`、`rooms:write`、`rooms:delegate`、`vehicles:read`、`vehicles:write`、`vehicles:delegate`、`assets:read`
- 預期能力：

| 能力 | 端點 | scope |
|---|---|---|
| 查空會議室、預約、取消 | `GET /rooms/availability/`、`POST /room-bookings/`、`POST /room-bookings/{id}/cancel/` | rooms:read／write |
| 查空車、預約、取車、還車、取消 | `GET /vehicles/availability/`、`POST /vehicle-bookings/`、`/{id}/pickup/`、`/{id}/return/`、`/{id}/cancel/` | vehicles:read／write |
| 代訂（帶 `booked_for_hrm_employee_id`） | 同上；只能查詢、取車、還車、取消**這個 token 自己代訂**的預約 | rooms:delegate、vehicles:delegate |
| 查資產 | `GET /assets/?code=` | assets:read |

- 預約、盤點等寫入要帶 `Idempotency-Key`（1～128 字元）。流程步驟一律用 controller 給的值；一般對話的預約才自己產生。
- **代訂只在 controller 的步驟指令下做**（見 AGENTS.md），一律透過 skill：`adm-vehicle-draft`、`adm-vehicle-book`、`adm-vehicle-trip`、`adm-room-book`。
- 資產只能查詢；**指派保管人（`POST /assets/{code}/assign/`）僅限 IT 人員以 session 登入操作**，任何 agent token 一律 403。
- API 文件：FreeForJoy ADM repo 的 `docs/agent-api.md`；schema 在 `${ADM_API_BASE}/schema/`。

## Workflow Controller

- **Base URL:** `${FLOW_CONTROLLER_URL}`（gateway 主機：`http://127.0.0.1:18090`，API 在 `/api` 底下）
- **Credential reference:** `${FLOW_CONTROLLER_TOKEN_ADMIN_AGENT}`（admin-agent 自己的 controller token）
- 只用 `flow-start`、`flow-report` 兩個 skill 呼叫，不得直接修改流程狀態。
- admin-agent 只回報步驟結果（`flow-report`），不啟動流程。
- `adm-vehicle-book`、`adm-vehicle-trip`、`adm-room-book` 寫入 ADM 前，會用同一把 token 唯讀查 `GET /api/flows/{flow_id}`，
  確認這一步確實是 controller 派給 admin-agent 的（skill 腳本自己做，不用手動查）。

## 行政申請系統（辦公用品、訪客、合約到期）

- 尚未連線：ADM 目前沒有辦公用品申請、訪客登記、合約到期清單的 API。相關要求只產生草稿或轉行政人員，不可捏造結果。

## 使用限制

- 認證資訊只能由環境變數或 secret manager 注入，不得寫入 workspace（gateway 主機：`~/.openclaw/secrets/freeforjoy.env`，skill 腳本會自動補讀）。
- API 未提供的能力視為不可用，不得用 shell、瀏覽器或其他 Agent 繞過。
- 工具未設定時明確回報「尚未連線」，不可捏造結果。

## 尚待設定

- 行政辦法文件位置
- 核決權限表與核准角色
- 辦公用品、訪客登記、合約到期的系統連線
