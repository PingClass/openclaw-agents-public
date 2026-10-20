# Controller 部署（gateway 主機）

controller 後端跑在 gateway 主機的 host 上（不是 docker），因為派工要呼叫 host 上的 `openclaw` CLI（見計畫 3.4 的 P0 結論）。
前端 build 成靜態檔，由同一個後端行程同源提供（`FLOW_CONTROLLER_FRONTEND_DIST`），不另外起 nginx。

```bash
cd ~/services/openclaw-agents && git pull   # 不放 ~/Documents：launchd 受 macOS TCC 限制讀不到
cd controller/backend && uv sync --no-dev
cd ../frontend && npm ci && npm run build   # 產生 controller/frontend/dist（型別檢查＋vite build）
cp ../deploy/ai.freeforjoy.flow-controller.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.freeforjoy.flow-controller.plist   # 首次
launchctl kickstart -k gui/$(id -u)/ai.freeforjoy.flow-controller                               # 更新後重啟
curl -s http://127.0.0.1:18090/healthz
curl -s http://127.0.0.1:18090/ | head -c 200            # 應回前端的 index.html
```

- 只改前端時：`npm ci && npm run build` 後不必重啟後端（後端每次請求都從 dist 讀檔）；改後端才需要 `kickstart`。
- `run-backend.sh` 預設 `FLOW_CONTROLLER_FRONTEND_DIST=<repo>/controller/frontend/dist`，可在 env 檔覆寫。
  目錄或 `index.html` 不存在時後端只記一筆警告、不提供前端，API 照常運作。
- `/api/*` 與 `/healthz` 永遠由後端處理；其他路徑若是 dist 內的檔案就回檔案，否則回 `index.html`（SPA fallback）。
- 確認連結是 `FLOW_CONTROLLER_PUBLIC_URL/approvals/<id>?token=…`，`PUBLIC_URL` 要設成使用者實際開前端的網址
  （例如區網的 `http://<gateway 主機 IP>:18090`，此時 `FLOW_CONTROLLER_BIND` 也要改成對應介面）。
- 設定與憑證放在 `~/.openclaw/secrets/freeforjoy.env`（權限 600），`run-backend.sh` 啟動時載入。
- 資料庫：`~/.openclaw/state/flow-controller/controller.db`；log：`~/.openclaw/logs/flow-controller*.log`。
- 管理指令：`cd controller/backend && set -a && . ~/.openclaw/secrets/freeforjoy.env && set +a && uv run flow-controller <指令>`。
  （env 檔沒有 `FLOW_CONTROLLER_DB_URL` 時要另外帶上 `FLOW_CONTROLLER_DB_URL=sqlite:///$HOME/.openclaw/state/flow-controller/controller.db`，
  否則指令會寫到 `./data/controller.db`。）

- 前端登入帳號：`uv run flow-controller create-user <名稱> --role admin|viewer [--identity <hrm_employee_id>]`
  （綁 `--identity` 的帳號在收件匣會看到自己的確認連結）。

## 流程 ①（visit）需要的設定

```bash
cd ~/services/openclaw-agents/controller/backend
set -a && . ~/.openclaw/secrets/freeforjoy.env && set +a
export FLOW_CONTROLLER_DB_URL=${FLOW_CONTROLLER_DB_URL:-sqlite:///$HOME/.openclaw/state/flow-controller/controller.db}

uv run --no-dev flow-controller init-db                       # 既有 DB 補上 identities.department 欄位
uv run --no-dev flow-controller seed-agents                   # 5 隻助理 agent（已建過可重跑）
uv run --no-dev flow-controller import-identities ../deploy/identities.json --with-agents   # 7 隻員工 agent 的 Identity
# 每隻 agent 一個 token（只印一次），寫進 freeforjoy.env：FLOW_CONTROLLER_TOKEN_<AGENT_ID 大寫、- 換 _>
for a in cs-agent admin-agent finance-agent mjolmire-businessman gobta main hinata souei luminous shion; do
  echo "FLOW_CONTROLLER_TOKEN_$(echo $a | tr 'a-z-' 'A-Z_')=$(uv run --no-dev flow-controller issue-agent-token $a --name visit)"
done   # 輸出貼進 ~/.openclaw/secrets/freeforjoy.env（權限 600），不要留在終端機紀錄
# admin 要收「流程停下」通知：把 admin 帳號連到一個 Identity（例如講師自己的）
# uv run --no-dev flow-controller create-user teacher --role admin --identity <hrm_employee_id>
launchctl kickstart -k gui/$(id -u)/ai.freeforjoy.flow-controller
```

- gateway 健康檢查預設查 `http://127.0.0.1:18789/readyz`（每 60 秒、逾時 3 秒），可用
  `FLOW_CONTROLLER_GATEWAY_URL`、`FLOW_CONTROLLER_GATEWAY_TIMEOUT`、`FLOW_CONTROLLER_GATEWAY_CHECK_INTERVAL` 覆蓋。
- 步驟契約與假 agent 的跑法見 `controller/backend/docs/flows/visit.md`。**不要**對正式的 controller（`dispatcher=openclaw`）跑 `fake_agents.py`。
