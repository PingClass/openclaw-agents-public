# 流程 ③：新人到職（`type=onboarding`）

對應計畫：`plan/multi-agent-workflows.md` 第 2 節 ③、3.6、4.1（A3、A4）、4.3（H1～H5）、5.2（it-agent、hr-agent）、7 的 ③ 驗收。
程式：`app/flows/onboarding.py`。這份文件是**下一批 skill 的實作契約**：每一步 agent 會收到什麼、
要用哪個 skill、做完用 `flow-report`（或事件）回報時要帶哪些欄位。派工指令格式、`flow-report` 的共通規則
和流程 ① 相同，見 [`visit.md`](visit.md) 的「派工指令格式」「flow-report」兩節。

## 總覽

| 計畫 | controller 步驟 | 種類 | 執行者 | skill | 時限 |
|---|---|---|---|---|---|
| 1 | （啟動流程） | — | hr-agent | `onboarding-list` → `flow-start` | — |
| — | `resolve_parties` | controller | controller 自己 | — | 立即 |
| 2 | `draft_laptop` | agent | it-agent | `laptop-allocation-draft` | 5 分鐘 |
| 3 | `notify_it` | controller | controller → IT 人員收件匣（`kind=task`） | — | 立即 |
| 3 | `await_laptop` | 等待事件 | IT 人員（Identity role=it，souei） | `agent-adm assign-laptop`（員工 agent，下一批） | 到職前一個工作天 18:00（至少 4 小時），逾時只標卡住 |
| 4 | `book_room` | agent | admin-agent | `adm-room-book` | 5 分鐘 |
| 5 | `onboarding_notice` | agent | hr-agent | `onboarding-notice` | 5 分鐘 |
| 5 | `confirm_manager` | controller | controller 自己 | — | 立即 |
| 6 | `ack_manager` | 人工確認 | 新人的部門主管 | —（員工 agent 用 `agent-inbox` 確認） | 2 個工作天，逾時只標卡住 |
| 6 | `report_gap` | controller | controller → HR 收件匣 | — | 立即（只有主管選「有缺漏」才會跑） |

所有時間以台北時間計；「工作天」＝週一～週五（demo 環境不算國定假日）。

### 路由

```
resolve_parties ─► draft_laptop ─► notify_it ─► await_laptop ─► book_room ─► onboarding_notice ─► confirm_manager
      │ 部門沒有主管／不只一位                                                          │ HRM 查不到主管／和 Identity 不一致
      └──► 停下（failed，system），通知 HR＋admin                                        └──► 停下，通知 HR＋admin
           （修好 Identity 後重試同一步）                                                    （重試 → 回 onboarding_notice 重查 HRM）

confirm_manager ─► ack_manager ─ack─────► 結束（succeeded）
                               └missing─► report_gap（把主管意見轉給 HR）─► 結束（succeeded）
```

- **部門沒有主管時，流程停下來通知 HR，不猜一個人**（7 的 ③ 驗收）。檢查兩次：
  1. `resolve_parties`：Identity 裡和新人同 `department`、`role == "manager"` 的人必須**剛好一位**。
  2. `confirm_manager`：hr-agent 用 HRM H4 查到的主管（`manager_hrm_employee_ids`）必須**剛好一位且就是 1. 的那位**。

  不符合時：步驟 `failed`（`error_kind=system`），總覽顯示「確認人資、IT 與部門主管：資訊部沒有部門主管……」；
  HR（Identity role=`hr`，shion）的收件匣收到 `kind=alert`「新人到職流程停下：…」，admin 也收到同樣的 alert。
  HR 在 HRM 指定主管、管理員更新 Identity（`import-identities`）後，在流程詳情按「重試這一步」即可繼續。
- 目前資訊部、管理部、人資部沒有主管（PROGRESS 決策紀錄），新人分到這三個部門一定會停在 `resolve_parties`。
- agent 步驟回報 `status=needs_human` 都不自動重試，直接停下交給人；其他失敗依 `max_attempts`（都是 2）重試同一步。
- 重試一律沿用同一個 `Idempotency-Key`：`{flow_id}:{step}`。

## 啟動（計畫步驟 1）

hr-agent 的 `onboarding-list`（每週一 09:00）用 HRM H3 查下週到職的新人，每位用 `flow-start` 呼叫：

```http
POST /api/flows
Authorization: Bearer ${FLOW_CONTROLLER_TOKEN_HR_AGENT}

{"type": "onboarding", "input": {
  "employee_id": "0b9c…（HRM 員工 UUID）",
  "name": "王小明",
  "employee_number": "E2026101",
  "department_id": 1,
  "department_name": "業務部",
  "job_title": "業務專員",
  "start_date": "2026-10-19"
}}
```

| 欄位 | 必填 | 說明 |
|---|---|---|
| `employee_id` | ✔ | HRM 員工 UUID。**去重鍵固定為 `hrm:employee:<id>`**，重複呼叫回傳既有流程（`created=false`） |
| `name` | ✔ | 姓名 |
| `employee_number` | ✔ | 員工編號 |
| `department_id` | ✔ | HRM 部門 id（數字字串會轉成整數）；onboarding_notice 用它查 H4 |
| `department_name` | ✔ | 部門名稱；**用來比對 Identity 的 `department`**（例如「業務部」），找部門主管 |
| `job_title` | | 職稱 |
| `start_date` | ✔ | 到職日 `YYYY-MM-DD` |

- 只有 hr-agent（`starters=["hr-agent"]`）與 admin 帳號可以啟動；其他助理 agent、員工 agent、milim 都回 403 `not_allowed_to_start`。
- 只保存上表欄位；電話、地址等其他欄位直接丟掉（計畫 4.3「只回傳流程需要的欄位」）。
- 流程標題：`10/19 到職 王小明（業務部）`。

## resolve_parties：確認人資、IT 與部門主管

| 對象 | 規則 | 找不到／不只一位 |
|---|---|---|
| 人資 | `role == "hr"`，剛好一位 | 停下、通知 admin |
| IT 人員 | `role == "it"`，剛好一位 | 停下、通知 admin |
| 部門主管 | `department == department_name`、`role == "manager"`、不是新人本人，剛好一位 | **停下、通知 HR 與 admin** |

- 解析結果放在 `state.parties`（`hr`／`it`／`manager`，各含 `hrm_employee_id`、`employee_number`、`display_name`、
  `department`、`role`、`agent_id`）。
- IT 人員的員工 agent 列在 `state.participants`，可以用自己的 token `GET /api/flows/{id}` 讀配發建議。

## 各步驟的輸入與 output

### draft_laptop（it-agent／`laptop-allocation-draft`）

查還沒有保管人的筆電（ADM A3：`GET /api/v1/assets/?category=laptop&unassigned=true&is_active=true`，
token `ADM_API_TOKEN_IT`，scope `assets:read`），依新人的部門與職稱挑一台。**只產生建議，不得指派保管人**。

輸入：

| 欄位 | 說明 |
|---|---|
| `new_hire` | `{hrm_employee_id, name, employee_number, department_id, department_name, job_title, start_date}` |
| `category` | 固定 `laptop` |
| `assets_query` | 要用的查詢（同上） |

output：

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `asset_code` | str | ✔ | 建議配發的 ADM 資產編號 |
| `asset_name` | str | ✔ | 名稱或型號 |
| `specs` | str | | 規格摘要 |
| `reason` | str | ✔ | 為什麼建議這一台（一句話） |
| `alternatives` | list | | 其他可配發的資產編號（依建議順序，通知 IT 時列前 3 個） |
| `available_count` | int | | 目前未指派的筆電數量 |

沒有任何未指派的筆電：`status=needs_human`，summary 說明原因（流程停下，總覽顯示「it-agent 回報需要人處理：…」）。

### notify_it（controller）

在 IT 人員（`parties.it`，souei）的收件匣放一則 **`kind=task`** 通知：

- `title`：`IT 任務：配發筆電給新人 王小明`
- `body`：新人姓名、員工編號、部門、職稱、到職日、建議資產編號（＋備選）、理由，以及回報方式
  （`POST /api/flows/{flow_id}/events {"type": "laptop_assigned", "asset_code": "…"}`）。
- `flow_id`：流程 id（用它查詳細資料）。

`agent-inbox` 不處理 `kind=task`，只列在輸出的 `tasks`。

### await_laptop（IT 人員送 `laptop_assigned`）

不派工。IT 人員在 ADM 指派保管人是**人工操作**（A4 `POST /api/v1/assets/{code}/assign/`
`{"custodian_hrm_employee_id": "<新人 UUID>"}`，**僅限 session 登入的 staff**）。souei 的員工 agent
（下一批：`skills/agent-adm` 的 `assign-laptop` 子指令）的做法：

1. `GET /api/inbox?unread_only=true`（souei 自己的 controller token），找 `kind == "task"` 的通知，取 `flow_id`。
2. `GET /api/flows/{flow_id}`：確認 `current_step == "await_laptop"`；建議資產在
   `state.steps.draft_laptop.asset_code`（備選 `alternatives`），新人在 `state.input.employee_id`。
3. 以 souei 的 ADM session（`ADM_AGENT_PASSWORD`）呼叫 A4 指派。建議的那台已經被指派走了，就改用備選。
   A4 的 Idempotency-Key 建議用 `agent-adm:souei:assign:<flow_id>`。
4. 用自己的 controller token 回報：

```http
POST /api/flows/{flow_id}/events
Authorization: Bearer ${FLOW_CONTROLLER_TOKEN_SOUEI}

{"type": "laptop_assigned", "asset_code": "LT-003", "note": "（可省略）"}
```

5. `POST /api/inbox/{notification_id}/read` 把任務標成已讀。

| 回應 | 情況 |
|---|---|
| 200 | 收下，流程進到 book_room |
| 403 `not_reporter` | **送出者不是這條流程的 IT 人員**（`parties.it` 的 `agent_id`，且其 Identity role 必須是 `it`）；其他員工 agent、助理 agent（含 it-agent）都不行 |
| 409 `step_not_current` | 流程目前不在等指派（太早、或已經回報過） |
| 422 `invalid_event` | `asset_code` 空白、`note` 不是字串 |
| 422 `unknown_event` | `type` 不是 `laptop_assigned` |

- 這一步不接受 `flow-report`（回 409 `use_events`）。
- output：`asset_code`、`suggested_asset_code`、`matches_suggestion`（和建議是否相同）、
  `custodian_hrm_employee_id`（新人 UUID）、`note`。
- 截止：到職前一個工作天 18:00（流程啟動時離截止不到 4 小時就給 4 小時）。逾時只標卡住
  （「等待「等 IT 人員在 ADM 指派筆電」已逾時，souei 已 … 沒有回報」），之後回報仍可接手。
- **controller 不自己查 ADM**（controller 不碰業務資料，計畫第 1 節）：目前以 IT 人員的回報為準。
  之後若要核對，可以在 await_laptop 後面加一個 it-agent 步驟（`assets:read` 查該資產的 custodian 是否為新人），不需要改 IT 人員這邊的契約。

### book_room（admin-agent／`adm-room-book`）

到職第一天的說明會議室，**代訂給人資**（`booked_for_hrm_employee_id` ＝ `parties.hr`）：人資主持到職說明，
而且人資一定存在（部門主管可能缺）。admin-agent 用 ADM A1 的代訂與 A2 的 `rooms:delegate`。

輸入：

| 欄位 | 說明 |
|---|---|
| `date` | 到職日 |
| `preferred_start_at`、`preferred_end_at` | 優先時段：到職日 09:30–10:30 |
| `earliest_start_at`、`latest_end_at` | 可調整範圍：到職日 09:00–12:00 |
| `duration_minutes` | 60 |
| `capacity` | 3（新人、主管、人資） |
| `purpose` | `新人到職說明：王小明（業務部）` |
| `booked_for_hrm_employee_id` | 人資的 HRM UUID |
| `booked_for` | `{hrm_employee_id, display_name}`（給人看） |
| `attendees` | `[新人姓名, 主管名稱, 人資名稱]` |

做法：

1. 先查 `GET /api/v1/room-bookings/`：已經有自己代訂、`booked_for` 與 `purpose` 相同、同一天的預約（上一次嘗試其實成功了）
   就直接回報那一筆（`created=false`），不要再訂。
2. `GET /api/v1/rooms/availability/?start_at&end_at&capacity` 查空會議室，優先 preferred 時段，沒有就在範圍內找。
3. `POST /api/v1/room-bookings/` `{"room", "start_at", "end_at", "purpose", "booked_for_hrm_employee_id"}`，
   Header `Idempotency-Key` 用指令裡的值；遇到 409 換一間／換時段時，鍵加上 `:2`、`:3` 後綴（ADM 同一個鍵不能用在不同內容）。
4. 整個範圍都沒有空會議室：`status=needs_human`。

output：

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `booking_id` | int | ✔ | ADM 會議室預約 id |
| `room_id` | int | ✔ | 會議室 id |
| `room_name` | str | ✔ | 會議室名稱 |
| `start_at`、`end_at` | str | ✔ | 預約時段（ISO 8601，含時區） |
| `created` | bool | | `true` 新建立；`false` 冪等重放或沿用既有預約 |

### onboarding_notice（hr-agent／`onboarding-notice`）

用 HRM H4 `GET /api/departments/{department_id}/managers/`（`HRM_API_TOKEN`，scope `department:read`）查部門主管，
產生到職清單。**查到幾位就回報幾位，不要自己挑一個人**；查不到也是 `status=ok`、`manager_hrm_employee_ids: []`，
由 controller 決定停下並通知 HR。

輸入：

| 欄位 | 說明 |
|---|---|
| `new_hire` | 同 draft_laptop |
| `department_id` | HRM 部門 id |
| `managers_query` | `GET /api/departments/{id}/managers/` |
| `expected_manager` | controller 從 Identity 解析的主管 `{hrm_employee_id, display_name}`（只供參考，不要拿來取代 HRM 的結果） |
| `hr` | 人資 `{hrm_employee_id, display_name}` |
| `laptop` | `{asset_code, asset_name}`（asset_code 是 IT 實際指派的那台） |
| `orientation` | `{room_name, start_at, end_at, booking_id}` |
| `manager_comment` | 目前一律 `null`（保留給之後「主管退回重產」） |

output：

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `manager_hrm_employee_ids` | list | ✔ | HRM 回傳的部門主管員工 UUID（查不到就是 `[]`） |
| `checklist` | str | ✔ | 到職清單全文：姓名、員工編號、部門、職稱、到職日、筆電資產編號、說明會議室與時間、主管第一週要做的事 |
| `items` | list | | 清單項目（字串陣列） |

- **到職清單不得包含電話、地址**（7 的 ③ 驗收）。controller 的防線：output 欄位名含 phone／address／電話／地址的整個欄位濾掉；
  `checklist`、`items` 文字裡像電話的號碼（`09xx-xxx-xxx`、`02-xxxx-xxxx`、`(02) xxxx-xxxx`、`09xx.xxx.xxx`、`+886 …`）
  與像台灣地址的片段（`〔縣市〕〔區〕…路／街〔…段〕〔…巷〕〔…弄〕…號〔…樓〕`）存進流程前就換成「［已移除］」，
  並在 output 加上 `pii_redacted: true`（`AgentStep.transform_output`）。這只是防線，skill 本身仍不得放入電話、地址。

### confirm_manager（controller）

比對 `manager_hrm_employee_ids` 和 `parties.manager`：

| 情況 | 結果 |
|---|---|
| 空陣列 | 停下，通知 HR：「HRM 查不到業務部的部門主管」 |
| 不含 `parties.manager` | 停下，通知 HR：「HRM 的業務部主管和 controller 的對照表（Main）不一致」 |
| 不只一位 | 停下，通知 HR：「HRM 的業務部有 2 位主管，無法決定由誰確認」 |
| 剛好就是 `parties.manager` | 繼續到 ack_manager |

停下後，HR 在 HRM 指定主管（必要時管理員更新 Identity），admin 在流程詳情對 `confirm_manager` 按「重試這一步」：
保存的 HRM 結果已經過時，所以 controller 不再拿舊資料比對，而是：

1. **先從 Identity 重新解析部門主管**（規則同 resolve_parties），更新 `state.parties.manager`——停下的原因可能就是對照表錯了。
   Identity 仍然沒有（或不只一位）主管時，再次停下並通知 HR＋admin（`manager_recheck` 保持 `true`，修好後再重試）。
2. **回到 `onboarding_notice` 重新派給 hr-agent**（attempt +1，同一個 Idempotency-Key；`expected_manager` 是重新解析後的主管），
   重查後再比對一次（`state.manager_recheck` 記住上一次是停下的）。

### ack_manager（部門主管）

- 確認者＝`parties.manager`。通知進該員工的收件匣（`kind=approval_request`，含一次性連結）。
- 問題：「請確認收到新人 王小明（業務部，10/19 到職）的到職清單；有缺漏請選「有缺漏」並說明，會轉給人資處理」。
- 選項：`ack`（確認收到 → 流程 `succeeded`）／`missing`（有缺漏 → report_gap，主管的意見轉給 HR）。
- 時限 2 個工作天，**逾時只標卡住**（超過一半就出現在總覽的卡住清單「等Main主管確認收到到職清單，已 …，10/14 10:00 截止」）；
  一次性連結 24 小時有效，過期由 admin 重送。
- 確認頁 `context`：`new_hire`、`employee_number`、`department`、`job_title`、`start_date`、`laptop`、`orientation`、`hr`、`checklist`。
- 員工 agent（main、hinata）用 `agent-inbox` 處理：步驟標題／問題含「到職」→ 一律選 `ack`（規則 `onboarding_ack`）。

### report_gap（controller）

主管選 `missing` 時，在 HR 收件匣放一則 `kind=info`：「Main 回報 王小明（…）的到職清單有缺漏：<意見>。請補齊後直接聯絡主管。」
流程結束（`succeeded`）。

## flow state 結構（給前端與除錯）

```json
{
  "input": {"employee_id": "…", "name": "王小明", "employee_number": "E2026101", "department_id": 1,
            "department_name": "業務部", "job_title": "業務專員", "start_date": "2026-10-19"},
  "parties": {"hr": {...}, "it": {...}, "manager": {...}},
  "participants": ["souei"],
  "steps": {"resolve_parties": {...}, "draft_laptop": {...}, "notify_it": {...}, "await_laptop": {...},
            "book_room": {...}, "onboarding_notice": {..., "pii_redacted": false}, "confirm_manager": {...}},
  "decisions": {"ack_manager": {"decision": "ack", "comment": "收到，謝謝"}},
  "manager_recheck": false
}
```

## 下一批 skill 的對照

| skill | 位置 | 步驟 |
|---|---|---|
| `onboarding-list` | `agents/hr-agent/skills/`（cron `0 9 * * 1`） | 啟動 |
| `laptop-allocation-draft` | `agents/it-agent/skills/` | draft_laptop |
| `assign-laptop`（`adm.py` 子指令） | `skills/agent-adm/`（souei 用 session） | await_laptop（送 `laptop_assigned`） |
| `adm-room-book` | `agents/admin-agent/skills/` | book_room |
| `onboarding-notice` | `agents/hr-agent/skills/` | onboarding_notice |
| `agent-inbox`（本 PR 已更新） | `skills/agent-inbox/` | ack_manager |

## 用假 agent 跑一次

```bash
cd controller/backend
FLOW_CONTROLLER_DISPATCHER=fake FLOW_CONTROLLER_DB_URL=sqlite:///./data/fake.db \
  uv run uvicorn --factory app.main:create_app --port 18091
# 另一個終端機：完整跑完（業務部，主管 main 確認收到）
FLOW_CONTROLLER_DB_URL=sqlite:///./data/fake.db \
  uv run python scripts/fake_agents.py --base-url http://127.0.0.1:18091 --setup-local --flow onboarding
# 部門沒有主管：停在 resolve_parties，印出 shion 收件匣的通知（exit 2）
FLOW_CONTROLLER_DB_URL=sqlite:///./data/fake.db \
  uv run python scripts/fake_agents.py --base-url http://127.0.0.1:18091 --setup-local --flow onboarding \
  --department 資訊部 --department-id 3
# 故意讓 souei 不回報：
... --flow onboarding --stall-at await_laptop
```
