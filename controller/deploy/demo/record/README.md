# 流程 ① 備援錄影

計畫 8 R2：課堂網路或 LINE 不穩時，用這支錄影代替現場示範。影片長約 2 分半（1280x720），內容：

| 段落 | 畫面 | 說明條 |
|---|---|---|
| 標題卡 | 流程 ①：客戶拜訪 → 派車 → 出車 → 報帳 | — |
| 總覽 | 示範模式（`?demo=1`）的總覽、助理 agent 狀態卡 | 總覽 |
| 啟動 | cs-agent 啟動流程，總覽即時出現「進行中 1」 | 啟動 |
| 時間軸 | 工作流詳情，每一步完成就捲到目前步驟 | 時間軸 |
| 人工確認 1／2 | Mjolmire 的收件匣 → 手機尺寸（390 寬）的一次性確認頁 → 核准派車 | 收件匣／人工確認 |
| 時間軸 | 代訂車輛、回報里程、登記取車還車、報帳草稿 | 時間軸 |
| 人工確認 2／2 | Main 的收件匣 → 手機確認頁 → 核准報帳 | 收件匣／人工確認 |
| 完成 | 時間軸全部完成 → 財會（Luminous）收到入帳通知 → 總覽「今日完成 1」 → 結尾卡 | 完成 |

畫面上只有假資料（`客戶 C（假資料）`、`FAKE-002（假車輛）`、員工 agent 的代號），沒有電話、地址或真實個資。
說明條與標題卡是錄影時注入在頁面上的（`record.py` 的 `OVERLAY_JS`），不改前端程式。

## 重錄

在 repo 根目錄（本機 Mac 即可，不需要 gateway 主機）：

```bash
controller/deploy/demo/record/record.sh                 # 輸出到 controller/deploy/demo/record/out/
controller/deploy/demo/record/record.sh ~/Movies/demo   # 指定輸出目錄
PACE=9 controller/deploy/demo/record/record.sh          # 每個 agent 步驟停久一點（預設 7 秒）
KEEP_TMP=1 controller/deploy/demo/record/record.sh      # 保留暫存 DB、controller 與 fake_agents 的 log
```

需要：`uv`、`node`／`npm`（前端沒 build 過時會自動 `npm ci && npm run build`）、網路（第一次要下載 Chromium 與套件）。
輸出：`flow1-visit-demo.webm`，以及用 `imageio-ffmpeg` 內附的 ffmpeg 轉出的 `flow1-visit-demo.mp4`（H.264，投影電腦較好播）。
影片**不進 repo**（`.gitignore`），另外存放（例如雲端硬碟、講師筆電）。

## 做了什麼

1. `record.sh`：建暫存目錄與 sqlite、挑隨機 port、只綁 `127.0.0.1`，以 `FLOW_CONTROLLER_DISPATCHER=fake` 起一個臨時 controller
   （掛上 `controller/frontend/dist`，不檢查 OpenClaw gateway）；建一個隨機密碼的臨時 admin 帳號、`seed-agents`。
   結束（含失敗、Ctrl-C）時一律停掉 controller、刪掉暫存目錄。
2. `record.py`：Playwright（Chromium headless，1280x720）先用 API 登入（影片不出現登入頁），再依上表錄影。
   開始錄後叫起 `scripts/fake_agents.py --setup-local --manual-approvals --pace 7`：
   - `--pace`：每個 agent 步驟停幾秒才回報，時間軸看得出逐步前進；
   - `--manual-approvals`：兩次人工確認不由假 agent 代按，而是錄影腳本在手機尺寸的確認頁上輸入意見、按下核准。
   手機畫面是一個同源的框頁（Playwright 攔截 `/__demo_phone__`），框裡的 iframe 是 390 寬的真實確認頁。
3. 流程沒有以 `succeeded` 結束、fake_agents 回非 0、或中途 Ctrl-C，腳本就回非 0，且不會寫出（或覆蓋舊的）影片；
   mp4 轉檔失敗時刪掉不完整的 mp4，只留 webm 並提示。
4. 外部 shell 帶進來的 `FLOW_CONTROLLER_*` 環境變數一律先清掉，臨時 controller 只吃 `record.sh` 自己設的值。

**不要**把 `fake_agents.py` 指向正式的 controller（gateway 主機上 `dispatcher=openclaw` 的那個）；`record.sh` 一律自己起臨時的。
