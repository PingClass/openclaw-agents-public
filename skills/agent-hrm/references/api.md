# agent-hrm 的 API 細節

只有要改 `scripts/hrm.py` 時才需要讀。HRM 的 session、CSRF 做法見 HRM repo 的 `docs/agent-clock-api.md`。

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `HRM_API_BASE` | `http://localhost:18000/api` | HRM 後端（寫 `http://localhost:18000` 也可以） |
| `HRM_AGENT_PASSWORD` | —（必填） | 員工 agent 的 HRM 密碼，帳號名＝agent id（和 `agent-clock` 同一組） |
| `HRM_EMPLOYMENT_PATH` | `/employments/` | 建立 Employment 的端點（HRM 補上時若路徑不同，改這個就好） |
| `OPENCLAW_STATE_DIR` | `~/.openclaw/state` | 狀態檔根目錄 |
| `FREEFORJOY_ENV_FILE` | `~/.openclaw/secrets/freeforjoy.env` | 環境變數沒設時從這個檔補讀（不覆蓋既有變數）；設成空字串可關閉 |

## 登入（session）

1. `POST /api/auth/login/` `{"username","password"}`（登入本身不需要 CSRF），回 200 與帳號資料
   （`role`、`company`、`employee`），`Set-Cookie` 給 `sessionid`、`csrftoken`
2. 之後的 POST 帶 `X-CSRFToken: <csrftoken cookie>`
3. 只有 `role` 是 `hr`（或 `admin`）能建立員工（HRM `EmployeePermission`）；shion 由 `seed_org_roles` 設成 hr

## 建立新人

```http
GET  /api/departments/?page_size=200          # 依代碼 ORG-SALES／ORG-CS／ORG-IT／ORG-ADMIN／ORG-HR 找部門（找不到再比名稱）
GET  /api/employees/?department=<部門 UUID>&page_size=1000   # 重跑時翻完該部門，用員工編號找回已建立的新人
                                                # （HRM 員工 API 沒有 search，帶了會被忽略）
POST /api/employees/
{"company": "<登入回應的 company>", "department": "<部門 UUID>", "employee_number": "NH-20261021-3F2A",
 "last_name": "林", "first_name": "宜庭", "email": "", "phone": "", "status": "active"}
GET  /api/employees/{id}/                       # start_date 有值＝主要 Employment 已建立
POST /api/employments/                          # ← HRM 目前沒有，見下一節
{"employee": "<員工 UUID>", "employment_type": "full_time", "start_date": "2026-10-21", "is_primary": true}
```

- HRM 對 session 寫入**沒有** Idempotency-Key，所以 POST 不在送出後重試（連線被拒才重試）；
  冪等靠狀態檔＋在該部門的員工清單裡比對員工編號（員工編號在公司內唯一，萬一還是重送也只會 400，不會多一位）。
- `employment_type`：`full_time` 85%、`part_time` 10%、`contractor` 5%。
- 部門權重：業務部 35、客服部 30、資訊部 12、管理部 12、人資部 11。
- 員工的 `status` 用 `active`：hr-agent 的 onboarding-list 查 `status=active`。

## 待 HRM 補上：Employment API

2026-10-10 查核 HRM（含 PR #2 `feat/H1-H5-service-token`）：`Employment` 只有 model 與 `EmploymentSerializer`
（`fields="__all__"`），**沒有任何 API 端點**，只能用 seed 指令或 `manage.py shell` 建立（HRM 沒有註冊任何 Django admin model）。沒有 Employment 的員工
`start_date` 是 `null`，hr-agent 的 `GET /api/employees/?start_date_after=…` 查不到，流程 ③ 不會啟動。

建議 HRM 補一個 `EmploymentViewSet`（`router.register("employments", …)`）：

| 項目 | 建議 |
|---|---|
| 端點 | `GET/POST /api/employments/`（`?employee=<uuid>` 篩選）；`GET/PATCH /api/employments/{id}/` |
| 權限 | 建立、修改：session 使用者 role `hr`／`admin`（同 `EmployeePermission`）；service token 一律 403（不加進 `credential_scopes`） |
| 多租戶 | `employee` 必須屬於使用者的公司（不是就 400） |
| 欄位 | `employee`、`employment_type`、`start_date`、`end_date`、`is_primary`（沿用 `EmploymentSerializer`） |
| 稽核 | `AuditLog` action `employment.created`（metadata：`employee_id`、`start_date`、`is_primary`） |

腳本在建立員工**之前**先 `GET /api/employments/?page_size=1`：回 404／405 就輸出 `employment_api_missing`
（`announce=true`），不建立任何資料，避免留下沒有到職日的半套新人。HRM 補上後不用改腳本
（路徑不同時設 `HRM_EMPLOYMENT_PATH`）。

在 HRM 補上之前，`decide`／`hire` 都只會回 `employment_api_missing`、不建立任何資料；demo 需要新人時，由講師在 HRM 主機用 `manage.py shell` 建立員工與主要 Employment（`is_primary=True`），再手動跑 onboarding-list。
