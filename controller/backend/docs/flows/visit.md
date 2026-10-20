# 流程 ①：客戶拜訪 → 派車 → 出車 → 報帳（`type=visit`）

對應計畫：`plan/multi-agent-workflows.md` 第 2 節 ①、3.4～3.7、9.4、9.5。
程式：`app/flows/visit.py`。這份文件是**下一批 skill 的實作契約**：每一步 agent 會收到什麼、
要用哪個 skill、做完用 `flow-report` 回報時 `output` 要有哪些欄位。

## 總覽

| 計畫 | controller 步驟 | 種類 | 執行者 | skill | 時限 |
|---|---|---|---|---|---|
| 1 | （啟動流程） | — | cs-agent | `crm-visit-list` → `flow-start` | — |
| — | `resolve_parties` | controller | controller 自己 | — | 立即 |
| 2 | `draft_vehicle` | agent | admin-agent | `adm-vehicle-draft` | 5 分鐘 |
| 3 | `confirm_vehicle` | 人工確認 | 業務本人 | —（員工 agent 從收件匣確認） | 拜訪前一天 18:00，逾時 → 流程 `expired` |
| 4 | `book_vehicle` | agent | admin-agent | `adm-vehicle-book` | 5 分鐘 |
| 5 | `await_trip` | 等待事件 | 業務本人 | —（`POST /api/flows/{id}/events`） | 拜訪當天 23:59，逾時只標卡住 |
| 5 | `record_trip` | agent | admin-agent | `adm-vehicle-trip` | 5 分鐘 |
| 6 | `draft_claim` | agent | finance-agent | `mileage-claim-draft` | 5 分鐘 |
| 7 | `approve_claim` | 人工確認 | 業務主管 | —（員工 agent 從收件匣確認） | 48 小時，逾時只標卡住 |
| 7 | `notify_finance` | controller | controller → 財會收件匣 | — | 立即 |
| 8 | `close_visit` | agent | cs-agent | `crm-visit-close` | 5 分鐘 |

所有時間以台北時間計。agent 步驟超過時限沒回報，總覽頁顯示「admin-agent 已 7 分鐘沒有回報」。
課堂示範模式（`input.demo=true`）的時限不同，見下方「示範模式」。

### 路由

```
resolve_parties ─► draft_vehicle ─► confirm_vehicle ─approve─► book_vehicle ─► await_trip ─► record_trip
                        ▲                │  revise ─► draft_vehicle                 │
                        │                │  reject ─► 結束（succeeded，不派車）      │ 409 時段衝突
                        └────────────────┴──────────────────────────────────────────┘ （最多 2 次，重查後直接預約）

record_trip ─► draft_claim ─► approve_claim ─approve─► notify_finance ─► close_visit ─► 結束（succeeded）
                    ▲              │  revise ─► draft_claim
                    └──────────────┤  reject ─► close_visit（不通知財會）
```

- **409 時段衝突**：`book_vehicle` 回報 `status=failed`、`error_kind=business`、`output.conflict=true`
  （或 `output.http_status=409`）→ 回到 `draft_vehicle` 重查空車（`exclude_vehicle_ids` 帶上**所有**衝突過的車，依發生順序、不重複；
  連線失敗等非衝突的失敗不算），
  重查後**直接預約，不再請業務確認**（7 驗收「時段衝突時自動重查空車」）。共 3 次（重試 2 次）都失敗就停下交給人，
  總覽顯示「ADM 回 409 時段衝突，已重試 2 次」。
- `book_vehicle` 的其他失敗（連線失敗、5xx）重試同一步，和 409 共用 3 次的上限。
- 任何步驟回報 `status=needs_human` 都不自動重試，直接停下交給人。
- 重試（自動或 admin 按「重試這一步」）一律沿用同一個 `Idempotency-Key`：`{flow_id}:{step}`。
  一步裡有兩個寫入時（目前只有 `close_visit`），指令另外列出 `Idempotency-Key.<名稱>: {flow_id}:{step}:<名稱>`，
  各寫入用自己的那一個（CRM 的冪等紀錄綁定 method＋path，同一個鍵打兩個端點會回 409）。

## 啟動（計畫步驟 1）

cs-agent 的 `crm-visit-list` 每找到一筆明天的拜訪，就用 `flow-start` 呼叫：

```http
POST /api/flows
Authorization: Bearer ${FLOW_CONTROLLER_TOKEN_CS_AGENT}

{"type": "visit", "input": {
  "follow_up_id": "0b7e4a52-3f7c-4d2e-9a1b-2c3d4e5f6a7b",
  "customer": {"name": "客戶 C", "external_id": "C-003"},
  "visit_start_at": "2026-10-15T10:00:00+08:00",
  "visit_end_at": "2026-10-15T12:00:00+08:00",
  "location": "台中 客戶 C 工廠",
  "seats": 2,
  "assignee_hrm_employee_id": "da1b72d2-89cb-4879-9c50-73e03a1ae993",
  "subject": "新品試吃"
}}
```

| 欄位 | 必填 | 說明 |
|---|---|---|
| `follow_up_id` | ✔ | CRM 待辦跟進 id（UUID 字串；舊的整數 id 也接受）。**去重鍵固定為 `crm:follow-up:<id>`**，重複呼叫回傳既有流程（`created=false`），呼叫端給的 `dedupe_key` 不採用 |
| `customer.name` | ✔ | 客戶名稱（畫面與確認頁顯示） |
| `customer.external_id` | | CRM 的 `external_customer_id` |
| `visit_start_at`、`visit_end_at` | ✔ | ISO 8601；沒帶時區視為台北時間；結束要晚於開始、最長 24 小時 |
| `location` | ✔ | 拜訪地點（派車的目的地） |
| `seats` | | 人數 1～20，預設 1 |
| `assignee_hrm_employee_id` | ✔ | 負責業務的 HRM 員工 UUID（CRM C2 的 `assignee_hrm_employee_id`） |
| `subject` | | 拜訪主題 |

| `demo` | | `true`＝課堂示範模式（見下一節）；不帶或 `false` 是正式流程。只有 admin 與 cs-agent 能帶 |

- 只有 cs-agent（`starters=["cs-agent"]`）與 admin 帳號可以啟動。
- 流程標題：`10/15 拜訪 客戶 C`（示範模式：`［示範］10/15 拜訪 客戶 C`）。

### 示範模式（課堂 demo，P6）

正式流程的派車確認截止是「拜訪前一天 18:00」，而 ADM 只接受「預約開始前 15 分鐘～預約結束」之間登記取車，
所以正式流程一定跨兩天。課堂上要在 10 分鐘內跑完，改用示範模式：`input` 帶 `"demo": true`。

| 項目 | 正式流程 | 示範模式 |
|---|---|---|
| 誰能啟動 | cs-agent、admin | 只有 admin 帳號，或 cs-agent（`crm-visit-list --demo`）；其他 agent 回 403 `demo_not_allowed` |
| 派車確認截止（`confirm_vehicle`） | 拜訪前一天 18:00 | 拜訪開始前 5 分鐘（但至少從建立確認起保留 5 分鐘） |
| 預約時段（`booking_start_at`～`booking_end_at`） | 拜訪開始～拜訪當天 23:59 | 拜訪開始～拜訪結束 |
| 等還車回報（`await_trip`） | 拜訪當天 23:59，逾時只標卡住 | 拜訪結束，逾時只標卡住 |
| 報帳核准（`approve_claim`） | 48 小時，逾時只標卡住 | 15 分鐘（過 7.5 分鐘就出現在卡住清單），逾時只標卡住 |
| 其他（agent 步驟 5 分鐘、409 重查、冪等、通知） | — | 不變 |

- 啟動時寫一筆稽核 `flow.demo_started`（actor 是啟動者）；列表與詳情的 `demo` 欄位為 `true`，前端顯示「示範」標籤。
- 示範模式只接受「今天（台北時間）、3 小時內開始、還沒結束」的拜訪，其他回 422 `demo_input_rejected`
  （admin 也一樣）：cs-agent 就算被客戶訊息等誘導帶 `demo=true`，也改不到明天以後的正式拜訪。
  同一筆待辦已經有流程時照常回傳既有流程（`created=false`），不再檢查。
- `demo` 必須是布林值，其他值回 422 `invalid_input`；其他流程類型帶 `demo=true` 回 422 `demo_not_supported`；
  `FLOW_CONTROLLER_DEMO_ENABLED=0` 時一律回 403 `demo_disabled`。
- 示範用的拜訪要排在「今天、約 20 分鐘後開始」：這樣講師核准、admin-agent 代訂完成時，
  已經進入 ADM 可以登記取車的時段（開始前 15 分鐘起），業務馬上就能回報里程。腳本見 `controller/deploy/demo/`，
  講師手冊見 `docs/classroom-demo.md`。

## resolve_parties：確認業務、主管與財會

controller 自己從 Identity 解析，**找不到人或不只一位就停下並通知 admin，不猜**：

| 對象 | 規則 |
|---|---|
| 業務 | `hrm_employee_id == assignee_hrm_employee_id`；必須有 `department` |
| 業務主管 | 和業務同 `department`、`role == "manager"`、不是業務本人，**剛好一位** |
| 財會 | `role == "finance"`，**剛好一位** |

- 停下時步驟為 `failed`（`error_kind=system`），總覽顯示「確認業務、主管與財會：找不到業務部的業務主管……」，
  並在 admin 的收件匣放一則 `kind=alert` 通知（admin 帳號要用 `create-user --identity` 連到一個 Identity，
  或 Identity 的 role 為 `admin`，才有收件匣）。補好 Identity 後，在流程詳情按「重試這一步」即可繼續。
- 解析結果放在 `state.parties`（`sales`／`manager`／`finance`，各含 `hrm_employee_id`、`employee_number`、
  `display_name`、`department`、`role`、`agent_id`）；業務的員工 agent 列在 `state.participants`，
  可以用自己的 token `GET /api/flows/{id}`。
- Identity 由 `controller/deploy/identities.json` 匯入（7 隻員工 agent）。

## 派工指令格式

每個 agent 步驟用 `openclaw agent --agent <id> --session-id wf-<flow_id>-<step>-<attempt>
--message-file <檔案> --timeout <秒> --json` 送出，訊息內容固定如下（`app/dispatch.py` 的 `build_step_message`）：

```
[workflow-controller 步驟指令]
flow_id: fl_0123456789abcdef
flow_type: visit
step: book_vehicle
attempt: 1
Idempotency-Key: fl_0123456789abcdef:book_vehicle
skill: adm-vehicle-book
---
<這一步要做什麼（中文說明）>
輸入（JSON）：
{ ...見各步驟的「輸入」... }
---
做完用 flow-report 回報：flow_id=…、step=…、attempt=…、status（ok／failed／needs_human）、summary（一句話）、output（JSON）。
status=ok 時 output 的欄位：
- booking_id（int，必填）：ADM 預約 id
...
失敗時 status=failed，error 寫原因，error_kind 填 business 或 system；需要人判斷時 status=needs_human。…
```

- skill 解析標頭的 `flow_id`、`step`、`attempt`、`Idempotency-Key`、`skill`，以及「輸入（JSON）：」後到下一個 `---` 之間的 JSON。
- 有多個寫入的步驟，標頭在 `Idempotency-Key` 後面多出幾行 `Idempotency-Key.<名稱>: <鍵>`（例如 close_visit 的
  `Idempotency-Key.interaction: fl_…:close_visit:interaction`、`Idempotency-Key.complete: fl_…:close_visit:complete`）。
- 寫入 ADM／CRM 時 `Idempotency-Key` header 一律用指令裡的值，**不得自己產生**。
- `openclaw agent` 的回覆文字（payloads）**不是**步驟結果；controller 只認 `flow-report`。

### flow-report

```http
POST /api/flows/{flow_id}/steps/{step}/result
Authorization: Bearer <該 agent 的 controller token>

{"status": "ok", "summary": "已代訂 ABC-1234", "attempt": 1, "output": {...}}
```

- `status=ok` 時，下表標「必填」的欄位缺少會回 **422 `missing_output_fields`**（狀態不變，可補齊再送）。
- `attempt` 建議帶上（指令裡的值），過期的回報會回 409 `stale_attempt`。
- `output` 不要放電話、地址（controller 會濾掉欄位名含 phone／address 等的欄位）。

## 各步驟的輸入與 output

### draft_vehicle（admin-agent／`adm-vehicle-draft`）

查空車（ADM `GET /api/v1/vehicles/availability/?start_at&end_at&seats`），產生派車草稿。**不預約**。

輸入：

| 欄位 | 說明 |
|---|---|
| `visit_start_at`、`visit_end_at` | 拜訪時段（ISO 8601） |
| `booking_start_at`、`booking_end_at` | **預約時段**：拜訪開始 → 拜訪當天 23:59。查空車與草稿的 `start_at`／`end_at` 都用這個 |
| `seats` | 人數 |
| `destination` | 目的地（＝`location`） |
| `purpose` | `客戶拜訪：<客戶名稱>` |
| `customer` | `{name, external_id}` |
| `sales` | `{hrm_employee_id, display_name}` |
| `exclude_vehicle_ids` | 這條流程裡**所有**預約遇到 409 衝突的車輛 id（要全部排除；第一次是 `[]`） |
| `sales_comment` | 業務在確認時選「退回重排」的意見（沒有則 `null`） |

output：

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `vehicle_id` | int | ✔ | ADM 車輛 id |
| `vehicle_code` | str | ✔ | 車號或代碼（給人看） |
| `vehicle_name` | str | | 車名或車型 |
| `seats` | int | | 座位數 |
| `start_at`、`end_at` | str | ✔ | 預約時段（ISO 8601，含時區），等於輸入的 `booking_start_at`／`booking_end_at` |
| `destination` | str | ✔ | 目的地 |
| `purpose` | str | ✔ | 用途 |

沒有可用車輛：`status=needs_human`，summary 說明原因。

### confirm_vehicle（業務本人）

- 確認者＝`parties.sales`。通知進該員工的收件匣（`kind=approval_request`，含一次性連結）。
- 選項：`approve`（核准派車）／`revise`（退回重排，comment 會給 draft_vehicle）／`reject`（不需要派車：流程結束、不預約）。
- 截止：拜訪前一天 18:00。逾時流程變 `expired`，業務收件匣收到 `approval_expired`：「……請自行到 ADM 預約公務車」。
- 一次性連結預設 24 小時有效（比截止早到期時 admin 可以重送）。
- 確認頁 `context`：`customer`、`visit`、`location`、`seats`、`vehicle`、`booking_window`、`deadline`。

員工 agent 扮演確認者的做法（確認者被 admin 切成「真人確認」時，見下方「確認者：agent 代理／真人」）：

```http
GET  /api/inbox?unread_only=true            （員工 agent 自己的 token；回傳它扮演的人的收件匣）
GET  /api/approvals/{approval_id}?token=…   （連結裡的一次性 token）
POST /api/approvals/{approval_id}/decision  {"token": "…", "decision": "approve", "comment": "…"}
```

### book_vehicle（admin-agent／`adm-vehicle-book`）

代業務預約：ADM `POST /api/v1/vehicle-bookings/`，body 帶 `booked_for_hrm_employee_id`，Header `Idempotency-Key`。

輸入：`vehicle_id`、`start_at`、`end_at`、`purpose`、`destination`、`booked_for_hrm_employee_id`（業務的 HRM UUID）。

output：

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `booking_id` | int | ✔ | ADM 預約 id |
| `vehicle_id` | int | ✔ | 車輛 id |
| `start_at`、`end_at` | str | ✔ | 預約時段 |
| `created` | bool | | `true` 新建立；`false` 冪等重放回傳既有預約 |

失敗：

| 情況 | 回報 |
|---|---|
| ADM 409 時段衝突（車已被訂） | `status=failed`、`error_kind=business`、`error="ADM 回 409 時段衝突"`、`output={"conflict": true, "http_status": 409, "vehicle_id": <id>}` → 回 draft_vehicle |
| ADM 409「Idempotency key already used …」 | `status=needs_human`（不是時段衝突，不要重查） |
| 連線失敗、5xx | `status=failed`、`error_kind=system` → 重試同一步 |

### await_trip（業務本人送 trip_report）

不派工。業務的員工 agent（`agent-crm` 的 `report-trip`）用**自己的 token** 送：

```http
POST /api/flows/{flow_id}/events
Authorization: Bearer ${FLOW_CONTROLLER_TOKEN_MJOLMIRE_BUSINESSMAN}

{"type": "trip_report", "pickup_odometer": 12000, "return_odometer": 12037, "note": "客戶對新品有興趣，下週報價"}
```

| 回應 | 情況 |
|---|---|
| 200 | 收下，流程進到 record_trip（回傳流程摘要） |
| 403 `not_reporter` | 不是這條流程的業務本人（其他員工 agent、助理 agent 都不行） |
| 409 `step_not_current` | 流程目前不在等回報（太早、或已經回報過） |
| 422 `invalid_event` | 里程不是整數、負數、還車小於取車、里程差超過 2000 km |
| 422 `unknown_event` | `type` 不是 `trip_report` |

- 流程 id 從業務收件匣的確認通知（`flow_id`）取得；業務 agent 也可以 `GET /api/flows/{id}` 查目前步驟。
- 這一步不接受 `flow-report`（回 409 `use_events`）。
- `note` 會當作 close_visit 的拜訪摘要（visit_note）與 ADM 的 condition_notes。
- 截止：拜訪當天 23:59。逾時只標卡住（「等待「等業務回報取車還車里程」已逾時，mjolmire-businessman 已 … 沒有回報」），之後回報仍可接手。

### record_trip（admin-agent／`adm-vehicle-trip`）

代業務登記：ADM `POST /api/v1/vehicle-bookings/{booking_id}/pickup/`，再 `/return/`，
body `{"odometer": …, "condition_notes": …}`。已登記過的（狀態已是 picked_up／returned）不要重送。

輸入：`booking_id`、`pickup_odometer`、`return_odometer`、`condition_notes`。

output：

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `booking_id` | int | ✔ | ADM 預約 id |
| `pickup_odometer` | int | ✔ | ADM 登記的取車里程 |
| `return_odometer` | int | ✔ | ADM 登記的還車里程 |
| `distance_km` | int | ✔ | 里程差 |

> ⚠️ ADM 的限制：取車只能在「預約開始前 15 分鐘」到「預約結束」之間登記（太早回 409 `Pickup is too early`，
> 過了回 409 `Booking window has ended`）。業務（`agent-crm report-trip`）是拜訪當天傍晚、還車後才回報，
> 所以預約時段一律到拜訪當天 23:59（`booking_end_at`），和等還車回報的截止一致。
> 仍遇到這兩種 409 時回報 `status=needs_human`，停下交給人。

### draft_claim（finance-agent／`mileage-claim-draft`）

里程差 × 費率（環境變數 `MILEAGE_RATE_TWD_PER_KM`，demo 假費率 5，要標明），產生報帳草稿。
**只交給 controller 保存（flow state），不寫入任何系統**；主管核准前沒有任何系統有正式金額。

輸入：`distance_km`、`visit_date`、`customer`、`sales`、`booking_id`、`rate_env`（固定 `MILEAGE_RATE_TWD_PER_KM`）、
`manager_comment`（主管退回重擬的意見）。

output：

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `distance_km` | int | ✔ | 里程差 |
| `rate_twd_per_km` | number | ✔ | 費率 |
| `amount_twd` | int | ✔ | 金額（四捨五入到元） |
| `rate_is_placeholder` | bool | ✔ | 是不是 demo 假費率 |
| `description` | str | ✔ | 報帳說明（日期、客戶、里程），不含電話地址 |

### approve_claim（業務主管）

- 確認者＝`parties.manager`。問題範例：「請核准 Mjolmire 10/15 拜訪「客戶 C」的交通費：37 km × NT$5/km（demo 假費率） = NT$185」。
- 選項：`approve`（→ notify_finance）／`revise`（退回重擬 → draft_claim）／`reject`（不核准 → 直接 close_visit，不通知財會）。
- 時限 48 小時，逾時只標卡住（車已經出過，不結束流程）。
- 確認頁 `context`：`sales`、`customer`、`visit_date`、`distance_km`、`rate_twd_per_km`、`rate_is_placeholder`、`amount_twd`、`description`。

### notify_finance（controller）

核准後，controller 在財會（`parties.finance`，目前是 luminous）的收件匣放一則 `kind=info` 通知（代替 LINE），
請財會手動入帳；同時把核准的報帳寫進 `state.claim`（`status=approved`、`approved_by`、`approved_at`）。

通知內容**全部由 controller 依 flow state 組成**（`visit.finance_notice`）：業務、拜訪日期、客戶、核准人、
金額算式（里程 × 費率＝金額，demo 假費率會標明）、車輛與 ADM 預約編號、流程 id。
**不引用** draft_claim 的 `description`——草稿是核准前寫的，常帶「草稿・待主管核准」，和「已核准」並列會誤導財會。

### close_visit（cs-agent／`crm-visit-close`）

CRM `POST /api/v1/interactions/` 記一筆 kind=visit 的拜訪互動（摘要格式依 CRM 的 C5 文件），
再 `POST /api/v1/follow-ups/{follow_up_id}/complete/`。**互動紀錄不寫報帳金額**。

冪等鍵：記互動用 `Idempotency-Key.interaction`（`{flow_id}:close_visit:interaction`），
完成待辦用 `Idempotency-Key.complete`（`{flow_id}:close_visit:complete`）。CRM 的冪等紀錄綁定 method＋path，
兩個寫入共用同一個鍵，第二個會回 409。

輸入：

| 欄位 | 說明 |
|---|---|
| `follow_up_id` | CRM 待辦跟進 id（UUID 字串） |
| `customer`、`visit_start_at`、`visit_end_at`、`location`、`subject`、`sales` | 拜訪資料 |
| `visit_note` | 業務在 trip_report 的 note（拜訪摘要內容） |
| `vehicle_code` | 車牌或車輛代碼（draft_vehicle 的 `vehicle_code`），C5 交通欄位 |
| `pickup_odometer`、`return_odometer` | 起訖里程（以 record_trip 的 ADM 實際登記為準），C5 交通欄位 |
| `distance_km` | 里程差 |
| `claim_status` | 報帳結果：approved／rejected |

output：

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `interaction_id` | str | ✔ | CRM 互動紀錄 id（UUID 字串） |
| `follow_up_id` | str | ✔ | CRM 待辦跟進 id（UUID 字串） |
| `follow_up_status` | str | ✔ | 完成後的狀態（例如 `completed`） |

## 確認者：agent 代理／真人（計畫 9.4）

每位確認者（Identity）有 `confirm_mode`：`agent`（預設，員工 agent 的 `agent-inbox` 代為確認）或 `human`（真人自己確認）。
課堂 demo 時講師把 main、gobta 切成 `human`，用 admin 帳號在前端收件匣切到該確認者、打開確認連結親自確認。

```http
GET /api/inbox/confirm-mode                       （員工 agent 自己的 token：回傳它扮演的人）
GET /api/inbox/confirm-mode?recipient=gobta       （admin／登入者：recipient 可以是 hrm_employee_id、員工編號、Identity id 或 agent id）
→ {"recipient": {...}, "confirm_mode": "human", "human": true, "agent_id": "gobta"}

PUT /api/inbox/confirm-mode?recipient=gobta       （只有 admin，寫稽核 identity.confirm_mode）
{"mode": "human"}                                  （或 "agent"）
```

- `human` 時，扮演他的員工 agent 讀收件匣拿不到確認連結（`link=null`、`link_hidden=true`），所以就算 agent-inbox
  沒先查確認方式也無法代為確認；admin 與本人帳號照常看得到。切回 `agent` 後，還沒決定的確認又看得到連結。
- `agent-inbox` 每次 `check` 先查 `GET /api/inbox/confirm-mode`，`human` 時回 `outcome=paused`，什麼都不做。
- 收件匣與收件人清單的 `recipient` 都帶 `confirm_mode`。

## flow state 結構（給前端與除錯）

```json
{
  "input": {...},
  "parties": {"sales": {...}, "manager": {...}, "finance": {...}},
  "participants": ["mjolmire-businessman"],
  "steps": {"resolve_parties": {...}, "draft_vehicle": {...}, "book_vehicle": {...}, "await_trip": {...},
            "record_trip": {...}, "draft_claim": {...}, "notify_finance": {...}, "close_visit": {...}},
  "decisions": {"confirm_vehicle": {"decision": "approve", "comment": null},
                "approve_claim": {"decision": "approve", "comment": "OK"}},
  "failures": {"book_vehicle": {"count": 1, "error": "...", "error_kind": "business", "output": {...},
                                 "history": [{"error": "...", "error_kind": "business", "output": {...}}]}},
  "claim": {"amount_twd": 185, "status": "approved", "approved_by": "Main", ...}
}
```

## 用假 agent 跑一次

```bash
cd controller/backend
FLOW_CONTROLLER_DISPATCHER=fake FLOW_CONTROLLER_DB_URL=sqlite:///./data/fake.db \
  uv run uvicorn --factory app.main:create_app --port 18091
# 另一個終端機
FLOW_CONTROLLER_DB_URL=sqlite:///./data/fake.db \
  uv run python scripts/fake_agents.py --base-url http://127.0.0.1:18091 --setup-local --conflicts 1
# 故意不回報：
FLOW_CONTROLLER_DB_URL=sqlite:///./data/fake.db \
  uv run python scripts/fake_agents.py --base-url http://127.0.0.1:18091 --setup-local --stall-at draft_vehicle
# 卡住後等 admin 在前端按「重試這一步」，再繼續跑完（P2.5 驗收）：
FLOW_CONTROLLER_DB_URL=sqlite:///./data/fake.db \
  uv run python scripts/fake_agents.py --base-url http://127.0.0.1:18091 --setup-local --stall-at draft_vehicle --wait-retry --timeout 1800
```
