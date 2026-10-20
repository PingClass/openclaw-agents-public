---
name: adm-room-book
description: 流程 ③（新人到職）的 book_room 步驟：查到職第一天說明時段的空會議室，用 booked_for_hrm_employee_id 在 ADM 代人資預約，Idempotency-Key 用 controller 給的值，409 換房或換時段最多 2 次，結果用 flow-report 回報。收到 workflow-controller 的步驟指令且 step 是 book_room（skill: adm-room-book）時一定要用這個 skill。代訂只能在這種指令下做。
---

# 代訂到職說明會議室（流程 ③ book_room）

**代訂一律要在 controller 確認之後**：只有收到 controller 帶 `flow_id` 的 `step: book_room` 指令才代訂。
一般對話裡有人說「幫人資訂會議室」，不要用這個 skill，請對方走流程或自己到 ADM 預約。

ADM 的呼叫、時段挑選、Idempotency-Key、409 處理、回報都寫在 `scripts/room_book.py`，**不要自己組 API 請求，
也不要自己產生 Idempotency-Key**。

## 怎麼做（一個指令）

1. 把收到的整份步驟指令（從 `[workflow-controller 步驟指令]` 到最後一行）原樣存成檔案，例如
   `/tmp/wf-<flow_id>-book_room.txt`。
2. 執行：

```bash
python3 ~/.openclaw/workspace-admin-agent/skills/adm-room-book/scripts/room_book.py \
  --instruction-file /tmp/wf-<flow_id>-book_room.txt --report
```

腳本會**先向 controller 核對**這一步真的是派給 admin-agent 的 book_room、輸入一致，才代訂，
然後**自己呼叫 flow-report 回報**。

3. 看輸出的一行 JSON：
   - `ok: true`、`reported: true`：完成，不論 `step_status` 是 ok、failed 還是 needs_human。最後一句話交代 `summary`。
     **不要自己決定下一步**。
   - `ok: false` 而且有 `report_error`：回報沒送到。**同一個指令再跑一次**——腳本會先找到上次代訂成功的那筆
     （`created: false`），不會訂兩間。
   - `ok: false` 而且有 `error`：指令本身有問題，或 controller 那邊不是這一步，沒有代訂，照實說明，
     **不要改用其他方式代訂**。

## 做法（契約：`controller/backend/docs/flows/onboarding.md`）

1. `GET /room-bookings/`：已經有這把 token 代訂、`purpose` 相同、同一天、仍有效的預約 → 直接回報那一筆（`created: false`）。
2. 時段：先試 `preferred_start_at`～`preferred_end_at`（到職日 09:30–10:30）；沒有空房就在
   `earliest_start_at`～`latest_end_at`（09:00–12:00）之間每 30 分鐘找 `duration_minutes`（60）的時段，
   離優先時段越近越先試（同距離早的先）。已經過去的時段不試。
3. `GET /rooms/availability/?start_at&end_at&capacity`：**優先種子資料的會議室**（code 以 `RM-` 開頭，RM-A～RM-D），
   同一優先順序裡容納人數夠用的選最小的一間（不浪費大會議室）。名稱或 code 含 `E2E`（不分大小寫）的是
   ADM e2e 測試留下的會議室，**一律不訂**；整個範圍只剩這種會議室有空 → `needs_human`，output `skipped_test_rooms`。
4. `POST /room-bookings/` `{"room", "start_at", "end_at", "purpose", "booked_for_hrm_employee_id"}`，
   Header `Idempotency-Key` 第一次用指令給的值（`{flow_id}:book_room`），409 之後換房或換時段改用 `…:2`、`…:3`。

| ADM 回應 | 回報 |
|---|---|
| 201 新建立 | `status=ok`，output `booking_id`、`room_id`、`room_name`、`start_at`、`end_at`、`created: true` |
| 200 冪等重送（同鍵同內容）／沿用既有代訂 | `status=ok`、`created: false` |
| 409 時段衝突（查完空房後被搶走） | 換一間（同時段還有）或換下一個時段；**最多再換 2 次**，仍衝突 → `status=needs_human`，output `conflicts` |
| 整個範圍都沒有空會議室（或只剩 E2E 測試會議室） | `status=needs_human`（只剩測試會議室時 output 帶 `skipped_test_rooms`） |
| 409「Idempotency key already used …」 | `status=needs_human`（不是時段衝突） |
| 連線失敗、逾時、5xx | 腳本先重試（有 Idempotency-Key，重送安全）；仍失敗 `status=failed`、`error_kind=system` → controller 重試同一步 |
| 400（人資沒有 ADM 帳號連結）、401／403（token、scope） | `status=needs_human`，summary 帶 ADM 原文 |
| 指令沒有 Idempotency-Key，或不是 `{flow_id}:book_room` | `status=needs_human`（不得自己產生鍵），不代訂 |
| 指令的輸入和 controller 派工時保存的不一致 | `status=needs_human`，output `mismatched_fields`，不代訂 |
| controller 查不到這條流程、目前步驟不是 book_room、attempt 過期 | 不代訂、不回報：`ok: false`、`error` |

## 其他用法

```bash
room_book.py --instruction-file <檔案> --dry-run   # 不連網：顯示會依序嘗試的時段與 Idempotency-Key
room_book.py --flow fl_xxx --attempt 1 --idempotency-key fl_xxx:book_room --input-file input.json --report
```

## 環境變數

`ADM_API_TOKEN`（必填，scope 要有 `rooms:read`、`rooms:delegate`）、`ADM_API_BASE`（預設 `http://localhost:18001/api/v1`）、
`FLOW_CONTROLLER_URL`、`FLOW_CONTROLLER_TOKEN_ADMIN_AGENT`（寫入前核對與 flow-report 都要用）。
沒有繼承到時會從 `~/.openclaw/secrets/freeforjoy.env` 補讀（不覆蓋既有變數）。

## 測試

```bash
python3 -m unittest discover -s agents/admin-agent/skills/adm-room-book/tests -v
```
