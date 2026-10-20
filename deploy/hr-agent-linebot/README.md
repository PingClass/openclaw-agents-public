# hr-agent LINE bot 後端（docker）

把 `agents/hr-agent` 用 OpenClaw gateway 跑成 LINE bot。workspace 直接掛 repo 內的 `agents/hr-agent`，更新 agent 定義 = `git pull` 後重啟容器。

```text
LINE → Cloudflare Tunnel（https://<網域>/line/webhook）→ host:18796 → 容器 18789 → hr agent
```

## 前置

- 官方 OpenClaw 原始碼放在 repo 的上一層（`OPENCLAW_SRC` 預設 `../../../openclaw`）：
  `git clone --depth 1 --branch v2026.8.1 https://github.com/openclaw/openclaw.git`
- Image：`openclaw-hr:local`，`docker compose build` 時帶 build arg `OPENCLAW_EXTENSIONS="line codex"`。
- 升級 OpenClaw：原始碼換到新的 tag → `docker compose build` → `docker compose up -d` → `config validate`。
- Port：host `18796`（`.env` 的 `GATEWAY_PORT` 可改），容器內固定 18789。

## 首次部署

```bash
cd <repo>/deploy/hr-agent-linebot
cp .env.example .env && chmod 600 .env     # 填 LINE_*、模型 key；token 用 openssl rand -hex 32
mkdir -p data/.openclaw && cp openclaw.json.example data/.openclaw/openclaw.json
docker compose up -d
docker compose logs -f openclaw
```

## LINE 與對外網址

1. Cloudflare Zero Trust → Tunnels → 現有 tunnel 加 public hostname，例如 `hr-linebot.<網域>` → `http://localhost:18796`。（主機的 cloudflared 是 remote-managed，不用改主機。）Cloudflare Access 要放行 `/line/webhook`，Bot Fight Mode 維持關閉。
2. LINE Developers Console → Messaging API → Webhook URL 填 `https://hr-linebot.<網域>/line/webhook` → Verify → 開啟 Use webhook；關閉自動回應與歡迎訊息。

## 誰能用（dmPolicy = open）

> ⚠️ 範本預設**任何加好友的人都能私訊**，所有對話都算在你的模型額度上。只給自己測試時，請先改成 `pairing`。

開放給學員：任何加好友的人都能私訊（`dmPolicy: open`、`allowFrom: ["*"]`），每人各自一個 session（`dmScope: per-channel-peer`）。
要改回只限核准的人，把 `dmPolicy` 改成 `pairing`、拿掉 `allowFrom`，再用：

```bash
docker compose exec openclaw node openclaw.mjs pairing list line
docker compose exec openclaw node openclaw.mjs pairing approve line <代碼>
```

開放後的防護：`commands.allowFrom.line: []`（沒人能用 slash 指令改模型等設定）、禁 `message`（不能主動傳訊給別人）、
群組停用、`tools.fs.workspaceOnly`。對外只該開 `/line/webhook`：Cloudflare 那邊建議只放行這個 path，其餘（Control UI）擋掉。

## 常用

```bash
docker compose exec openclaw node openclaw.mjs config validate
docker compose exec openclaw node openclaw.mjs skills list
docker compose exec openclaw node openclaw.mjs channels logs --channel line
docker compose up -d --force-recreate      # 改 .env 後
git pull && docker compose restart         # 更新 agent 定義
```

## 權限設計

- hr agent 實際可用工具只有 `read`（讀 `docs/hr-policy.md`，限 workspace 內）；禁 `group:runtime`、`group:sessions`、`write`、`edit`、`apply_patch`、`message`、`ask_user`、`intent`。
- 群組訊息停用（`groupPolicy: disabled`），web search/fetch 停用。
- 憑證只放 `.env`，不寫進 `openclaw.json` 或 workspace。
