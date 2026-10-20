# 課堂 demo 腳本（流程 ①）

在 gateway 主機上跑；講師手冊（事前檢查、時間表、每一步要講什麼、出錯時的備援、課後 reset）在
`docs/classroom-demo.md`（講師內部文件，未收錄於公開版）。

```bash
cd ~/services/openclaw-agents
python3 controller/deploy/demo/demo.py check --ping   # 課前檢查
python3 controller/deploy/demo/demo.py prepare        # main、gobta 改成真人確認；建今天約 20 分鐘後的拜訪
python3 controller/deploy/demo/demo.py start          # 叫 cs-agent 以示範模式啟動流程（備援：--no-agent）
python3 controller/deploy/demo/demo.py trip           # gobta 回報取車／還車里程
python3 controller/deploy/demo/demo.py status
python3 controller/deploy/demo/demo.py reset          # 課後：切回 agent 代理（--cancel 取消沒跑完的流程）
```

- 每個子指令都有 `--dry-run`（不連網、不寫入）。
- 只用標準函式庫；憑證只從環境變數讀（自動補讀 `~/.openclaw/secrets/freeforjoy.env`），必要的是
  `FLOW_CONTROLLER_ADMIN_PASSWORD`。其他環境變數見 `demo.py` 開頭。
- 測試：`controller/backend/tests/test_demo_script.py`（假派工的 controller＋替身 crm.py／openclaw）。
