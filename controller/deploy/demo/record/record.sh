#!/usr/bin/env bash
# 流程 ① 備援錄影（計畫 8 R2）：起一個臨時、假派工的 controller，用 fake_agents.py 推進流程，
# 同時用 Playwright（Chromium headless，1280x720）錄下前端示範模式的畫面。
#
# 用法：controller/deploy/demo/record/record.sh [輸出目錄]
#   輸出目錄預設 controller/deploy/demo/record/out/（已加進 .gitignore，影片不進 repo）
#   環境變數：PACE（每個 agent 步驟停留秒數，預設 7）、KEEP_TMP=1（保留暫存 DB 與 log 以便除錯）
#
# 不碰正式環境：DB 是暫存 sqlite、port 隨機、只綁 127.0.0.1、FLOW_CONTROLLER_DISPATCHER=fake，結束時一律停掉。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONTROLLER_DIR="$(cd "$HERE/../../.." && pwd)"
BACKEND_DIR="$CONTROLLER_DIR/backend"
FRONTEND_DIR="$CONTROLLER_DIR/frontend"
OUT_DIR="${1:-$HERE/out}"
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
PACE="${PACE:-7}"
PLAYWRIGHT_VERSION="${PLAYWRIGHT_VERSION:-1.63.0}"

# 1) 前端 dist（沒有就 build）、後端套件
if [[ ! -f "$FRONTEND_DIR/dist/index.html" ]]; then
  echo "[record] build 前端…"
  (cd "$FRONTEND_DIR" && npm ci --silent && npm run build)
fi
(cd "$BACKEND_DIR" && uv sync -q)
# Chromium（已下載過會直接略過）
uvx --from "playwright==$PLAYWRIGHT_VERSION" playwright install chromium >/dev/null

# 2) 暫存目錄、隨機 port、臨時 admin 帳號（密碼隨機，只存在這次執行的環境變數）
# 清掉外部帶進來的 FLOW_CONTROLLER_*（例如正式環境的 DB_URL、token、COOKIE_SECURE），臨時 controller 只用下面設定的值
while IFS= read -r var; do unset "$var"; done < <(compgen -e | grep '^FLOW_CONTROLLER_' || true)
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/fc-demo-record.XXXXXX")"
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
BASE_URL="http://127.0.0.1:$PORT"
export FLOW_CONTROLLER_DB_URL="sqlite:///$TMP_DIR/demo.db"
export FLOW_CONTROLLER_DISPATCHER=fake
export FLOW_CONTROLLER_PUBLIC_URL="$BASE_URL"
export FLOW_CONTROLLER_FRONTEND_DIST="$FRONTEND_DIR/dist"
export FLOW_CONTROLLER_GATEWAY_CHECK_INTERVAL=0   # 本機沒有 OpenClaw gateway：不檢查，畫面不會出現離線提示
export DEMO_ADMIN_USER=demo
DEMO_ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
export DEMO_ADMIN_PASSWORD

SERVER_PID=""
cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    echo "[record] 已停掉臨時 controller（port ${PORT}）"
  fi
  if [[ "${KEEP_TMP:-0}" == "1" ]]; then
    echo "[record] 保留暫存目錄 $TMP_DIR"
  else
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT
# Ctrl-C／kill：直接結束（交給 EXIT 收尾），不要在 handler 跑完後繼續往下執行
trap 'exit 130' INT TERM

cd "$BACKEND_DIR"
uv run -q flow-controller init-db >/dev/null
FLOW_CONTROLLER_NEW_PASSWORD="$DEMO_ADMIN_PASSWORD" \
  uv run -q flow-controller create-user "$DEMO_ADMIN_USER" --role admin --display-name 講師 >/dev/null
# 先建好 5 隻助理 agent，開頭的總覽才看得到狀態卡（fake_agents --setup-local 之後重跑也沒關係）
uv run -q flow-controller seed-agents >/dev/null

# 3) 起臨時 controller（用 .venv 裡的 uvicorn 直接跑，kill 時才不會留下孤兒行程）
"$BACKEND_DIR/.venv/bin/uvicorn" --factory app.main:create_app --host 127.0.0.1 --port "$PORT" --workers 1 \
  >"$TMP_DIR/controller.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 60); do
  curl -sf "$BASE_URL/healthz" >/dev/null 2>&1 && break
  sleep 0.5
done
if ! curl -sf "$BASE_URL/healthz" >/dev/null; then
  echo "[record] controller 起不來："
  tail -30 "$TMP_DIR/controller.log"
  exit 1
fi
echo "[record] 臨時 controller：${BASE_URL}（DB $TMP_DIR/demo.db）"

# 4) 錄影（record.py 會在適當時機叫起 fake_agents.py）
uv run -q --no-project --with "playwright==$PLAYWRIGHT_VERSION" --with "imageio-ffmpeg==0.6.0" \
  python "$HERE/record.py" --base-url "$BASE_URL" --out-dir "$OUT_DIR" --pace "$PACE" \
  --backend-dir "$BACKEND_DIR" --log "$TMP_DIR/fake_agents.log"
