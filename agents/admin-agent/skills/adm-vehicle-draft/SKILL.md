---
name: adm-vehicle-draft
description: 流程 ①（客戶拜訪派車）的 draft_vehicle 步驟：查 ADM 空車，依人數挑車，產生派車草稿並用 flow-report 回報，不預約。收到 workflow-controller 的步驟指令且 step 是 draft_vehicle（skill: adm-vehicle-draft）時一定要用這個 skill。
---

# 派車草稿（流程 ① draft_vehicle）

controller 派來 `step: draft_vehicle` 的指令時，用這個 skill 查空車、挑一台車、把草稿回報給 controller。
**只產生草稿，不預約**；預約要等業務在 controller 確認後，由 `book_vehicle` 步驟（`adm-vehicle-book`）做。

ADM 的呼叫、挑車規則、回報都寫在 `scripts/vehicle_draft.py`，**不要自己組 API 請求，也不要自己挑車**。

## 怎麼做（一個指令）

1. 把收到的整份步驟指令（從 `[workflow-controller 步驟指令]` 到最後一行）原樣存成檔案，例如
   `/tmp/wf-<flow_id>-draft_vehicle.txt`。
2. 執行：

```bash
python3 ~/.openclaw/workspace-admin-agent/skills/adm-vehicle-draft/scripts/vehicle_draft.py \
  --instruction-file /tmp/wf-<flow_id>-draft_vehicle.txt --report
```

腳本會解析指令裡的 `flow_id`、`step`、輸入（JSON），查 ADM、挑車，然後**自己呼叫 flow-report 回報**
（`~/.openclaw/skills/flow-report/scripts/flow_report.py`）。你不用再另外回報。

3. 看輸出的一行 JSON：
   - `ok: true`、`reported: true`：完成。最後一句話交代 `summary` 就好（例如「派車草稿：CAR-01（五人座轎車）…」）。
   - `ok: false` 而且有 `report_error`：回報沒送到 controller。**同一個指令再跑一次**（這一步只查不寫，可以安全重跑）。
   - `ok: false` 而且有 `error`：指令本身有問題（不是 draft_vehicle、沒有 flow_id）。不要執行，照實說明。

## 業務退回重排（輸入的 `sales_comment` 不是 null）

腳本看不懂自然語言意見，你依意見加參數再跑：

| 業務意見 | 加的參數 |
|---|---|
| 要大一點的車、要坐 6 個人 | `--min-seats 6` |
| 指定要某台車（CAR-02、DEMO-0002） | `--prefer CAR-02`（沒空就照一般規則） |
| 不要某台車 | `--exclude CAR-03`（可重複） |

意見和車無關（例如「時間改下午」）時照常跑，不要自己改預約時段；時段由 controller 給。

## 腳本做什麼

| 項目 | 規則 |
|---|---|
| 查空車 | `GET /api/v1/vehicles/availability/?start_at=<booking_start_at>&end_at=<booking_end_at>&seats=<seats>` |
| 預約時段 | 一律用輸入的 `booking_start_at`／`booking_end_at`（拜訪開始 → 拜訪當天 23:59），不是拜訪時段 |
| 挑車 | 排除 `exclude_vehicle_ids`（上次 409 衝突的車）→ 座位數夠的車裡選座位最少的 → 同座位依車輛代碼 |
| 沒有車 | `status=needs_human`，summary 說明哪個時段、幾人座、排除了哪些車 |
| ADM 連不上／5xx | 重試 3 次後 `status=failed`、`error_kind=system`（controller 會重試這一步） |
| 缺 `ADM_API_TOKEN` 等設定 | `status=needs_human`，summary 說明缺什麼 |

status=ok 的 output（欄位照 `controller/backend/docs/flows/visit.md`）：

| 欄位 | 說明 |
|---|---|
| `vehicle_id` | ADM 車輛 id |
| `vehicle_code` | 車輛代碼（例如 `CAR-01`） |
| `vehicle_name`、`seats`、`plate_number` | 車名、座位數、車牌（選填） |
| `start_at`、`end_at` | 等於輸入的 `booking_start_at`／`booking_end_at` |
| `destination`、`purpose` | 照輸入 |
| `available_count` | 這個時段可用的車數（給人參考） |

## 其他用法

```bash
# 只看結果、不回報（除錯用；之後要回報就加 --report 重跑）
vehicle_draft.py --instruction-file <檔案>
# 試跑：不連網、不回報
vehicle_draft.py --instruction-file <檔案> --dry-run
# 不用整份指令，分開給（手動補跑時）
vehicle_draft.py --flow fl_xxx --attempt 1 --input-file input.json --report
```

## 環境變數

`ADM_API_TOKEN`（必填）、`ADM_API_BASE`（預設 `http://localhost:18001/api/v1`）、
flow-report 用的 `FLOW_CONTROLLER_URL`、`FLOW_CONTROLLER_TOKEN_ADMIN_AGENT`。
沒有繼承到時會從 `~/.openclaw/secrets/freeforjoy.env` 補讀（不覆蓋既有變數）。
`--agent` 預設讀 `OPENCLAW_AGENT_ID`，再預設 `admin-agent`。
`FLOW_REPORT_SCRIPT` 可改 flow-report 腳本路徑。

## 測試

```bash
python3 -m unittest discover -s agents/admin-agent/skills/adm-vehicle-draft/tests -v
```
