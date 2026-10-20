# it-agent 的 skill

部署時整個 workspace 複製到 `~/.openclaw/workspace-it-agent`，skill 在 `skills/<name>/`。

| skill | 流程步驟 | 腳本 |
|---|---|---|
| [`laptop-allocation-draft`](laptop-allocation-draft/SKILL.md) | ③ `draft_laptop`：查未指派的筆電，依新人職務產生配發建議（不指派） | `laptop_draft.py` |

步驟契約：`controller/backend/docs/flows/onboarding.md`。

## 共通行為

和 admin-agent 的 skill 相同（見 [`agents/admin-agent/skills/README.md`](../../admin-agent/skills/README.md)）：

- 參數：`--instruction-file <controller 的整份步驟指令|->`（或分開給 `--flow`、`--attempt`、`--input-file`／`--input-json`）、
  `--report`（做完自己呼叫 flow-report）、`--dry-run`（不連網、不回報）、`--agent`（預設 `OPENCLAW_AGENT_ID`，再預設 `it-agent`）。
- 輸出：stdout 一行 JSON（`ok`、`step_status`、`error_kind`、`summary`、`output`）。
- 共用程式 `scripts/adm_flow.py` 是 admin-agent skill 的**完全相同**副本；改的時候所有副本一起改
  （`agents/admin-agent/skills/_testkit` 的測試會檢查）。

## 權限紅線

- it-agent 的 ADM token（`ADM_API_TOKEN_IT`）只有 `assets:read`。
- **指派保管人只能由 IT 人員在 ADM 操作**（ADM A4 `POST /api/v1/assets/{code}/assign/` 僅限 session 登入的 staff，
  任何 agent token 一律 403）。skill 腳本只送 GET，測試會檢查。

## 測試

```bash
python3 -m unittest discover -s agents/it-agent/skills/laptop-allocation-draft/tests -v
# 或和 admin-agent 的 skill 一起跑
uvx pytest -q agents/admin-agent/skills agents/it-agent/skills
```
