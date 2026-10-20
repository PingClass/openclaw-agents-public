---
name: onboarding-notice
description: 流程 ③（新人到職）步驟 5 onboarding_notice：用 HRM 查新人的部門主管，產生給主管的到職清單（不含電話、地址），再用 flow-report 回報 controller。收到「[workflow-controller 步驟指令]」且 step 是 onboarding_notice、skill 是 onboarding-notice 時一定要用這個 skill。
---

# 到職清單（流程 ③ 步驟 5）

controller 用 `openclaw agent` 派來一則步驟指令，標頭長這樣：

```
[workflow-controller 步驟指令]
flow_id: fl_0123456789abcdef
flow_type: onboarding
step: onboarding_notice
attempt: 1
Idempotency-Key: fl_0123456789abcdef:onboarding_notice
skill: onboarding-notice
---
...
輸入（JSON）：
{ "new_hire": {...}, "department_id": "...", "expected_manager": {...}, "hr": {...}, "laptop": {...}, "orientation": {...} }
---
...
```

所有事情——解析指令、查 HRM、組清單、回報——都在 `scripts/onboarding_notice.py` 裡。
**不要自己組 HRM 請求，也不要自己挑主管。**

## 怎麼做（一個指令）

把收到的步驟指令**全文原封不動**交給腳本，加 `--report` 讓它做完直接用 `flow-report` 回報：

```bash
python3 ~/.openclaw/workspace-hr-agent/skills/onboarding-notice/scripts/onboarding_notice.py \
  --agent hr-agent --report --message-file - <<'STEP_MESSAGE'
[workflow-controller 步驟指令]
flow_id: …
（從標頭到最後一行，照貼，不要改任何字）
STEP_MESSAGE
```

腳本會：

1. HRM `GET /api/departments/{department_id}/managers/`（`HRM_API_TOKEN`，scope `department:read`）
2. 查到幾位主管就回報幾位：`manager_hrm_employee_ids`；**查不到就回 `[]`，仍然 `status=ok`**——
   controller 會停下並通知人資，**你不要自己挑一個人當主管，也不要改寄給別人**
   （`expected_manager` 只是 controller 的參考，不能拿來取代 HRM 的結果）
3. 產生到職清單 `checklist`（全文）與 `items`（項目）：姓名、員工編號、部門、職稱、到職日、筆電資產編號、
   到職說明的會議室與時間、部門主管、人資窗口、主管第一週要做的事
4. 呼叫 `~/.openclaw/skills/flow-report/scripts/flow_report.py` 回報，並帶上指令的 `attempt`

**到職清單不含電話、地址**（新人與任何人的都不行），也不放 email——HRM 的主管端點會回 email，腳本不會用它。
自由文字（姓名、會議室名稱、職稱）裡像電話或地址的片段會被換成「［已移除］」。

## 看輸出

腳本只吐一行 JSON：

| 欄位 | 意思 |
|---|---|
| `reported: true` | 已回報 controller，這一步結束。最終輸出照抄 `detail` 一句話即可，不要發其他訊息 |
| `outcome` | `notice_ready`（有主管）、`no_manager`（HRM 查不到主管，已照實回報 `[]`）、`step_needs_human`、`step_failed` |
| `report_status` | 回報的狀態：`ok`；`needs_human`（部門不存在、HRM 拒絕 token、沒有設定 `HRM_API_TOKEN`）；`failed`＋`error_kind=system`（連不上 HRM、5xx，controller 會重試） |
| `reported: false`、`announce: true` | 清單做好了但 flow-report 送不出去：**原指令再跑一次**（只讀 HRM，重跑安全）；還是失敗就在最終輸出照抄 `report_error` |
| `outcome: config_error` | 指令缺 flow_id、step 不是 onboarding_notice、輸入缺 department_id、找不到 flow-report skill（這種情況沒有回報 controller），在最終輸出說明原因 |

`needs_human`／`failed`／`no_manager` 都已經回報給 controller，由 controller 決定重試、停下或通知人資；
你只要在最終輸出說一句發生什麼事，**不要自己決定下一步，也不要聯絡其他 agent 或主管**。

## 除錯

不加 `--report` 時，腳本只查 HRM、組清單，輸出 `report_status`、`summary`、`output` 與一行 `next` 提示，之後自己用 flow-report 回報。
沒有步驟指令全文時，也可以用 `--flow <flow_id> --attempt <n> --input-file <輸入 JSON>`。
`--dry-run` 只解析指令、顯示會查的端點，不連網、不回報。

環境變數：`HRM_API_TOKEN`、`HRM_API_BASE`（選填，預設 `http://localhost:18000/api`）、`FLOW_CONTROLLER_URL`、
`FLOW_CONTROLLER_TOKEN_HR_AGENT`（由 flow-report 使用）、`FLOW_SKILLS_DIR`（選填，預設 `~/.openclaw/skills`）。
