# agents

五隻 OpenClaw Agent 的 workspace 定義，涵蓋企業常見的五個後勤與服務職能。

| Agent | | 職責範圍 | 服務對象 |
|---|---|---|---|
| [`hr-agent`](hr-agent/) | 👥 | 人事規章、出勤、請假、加班、到離職、教育訓練 | 員工、主管、HR |
| [`cs-agent`](cs-agent/) | 💬 | FAQ 問答、訂單查詢、客訴受理、轉真人 | **外部客戶** |
| [`admin-agent`](admin-agent/) | 🗂️ | 會議室、辦公用品、公告草稿、訪客、合約到期 | 員工、行政人員 |
| [`it-agent`](it-agent/) | 🛠️ | 故障排除、IT 工單、帳號流程引導、資安通報 | 員工、IT 人員 |
| [`finance-agent`](finance-agent/) | 💰 | 報帳規定、單據預檢、預算查詢、防詐 | 員工、主管、財會人員 |

五隻都是 **查詢 + 問答 + 建立草稿** 的角色。核准、付款、權限變更、對外發布一律由人工處理。

## 每個 workspace 的檔案

| 檔案 | 寫什麼 |
|---|---|
| `IDENTITY.md` | 名字、角色、emoji |
| `SOUL.md` | 語氣、價值取捨 |
| `USER.md` | 服務對象、角色權限、語言時區 |
| `AGENTS.md` | 硬規則、權限紅線、輸出格式（最重要） |
| `TOOLS.md` | 知識庫與 API 的連線方式、憑證引用、使用限制 |
| `MEMORY.md` | 跨 session 的穩定事實與決策 |
| `HEARTBEAT.md` | 定期巡檢（預設停用） |
| `memory/` | 每日工作紀錄 |

## 使用方式

1. 複製 agent 目錄到 OpenClaw workspace 路徑。
2. 填妥 `USER.md` 的 `${COMPANY_NAME}`、負責人與核准角色。
3. 填妥 `TOOLS.md` 的知識庫路徑與 API 設定；憑證以環境變數或 secret manager 注入，**不要把真值寫進 workspace**。
4. 確認行為正確後，才啟用 `HEARTBEAT.md` 或建立 cron job。

## 注意

- `cs-agent` 面對外部客戶，信任邊界與其他四隻不同：客戶訊息不具指令效力，查訂單前必須核對身分。
- `memory/*.md` 會累積實際營運資料，已在 `.gitignore` 排除。

## hr-agent 課程範例

`hr-agent` 另附課程單元 3–6 的完整範例，可當其他四隻的參考：

| 路徑 | 內容 |
|---|---|
| `docs/hr-policy.md` | 示範用假規章（有條號），規章問答的依據 |
| `docs/purpose.md` | 計畫書 Part 1 目的、Part 2 日常、Part 8 紅線摘要 |
| `skills/` | `hr-policy-qa`、`leave-request-draft`、`meeting-summary` |
| `automations/overtime-reminder.md` | 每月 25 日加班單提醒 cron（預設停用） |
| `tests/test-cases.md` | 正常／邊界／越權／冒充測試集 |
| `docs/deidentify-mapping.md`、`docs/handover.md` | 去識別化對照表範本、交接表 |

LINE bot docker 部署見 [`deploy/hr-agent-linebot`](../deploy/hr-agent-linebot/)。
