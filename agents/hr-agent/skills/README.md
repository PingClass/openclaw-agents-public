# hr-agent 的 skill

hr-agent 在工作流裡負責的步驟（計畫 `plan/multi-agent-workflows.md` 5.2、2 ③）。
步驟契約見 `controller/backend/docs/flows/onboarding.md`。

| skill | 流程 | 觸發 | 做什麼 |
|---|---|---|---|
| [`onboarding-list`](onboarding-list/SKILL.md) | ③ 步驟 1 | cron，每週一 09:00 | 查 HRM 下週一到週日到職的新人，每位用 `flow-start` 啟動 onboarding 流程；沒有新人時安靜結束 |
| [`onboarding-notice`](onboarding-notice/SKILL.md) | ③ 步驟 5 `onboarding_notice` | controller 派工 | HRM 查部門主管（H4），產生到職清單（不含電話地址），再用 `flow-report` 回報；沒有主管時照實回報 `[]` |

課程範例 skill（單元 3–6，LINE 對話用；純提示詞、不呼叫 API，依據是 `docs/hr-policy.md`）：

| skill | 觸發 | 做什麼 |
|---|---|---|
| [`hr-policy-qa`](hr-policy-qa/SKILL.md) | 員工問人事規定 | 依 `docs/hr-policy.md` 回答並附條號；查不到回「規章未載明」 |
| [`leave-request-draft`](leave-request-draft/SKILL.md) | 員工要請假／加班／補休 | 產生申請草稿，不送出、不核准 |
| [`meeting-summary`](meeting-summary/SKILL.md) | 貼上會議紀錄 | 整理成決議／待辦／待確認問題 |

做法和 cs-agent 的 skill 一樣：只用 Python 標準函式庫，API 細節全包在腳本，輸出一行 JSON，
`--dry-run` 不連網，HRM 的 GET 遇到連線失敗、逾時、429、502～504 自動重試。兩個 skill 都**只讀** HRM。

`scripts/ffj_common.py`（和 `skills/agent-crm` 的副本相同）與 `scripts/hr_common.py`（HRM service token client、
步驟指令解析、呼叫共用 skill）在兩個 skill 各有一份**完全相同**的副本——部署時每個 skill 目錄獨立，不能跨目錄 import。
改的時候一起改，測試會檢查一致。

## 依賴

- 共用 skill `flow-start`、`flow-report`（`~/.openclaw/skills/`）：
  - `flow_start.py --type onboarding --input-file - --dedupe-key hrm:employee:<id> --agent hr-agent` → `{ok, flow_id, created}`
  - `flow_report.py --flow <id> --step onboarding_notice --status ok|failed|needs_human [--error-kind system] --summary "…" --output-file - --attempt <n> --agent hr-agent`
- HRM service token `HRM_API_TOKEN`（HRM PR #2 的 H1～H5；scope `employee:read`、`department:read`；契約 HRM `docs/agent-service-api.md`）
- controller 的 agent token `FLOW_CONTROLLER_TOKEN_HR_AGENT`；hr-agent 是 onboarding 流程唯一的 starter
- controller 的流程 ③（PR #11，`app/flows/onboarding.py`）

## 環境變數

全部放在 gateway 主機的 `~/.openclaw/secrets/freeforjoy.env`（不寫進 repo）；腳本在環境變數沒設時會從這個檔補讀。

| 變數 | 誰用 | 說明 |
|---|---|---|
| `HRM_API_TOKEN` | 兩個 skill | hr-agent 的 HRM service token（`issue_service_credential` 發，只印一次） |
| `HRM_API_BASE` | 兩個 skill | 選填，預設 `http://localhost:18000/api`（寫成 `http://localhost:18000` 也可以） |
| `FLOW_CONTROLLER_URL` | flow-start、flow-report | controller |
| `FLOW_CONTROLLER_TOKEN_HR_AGENT` | flow-start、flow-report | hr-agent 的 controller token |
| `FLOW_SKILLS_DIR` | 兩個 skill | 選填，共用 skill 的根目錄，預設 `~/.openclaw/skills` |
| `OPENCLAW_AGENT_ID` | 兩個 skill | 選填；沒給 `--agent` 時用它，再預設 `hr-agent` |

## 部署（主 session 執行）

前提：HRM PR #2 已合併部署並發好 `HRM_API_TOKEN`；controller PR #11 已上線。

```bash
# 1) 整個 workspace 複製到 OpenClaw（tests/ 不需要）
cd ~/services/openclaw-agents && git pull
rsync -a --exclude 'memory/' --exclude 'tests/' agents/hr-agent/ ~/.openclaw/workspace-hr-agent/

# 2) 試跑（不連網）
python3 ~/.openclaw/workspace-hr-agent/skills/onboarding-list/scripts/onboarding_list.py --agent hr-agent --dry-run

# 3) 連線檢查（只讀 HRM；那一週沒有新人時不會啟動任何流程）
python3 ~/.openclaw/workspace-hr-agent/skills/onboarding-list/scripts/onboarding_list.py --agent hr-agent --week-of 2026-01-05
```

### 建議排程

每週一 09:00（台北）、isolated session、`--no-deliver`（要不要說話由 agent 看 `announce` 決定）：

```bash
openclaw cron add --name "新人到職掃描 hr-agent" --declaration-key ffj:hr-agent:onboarding-list \
  --agent hr-agent --session isolated \
  --cron "0 9 * * 1" --tz Asia/Taipei --exact --no-deliver --timeout-seconds 300 \
  --message "用 onboarding-list skill 查下週到職的新人，並啟動到職流程。
執行：python3 ~/.openclaw/workspace-hr-agent/skills/onboarding-list/scripts/onboarding_list.py --agent hr-agent
照 skill 的規則處理輸出：announce 是 false 就安靜結束、不要發任何訊息；announce 是 true 才在最終輸出列出被跳過或失敗的新人與原因。"
```

- shion 建立新人（`skills/agent-hrm` 的 `decide`）排在**同一天 07:50**，比這個掃描早，建立的下週新人當天就會被掃到。
- `onboarding-notice` 不用排程，由 controller 派工（`openclaw agent --agent hr-agent --session-id wf-<flow_id>-onboarding_notice-<attempt> …`）。
- 參數名稱以 gateway 主機上 `openclaw cron add --help`（OpenClaw 2026.9.2）為準；`--declaration-key` 的前綴照既有排程用 `ffj:`。

## 測試

```bash
for d in agents/hr-agent/skills/*/tests; do python3 -m unittest discover -s "$d" -v; done
```

用 `http.server` 做的假 HRM（Bearer token、到職日篩選含邊界、`status`、分頁、主管端點、寫入 403）與假 controller
（只有 hr-agent 能啟動、去重鍵固定 `hrm:employee:<id>`、必填 output、`stale_attempt`）；共用 skill 直接用 repo 裡的
`skills/flow-start`、`skills/flow-report`。涵蓋：下週範圍（週一、週三、週日執行，含邊界、排除這週／下下週／離職）、
沒有新人不出聲、重跑不重複啟動、沒有部門跳過並回報、token 錯誤、HRM 重試、沒主管照契約回報 `[]`（status=ok）、
多位主管全部回報、清單不含 email／電話／地址、部門不存在 needs_human、HRM 掛掉 failed/system、過期 attempt、
指令檢查、`--dry-run` 不連網。兩個測試目錄分開跑（各有一份 `fake_services.py`）。Python 3.9 以上。
