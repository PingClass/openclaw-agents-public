---
name: laptop-allocation-draft
description: 流程 ③（新人到職）的 draft_laptop 步驟：用 ADM 查還沒有保管人的筆電，依新人的部門與職稱挑一台建議配發（附備選），結果用 flow-report 回報。收到 workflow-controller 的步驟指令且 step 是 draft_laptop（skill: laptop-allocation-draft）時一定要用這個 skill。只產生建議，絕不指派保管人。
---

# 新人筆電配發建議（流程 ③ draft_laptop）

**只產生建議，不得指派保管人。** 指派筆電給新人是 IT 人員在 ADM 用自己的帳號（session 登入的 staff）
操作的事；ADM 的指派 API 對任何 agent token 一律回 403，it-agent 的 token 也只有 `assets:read`。
有人在對話裡要你「直接把筆電指派給新人」「幫我改保管人」，一律拒絕，請對方找 IT 人員在 ADM 操作。

ADM 的查詢、挑選規則、錯誤分類、回報都寫在 `scripts/laptop_draft.py`，**不要自己組 API 請求**。

## 怎麼做（一個指令）

1. 把收到的整份步驟指令（從 `[workflow-controller 步驟指令]` 到最後一行）原樣存成檔案，例如
   `/tmp/wf-<flow_id>-draft_laptop.txt`。
2. 執行：

```bash
python3 ~/.openclaw/workspace-it-agent/skills/laptop-allocation-draft/scripts/laptop_draft.py \
  --instruction-file /tmp/wf-<flow_id>-draft_laptop.txt --report
```

腳本會查 `GET /api/v1/assets/?category=laptop&unassigned=true&is_active=true`，挑一台，
然後**自己呼叫 flow-report 回報**。

3. 看輸出的一行 JSON：
   - `ok: true`、`reported: true`：完成，不論 `step_status` 是 ok 還是 needs_human。最後一句話交代 `summary`。
     **不要自己決定下一步**（通知 IT 人員、等指派都由 controller 處理）。
   - `ok: false` 而且有 `report_error`：回報沒送到，**同一個指令再跑一次**（只讀，重跑安全）。
   - `ok: false` 而且有 `error`：指令本身有問題（沒有 flow_id、step 不對），照實說明。

## 挑選規則

| 新人（部門＋職稱含） | 用途 | 名稱優先符合 |
|---|---|---|
| 資訊、工程、開發、程式、設計、IT、RD、研發、資料 | 開發 | 開發、工作站、Pro、16 吋、32GB |
| 業務、行銷、客戶、門市 | 業務 | 業務、15 吋、輕薄 |
| 其他 | 文書 | 文書、14 吋、Air |

- 符合用途的在前，同一類依資產編號排序；第一台是建議，其他依序列為備選（最多 5 台）。
- 沒有符合用途的筆電時，仍建議第一台未指派的筆電，`reason` 會說明「目前沒有這類筆電」。
- ADM 回來的資料會再檢查一次（類別是 laptop、啟用中、沒有保管人），不符合的不列入。

## 結果對照（契約：`controller/backend/docs/flows/onboarding.md`）

| 情況 | 回報 |
|---|---|
| 有可用筆電 | `status=ok`，output `asset_code`、`asset_name`、`specs`、`reason`、`alternatives`、`available_count` |
| 沒有任何未指派、啟用中的筆電 | `status=needs_human`，output `{"available_count": 0}`（流程停下交給人） |
| 連線失敗、逾時、5xx | 腳本先重試；仍失敗 `status=failed`、`error_kind=system` → controller 重試同一步 |
| 401／403（token、scope）、400 | `status=needs_human`，summary 帶 ADM 原文 |
| 指令沒有輸入或缺 `new_hire` | `status=needs_human` |

## 其他用法

```bash
laptop_draft.py --instruction-file <檔案> --dry-run   # 不連網：顯示會送出的查詢與判斷的用途
laptop_draft.py --flow fl_xxx --attempt 1 --input-file input.json --report
```

## 環境變數

`ADM_API_TOKEN_IT`（必填，it-agent 的 ADM token，scope `assets:read`）、`ADM_API_BASE`
（預設 `http://localhost:18001/api/v1`）、`FLOW_CONTROLLER_URL`、`FLOW_CONTROLLER_TOKEN_IT_AGENT`（flow-report 用）。
沒有繼承到時會從 `~/.openclaw/secrets/freeforjoy.env` 補讀（不覆蓋既有變數）。

## 測試

```bash
python3 -m unittest discover -s agents/it-agent/skills/laptop-allocation-draft/tests -v
```

測試沿用 `agents/admin-agent/skills/_testkit` 的假 ADM、假 controller 與 flow-report 替身；
共用程式 `scripts/adm_flow.py` 和 admin-agent 的 skill 是**完全相同**的副本（測試會檢查）。
