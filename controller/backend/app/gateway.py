"""OpenClaw gateway 健康狀態（計畫 3.7，P0 結論）。

gateway 在 127.0.0.1:18789，`GET /readyz` 不需認證：
- 200 `{ready: true, failing: []}`：正常
- 503（或 failing 不為空）：頻道異常
- 連不上、逾時：gateway 離線

背景每分鐘查一次（逾時 3 秒），結果放進 /api/stats/summary 與 /api/agents 的 `gateway` 欄位。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

STATUS_UNKNOWN = "unknown"
STATUS_OK = "ok"
STATUS_OFFLINE = "offline"
STATUS_DEGRADED = "degraded"

# fetch(url, timeout) → (HTTP 狀態碼, 回應本文)；連不上丟 OSError
Fetch = Callable[[str, float], tuple[int, str]]


def urllib_fetch(url: str, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  固定的本機網址
            return response.status, response.read(65536).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536).decode(errors="replace")


def interpret(status_code: int, body: str) -> dict[str, Any]:
    """把 /readyz 的回應轉成前端顯示用的狀態。"""
    try:
        data = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    failing = data.get("failing") or []
    if not isinstance(failing, list):
        failing = [failing]
    failing = [
        str(item.get("id") or item.get("name") or item) if isinstance(item, dict) else str(item)
        for item in failing
    ]
    if status_code == 200 and data.get("ready", True) is not False and not failing:
        return {"status": STATUS_OK, "message": "gateway 正常", "failing": []}
    detail = "、".join(failing) if failing else f"HTTP {status_code}"
    return {"status": STATUS_DEGRADED, "message": f"頻道異常：{detail}", "failing": failing}


class GatewayMonitor:
    def __init__(self, *, url: str, timeout: float, clock: Callable[[], datetime],
                 fetch: Fetch | None = None) -> None:
        self.url = url.rstrip("/") + "/readyz"
        self.timeout = timeout
        self.clock = clock
        self.fetch: Fetch = fetch or urllib_fetch
        self._state: dict[str, Any] = {
            "status": STATUS_UNKNOWN,
            "message": "尚未檢查 gateway",
            "failing": [],
            "checked_at": None,
        }

    def snapshot(self) -> dict[str, Any]:
        return dict(self._state, failing=list(self._state["failing"]))

    async def check(self) -> dict[str, Any]:
        try:
            status_code, body = await asyncio.wait_for(
                asyncio.to_thread(self.fetch, self.url, self.timeout), timeout=self.timeout + 1
            )
            result = interpret(status_code, body)
        except (OSError, TimeoutError, ValueError, http.client.HTTPException) as exc:
            result = {"status": STATUS_OFFLINE, "message": f"gateway 離線（{type(exc).__name__}）",
                      "failing": []}
        checked_at = self.clock().astimezone(UTC)
        self._state = {**result, "checked_at": checked_at.isoformat().replace("+00:00", "Z")}
        return self.snapshot()

    async def run(self, interval: float) -> None:
        while True:
            try:
                await self.check()
            except Exception:  # noqa: BLE001  健康檢查失敗不能讓服務停掉
                logger.exception("gateway 健康檢查失敗")
            await asyncio.sleep(interval)
