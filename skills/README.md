# skills：工作流共用 skill 與員工 agent 的模擬營運 skill

這個目錄有兩類 skill，部署時都複製到 gateway 主機的 `~/.openclaw/skills/<skill>/`（所有 agent 共用）：

1. **工作流共用 skill**（計畫 5.1）：助理 agent 和 controller 溝通用的 `flow-start`、`flow-report`，見下一節。
2. **員工 agent 的模擬營運 skill**（計畫 9.3）：`agent-crm`、`agent-adm`、`agent-inbox`、`agent-hrm`，以及 milim 扮客戶的
   [`customer-sim`](customer-sim/README.md)。

## 工作流共用 skill：flow-start、flow-report

| skill | 誰用 | 對應 controller API |
|---|---|---|
| [`flow-start`](flow-start/SKILL.md) | 流程定義 `starters` 列出的助理 agent（`visit`、`refund`：cs-agent） | `POST /api/flows`（type、input、title、dedupe_key） |
| [`flow-report`](flow-report/SKILL.md) | 被 controller 派到步驟的助理 agent | `POST /api/flows/{id}/steps/{step}/result`（status、summary、output、error、error_kind、attempt） |

```bash
python3 ~/.openclaw/skills/flow-start/scripts/flow_start.py --type <visit|refund|onboarding> \
  --input-file <JSON 或 -> [--dedupe-key <key>] [--title <標題>] [--agent <id>] [--dry-run]
#  → {"ok": true, "flow_id": "fl_…", "created": true, "flow_status": "running", …}

python3 ~/.openclaw/skills/flow-report/scripts/flow_report.py --flow <flow_id> --step <step> \
  --status ok|failed|needs_human [--error-kind business|system] [--error <原因>] \
  --summary "<一句話>" --output-file <JSON 或 -> [--attempt <n>] [--agent <id>] [--dry-run]
#  → {"ok": true, "flow_id": "fl_…", "step": "book_vehicle", "flow_status": "running", …}
```

- 兩個 skill **各自獨立**：controller client `scripts/flow_common.py` 在兩個 skill 各放一份完全相同的副本
  （`tests/test_flow.py` 檢查一致），不依賴 `agent-*` 的 `ffj_common.py`。
- token：`FLOW_CONTROLLER_TOKEN_<AGENT>`（agent id 轉大寫、`-` 換 `_`，例如 `FLOW_CONTROLLER_TOKEN_CS_AGENT`）；
  agent id 取 `--agent`，沒帶時讀 `OPENCLAW_AGENT_ID`。環境變數沒有時從 `~/.openclaw/secrets/freeforjoy.env`
  補讀（不覆蓋既有）。`FLOW_CONTROLLER_URL` 寫 `http://127.0.0.1:18090` 或 `…:18090/api` 都可以。
- 失敗時輸出 `{"ok": false, "error": {"http_status", "code", "message", "hint", "retryable"}}`：controller 拒絕或連不上
  exit 1；參數、設定、輸入檔錯誤 exit 2。`code` 是 controller 的錯誤碼（`revision_conflict`、`not_allowed_to_start`、
  `missing_output_fields`、`stale_attempt`、`not_assignee`、`validation_error`……），`hint` 是給 agent 的下一步。
- 重試：連線被拒一律重試。逾時、連線中斷、502～504 時，flow-report 一律重試（controller 只接受一次回報；
  重送遇到 409 會查流程的稽核紀錄，確認是自己先前的回報就回 `already_reported: true`）；flow-start 只在有去重鍵
  （`visit`，或帶 `--dedupe-key`）時重試，避免建出兩條流程。409 `revision_conflict`（和其他更新同時發生）flow-report 自動重送 3 次。
- `--dry-run`：不連網、不需要 token，只顯示會送出的請求。
- agent 專用 skill 的腳本輸出 output JSON →（agent）再用 flow-report 回報，兩步寫在各 SKILL.md。
  步驟契約（每一步的輸入、output 欄位）：`controller/backend/docs/flows/visit.md`（①）、`refund.md`（②）。

### 部署（主 session 執行）

```bash
cd ~/services/openclaw-agents && git pull
for s in flow-start flow-report; do
  rsync -a --delete --exclude tests "skills/$s/" "$HOME/.openclaw/skills/$s/"
done

# 助理 agent 的 controller token（seed-agents 建好 5 隻助理後，各發一把；token 只印一次）
# 存進 ~/.openclaw/secrets/freeforjoy.env：FLOW_CONTROLLER_TOKEN_CS_AGENT、_ADMIN_AGENT、_FINANCE_AGENT、_IT_AGENT、_HR_AGENT
cd ~/services/openclaw-agents/controller/backend
uv run flow-controller issue-agent-token admin-agent    # 已發過的不用重發

# 試跑（不連網）
echo '{"follow_up_id": 1}' | python3 ~/.openclaw/skills/flow-start/scripts/flow_start.py \
  --type visit --input-file - --agent cs-agent --dry-run
echo '{}' | python3 ~/.openclaw/skills/flow-report/scripts/flow_report.py --agent admin-agent \
  --flow fl_test --step draft_vehicle --status needs_human --summary "dry run" --output-file - --dry-run
```

不需要排程：flow-start 由各 agent 自己的排程 skill（例如 cs-agent 的 `crm-visit-list`）呼叫，flow-report 由 controller 派工觸發。

## 員工 agent 的模擬營運 skill


gateway 主機上 7 隻扮演員工的 OpenClaw agent（計畫 `plan/multi-agent-workflows.md` 9.3 方案甲）
用這些 skill 在 ADM、CRM、HRM 產生每天的營運活動，並在工作流裡扮演人工確認者。
做法照既有的 `agent-clock`：API 細節全包在 Python 腳本（只用標準函式庫），agent 只看腳本輸出
JSON 的 `announce` 欄位決定要不要發訊息；決策寫進狀態檔，重跑不重擲骰。

| skill | 腳本 | 誰用 | 用途 |
|---|---|---|---|
| [`agent-crm`](agent-crm/SKILL.md) | `crm.py decide\|visit\|report-trip\|status` | mjolmire-businessman、gobta | 每工作日 30% 排一筆 1～3 個工作天後的客戶拜訪（CRM FollowUp kind=visit）；拜訪結束前向 controller 回報取車／還車里程（流程 ① 步驟 5） |
| [`agent-hrm`](agent-hrm/SKILL.md) | `hrm.py decide\|hire\|status` | shion | 每週一 20% 在 HRM 建立一位下週到職的新人（假名、去識別化；業務部、客服部機率較高），流程 ③ 的資料來源；`hire` 立刻建一位（demo） |
| [`agent-adm`](agent-adm/SKILL.md) | `adm.py decide\|book-room\|status` | 7 隻全部 | 每工作日 10% 預約會議室；luminous 另外 5% 預約公務車（自己用）；409 換時段最多 2 次 |
| [`agent-inbox`](agent-inbox/SKILL.md) | `inbox.py check` | 確認者（見下） | 讀自己的 controller 收件匣，依角色規則核准／退回並寫理由：業務派車 90% 核准；業務主管報帳 >NT$500 時 15% 退回；客服主管退款決定 80/10/10（同意／不退／轉人工）、回覆確認 90% 核准；部門主管對到職清單（③）一律確認收到；其他 95% 核准；`kind=task` 的任務通知只列在輸出的 `tasks` |

`agent-adm` 另有 **`adm.py assign-laptop`**（只有 IT 人員 souei 用）：處理流程 ③ 的 IT 任務——讀自己 controller 收件匣裡
`kind=task` 的通知（或 `--flow` 指定），用 souei 的 ADM session（staff）呼叫 A4 把 it-agent 建議的筆電指派給新人
（`custodian_hrm_employee_id`；已被別人指派就改用備選，新人名下已有筆電視為成功），再 `POST /api/flows/{id}/events`
`{"type": "laptop_assigned", "asset_code"}`、把任務標成已讀。建議與備選都不能用時 `needs_human`（第一次 announce）。
細節見 [`agent-adm/SKILL.md`](agent-adm/SKILL.md)「IT 人員的配發筆電任務」。

共用程式 `scripts/ffj_common.py` 在四個 skill 各有一份**完全相同**的副本（部署時每個 skill 目錄
獨立複製，不能跨目錄 import）。改的時候一起改（助理 agent 的 skill 也有副本），`tests/test_common.py` 會檢查一致。

## 共通行為

- 參數：`--agent <id>`（預設讀 `OPENCLAW_AGENT_ID`）、`--dry-run`（**不連網**、不寫狀態檔、不排程，只顯示決策）
- 輸出：stdout 一行 JSON，含 `ok`、`outcome`、`announce`、`detail`；成功 exit 0，失敗 exit 1（cron 記成 error）
- 網路錯誤重試：連線被拒一律重試；逾時、429、502～504 只在請求是冪等時重試（GET、帶 Idempotency-Key 的 ADM 預約）
- `announce` 只有需要人處理的設定錯誤才是 `true`；日常結果與「連不上系統」都是 `false`（不洗版）
- 狀態檔：`~/.openclaw/state/<skill>/<agent>/…`（可用 `OPENCLAW_STATE_DIR` 改根目錄）

## 環境變數

全部放在 gateway 主機的 `~/.openclaw/secrets/freeforjoy.env`（權限 600），**不寫進 repo**。
腳本先讀環境變數；cron 叫起的 agent 沒繼承到時，會從這個檔補讀（不覆蓋既有變數，
`FREEFORJOY_ENV_FILE=` 設空字串可關閉）。

| 變數 | 誰用 | 說明 |
|---|---|---|
| `CRM_AGENT_PASSWORD` | agent-crm | 員工 agent 的 CRM 密碼（帳號名＝agent id） |
| `ADM_AGENT_PASSWORD` | agent-adm、agent-crm（report-trip 查車輛里程） | 員工 agent 的 ADM 密碼（assign-laptop 用 souei 的帳號，要是 staff） |
| `HRM_AGENT_PASSWORD` | agent-hrm | 員工 agent 的 HRM 密碼（和 `agent-clock` 同一組） |
| `HRM_API_BASE` | agent-hrm | 預設 `http://localhost:18000/api` |
| `HRM_EMPLOYMENT_PATH` | agent-hrm | 選填；建立 Employment 的端點，預設 `/employments/`（HRM 尚未提供，見 `agent-hrm/references/api.md`） |
| `FLOW_CONTROLLER_TOKEN_<AGENT>` | agent-crm（report-trip）、agent-inbox、agent-adm（assign-laptop：`FLOW_CONTROLLER_TOKEN_SOUEI`） | 每隻員工 agent 自己的 controller token，例如 `FLOW_CONTROLLER_TOKEN_MJOLMIRE_BUSINESSMAN`、`FLOW_CONTROLLER_TOKEN_GOBTA`、`FLOW_CONTROLLER_TOKEN_MAIN` |
| `CRM_USER_ID_<AGENT>` | agent-crm | 業務在 CRM 的 user id（`CRM_USER_ID_GOBTA`、`CRM_USER_ID_MJOLMIRE_BUSINESSMAN`）。CRM 的 `/api/me/` 不回 id，第一次建立拜訪前需要；之後會快取 |
| `CRM_API_BASE` | agent-crm | 預設 `http://localhost:18081/api` |
| `ADM_API_BASE` | agent-adm、agent-crm | 預設 `http://localhost:18001/api/v1` |
| `FLOW_CONTROLLER_URL` | agent-crm、agent-inbox | 預設 `http://127.0.0.1:18090` |
| `CRM_WORKSPACE_ID` | agent-crm | 選填；帳號在多個 workspace 時送 `X-Workspace-ID` |
| `AGENT_INBOX_PAUSED` | agent-inbox | 選填；逗號分隔的 agent id 或 `all`，課堂 demo 改由真人確認時用 |
| `OPENCLAW_BIN` | agent-crm | 選填；預設 `openclaw` |
| `FLOW_CONTROLLER_TOKEN_<助理 AGENT>` | flow-start、flow-report | 助理 agent 的 controller token：`FLOW_CONTROLLER_TOKEN_CS_AGENT`、`…_ADMIN_AGENT`、`…_FINANCE_AGENT`、`…_IT_AGENT`、`…_HR_AGENT` |
| `FLOW_SKILL_RETRY_DELAYS` | flow-start、flow-report | 選填；網路重試的等待秒數（逗號分隔，預設 `2,5,10`；測試用 `0,0,0`） |

## 前置條件

1. **種子資料**（另一個工作包）：CRM 的 workspace、員工帳號（membership）、`UserProfile.hrm_employee_id`、客戶；
   ADM 的員工帳號＋PersonLink、會議室、車輛。帳號名稱＝agent id，密碼和上表一致。
2. **controller**：7 隻員工 agent 登記為 employee、各發一把 token、Identity 填 `agent_id`
   （這樣 `GET /api/inbox` 才找得到「這個 token 扮演的人」）：

   ```bash
   cd ~/services/openclaw-agents/controller/backend
   for a in mjolmire-businessman gobta main hinata souei luminous shion; do
     uv run flow-controller upsert-agent "$a" --role employee
     uv run flow-controller issue-agent-token "$a"     # token 只印一次，存成 FLOW_CONTROLLER_TOKEN_<AGENT>
   done
   uv run flow-controller import-identities ../deploy/identities.json   # 流程 ① 工作包（PR #4）提供；每筆要帶 "agent_id"
   ```
3. **流程 ①** 的 `POST /api/flows/{id}/events`（`type=trip_report`）由 controller 的流程 ① 工作包提供。

## 建議排程（只新增，不改既有的打卡排程）

時間都是台北時間、週一到週五。打卡的 `agent-clock decide` 在 08:10 起每 8 分鐘一隻，
上班卡 08:30～09:00、下班卡 18:00～18:30，以下排程刻意錯開。

| 排程 | agent | 時間（cron） | 說明 |
|---|---|---|---|
| 拜訪決策 | mjolmire-businessman | `40 7 * * 1-5` | `crm.py decide`；擲中會自己排拜訪結束前 10～20 分鐘的一次性 `report-trip`（公務車預約結束後 ADM 就不收取車登記） |
| 拜訪決策 | gobta | `44 7 * * 1-5` | 同上 |
| 行政預約決策 | main／shion／souei／gobta／mjolmire-businessman／luminous／hinata | `50 9`、`56 9`、`2 10`、`8 10`、`14 10`、`20 10`、`26 10`（`* * 1-5`） | `adm.py decide`；排在上班打卡之後，避免和 08:30～09:00 的打卡搶 gateway 的並行數 |
| 檢查收件匣 | mjolmire-businessman、gobta、main（流程 ①） | `45 9 * * 1-5` ＋ `15,45 10-17 * * 1-5` | `inbox.py check`，09:45～17:45 每 30 分鐘；三隻各錯開 2 分鐘（`45/47/49`、`15,45/17,47/19,49`） |
| 檢查收件匣 | hinata（②） | `21 9-17 * * 1-5` | 流程 ② 的退款決定、回覆確認（時限各 4 小時，每小時一次即可在過半前處理） |
| IT 任務（配發筆電） | souei（③） | `7 10-17 * * 1-5` | `adm.py assign-laptop`，工作日 10:07～17:07 每小時一次；**另一個排程**，不併入 agent-inbox（souei 在 ③ 沒有人工確認要做，只有 `kind=task`） |
| 新人建立決策 | shion | `50 7 * * 1` | `hrm.py decide`；排在 hr-agent 週一 09:00 的 onboarding-list 掃描之前，建立的下週新人當天就會被掃到（09:00 之後才跑會自動改成下下週到職） |

- 拜訪決策排在 **08:00 之前**：cs-agent 每個工作日 08:00 掃「明天」的拜訪來啟動流程 ①，
  08:00 之後才排的「明天」拜訪會趕不上掃描。腳本有保護：08:00 以後執行 decide 時至少排 2 個工作天後
  （所以排在 08:20 也不會出錯，只是不會有隔天的拜訪）。
- 檢查收件匣每隻每天 17 次 agent 執行，大部分是 `nothing_to_do`。三隻共約 51 次／天，比計畫 9.6 估的
  「3～6 次」多很多；用量吃緊時改成每小時一次（`45 9-17 * * 1-5`），確認最慢晚 1 小時。
- report-trip 的一次性 job 跑完自動刪除，不會累積。

## 部署（主 session 執行）

部署（複製 skill、`openclaw cron add`）避開 08:00–09:40、17:50–18:40 的打卡時段。

```bash
# 1) 在 gateway 主機拉最新的 repo，把三個 skill 複製到 OpenClaw（tests/ 不需要）
cd ~/services/openclaw-agents && git pull
for s in agent-crm agent-adm agent-inbox agent-hrm; do
  rsync -a --delete "skills/$s/" "$HOME/.openclaw/skills/$s/"
done

# 2) 補環境變數到 ~/.openclaw/secrets/freeforjoy.env（見上表），然後試跑（不連網）
python3 ~/.openclaw/skills/agent-crm/scripts/crm.py decide --agent gobta --dry-run
python3 ~/.openclaw/skills/agent-adm/scripts/adm.py decide --agent luminous --dry-run
python3 ~/.openclaw/skills/agent-inbox/scripts/inbox.py check --agent main --dry-run --simulate expense --amount 820
python3 ~/.openclaw/skills/agent-hrm/scripts/hrm.py decide --agent shion --dry-run

# 3) 連線檢查（只讀）
python3 ~/.openclaw/skills/agent-crm/scripts/crm.py status --agent gobta
python3 ~/.openclaw/skills/agent-adm/scripts/adm.py status --agent luminous
python3 ~/.openclaw/skills/agent-hrm/scripts/hrm.py status --agent shion   # hrm.employment_api 必須是 true 才會建新人

# 4) 新增排程（cron delivery 一律 --no-deliver；要不要發訊息由 agent 看 announce 決定）
openclaw cron add --name "拜訪決策 gobta" --agent gobta --session isolated \
  --cron "44 7 * * 1-5" --tz Asia/Taipei --no-deliver --timeout-seconds 300 \
  --message "用 agent-crm skill 做今天的拜訪決策。
執行：python3 ~/.openclaw/skills/agent-crm/scripts/crm.py decide --agent gobta
照 skill 的規則處理輸出：announce 是 true 才發訊息，false 就安靜結束、不要發任何訊息。"

openclaw cron add --name "行政預約決策 luminous" --agent luminous --session isolated \
  --cron "20 10 * * 1-5" --tz Asia/Taipei --no-deliver --timeout-seconds 300 \
  --message "用 agent-adm skill 做今天的行政預約決策。
執行：python3 ~/.openclaw/skills/agent-adm/scripts/adm.py decide --agent luminous
照 skill 的規則處理輸出：announce 是 true 才發訊息，false 就安靜結束、不要發任何訊息。"

openclaw cron add --name "檢查收件匣 main" --agent main --session isolated \
  --cron "19,49 10-17 * * 1-5" --tz Asia/Taipei --no-deliver --timeout-seconds 300 \
  --message "用 agent-inbox skill 處理收件匣裡的待確認。
執行：python3 ~/.openclaw/skills/agent-inbox/scripts/inbox.py check --agent main
照 skill 的規則處理輸出：announce 是 true 才發訊息，false 就安靜結束、不要發任何訊息。"
# 09:45 那一次另外加一筆 --cron "49 9 * * 1-5"（或改用 --cron "*/30 …" 搭配 openclaw 的 stagger 設定）

openclaw cron add --name "新人建立決策 shion" --declaration-key ffj:shion:agent-hrm-decide \
  --agent shion --session isolated \
  --cron "50 7 * * 1" --tz Asia/Taipei --exact --no-deliver --timeout-seconds 300 \
  --message "用 agent-hrm skill 做這週的新人建立決策。
執行：python3 ~/.openclaw/skills/agent-hrm/scripts/hrm.py decide --agent shion
照 skill 的規則處理輸出：announce 是 true 才發訊息，false 就安靜結束、不要發任何訊息。"

# 流程 ③：souei 處理配發筆電任務（每小時一次；沒有任務時不出聲）
openclaw cron add --name "IT 任務 souei" --agent souei --session isolated \
  --cron "7 10-17 * * 1-5" --tz Asia/Taipei --exact --no-deliver --timeout-seconds 300 \
  --declaration-key "agent-adm:assign-laptop:souei" \
  --message "用 agent-adm skill 處理收件匣裡的 IT 任務（新人配發筆電）。
執行：python3 ~/.openclaw/skills/agent-adm/scripts/adm.py assign-laptop --agent souei
照 skill 的規則處理輸出：announce 是 true 才發訊息，false 就安靜結束、不要發任何訊息。"

# 5) 確認
openclaw cron list --json
```

`openclaw cron add` 的週期排程參數名稱（`--cron`、`--tz`）請以 gateway 主機上
`openclaw cron add --help`（OpenClaw 2026.9.2）為準，或照既有「出勤決策」排程的設定
（`openclaw cron list --json`）複製；一次性排程的參數（`--at`、`--delete-after-run`、`--declaration-key`）
和 `agent-clock` 相同，已由腳本處理。

## 測試

```bash
python3 -m unittest discover -s skills/tests -v      # 或：cd skills/tests && uvx pytest -q
python3 -m unittest discover -s skills/customer-sim/tests -v
```

`tests/test_flow.py`（flow-start、flow-report）用假 controller 實際執行腳本，涵蓋：成功、去重、
409 `revision_conflict`（自動重送與持續衝突）、403 `not_allowed_to_start`／`not_assignee`、422 `invalid_input`／
`missing_output_fields`、401、409 `stale_attempt`、503／連線中斷重試、回應遺失後確認已回報、連不上、`--dry-run`、secrets 檔補讀。

agent-* 的測試用 `http.server` 做的假 CRM、ADM、HRM、controller，加上假的 `openclaw` 執行檔，涵蓋：擲骰門檻、重跑不重擲骰、
回應遺失不重複建立（含 HRM 建員工／Employment）、409 換時段最多 2 次、一次性 token 已用／重送、網路錯誤重試、`--dry-run` 不連網也能跑。
只用標準函式庫，Python 3.9 以上。
