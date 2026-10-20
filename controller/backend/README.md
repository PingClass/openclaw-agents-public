# Workflow Controller 後端

多 agent 工作流的 controller（計畫：`plan/multi-agent-workflows.md` 第 1、3 節）。
負責流程狀態、派工給 OpenClaw agent、人工確認、逾時與重試、稽核；**不碰業務資料**，
真正的資料留在 ADM／CRM／HRM。

- Python 3.12＋FastAPI＋SQLite（SQLAlchemy 2），uv 管理相依，pytest。
- 單一 worker（SSE 事件匯流排與背景派工都在同一個行程內）。

## 目錄

```
app/
  main.py         FastAPI 進入點（uvicorn --factory app.main:create_app）
  config.py       環境變數 FLOW_CONTROLLER_*
  models.py       資料表：Flow、Step、Agent、AgentToken、Approval、Identity、
                  Notification（通知收件匣）、ChannelMessage（對外訊息，模擬通道）、AuditLog、User、UserSession
  engine.py       流程引擎：推進、派工、建立人工確認＋通知、處理回報、逾時掃描
  flows/          流程定義，每種流程一個模組（base.py 是介面，demo.py 是測試用假流程，
                  visit.py 是流程 ①、refund.py 是流程 ②、onboarding.py 是流程 ③，
                  步驟契約見 docs/flows/visit.md、refund.md、onboarding.md）
  channels.py     對外通道：模擬客戶訊息（in）與流程送出的回覆（out），LINE 接上前的替代
  dispatch.py     派工介面：OpenClawCliDispatcher、FakeDispatcher；送給 agent 的指令格式集中在這裡
  gateway.py      OpenClaw gateway 的 /readyz 健康檢查（總覽的「gateway 離線／頻道異常」）
  notify.py       通知管道：InboxNotifier（之後加 LINE）
  stuck.py        「卡住」的四種原因
  sanitize.py     個資過濾（API 回應不含電話、地址）
  routers/        API 路由
  cli.py          管理指令
tests/            pytest
scripts/
  fake_agents.py  假 agent：用 agent token 直接呼叫 API 跑完整條 ①（預設）、②（--flow refund）
                  或 ③（--flow onboarding，角色在 fake_onboarding.py）；--stall-at 故意不回報
docs/flows/       每條流程的步驟契約（給 skill 實作）
```

## 本機開發

```bash
cd controller/backend
uv sync                          # 建立 .venv（Python 3.12）
uv run flow-controller seed-agents
uv run flow-controller create-user admin --role admin          # 互動輸入密碼
FLOW_CONTROLLER_DISPATCHER=fake FLOW_CONTROLLER_CORS_ORIGINS=http://localhost:5173 \
  uv run uvicorn --factory app.main:create_app --reload --port 18090
curl http://localhost:18090/healthz
```

`FLOW_CONTROLLER_DISPATCHER=fake` 只記錄派工、不叫起 agent；用 agent token 直接呼叫
`/api/flows/{id}/steps/{step}/result` 就能扮演 agent（計畫 P2 的「假 agent」）。
`scripts/fake_agents.py` 把整條 ① 或 ② 的每個角色都扮演一次（用法見 `docs/flows/visit.md`、`refund.md` 最後一節）。

## 測試

```bash
uv run pytest                          # 全部
uv run pytest --cov=app --cov-report=term-missing
```

涵蓋：revision 衝突被拒、一次性 token 用過／過期／重送後失效、viewer 不能重試／取消／重送、
demo 流程用 FakeDispatcher 從開始跑到結束、四種卡住原因與文字、冪等鍵格式、
OpenClawCliDispatcher（用假的 openclaw 執行檔測 session-id、message-file、status、逾時與並行上限）、用真的 uvicorn 連 SSE 收事件；
流程 ①：完整跑完（含兩次人工確認）、409 回步驟 2 最多 2 次、派車確認逾時 → expired、非業務本人送 trip_report 被拒、
重試的 Idempotency-Key 不變、找不到主管時停下通知 admin、agent 不回報 → 卡住、gateway `/readyz` 失敗 → 總覽顯示離線、
`scripts/fake_agents.py` 對真的 uvicorn 跑完整條；
流程 ③：完整跑完、部門沒有主管（Identity 或 HRM）時停下並通知 HR、非 IT 人員送 laptop_assigned 被拒（403 `not_reporter`）、
主管確認逾時只標卡住、只有 hr-agent 能啟動、以 HRM 員工 id 去重、到職清單裡的電話與地址被遮掉、
主管對不上時更新 Identity 後重試會重新解析主管。

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `FLOW_CONTROLLER_DB_URL` | `sqlite:///./data/controller.db` | SQLite 路徑；Docker 內預設 `/data/controller.db` |
| `FLOW_CONTROLLER_PUBLIC_URL` | `http://localhost:18090` | 前端對外網址，用來組確認連結 `<url>/approvals/<id>?token=…` |
| `FLOW_CONTROLLER_DISPATCHER` | `openclaw` | `openclaw`（正式）或 `fake`（開發） |
| `FLOW_CONTROLLER_OPENCLAW_BIN` | `openclaw` | openclaw 執行檔路徑 |
| `FLOW_CONTROLLER_DISPATCH_TIMEOUT` | `600` | 呼叫 `openclaw agent` 一輪的逾時（秒） |
| `FLOW_CONTROLLER_MAX_CONCURRENT_DISPATCH` | `3` | 同時派工上限 |
| `FLOW_CONTROLLER_SCAN_INTERVAL` | `60` | 逾時掃描間隔（秒），`0` 代表關閉 |
| `FLOW_CONTROLLER_APPROVAL_TTL_HOURS` | `24` | 確認連結有效期限（若步驟時限更短，以步驟時限為準） |
| `FLOW_CONTROLLER_SESSION_TTL_HOURS` | `12` | 登入 session 期限 |
| `FLOW_CONTROLLER_COOKIE_SECURE` | `false` | 走 HTTPS 時設 `true` |
| `FLOW_CONTROLLER_TZ` | `Asia/Taipei` | 「今日」統計與截止時間的顯示時區 |
| `FLOW_CONTROLLER_CORS_ORIGINS` | （空） | 以逗號分隔，Vite 開發時填 `http://localhost:5173` |
| `FLOW_CONTROLLER_SIM_TARGETS` | `cs-agent` | `/api/sim/messages` 允許送達的 agent |
| `FLOW_CONTROLLER_SSE_KEEPALIVE` | `15` | SSE keepalive 間隔（秒） |
| `FLOW_CONTROLLER_GATEWAY_URL` | `http://127.0.0.1:18789` | OpenClaw gateway；每分鐘 `GET <url>/readyz` |
| `FLOW_CONTROLLER_GATEWAY_TIMEOUT` | `3` | `/readyz` 逾時（秒） |
| `FLOW_CONTROLLER_GATEWAY_CHECK_INTERVAL` | `60` | gateway 檢查間隔（秒），`0` 代表關閉 |
| `FLOW_CONTROLLER_FRONTEND_DIST` | （空） | 前端 build 產物目錄（`controller/frontend/dist`）；設定後同源提供前端並做 SPA fallback（`/api`、`/healthz` 除外） |
| `FLOW_CONTROLLER_DEMO_ENABLED` | `true` | 是否接受課堂示範模式的流程（`input.demo=true`，時限縮短）；設 `0` 關閉 |
| `FLOW_CONTROLLER_NEW_PASSWORD` | — | 只給 `create-user` 用，避免密碼出現在指令列 |

帳號、密碼、token 一律不寫進 repo；部署時存在 gateway 主機的 `~/.openclaw/secrets/freeforjoy.env`。

## 管理指令

```bash
uv run flow-controller init-db
uv run flow-controller seed-agents                                   # 5 隻助理 agent
uv run flow-controller upsert-agent milim --role simulator --display-name 米琳
uv run flow-controller upsert-agent main --role employee
uv run flow-controller issue-agent-token cs-agent                    # token 只印一次
uv run flow-controller issue-agent-token milim --can-simulate        # 可呼叫模擬入口
uv run flow-controller revoke-agent-token cs-agent
uv run flow-controller create-user teacher --role admin --identity <hrm_employee_id>
uv run flow-controller create-user guest --role viewer
uv run flow-controller import-identities identities.json             # - 代表 stdin
uv run flow-controller import-identities ../deploy/identities.json --with-agents   # 7 隻員工 agent
```

`identities.json` 是陣列（或 `{"identities": [...]}`），以 `hrm_employee_id` 對應，可重複執行：

```json
[{"hrm_employee_id": "<HRM 員工 UUID>", "employee_number": "AGT-MAIN", "display_name": "Main",
  "role": "manager", "department": "業務部", "adm_user_id": "12", "crm_user_id": "34",
  "line_user_id": null, "agent_id": "main"}]
```

- `role`：流程用的角色，`sales`（業務）／`manager`（部門主管）／`finance`（財會）／`it`／`hr`／`admin`（收 admin 通知）。
- `department`：部門名稱；「業務主管」＝同部門、role 為 manager 的人。
- `agent_id` 是扮演這個人的員工 agent（計畫 9.4），該 agent 可以用自己的 token 讀這個人的收件匣並回覆確認。
  `--with-agents` 會一併建立這些員工 agent（role=employee），之後再用 `issue-agent-token` 發 token。
- 電話、地址欄位不會匯入。
- `controller/deploy/identities.json` 是 gateway 主機上 7 隻員工 agent 的對照表（沒有個資與憑證）。

## API

全部掛在 `/api` 底下（前端的確認頁路徑 `/approvals/:id` 和 API 同網域時才不會衝突）；`/healthz` 另外留在根目錄。

| Method | Path | 權限 | 用途 |
|---|---|---|---|
| POST | `/api/flows` | 助理 agent token（須列在流程定義的 `starters`）或 admin | 啟動流程 `{type, input, title?, dedupe_key?}`；同一 `dedupe_key` 回傳既有流程（併發時也只建一條）。員工 agent、扮客戶的 milim 不能啟動流程。`input.demo=true` 是課堂示範模式（目前只有 visit；只有 admin 與流程定義的 `demo_starters` 能用，寫稽核 `flow.demo_started`；`FLOW_CONTROLLER_DEMO_ENABLED=0` 可關閉） |
| GET | `/api/flows/{id}` | 登入，或參與該流程的 agent（啟動者、被指派步驟、或列在 `state.participants`，例如業務本人） | 流程詳情：步驟時間軸、人工確認、稽核、卡住原因、`messages`（流程送給客戶的訊息） |
| POST | `/api/flows/{id}/steps/{step}/result` | 被指派的 agent | 回報 `{status: ok/failed/needs_human, summary, output, error, error_kind, attempt?, revision?}`；`status=ok` 時缺必填 output 欄位回 422 `missing_output_fields`，內容不符流程規則（例如回覆金額和主管決定不一致）回 422 `invalid_output` |
| POST | `/api/flows/{id}/events` | 流程指定的回報者（員工 agent token） | 外部事件，例如 visit 的 `{"type":"trip_report","pickup_odometer":int,"return_odometer":int,"note":str}`（只有該流程的業務本人能送）、onboarding 的 `{"type":"laptop_assigned","asset_code":str,"note":str}`（只有該流程的 IT 人員，Identity role=it，能送）；否則 403 `not_reporter` |
| GET | `/api/approvals/{id}?token=` | 一次性 token | 確認頁內容（只含該步驟需要的資料）；`fields` 是要填的欄位（例如 ② 的退款金額） |
| POST | `/api/approvals/{id}/decision` | 一次性 token | `{token, decision, comment?, data?}`，token 用過即失效；`data` 不合法回 422 `invalid_decision_data`（token 不會用掉） |
| POST | `/api/flows/{id}/cancel` | admin | `{revision, reason?}` |
| POST | `/api/flows/{id}/steps/{step}/retry` | admin | `{revision}`；同一個 Idempotency-Key 重派 |
| POST | `/api/approvals/{id}/resend` | admin | `{revision}`；產生新連結，舊連結失效 |
| GET | `/api/flows?type=&status=&stuck=&from=&to=&limit=&offset=` | 登入 | 列表；`status` 可逗號分隔；`from`/`to` 可給日期（台北時間）或 ISO 時間 |
| GET | `/api/agents?role=`、`/api/agents/{id}?result=&from=&to=` | 登入 | agent 狀態卡片與步驟紀錄（含 `error_kind`）；列表與詳情都含 `gateway`。`result`：`succeeded`／`failed`／`running`（含已派出）／`skipped`，其他值回 422 `invalid_result` |
| GET | `/api/stats/summary` | 登入 | 總覽統計、卡住清單、等待確認清單、`gateway`（`ok`／`offline` gateway 離線／`degraded` 頻道異常／`unknown`）。`counts.running` 只算 status=running（不含等待確認），`counts.active`＝running＋waiting_approval；`stuck` 是這兩者的子集 |
| GET | `/api/events` | 登入 | SSE；連上先送 `hello`，之後推 `flow.*`、`step.updated`、`approval.*`、`notification.created` |
| GET | `/api/inbox?recipient=&unread_only=` | 登入或對應的員工 agent | 通知收件匣（0.5）；確認連結只給本人、扮演他的 agent 與 admin 看；標已讀只限本人、扮演他的 agent 與 admin。確認通知帶 `approval_status`（`pending`／`superseded` 已重送舊連結失效／`link_expired`／`decided`／`expired`／`cancelled`）與 `approval_decision`（已決定時的選項文字） |
| GET | `/api/inbox/recipients` | 登入 | 有收件匣的人與未讀數（含 `confirm_mode`） |
| GET | `/api/inbox/confirm-mode?recipient=` | 登入或對應的員工 agent（只能查自己） | 確認者目前由誰確認：`agent`（員工 agent 代理）／`human`（真人）；agent-inbox 每次檢查前先查 |
| PUT | `/api/inbox/confirm-mode?recipient=` | admin | `{mode: agent/human}`，寫稽核 `identity.confirm_mode`；`human` 時扮演他的 agent 讀收件匣拿不到確認連結（課堂 demo 由講師親自確認，見 `docs/flows/visit.md`） |
| POST | `/api/inbox/{id}/read`、`/api/inbox/read-all?recipient=` | 收件人本人、扮演他的 agent 或 admin | 標已讀（viewer 不能標別人的） |
| POST | `/api/sim/messages` | 標記為可模擬的 agent token | 模擬客戶傳訊給 cs-agent（9.5）：記進模擬對話、寫稽核，透過派工介面送出（標示「模擬客戶、external_customer_id=…」）。**只轉送，不解讀內容**，不會建立或決定任何人工確認 |
| GET | `/api/sim/messages?external_customer_id=&since=&limit=` | 標記為可模擬的 agent token | 讀模擬對話（客戶訊息與流程送出的回覆，舊到新） |
| POST | `/api/auth/login`、`/api/auth/logout`；GET `/api/auth/me` | — | session cookie（`fc_session`，HttpOnly、SameSite=Lax） |

錯誤格式統一為 `{"detail": {"code": "...", "message": "..."}}`。常見代碼：`revision_conflict`（409）、`not_allowed_to_start`／`not_involved`（403）、
`token_used`／`token_expired`（410）、`invalid_token`（403）、`step_not_current`（409）、`stale_attempt`（409）。

### revision

`Flow.revision` 由 SQLAlchemy 的 `version_id_col` 管理：每次寫入 +1，並以 compare-and-swap 更新，
兩個同時寫入同一條流程的交易，後到的會失敗。人的操作（取消、重試、重送）必須帶畫面上的 revision，
不符就回 409。agent 回報可以帶 `revision` 或 `attempt`，帶了就檢查。

### 卡住

`stuck` 欄位由後端計算，每條流程只給一個原因：

| code | 判斷 | reason 範例 |
|---|---|---|
| `agent_no_report` | 步驟已派出，超過時限沒有回報 | `admin-agent 已 7 分鐘沒有回報` |
| `approval_waiting_too_long` | 人工確認超過時限的一半 | `等王經理確認草稿，已 2 小時，14:00 截止` |
| `retries_exhausted` | 失敗次數達上限，或 agent 回報 needs_human | `ADM 回 409 時段衝突，已重試 2 次` |
| `system_error` | 連線失敗、5xx、派工失敗（重試用完後） | `產生草稿：ADM 無法連線` |

`waited_seconds` 是已等待秒數。

## 派工給 agent

`OpenClawCliDispatcher` 依 P0 結論（計畫 3.4）執行：

```
openclaw agent --agent <agent id> --session-id wf-<flow_id>-<step>-<attempt> \
  --message-file <暫存檔> --timeout <FLOW_CONTROLLER_DISPATCH_TIMEOUT> --json
```

- 指令開頭固定帶 `flow_id`、`flow_type`、`step`、`attempt`、`Idempotency-Key: {flow_id}:{step}`、`skill`，
  接著是說明、`輸入（JSON）`、回報方式與 output 欄位；格式集中在 `app/dispatch.py` 的 `build_step_message`。
- 每一次派工都用新的 session；模擬客戶訊息（`/api/sim/messages`）同一位客戶沿用 `sim-<agent>-<external_customer_id>`。
- exit 0 且輸出 JSON 的 `status == "ok"` 才算派送成功；`status:"timeout"`、非 0 結束碼都算系統錯誤，依步驟的 `max_attempts` 自動重試。
- payloads（agent 的回覆文字）不當成步驟結果：派工完成不代表步驟完成，agent 要用 `flow-report` 回報，超過時限沒回報就顯示「卡住」。
- 全域同時最多 `FLOW_CONTROLLER_MAX_CONCURRENT_DISPATCH`（3）個，同一隻 agent 一次只跑一個。

## 新增流程

在 `app/flows/<type>.py` 定義 `DEFINITION = FlowDefinition(...)`，加進 `app/flows/__init__.py` 的 `REGISTRY`：

- `AgentStep`：派給 agent；`timeout`（預設 5 分鐘）、`max_attempts`、`on_failure`（失敗回到哪一步，或依失敗內容決定的函式）、
  `route`、`skill`、`inputs`（指令裡的 JSON）、`outputs`（output 欄位，必填的缺少時回報被拒）、
  `transform_output`（status=ok 的 output 存進流程前的整理，例如 onboarding 遮掉清單文字裡的電話；預設不整理）。
- `ApprovalStep`：人工確認；`approver`（回傳 hrm_employee_id）、`question`、`options`、`routes`（決定 → 下一步／`END`／`FAIL`／步驟名稱）、
  `timeout`（可用函式依狀態算截止時間，例如「拜訪前一天 18:00」）、`on_timeout`（expire／fail／stuck）、`context`（確認頁顯示的內容）。
- `WaitStep`：等待外部事件（例如業務回報還車），不主動派工；`reporter` 依狀態決定誰能回報，
  有 `event_type` 時只能用 `POST /api/flows/{id}/events` 完成（`accept_event` 驗證內容）。
- `ControllerStep`：controller 在同一個交易內完成的動作（解析確認對象、發通知），不派工；
  丟 `StepStop` 代表設定問題，步驟停下並通知 admin（Identity 有 admin 帳號連結或 role=admin 的人）。
- `ApprovalStep` 需要人填資料時（例如退款金額）：`fields`（確認頁要填的欄位）＋`accept_decision`（驗證 `data`，結果存進 `state.decisions`）。
- `AgentStep.validate_output`：回報內容的進一步檢查（丟 `ValueError` → 422 `invalid_output`）。
- 失敗紀錄 `state.failures[<step>].history` 保留每次失敗（最多 10 筆），例如 ① 據此排除所有 409 衝突過的車。

`app/flows/demo.py` 是最小範例；`app/flows/visit.py` 是流程 ①、`app/flows/refund.py` 是流程 ②、
`app/flows/onboarding.py` 是流程 ③（步驟契約：`docs/flows/visit.md`、`docs/flows/refund.md`、`docs/flows/onboarding.md`）。

收件匣通知的 `kind`：`approval_request`（確認，含一次性連結）、`approval_expired`、`info`、`alert`（流程停下，需要人處理）、
`task`（指派給員工的工作，例如 ③ 給 IT 人員的「配發筆電」；agent-inbox 只列出，由負責的 skill 處理）。

## 部署

gateway 主機上的 OpenClaw 是主機安裝，容器裡沒有 `openclaw` 指令，所以**正式派工時建議直接在主機上跑**：

```bash
cd <repo>/controller/backend   # 實際路徑依部署而定
uv sync --no-dev
set -a; source ~/.openclaw/secrets/freeforjoy.env; set +a
FLOW_CONTROLLER_DB_URL=sqlite:///$HOME/.openclaw/state/flow-controller/controller.db \
  uv run uvicorn --factory app.main:create_app --host 127.0.0.1 --port 18090 --workers 1
```

（之後可包成 launchd 常駐。）只需要 API、不需要真的派工時，也可以用 Docker：

```bash
docker build -t flow-controller-backend controller/backend
docker run -d --name flow-controller -p 127.0.0.1:18090:18090 \
  -v flow-controller-data:/data -e FLOW_CONTROLLER_DISPATCHER=fake flow-controller-backend
docker exec flow-controller python -m app seed-agents
```

- 只能用 **1 個 worker**（SSE 與背景派工在行程內）。
- 資料表在啟動時自動建立（`create_all`）；沒有導入 Alembic。既有資料表的**新增欄位**寫在 `app/db.py` 的
  `ADDED_COLUMNS`，啟動或 `flow-controller init-db` 時自動 `ALTER TABLE … ADD COLUMN`（只加欄位，不改不刪）。
  目前有：`identities.department`、`approvals.fields`。新的資料表（例如 `channel_messages`）由 `create_all` 建立。
