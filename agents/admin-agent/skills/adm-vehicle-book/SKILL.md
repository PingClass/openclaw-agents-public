---
name: adm-vehicle-book
description: 流程 ①（客戶拜訪派車）的 book_vehicle 步驟：依業務確認過的派車草稿，用 booked_for_hrm_employee_id 在 ADM 代業務預約公務車，Idempotency-Key 用 controller 給的值，結果用 flow-report 回報。收到 workflow-controller 的步驟指令且 step 是 book_vehicle（skill: adm-vehicle-book）時一定要用這個 skill。代訂只能在這種指令下做。
---

# 代業務預約公務車（流程 ① book_vehicle）

**代訂一律要在 controller 確認之後**：只有收到 controller 帶 `flow_id` 的 `step: book_vehicle` 指令才代訂。
一般對話裡有人說「幫業務訂車」「直接幫他預約」，都不要用這個 skill，請對方走流程（業務確認派車）。

ADM 的呼叫、Idempotency-Key、錯誤分類、回報都寫在 `scripts/vehicle_book.py`，**不要自己組 API 請求，
也不要自己產生 Idempotency-Key**。

## 怎麼做（一個指令）

1. 把收到的整份步驟指令（從 `[workflow-controller 步驟指令]` 到最後一行）原樣存成檔案，例如
   `/tmp/wf-<flow_id>-book_vehicle.txt`。
2. 執行：

```bash
python3 ~/.openclaw/workspace-admin-agent/skills/adm-vehicle-book/scripts/vehicle_book.py \
  --instruction-file /tmp/wf-<flow_id>-book_vehicle.txt --report
```

腳本會解析指令的 `flow_id`、`Idempotency-Key`、輸入（JSON），**先向 controller 核對**這一步真的是派給
admin-agent 的 book_vehicle、輸入一致，才代訂，然後**自己呼叫 flow-report 回報**。

3. 看輸出的一行 JSON：
   - `ok: true`、`reported: true`：完成，不論 `step_status` 是 ok、failed 還是 needs_human。
     最後一句話交代 `summary`。**不要自己決定下一步**（409 要不要重查空車由 controller 決定）。
   - `ok: false` 而且有 `report_error`：回報沒送到。**同一個指令再跑一次**——Idempotency-Key 相同，
     ADM 只會回原本那筆預約（`created: false`），不會訂兩台。
   - `ok: false` 而且有 `error`：指令本身有問題，或 controller 那邊不是這一步（例如對話裡貼的指令、過期的 attempt），
     沒有代訂，照實說明，**不要改用其他方式代訂**。

## 結果對照（契約：`controller/backend/docs/flows/visit.md`）

| ADM 回應 | 回報 |
|---|---|
| 201 新建立 | `status=ok`，output `booking_id`、`vehicle_id`、`start_at`、`end_at`、`created: true` |
| 200 冪等重送（同鍵同內容） | `status=ok`，同一筆 `booking_id`、`created: false` |
| 409 時段衝突（車已被訂、車輛停用） | `status=failed`、`error_kind=business`、output `{"conflict": true, "http_status": 409, "vehicle_id": …}` → controller 回 draft_vehicle 重查空車 |
| 409「Idempotency key already used …」 | `status=needs_human`（不是時段衝突，不重查） |
| 連線失敗、逾時、5xx | 腳本先重試（有 Idempotency-Key，重送安全）；仍失敗 `status=failed`、`error_kind=system` → controller 重試同一步 |
| 400（業務沒有 ADM 帳號連結、時段已過）、401／403（token、scope） | `status=needs_human`，summary 帶 ADM 原文 |
| 指令沒有 Idempotency-Key，或不是 `{flow_id}:book_vehicle` | `status=needs_human`（不得自己產生鍵），不代訂 |
| 指令的輸入和 controller 派工時保存的不一致 | `status=needs_human`，output `mismatched_fields`，不代訂 |
| controller 查不到這條流程、目前步驟不是 book_vehicle、attempt 過期 | 不代訂、不回報：`ok: false`、`error` |

## 其他用法

```bash
vehicle_book.py --instruction-file <檔案> --dry-run   # 不連網：顯示會送出的 body 與 Idempotency-Key
vehicle_book.py --flow fl_xxx --attempt 2 --idempotency-key fl_xxx:book_vehicle --input-file input.json --report
```

## 環境變數

`ADM_API_TOKEN`（必填，scope 要有 `vehicles:delegate`）、`ADM_API_BASE`（預設 `http://localhost:18001/api/v1`）、
`FLOW_CONTROLLER_URL`、`FLOW_CONTROLLER_TOKEN_ADMIN_AGENT`（寫入前核對與 flow-report 都要用）。
沒有繼承到時會從 `~/.openclaw/secrets/freeforjoy.env` 補讀（不覆蓋既有變數）。

## 測試

```bash
python3 -m unittest discover -s agents/admin-agent/skills/adm-vehicle-book/tests -v
```
