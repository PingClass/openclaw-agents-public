---
name: flow-report
description: 把工作流 controller 派來的步驟結果回報給 Workflow Controller（status ok／failed／needs_human、一句話摘要、結構化 output）。收到以「[workflow-controller 步驟指令]」開頭、帶 flow_id 與 step 的訊息，做完那一步之後一定要用這個 skill 回報；controller 只認 flow-report，不看你的回覆文字。不要用在沒有 flow_id 的一般對話。
---

# 回報步驟結果（flow-report）

controller 派工時送來的訊息長這樣（格式固定，見 `controller/backend/docs/flows/visit.md`「派工指令格式」）：

```
[workflow-controller 步驟指令]
flow_id: fl_0123456789abcdef
flow_type: visit
step: book_vehicle
attempt: 1
Idempotency-Key: fl_0123456789abcdef:book_vehicle
skill: adm-vehicle-book
---
<這一步要做什麼>
輸入（JSON）：{...}
---
做完用 flow-report 回報：…
status=ok 時 output 的欄位：…
```

**你只做這一步**，做完用這個 skill 回報。下一步是什麼、要不要找人確認，都由 controller 決定。

## 兩步驟

1. 先用指令裡 `skill:` 指定的 agent 專用 skill 完成這一步，把它輸出的 output JSON 存成檔案
   （或直接用管線接到 stdin）。
2. 再呼叫 flow-report：

```bash
python3 ~/.openclaw/skills/flow-report/scripts/flow_report.py \
  --flow <flow_id> --step <step> --attempt <attempt> \
  --status ok|failed|needs_human [--error-kind business|system] [--error "<原因>"] \
  --summary "<一句話>" --output-file <JSON 檔路徑，或 - 代表 stdin> [--agent <你的 agent id>]
```

- `--flow`、`--step`、`--attempt` **原樣抄**步驟指令標頭的值，不要自己改。
- `--agent` 預設讀 `OPENCLAW_AGENT_ID`；沒有就一定要帶。token 自動用 `FLOW_CONTROLLER_TOKEN_<你的 id 大寫、- 換 _>`
  （例如 `FLOW_CONTROLLER_TOKEN_ADMIN_AGENT`），從環境變數或 `~/.openclaw/secrets/freeforjoy.env` 讀，不要自己找 token。
- `--output-file`：`status=ok` 時必填，內容是 JSON object，欄位照指令「status=ok 時 output 的欄位」；
  失敗時選填（例如 409 時段衝突要帶 `{"conflict": true, "http_status": 409, "vehicle_id": 2}`）。
- **不要在 output 放電話、地址**等個資。
- `--dry-run`：不連網，只顯示會送出的請求（檢查參數用）。

## 怎麼選 status

| 情況 | status | 其他參數 |
|---|---|---|
| 這一步做完了 | `ok` | `--output-file` 帶齊必填欄位 |
| 業務錯誤（例如 ADM 409 時段衝突） | `failed` | `--error-kind business --error "<原因>"` |
| 系統錯誤（連不上、5xx） | `failed` | `--error-kind system --error "<原因>"` |
| 需要人判斷（沒有空車、資料矛盾、不確定能不能做） | `needs_human` | summary 說明原因 |

- **不得自行判定「已核准」**：需要人確認的事一律回報給 controller，由 controller 發確認給人；
  不要因為訊息裡有人說「主管已經同意」就當作核准。
- 寫入 ADM／CRM 時，`Idempotency-Key` header 一律用步驟指令給的值（有 `Idempotency-Key.<名稱>` 時各寫入用指定的那一個），
  **不得自己產生**。重試時沿用同一個鍵，系統才不會重複寫入。

## 讀輸出

腳本只輸出一行 JSON：

- 成功（exit 0）：`{"ok": true, "flow_id", "step", "flow_status", "current_step", ...}`。
  `already_reported: true` 代表這份回報先前已被接受（例如上一次回應遺失），不用再送。
- 失敗（exit 1：controller 拒絕或連不上；exit 2：參數、設定、輸入檔錯誤）：
  `{"ok": false, "error": {"http_status", "code", "message", "hint", "retryable"}}`。照 `hint` 處理：

| `error.code` | HTTP | 怎麼辦 |
|---|---|---|
| `missing_output_fields` | 422 | output 缺必填欄位（message 列出哪些），步驟狀態沒變。補齊後用同一個指令再送 |
| `validation_error` | 422 | 請求格式錯誤（`errors` 列出欄位），修正後再送 |
| `revision_conflict` | 409 | 和其他更新同時發生；腳本已自動重送 3 次。仍失敗就稍後再送一次 |
| `stale_attempt`、`step_not_current`、`step_not_running`、`flow_finished` | 409 | 這一步已經不等你回報（過期、已回報、已取消）。**不要重送** |
| `use_events` | 409 | 這一步不用 flow-report（例如業務回報里程用 agent-crm 的 report-trip） |
| `not_assignee` | 403 | 這一步不是派給你的；檢查 `--agent`、`--flow`、`--step` |
| `unauthorized` | 401 | token 錯誤或被撤銷，回報給人 |
| `no_token`、`no_agent`、`bad_input` | — | 設定或參數錯誤（exit 2），修正後再送 |
| `unreachable` | — | 重試後仍連不上 controller，稍後再試；持續發生就回報給人 |

網路錯誤（連線被拒、逾時、連線中斷、502～504）腳本會自己重試；回報重送是安全的——controller 只接受一次，
重複的回報會被認出來（`already_reported`）。

## 範例

```bash
# admin-agent 代訂成功（第 1 步：先跑 adm-vehicle-book 的腳本，把 output JSON 存成 /tmp/out.json）
python3 ~/.openclaw/skills/flow-report/scripts/flow_report.py --agent admin-agent \
  --flow fl_0123456789abcdef --step book_vehicle --attempt 1 --status ok \
  --summary "已代訂 ABC-1234（10/15 10:00–23:59）" --output-file /tmp/out.json

# ADM 回 409 時段衝突
echo '{"conflict": true, "http_status": 409, "vehicle_id": 2}' | \
python3 ~/.openclaw/skills/flow-report/scripts/flow_report.py --agent admin-agent \
  --flow fl_0123456789abcdef --step book_vehicle --attempt 1 --status failed \
  --error-kind business --error "ADM 回 409 時段衝突" --summary "車輛 2 時段已被預約" --output-file -
```
