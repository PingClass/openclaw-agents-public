# agent-crm 的 API 細節

只有要改 `scripts/crm.py` 時才需要讀。

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `CRM_API_BASE` | `http://localhost:18081/api` | CRM 後端（gateway 主機的 docker compose 發布在 18081） |
| `CRM_AGENT_PASSWORD` | —（必填） | 員工 agent 的 CRM 密碼，帳號名＝agent id |
| `CRM_WORKSPACE_ID` | （空） | 帳號屬於多個 workspace 時才需要，送 `X-Workspace-ID` |
| `CRM_USER_ID_<AGENT>` | （空） | 自己在 CRM 的 user id（見下方「負責人」），例如 `CRM_USER_ID_GOBTA` |
| `ADM_API_BASE` | `http://localhost:18001/api/v1` | 查公務車預約與車輛里程 |
| `ADM_AGENT_PASSWORD` | —（report-trip 需要） | 員工 agent 的 ADM 密碼 |
| `FLOW_CONTROLLER_URL` | `http://127.0.0.1:18090` | controller |
| `FLOW_CONTROLLER_TOKEN_<AGENT>` | —（report-trip 需要） | 員工 agent 自己的 controller token，例如 `FLOW_CONTROLLER_TOKEN_MJOLMIRE_BUSINESSMAN` |
| `OPENCLAW_BIN` | `openclaw` | 排一次性 cron 用的 CLI |
| `OPENCLAW_STATE_DIR` | `~/.openclaw/state` | 狀態檔根目錄 |
| `FREEFORJOY_ENV_FILE` | `~/.openclaw/secrets/freeforjoy.env` | 環境變數沒設時從這個檔補讀（不覆蓋既有變數）；設成空字串可關閉 |

## CRM（session 登入）

1. `GET /api/auth/csrf/` 取得 `csrftoken` cookie
2. `POST /api/auth/login/`（帶 `X-CSRFToken`）`{"username","password"}`，成功 200；Django 登入後會換新的 csrftoken
3. 之後的 POST 一律帶最新的 `X-CSRFToken` cookie 值

session 使用者**沒有**伺服器端的 Idempotency-Key 保護（只有 service credential 有），
所以 `POST /api/v1/follow-ups/` 不在送出後重試；重跑時先查
`GET /api/v1/follow-ups/?kind=visit&status=pending&assignee_hrm_employee_id=<自己>&due_after=<時間>`，
同客戶、同時間的待辦已經存在就沿用。

### 拜訪待辦

```http
POST /api/v1/follow-ups/
{"customer":"<客戶 UUID>","title":"<見下>","kind":"visit","due_at":"2026-10-14T14:00:00+08:00","assignee":<自己的 CRM user id>}
```

`due_at` 是拜訪開始時間（台北時間）。FollowUp 只有 `title` 能放內容（上限 200 字），格式固定，
cs-agent／admin-agent 可以照這個格式解析出地點與人數：

```text
[拜訪] <客戶名稱>｜地點：<地點>｜人數：<n> 人｜時長：<分鐘> 分鐘｜目的：<一句話>
```

- 地點：依客戶 id 固定挑一個（同一位客戶永遠在同一個地方），附近十個常見地點之一。
- 人數：出發人數 1～3（派車查空車時的 `seats`）。
- 時長：60／90／120 分鐘（派車的時段）。

### 負責人（assignee）

FollowUp 的 `assignee` 要填 CRM 的 user id，但 CRM 的 `/api/me/` 不回 id。腳本依序：

1. `~/.openclaw/state/agent-crm/<agent>/profile.json` 的快取
2. 環境變數 `CRM_USER_ID_<AGENT>`
3. `/api/me/` 若之後補上 `id`／`user_id` 欄位就直接用
4. `GET /api/v1/follow-ups/?assignee_hrm_employee_id=<自己的 HRM UUID>` 任一筆的 `assignee`

都拿不到就回 `config_error`。CRM 的 `UserProfile.hrm_employee_id` 要先對應好（種子指令負責）。

## controller（員工 agent token）

`Authorization: Bearer $FLOW_CONTROLLER_TOKEN_<AGENT>`

### 找等待回報里程的流程

controller 的 `GET /api/flows` 目前只給登入的使用者；員工 agent token 會被拒（401），腳本會略過。
候選流程依序來自：

1. `GET /api/flows?type=visit&status=running,waiting_approval`（之後若開放員工 token 就會用到）
2. `GET /api/inbox`：自己收件匣裡每則通知的 `flow_id`（派車確認通知）
3. `~/.openclaw/state/agent-inbox/<agent>/handled.json`：agent-inbox 處理派車確認時記下的 `flow_id` 與確認頁內容

每個候選再 `GET /api/flows/{id}`（403 就改用確認頁內容判斷），條件：

- `type` 是 `visit`、狀態不是結束（succeeded／failed／expired／cancelled）
- 流程狀態裡標示業務（key 含 `sales`）的 HRM UUID 是自己（有標示時才檢查）
- 拜訪日期是今天（或 3 天內補報），而且已經到了拜訪結束前 30 分鐘以內（或已經結束）
- 目前步驟是等待外部事件（`kind == "wait"`）

拜訪時間依序從：流程狀態的 `visit_start_at`／`visit_at`／`visit_start`／`due_at`／`start_at`／`scheduled_at`、
確認頁內容裡的「YYYY-MM-DD HH:MM」或「M/D HH:MM–HH:MM」（流程 ① 確認頁 context 的 `visit`）字串、
自己的拜訪索引（以 `follow_up_id` 對應）、流程標題的 `M/D`。結束時間依序從 `visit_end_at`／`visit_end`／`end_at`、
「M/D HH:MM–HH:MM」，都沒有就用拜訪時間＋時長（預設 90 分鐘）。

**為什麼不能等到傍晚**：流程 ① 的公務車預約時段＝拜訪時段，ADM 只接受「預約開始前 15 分鐘～預約結束」
之間登記取車（過了回 409 `Booking window has ended`），而 controller 收到 trip_report 後還要派工給
admin-agent 去登記。所以 decide 把一次性 report-trip 排在拜訪結束前 10～20 分鐘。

### 回報里程

```http
POST /api/flows/{flow_id}/events
{"type":"trip_report","pickup_odometer":12000,"return_odometer":12036,"note":"今天拜訪客戶 3，地點在台北市內湖區瑞光路，往返共 36 公里，車況正常。"}
```

2xx 成功；403／404／409／410 視為「這條流程不歸我或不在等待中」，略過不算錯。
controller 收到後把步驟 5 派給 admin-agent 去 ADM 登記取車／還車。

## ADM（取車里程從哪來）

ADM 規定取車里程不能小於車輛目前的里程，所以不能亂編：

1. 流程狀態裡有 `pickup_odometer` 就用
2. session 登入 ADM（`POST /api/v1/auth/login/`），`GET /api/v1/vehicle-bookings/`
   找 `booked_for_username`（或 `booked_by_username`）是自己、狀態 active／picked_up、
   與拜訪時間同一天且最接近的那筆（流程狀態有 `booking_id` 時優先用）；
   已取車就用它的 `pickup_odometer`，否則 `GET /api/v1/vehicles/` 取那台車的 `odometer`
3. 流程狀態裡的 `odometer`
4. 都沒有 → `report_failed`（announce），請人用 `--pickup-odometer` 補

還車里程＝取車里程＋往返公里數（自己排的拜訪用地點的距離 ×2 加一點誤差，否則 10～80 km 隨機），
產生後存進 `~/.openclaw/state/agent-crm/<agent>/trips.json`，重送用同一組數字。

## 狀態檔

| 檔案 | 內容 |
|---|---|
| `<date>.json` | 當天的決策（擲骰結果、拜訪計畫、客戶、follow_up_id、report-trip 排程時間） |
| `visits.json` | 自己排過的拜訪索引（follow_up_id → 客戶、時間、地點、距離） |
| `trips.json` | 里程回報（flow_id → 取車／還車里程、狀態） |
| `profile.json` | 自己的 CRM user id 快取 |

## 一次性 cron

```bash
openclaw cron add --name "拜訪里程回報 gobta 2026-10-14 14:00" \
  --declaration-key agent-crm:report-trip:gobta:2026-10-14T1400 \
  --agent gobta --session isolated --at 2026-10-14T15:16:00+08:00 \
  --delete-after-run --no-deliver --timeout-seconds 300 \
  --message "用 agent-crm skill 回報今天拜訪客戶的取車／還車里程。…" --json
```

一筆拜訪一個 job（declaration-key 帶拜訪時間），同一天兩筆拜訪各自在自己的預約時段內回報，不會互相覆蓋。
