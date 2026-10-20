#!/usr/bin/env bash
# 在 gateway 主機 host 上啟動 controller 後端（由 launchd 呼叫）。
# 憑證與設定從 ~/.openclaw/secrets/freeforjoy.env 讀取，不寫進 repo。
set -euo pipefail
export PATH=/usr/local/bin:/opt/homebrew/bin:$PATH
set -a
# shellcheck disable=SC1090
. "$HOME/.openclaw/secrets/freeforjoy.env"
set +a
export FLOW_CONTROLLER_DB_URL="${FLOW_CONTROLLER_DB_URL:-sqlite:///$HOME/.openclaw/state/flow-controller/controller.db}"
export FLOW_CONTROLLER_DISPATCHER="${FLOW_CONTROLLER_DISPATCHER:-openclaw}"
export FLOW_CONTROLLER_OPENCLAW_BIN="${FLOW_CONTROLLER_OPENCLAW_BIN:-/opt/homebrew/bin/openclaw}"
mkdir -p "$HOME/.openclaw/state/flow-controller"
CONTROLLER_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# 前端靜態檔（controller/frontend 執行 npm run build 的產物）；目錄不存在時後端只記警告、API 照常
export FLOW_CONTROLLER_FRONTEND_DIST="${FLOW_CONTROLLER_FRONTEND_DIST:-$CONTROLLER_DIR/frontend/dist}"
cd "$CONTROLLER_DIR/backend"
exec uv run --no-dev uvicorn --factory app.main:create_app \
  --host "${FLOW_CONTROLLER_BIND:-127.0.0.1}" --port "${FLOW_CONTROLLER_PORT:-18090}" --workers 1
