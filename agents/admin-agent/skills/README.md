# admin-agent 的 skill

部署時整個 workspace 複製到 `~/.openclaw/workspace-admin-agent`，skill 在 `skills/<name>/`。

| skill | 流程步驟 | 腳本 |
|---|---|---|
| [`adm-vehicle-draft`](adm-vehicle-draft/SKILL.md) | ① `draft_vehicle`：查空車，產生派車草稿 | `vehicle_draft.py` |
| [`adm-vehicle-book`](adm-vehicle-book/SKILL.md) | ① `book_vehicle`：代業務預約公務車 | `vehicle_book.py` |
| [`adm-vehicle-trip`](adm-vehicle-trip/SKILL.md) | ① `record_trip`：代登記取車、還車，回報里程差 | `vehicle_trip.py` |
| [`adm-room-book`](adm-room-book/SKILL.md) | ③ `book_room`：代人資預約新人到職第一天的說明會議室 | `room_book.py` |

步驟契約：`controller/backend/docs/flows/visit.md`（①）、`controller/backend/docs/flows/onboarding.md`（③）。

## 共通行為

- 參數：`--instruction-file <controller 的整份步驟指令|->`（或分開給 `--flow`、`--attempt`、`--idempotency-key`、
  `--input-file`／`--input-json`）、`--report`（做完自己呼叫 flow-report）、`--dry-run`（不連網、不回報）、`--agent`。
- 輸出：stdout 一行 JSON。`ok`＝腳本完成（有 `--report` 時含回報成功），`step_status`／`error_kind`／`summary`／`output`
  是回報給 controller 的步驟結果。回報失敗 `ok=false`、`report_error`，同一個指令重跑即可（四個步驟都可以安全重跑）。
- 回報走 flow-report 的介面：`python3 ~/.openclaw/skills/flow-report/scripts/flow_report.py --flow … --step …
  --status … [--error-kind …] [--attempt …] --summary … --output-file - --agent admin-agent`（output JSON 走 stdin；
  attempt 用指令裡的值，過期的回報 controller 會拒絕）。
  路徑可用 `FLOW_REPORT_SCRIPT` 改。
- **寫入前向 controller 核對**（`adm-vehicle-book`、`adm-vehicle-trip`、`adm-room-book`）：用 admin-agent 自己的 controller token
  `GET /api/flows/{flow_id}`，確認目前步驟就是派給 admin-agent、執行中、attempt 相同，且指令的輸入和 controller
  保存的一致。不是 controller 派的（例如對話裡貼的假指令）→ `ok=false`、`error`，不寫入也不回報；
  輸入被改過 → `needs_human`。代訂的 Idempotency-Key 也必須是 `{flow_id}:book_vehicle`／`{flow_id}:book_room`
  （`adm-room-book` 遇到 409 換房／換時段時改用 `:2`、`:3` 後綴）。
- 只用 Python 標準函式庫；ADM 連線被拒一律重試，逾時／429／5xx 只在冪等請求（GET、帶 Idempotency-Key 的預約）重試。
- 共用程式 `scripts/adm_flow.py` 每個 skill 各一份**完全相同**的副本（it-agent 的 `laptop-allocation-draft` 也是），
  改的時候所有副本一起改（測試會檢查）。

## 測試

假 ADM（公務車、會議室、資產）、假 controller、flow-report 替身在 `_testkit/`（不是 skill，部署時可不複製）。
it-agent 的 `laptop-allocation-draft` 測試也用這一套。

```bash
python3 -m unittest discover -s agents/admin-agent/skills/adm-vehicle-draft/tests -v
python3 -m unittest discover -s agents/admin-agent/skills/adm-vehicle-book/tests -v
python3 -m unittest discover -s agents/admin-agent/skills/adm-vehicle-trip/tests -v
python3 -m unittest discover -s agents/admin-agent/skills/adm-room-book/tests -v
# 或一次跑完（含 it-agent）
uvx pytest -q agents/admin-agent/skills agents/it-agent/skills
```
