# Workflow Controller 前端

計畫 3.7、0.5 的前端（P2 第一批頁面＋P2.5 補完）。Vue 3＋Vite＋TypeScript，工具鏈與 ADM 前端一致（vitest、vue-tsc）。
API 契約見 `../backend/README.md`；前端不改 API 行為。

## 頁面

| 路徑 | 內容 | 登入 |
|---|---|---|
| `/login` | 登入（controller 自己的帳號表） | — |
| `/` | 總覽：gateway 離線／頻道異常提示、卡住清單（置頂、紅色）、今日統計（「進行中」不含等待確認）、等待確認、五隻助理 agent 狀態卡 | 要 |
| `/agents` | 助理 agent 卡片：狀態（閒置／執行中／異常）、正在執行的步驟、今日完成與失敗、平均耗時、最後回報、最近錯誤；gateway 提示 | 要 |
| `/agents/:id` | 這隻 agent 的步驟紀錄（時間、流程、步驟、結果、耗時、錯誤），依結果與日期篩選；條件放在網址上 | 要 |
| `/flows` | 工作流列表：類型、狀態、日期、只看卡住；條件放在網址上 | 要 |
| `/flows/:id` | 卡住原因（卡在哪一步、誰、最後的錯誤、處理建議）、操作（重試這一步／重送確認通知／取消流程）、步驟時間軸、人工確認紀錄、稽核紀錄 | 要 |
| `/inbox` | 通知收件匣（0.5）：admin 可切換任一位確認者；其他帳號看自己綁定的確認者；確認連結只給本人與 admin；已決定、已重送、已過期、已取消的確認顯示狀態、不再給連結 | 要 |
| `/approvals/:id?token=` | 一次性確認頁：手機優先、免登入、只顯示該步驟的 context | 免 |

- **權限**：寫入操作（重試、重送、取消）包在 `AdminOnly` 裡，viewer 連 DOM 都不會出現。每個操作先顯示確認框（說明後果），
  確定後帶畫面上的 `revision` 呼叫 API；後端寫入稽核。revision 不符（別人剛改過）時重新載入並提示再確認一次。
  「看到卡住 → 找到原因 → 重試」：總覽卡住清單 → 詳情的卡住區塊（原因、卡在哪一步、最後的錯誤、建議）→ 正下方的「重試這一步」。
- **示範模式**（課堂投影）：頁首「示範模式」按鈕切換，或網址加 `?demo=1`（`?demo=0` 關閉）；記在 localStorage。
  字放大 125%，導覽只留總覽，工作流詳情只顯示卡住原因與步驟時間軸（隱藏確認紀錄、稽核、操作）。
- **即時更新**（`src/live.ts`）：SSE `GET /api/events`，事件經 300ms 防抖後重抓資料；斷線時改成每 10 秒輪詢，
  每 30 秒試著重連 SSE，連上後停止輪詢並補抓一次。標題列顯示目前是「即時」或「輪詢中」。
- **個資**：後端已過濾電話、地址；前端顯示 context／稽核內容前再用同一套規則過濾一次（`src/format.ts` 的 `sanitize`），
  摘要、錯誤、通知內文等自由文字裡的電話號碼也以 `maskText` 遮掉。

## 開發

```bash
npm ci
# 另一個終端機先起後端（見 ../backend/README.md），預設 proxy 到 http://localhost:18090
npm run dev                 # http://localhost:5173
VITE_API_PROXY_TARGET=http://127.0.0.1:18091 npm run dev   # 後端在別的 port 時
```

## 測試與 build

```bash
npm test            # vitest＋覆蓋率（門檻 lines 80%）
npm run typecheck   # vue-tsc
npm run build       # 型別檢查＋vite build → dist/
```

正式環境由後端同源提供 `dist/`：設定 `FLOW_CONTROLLER_FRONTEND_DIST=<repo>/controller/frontend/dist`（`deploy/run-backend.sh` 已預設）。
