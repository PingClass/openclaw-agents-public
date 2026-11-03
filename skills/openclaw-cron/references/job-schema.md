# Cron Job JSON 結構

取自 OpenClaw 2026.10.9 的 `openclaw cron get <id> --json`（2026-11-03）。
**只用來閱讀與除錯**：job 存在 SQLite，修改一律走 `openclaw cron edit`，不要手改 JSON。

## Job 物件

```json
{
  "id": "uuid",
  "declarationKey": "optional idempotent key",
  "agentId": "main",
  "name": "Job name",
  "enabled": true,
  "createdAtMs": 1791597172999,
  "updatedAtMs": 1793692154253,
  "schedule": { "...": "見下" },
  "sessionTarget": "isolated",
  "wakeMode": "now",
  "payload": { "...": "見下" },
  "delivery": { "...": "見下" },
  "scheduledToolPolicy": { "version": 1, "mode": "trusted" },
  "failureAlert": null,
  "state": { "...": "見下" },
  "configRevision": "string",
  "effectiveAgentId": "main",
  "status": "ok"
}
```

`get` 輸出另外把 `nextRunAtMs`、`lastRunAtMs`、`lastRunStatus` 等 state 欄位複製到頂層，方便讀取。

## schedule

```json
{ "kind": "cron", "expr": "49 10-17 * * 1-5", "tz": "Asia/Taipei", "staggerMs": 0 }
{ "kind": "at", "at": "2026-12-01T01:00:00Z" }
{ "kind": "every", "...": "以 --every 10m 等 duration 建立（實際欄位名未查證）" }
```

`staggerMs: 0` 對應 `--exact`。

## payload

```json
{ "kind": "agentTurn", "message": "prompt", "timeoutSeconds": 300,
  "toolsAllow": ["*"], "lightContext": true, "model": "provider/model" }
{ "kind": "systemEvent", "text": "事件內容" }
```

- `toolsAllow` 對應 `--tools`；`lightContext` 對應 `--light-context`。
- 沒有 `model` 就用 agent 預設模型。
- command／script payload 的欄位名未查證，需要時用 `get` 看實際 job。

## delivery

```json
{ "mode": "none", "channel": "last" }
{ "mode": "announce", "channel": "discord", "to": "<channel-id>" }
```

- `mode: none` = 不送出（`--no-deliver`）；`announce` = 送到 `channel`/`to`。
- `channel: last` = 送到最後互動的頻道。

## state（排程器維護，唯讀）

```json
{
  "nextRunAtMs": 1793695740000,
  "lastRunAtMs": 1793692140068,
  "lastRunStatus": "ok",
  "lastStatus": "ok",
  "lastDurationMs": 14185,
  "lastDelivered": false,
  "lastDeliveryStatus": "not-requested",
  "lastFailureNotificationDeliveryStatus": "not-requested",
  "consecutiveErrors": 0,
  "consecutiveSkipped": 0
}
```

## 重試

舊版文件記載 recurring job 連續失敗會指數退避（30s → 1m → 5m → 15m → 60m），
本版未重新查證。需要通知請用 `--failure-alert*`（見 SKILL.md）。
