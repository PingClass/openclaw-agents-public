# openclaw-agents

用 [OpenClaw](https://github.com/openclaw/openclaw) 做的企業後勤 Agent 範例：五隻部門助理 agent、串起它們的多 agent 工作流 controller，以及把 agent 接上 LINE bot 的 docker 部署。

課程教材用途。範例公司「FreeForJoy」與裡面的員工（milim、shion、souei…）、客戶、規章、資料全部是虛構的。

## 目錄

| 目錄 | 內容 |
|---|---|
| [`agents/`](agents/) | 五隻 agent 的 workspace（hr、cs、admin、it、finance）：身分、規則、權限紅線、skills |
| [`skills/`](skills/) | 所有 agent 共用的 skill：工作流 `flow-start`／`flow-report`，以及模擬員工、客戶的營運 skill |
| [`controller/`](controller/) | 多 agent 工作流 controller：後端（FastAPI＋SQLite）、前端（Vue）、部署與課堂 demo 腳本 |
| [`deploy/hr-agent-linebot/`](deploy/hr-agent-linebot/) | 用 docker 把 hr-agent 跑成 LINE bot |

從 [`agents/README.md`](agents/README.md) 開始看。`hr-agent` 附有課程單元 3–6 的完整範例（假規章、skill、cron、測試集、交接表）。

## 注意

- 這是從內部開發 repo 整理出的公開快照。文件裡提到的 `plan/`、`docs/classroom-demo.md` 是講師內部的計畫與上課手冊，沒有收錄。
- 「gateway 主機」指跑 OpenClaw gateway 與 controller 的那台機器；路徑、port、secrets 檔位置都是範例，請換成自己的環境。
- 所有憑證都由環境變數或 secrets 檔注入，repo 裡沒有任何真實 token。
- `deploy/hr-agent-linebot` 的設定範本預設**開放任何人私訊**，只給自己測試時請先改成 `pairing`。

## License

[MIT](LICENSE)
