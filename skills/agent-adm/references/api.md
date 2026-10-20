# agent-adm 的 API 細節

只有要改 `scripts/adm.py` 時才需要讀。ADM 的完整說明在 FreeForJoy/adm 的 `docs/agent-api.md`。

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `ADM_API_BASE` | `http://localhost:18001/api/v1` | ADM 後端（gateway 主機的 docker compose 發布在 18001） |
| `ADM_AGENT_PASSWORD` | —（必填） | 員工 agent 的 ADM 密碼，帳號名＝agent id |
| `OPENCLAW_STATE_DIR` | `~/.openclaw/state` | 狀態檔根目錄 |
| `FREEFORJOY_ENV_FILE` | `~/.openclaw/secrets/freeforjoy.env` | 環境變數沒設時從這個檔補讀（不覆蓋既有變數）；設成空字串可關閉 |

## session 登入

`POST /api/v1/auth/login/` `{"username","password"}`（這個端點不檢查 CSRF），回 200 並設定
`sessionid`、`csrftoken` cookie。之後的 POST 帶 `X-CSRFToken: <csrftoken>`。

員工以 session 預約的是「自己用」的預約（`booked_by` 是自己、`booked_for` 為空）；
session 使用者不能代訂，代訂只有 admin-agent 的 delegate token 能做。

## 會議室

```http
GET  /api/v1/rooms/availability/?start_at=…&end_at=…&capacity=4
POST /api/v1/room-bookings/
Idempotency-Key: agent-adm:<agent>:room:<雜湊>
{"room":1,"start_at":"2026-10-13T14:00:00+08:00","end_at":"2026-10-13T15:00:00+08:00","purpose":"客訴案例檢討"}
```

## 公務車（luminous 自己用）

```http
GET  /api/v1/vehicles/availability/?start_at=…&end_at=…&seats=1
POST /api/v1/vehicle-bookings/
Idempotency-Key: agent-adm:luminous:vehicle:<雜湊>
{"vehicle":1,"start_at":"…","end_at":"…","purpose":"行政外出：銀行對帳與票據送件","destination":"台北市中正區銀行"}
```

## 回應與重試

| 狀態 | 腳本怎麼做 |
|---|---|
| 201 | 新建成功 |
| 200 | 同鍵同內容的重送，回原本那筆（也算成功） |
| 409 | 時段衝突（或同鍵不同內容）→ 換一個時段，最多共 3 次 |
| 400 `start_at must be in the future` | 時段已過 → 換時段 |
| 5xx／連線失敗 | 這次嘗試保持「結果未知」，結束並 exit 1；重跑用同一個鍵、同一份內容重送 |
| 其他 400／403 | 停止並回報（announce） |

ADM 的冪等以「預約人＋Idempotency-Key」判斷，所以預約的 POST 逾時或 5xx 時可以安全重試。

## 時段

- 日期：今天（15:30 以前才考慮）或之後 2 個工作天
- 會議室：10:00～16:00 每半小時一個開始時間，30／60／90 分鐘，2～8 人；結束不超過 17:30
- 公務車：10:00～15:00 之間開始，60～180 分鐘，1～2 人
- 一定在現在 30 分鐘以後

## 狀態檔

`~/.openclaw/state/agent-adm/<agent>/<date>.json`：

```json
{
  "decision": {"room": true, "vehicle": false},
  "rolls": {"room": 0.04, "vehicle": null},
  "room": {
    "capacity": 4, "purpose": "業務週會",
    "attempts": [{"start_at": "…", "end_at": "…", "target": 1, "key": "…", "result": "conflict"},
                 {"start_at": "…", "end_at": "…", "target": 2, "key": "…", "result": "booked"}],
    "booking_id": 12
  },
  "manual": {"<參數雜湊>": { "…book-room 的同一種紀錄…" }}
}
```

`result` 是 `sent` 代表送出後結果未知，重跑時會用同一個鍵重送。

## assign-laptop（流程 ③，IT 人員 souei）

需要 `ADM_AGENT_PASSWORD`（souei 的 ADM 帳號要是 staff）、`FLOW_CONTROLLER_URL`、`FLOW_CONTROLLER_TOKEN_SOUEI`。
**不使用任何 ADM agent token**（ADM 的 A4 對 token 一律 403）。

```http
# controller（souei 自己的 token）
GET  /api/inbox?unread_only=true&limit=200          → items[] 裡 kind == "task" 的 flow_id
GET  /api/flows/{flow_id}                            → current_step == "await_laptop"；
                                                       state.input.employee_id（新人 HRM UUID）、name、employee_number；
                                                       state.steps.draft_laptop.asset_code／alternatives
# ADM（souei 的 session）
GET  /api/v1/assets/?category=laptop&unassigned=false&is_active=all   → 新人名下已有筆電就視為已指派
GET  /api/v1/assets/?code=<code>&is_active=all                         → 依建議、備選順序檢查
POST /api/v1/assets/{code}/assign/
X-CSRFToken: <csrftoken>
Idempotency-Key: agent-adm:souei:assign:<flow_id>    （ADM 目前不檢查；指派本身就是冪等的）
{"custodian_hrm_employee_id": "<新人 HRM UUID>",
 "custodian_display_name": "<state.input.name>",          （姓名空白就不帶）
 "custodian_employee_number": "<state.input.employee_number>"}   （選填）
# controller
POST /api/flows/{flow_id}/events {"type": "laptop_assigned", "asset_code": "NB-004",
                                  "note": "（換備選、或保管人是佔位帳號時說明）"}
POST /api/inbox/{notification_id}/read
```

| 回應 | 腳本怎麼做 |
|---|---|
| A4 200 且 `custodian_hrm_employee_id` 是新人 | 成功，送事件；`custodian_is_placeholder` 為 true 時（ADM 自動建立的未啟用帳號）在 note 註明 `custodian_username`；新人名下已有筆電的「已指派」路徑也一樣看資產的 `custodian_is_placeholder` |
| A4 400（流程沒有新人姓名、或新人的 HRM ID 連到停用帳號） | needs_human（換別台也一樣），不送事件 |
| A4 401／403 | `forbidden`、announce（帳號不是 staff） |
| A4 404／409（不存在、停用） | 換下一台備選 |
| A4 5xx／連線失敗 | 結束（exit 1、不 announce），下一輪重跑 |
| 事件 200 | 標已讀，狀態檔記 `reported` |
| 事件 409 `step_not_current` | 再查流程：`state.steps.await_laptop.asset_code` 已有值 → 視為已回報（上次回應遺失） |
| 事件 403 `not_reporter`、422 | `error`、announce |

狀態檔 `~/.openclaw/state/agent-adm/souei/assign-laptop.json`：
`{"flows": {"<flow_id>": {"status": "assigned|reported|needs_human", "asset_code", "already", "tried", "announced", "placeholder_username"}}}`。
`placeholder_username` 只在保管人是 ADM 自動建立、尚未啟用的佔位帳號時出現（含重跑走「已指派」路徑）。

ADM 的佔位保管人（A4 補強，FreeForJoy/adm）：HRM 員工 ID 找不到 PersonLink 且有帶 `custodian_display_name` 時，
ADM 在同一個 transaction 內建立停用、不可登入的使用者（username `hrm-<員工編號>`，撞名改用 UUID 前綴）與 PersonLink，
再完成指派；稽核記為 `person.placeholder_created`（「自動建立保管人帳號」）。已存在的佔位帳號直接沿用，重跑安全。
`announced` 讓同一條流程的「需要人處理」只喊一次。
