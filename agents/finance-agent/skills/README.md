# finance-agent 的 skill

部署時整個 workspace 複製到 `~/.openclaw/workspace-finance-agent`，skill 在 `skills/<name>/`。

| skill | 流程步驟 | 腳本 |
|---|---|---|
| [`mileage-claim-draft`](mileage-claim-draft/SKILL.md) | ① `draft_claim`：里程差 × 費率，產生交通費報帳草稿 | `mileage_claim.py` |
| [`refund-assessment`](refund-assessment/SKILL.md) | ② `assess_refund`：退款評估（推估），寫 CRM 案件內部備註 kind=assessment | `refund_assessment.py` |
| [`refund-decision-note`](refund-decision-note/SKILL.md) | ② `record_decision`：把客服主管的決定寫進 CRM 案件內部備註 kind=decision | `refund_decision_note.py` |

步驟契約：`controller/backend/docs/flows/visit.md`（①）、`controller/backend/docs/flows/refund.md`（②）。

## ② 兩個 skill 的共通行為

- 參數：`--instruction-file <controller 的整份步驟指令|->`（或分開給 `--flow`、`--attempt`、`--idempotency-key`、
  `--input-file`／`--input-json`）、`--report`（做完自己呼叫 flow-report）、`--dry-run`（不連網、不寫入、不回報）、`--agent`。
- 輸出：stdout 一行 JSON。`ok`＝腳本完成（有 `--report` 時含回報成功），`step_status`／`error_kind`／`summary`／`output`
  是回報給 controller 的步驟結果。
- **寫入前向 controller 核對**（同 admin-agent）：用 finance-agent 自己的 controller token `GET /api/flows/{flow_id}`，
  確認目前步驟就是派給 finance-agent、執行中、attempt 相同，且指令的輸入和 controller 保存的一致。
- **同一條流程只寫一筆備註**：先讀同類備註，自己寫過就沿用；沒有才帶 controller 給的 Idempotency-Key POST。
  兩個 skill 都可以安全重跑。
- 只寫 CRM 案件內部備註（`case_note`），不寫互動紀錄、不改案件、不聯絡客戶。
- 共用程式 `scripts/finance_flow.py` 兩個 skill 各一份**完全相同**的副本，改的時候兩份一起改（測試會檢查）。

## 測試

假 CRM、假 controller 在 `_testkit/`（不是 skill，部署時可不複製）；回報走 repo 裡真的 `skills/flow-report`。

```bash
python3 -m unittest discover -s agents/finance-agent/skills/mileage-claim-draft/tests -v
python3 -m unittest discover -s agents/finance-agent/skills/refund-assessment/tests -v
python3 -m unittest discover -s agents/finance-agent/skills/refund-decision-note/tests -v
# 或一次跑完
uvx pytest -q agents/finance-agent/skills
```
