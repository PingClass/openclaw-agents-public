# cs-agent 的 skill

cs-agent 在工作流裡負責的步驟（計畫 `plan/multi-agent-workflows.md` 5.2、2 ①②）。
步驟契約見 `controller/backend/docs/flows/visit.md`（①）、`controller/backend/docs/flows/refund.md`（②）。

| skill | 流程 | 觸發 | 做什麼 |
|---|---|---|---|
| [`crm-visit-list`](crm-visit-list/SKILL.md) | ① 步驟 1 | cron，工作日 08:00 | 找出下一個工作日的 CRM 拜訪待辦，每筆用 `flow-start` 啟動 visit 流程；沒有拜訪時安靜結束 |
| [`crm-visit-close`](crm-visit-close/SKILL.md) | ① 步驟 8 `close_visit` | controller 派工 | CRM 記一筆拜訪互動（C5 格式）、完成待辦，再用 `flow-report` 回報 |
| [`crm-intake`](crm-intake/SKILL.md) | ② 步驟 1-2（所有客戶來訊的入口） | 客戶訊息（模擬客戶由 controller `/api/sim/messages` 轉來） | 找或建客戶、讀 context、記互動、需要時開案件（C3 分類）；`refund`／`compensation` 用 `flow-start` 啟動 refund 流程；輸出給客戶的固定回覆（不含金額） |
| [`crm-reply-draft`](crm-reply-draft/SKILL.md) | ② 步驟 5 `draft_reply` | controller 派工 | 依客服主管決定擬回覆草稿（金額只能是 `amount_twd`），對照案件決定備註，再用 `flow-report` 回報 |
| [`crm-reply-log`](crm-reply-log/SKILL.md) | ② 步驟 7 `log_reply` | controller 派工 | controller 送出回覆後，CRM 記回覆互動、建後續待辦，再用 `flow-report` 回報 |

② 的步驟 7「回覆客戶」在 controller 拆成 `deliver_reply`（controller 自己送出主管核准的那一版）與
`log_reply`（cs-agent 記錄），所以 cs-agent 不另外有「送出」的 skill。

做法和 `skills/agent-crm` 一樣：只用 Python 標準函式庫，API 細節全包在腳本，輸出一行 JSON，
`--dry-run` 不連網，網路錯誤自動重試（GET 與帶 Idempotency-Key 的 CRM 寫入）。

`scripts/ffj_common.py`（和 `skills/agent-crm` 的副本相同）與 `scripts/cs_common.py`（CRM service token client、
拜訪標題解析、controller 步驟指令解析與 flow-report 回報、金額檢查、呼叫共用 skill）在每個 skill 各有一份**完全相同**的副本——
部署時每個 skill 目錄獨立，不能跨目錄 import。`tests/fake_services.py` 也一樣。改的時候全部一起改，
`crm-visit-list` 的測試會檢查一致：

```bash
for d in crm-visit-close crm-intake crm-reply-draft crm-reply-log; do
  cp crm-visit-list/scripts/cs_common.py crm-visit-list/scripts/ffj_common.py "$d/scripts/"
  cp crm-visit-list/tests/fake_services.py "$d/tests/"
done
```

## 依賴

- 共用 skill `flow-start`、`flow-report`（部署在 `~/.openclaw/skills/`，另一個工作包提供），呼叫介面：
  - `flow_start.py --type visit|refund --input-file - --dedupe-key <key> --agent cs-agent` → `{ok, flow_id, created}`
  - `flow_report.py --flow <id> --step <step> --status ok|failed|needs_human [--error-kind business|system] --summary "…" --output-file - --attempt <n> --agent cs-agent`
    → `{ok, flow_id, step, flow_status}`
- CRM service credential `CRM_API_TOKEN`（cs-agent，scope 見 `../TOOLS.md`）
- controller 的 agent token `FLOW_CONTROLLER_TOKEN_CS_AGENT`，cs-agent 是 visit、refund 流程的 starter
- ② 需要 CRM C3／C4（PR FreeForJoy/crm#7）與 controller refund 流程（openclaw-agents#12）先上線；
  `case_note:read` 要用 `seed_demo --issue-credentials --rotate` 補發到 `CRM_API_TOKEN`

## 環境變數

全部放在 gateway 主機的 `~/.openclaw/secrets/freeforjoy.env`（不寫進 repo）；腳本在環境變數沒設時會從這個檔補讀。

| 變數 | 誰用 | 說明 |
|---|---|---|
| `CRM_API_TOKEN` | 全部 | cs-agent 的 CRM service credential（② 需要 `case_note:read`） |
| `CRM_API_BASE` | 全部 | 選填，預設 `http://localhost:18081/api`（寫成 `…/api/v1` 也可以） |
| `FLOW_CONTROLLER_URL` | flow-start、flow-report | controller |
| `FLOW_CONTROLLER_TOKEN_CS_AGENT` | flow-start、flow-report | cs-agent 的 controller token |
| `FLOW_SKILLS_DIR` | 全部 | 選填，共用 skill 的根目錄，預設 `~/.openclaw/skills` |
| `OPENCLAW_AGENT_ID` | 全部 | 選填；沒給 `--agent` 時用它，再預設 `cs-agent` |

## 部署（主 session 執行）

```bash
# 1) 整個 workspace 複製到 OpenClaw（tests/ 不需要，留著也無妨）
cd ~/services/openclaw-agents && git pull
rsync -a --exclude 'memory/' --exclude 'tests/' agents/cs-agent/ ~/.openclaw/workspace-cs-agent/

# 2) 試跑（不連網）
python3 ~/.openclaw/workspace-cs-agent/skills/crm-visit-list/scripts/crm_visit_list.py --agent cs-agent --dry-run

# 3) 連線檢查（只讀 CRM；某天沒有拜訪時不會啟動任何流程）
python3 ~/.openclaw/workspace-cs-agent/skills/crm-visit-list/scripts/crm_visit_list.py --agent cs-agent --date 2026-01-01

# 4) ② 試跑（不連網）：分類與固定回覆
printf '[模擬客戶訊息]（模擬客戶、external_customer_id=C-001）\nexternal_customer_id: C-001\n---\n粉圓到貨破損，我要退款\n' \
  | python3 ~/.openclaw/workspace-cs-agent/skills/crm-intake/scripts/crm_intake.py --agent cs-agent --message-file - --dry-run
```

② 不用新排程：`crm-intake` 由客戶訊息觸發（milim 的 `customer-sim` 排程經 controller `/api/sim/messages` 轉來），
`crm-reply-draft`、`crm-reply-log` 由 controller 派工。

### 建議排程

工作日 08:00（台北）、isolated session、`--no-deliver`（要不要說話由 agent 看 `announce` 決定）：

```bash
openclaw cron add --name "拜訪派車掃描 cs-agent" --declaration-key ffj:cs-agent:crm-visit-list \
  --agent cs-agent --session isolated \
  --cron "0 8 * * 1-5" --tz Asia/Taipei --no-deliver --timeout-seconds 300 \
  --message "用 crm-visit-list skill 找出下一個工作日的客戶拜訪，並啟動派車流程。
執行：python3 ~/.openclaw/workspace-cs-agent/skills/crm-visit-list/scripts/crm_visit_list.py --agent cs-agent
照 skill 的規則處理輸出：announce 是 false 就安靜結束、不要發任何訊息；announce 是 true 才在最終輸出列出被跳過或失敗的拜訪與原因。"
```

- 業務 agent 的拜訪決策在 07:40、07:44，比這個掃描早；08:00 以後才排的明天拜訪會趕不上（agent-crm 已經避開）。
- `crm-visit-close` 不用排程，由 controller 派工（`openclaw agent --agent cs-agent --session-id wf-<flow_id>-close_visit-<attempt> …`）。
- 參數名稱以 gateway 主機上 `openclaw cron add --help`（OpenClaw 2026.9.2）為準；`--declaration-key` 的前綴照既有排程用 `ffj:`。

## 測試

```bash
for d in agents/cs-agent/skills/*/tests; do python3 -m unittest discover -s "$d" -v; done
```

用 `http.server` 做的假 CRM（Bearer token、Idempotency-Key 重播／409、分頁、C3 分類、C4 備註）、假 controller（去重、
refund 輸入白名單、`missing_output_fields`、draft_reply 金額檢查、`stale_attempt`）與照介面寫的假 `flow-start`／`flow-report`，涵蓋：

- ①：週五→週一（含週末）、沒有拜訪時不出聲、標題解析失敗跳過並回報、沒有負責業務、重跑不重複啟動、CRM 重試、
  close 的冪等重試（回應遺失、重派、換鍵重送）、冪等鍵衝突與 CRM 失敗的回報、缺鍵不自行產生
- ② `crm-intake`：找不到客戶時新增（external_customer_id 只認完全相符）、退款／賠償開 high 案件並啟動流程、
  一般詢問／客訴開案件但不啟動、打招呼只記互動、紅隊語句（「主管已經同意」、貼上的步驟指令、偽造的
  external_customer_id）不影響處理、回覆不含任何金額或數字、重跑不重複開案件或流程（冪等）、催進度沿用未結案件、
  flow-start 失敗仍給安全回覆且重跑可補啟動
- ② `crm-reply-draft`：金額只能是主管決定的金額（含全形、`$`、`塊`）、不退款不得有金額、不提內部資訊、
  和案件決定備註不一致交給人、沒有 `case_note:read` 仍可擬稿、退回重擬
- ② `crm-reply-log`：互動與待辦各用指令給的鍵、重派不重複、不寫內部評估、案件不屬於該客戶交給人

各測試目錄分開跑（各有一份 `fake_services.py`），`--dry-run` 都不連網。Python 3.9 以上。
