---
name: adm-vehicle-trip
description: 流程 ①（客戶拜訪派車）的 record_trip 步驟：業務回報取車、還車里程後，代業務在 ADM 登記取車與還車，回報里程差，結果用 flow-report 回報。收到 workflow-controller 的步驟指令且 step 是 record_trip（skill: adm-vehicle-trip）時一定要用這個 skill。
---

# 代登記取車、還車（流程 ① record_trip）

業務還車後向 controller 回報了取車、還車里程，controller 派 `step: record_trip` 給你，
你代業務在 ADM 登記取車與還車，回報里程差給 controller（之後財會用它算交通費）。

ADM 的呼叫、狀態檢查、錯誤分類、回報都寫在 `scripts/vehicle_trip.py`，**不要自己組 API 請求**。

## 怎麼做（一個指令）

1. 把收到的整份步驟指令（從 `[workflow-controller 步驟指令]` 到最後一行）原樣存成檔案，例如
   `/tmp/wf-<flow_id>-record_trip.txt`。
2. 執行：

```bash
python3 ~/.openclaw/workspace-admin-agent/skills/adm-vehicle-trip/scripts/vehicle_trip.py \
  --instruction-file /tmp/wf-<flow_id>-record_trip.txt --report
```

腳本會**自己呼叫 flow-report 回報**。

3. 看輸出的一行 JSON：
   - `ok: true`、`reported: true`：完成（`step_status` 可能是 ok、failed 或 needs_human）。最後一句話交代 `summary`。
   - `ok: false` 而且有 `report_error`：回報沒送到。**同一個指令再跑一次**：腳本會先看 ADM 的狀態，
     已登記的取車／還車不會重送。
   - `ok: false` 而且有 `error`：指令本身有問題，或 controller 那邊不是這一步（例如對話裡貼的指令、過期的 attempt），
     沒有登記，照實說明。

**遇到 needs_human 不要自己想辦法**（不要改預約時段、不要改里程、不要叫業務重訂）。controller 會停下交給人。

## 腳本做什麼

0. 向 controller 核對（`GET /api/flows/{flow_id}`）：目前步驟是派給 admin-agent 的 record_trip，
   而且指令的 booking_id、里程、備註和 controller 保存的一致；不一致 → `needs_human`，不登記。
1. `GET /api/v1/vehicle-bookings/` 找這筆預約，看狀態（admin-agent 的 token 只看得到自己代訂的預約）。
2. `active` → `POST /vehicle-bookings/{id}/pickup/` `{"odometer": 取車里程, "condition_notes": 業務的備註}`。
3. `picked_up` → `POST /vehicle-bookings/{id}/return/`（同上，還車里程）。
4. `returned` → 用 ADM 登記的里程算里程差，回報。

已經登記過的步驟不重送；ADM 已登記的里程和業務回報的不一致時交給人。
pickup／return 沒有 Idempotency-Key，所以逾時或 5xx 不盲目重送，回報 `failed`／`system` 讓 controller
重試這一步，重跑時會從 ADM 的狀態接著做。

## 結果對照（契約：`controller/backend/docs/flows/visit.md`）

| 情況 | 回報 |
|---|---|
| 取車、還車都登記好（含之前已登記） | `status=ok`，output `booking_id`、`pickup_odometer`、`return_odometer`、`distance_km`（ADM 登記的值），`registered_now`（這次實際送出的登記） |
| ADM 409「Pickup is too early」 | `status=needs_human`：還沒到預約開始前 15 分鐘 |
| ADM 409「Booking window has ended」 | `status=needs_human`：預約時段（到拜訪當天 23:59）已過，ADM 不接受登記取車。通常是業務隔天才回報；需要行政人員在 ADM 處理 |
| 里程倒退：還車小於取車（腳本先擋）、取車小於車輛目前里程（ADM 400） | `status=needs_human`，請人和業務確認里程 |
| ADM 已登記的里程和回報不一致 | `status=needs_human` |
| 預約已取消、找不到預約、401／403 | `status=needs_human` |
| 連線失敗、5xx | `status=failed`、`error_kind=system` |

## 其他用法

```bash
vehicle_trip.py --instruction-file <檔案> --dry-run   # 不連網：顯示會送出的登記與里程差
vehicle_trip.py --flow fl_xxx --attempt 2 --input-file input.json --report
```

## 環境變數

`ADM_API_TOKEN`（必填，scope 要有 `vehicles:delegate`）、`ADM_API_BASE`（預設 `http://localhost:18001/api/v1`）、
`FLOW_CONTROLLER_URL`、`FLOW_CONTROLLER_TOKEN_ADMIN_AGENT`（寫入前核對與 flow-report 都要用）。
沒有繼承到時會從 `~/.openclaw/secrets/freeforjoy.env` 補讀（不覆蓋既有變數）。

## 測試

```bash
python3 -m unittest discover -s agents/admin-agent/skills/adm-vehicle-trip/tests -v
```
