#!/usr/bin/env python3
"""flow-start：向 Workflow Controller 啟動一條工作流（計畫 5.1）。

    python3 flow_start.py --type visit|refund|onboarding --input-file <JSON 檔或 -> \
        [--dedupe-key <key>] [--title "<標題>"] [--agent <id>] [--dry-run]

對應 controller 的 POST /api/flows（body：type、input、title、dedupe_key）。token 用
FLOW_CONTROLLER_TOKEN_<AGENT>。只有流程定義裡 starters 列出的助理 agent 能啟動（例如 visit 只有 cs-agent）。

去重：同一件事重複啟動會回傳既有流程（created=false），不會建第二條。visit 的去重鍵由 controller
固定為 crm:follow-up:<follow_up_id>（呼叫端給的 --dedupe-key 不採用）。

stdout 一律一行 JSON：
  成功  {"ok": true, "flow_id", "created", "flow_status", "title", ...}，exit 0
  失敗  {"ok": false, "error": {"http_status", "code", "message", "hint", "retryable"}}，
        controller 拒絕或連不上 exit 1；參數、設定、輸入檔錯誤 exit 2
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flow_common as c  # noqa: E402

import re  # noqa: E402

# 已規劃的流程類型；其他類型（例如 controller 的 demo）也照送，由 controller 判斷（unknown_flow_type）。
FLOW_TYPES = ("visit", "refund", "onboarding")
TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# controller 的流程定義自己決定去重鍵的類型（不需要 --dedupe-key 也能安全重送）。
SERVER_DEDUPE_TYPES = {"visit"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="啟動工作流")
    p.add_argument("--type", required=True, help="流程類型：" + "／".join(FLOW_TYPES))
    p.add_argument("--input-file", required=True, help="流程輸入 JSON 檔路徑，- 代表 stdin")
    p.add_argument("--dedupe-key", help="去重鍵（≤128 字元；同一件事用同一個鍵，重送不會建第二條）")
    p.add_argument("--title", help="流程標題（選填；visit 由 controller 自動產生）")
    p.add_argument("--agent", help="agent id（預設讀 OPENCLAW_AGENT_ID）")
    p.add_argument("--dry-run", action="store_true", help="不連網，只顯示會送出的請求")
    return p


def main(argv: list | None = None) -> None:
    c.load_env_file()
    args = build_parser().parse_args(argv)
    result: dict = {"command": "flow-start", "type": args.type}

    def handler() -> int:
        agent = c.resolve_agent(args.agent)
        result["agent"] = agent
        if not TYPE_RE.match(args.type):
            raise c.SkillError("bad_input", f"--type 格式不對：{args.type!r}（例如 {'／'.join(FLOW_TYPES)}）")
        data = c.read_json_object(args.input_file, "流程輸入 JSON")
        body: dict = {"type": args.type, "input": data}
        if args.title:
            body["title"] = args.title.strip()[:200]
        if args.dedupe_key:
            key = args.dedupe_key.strip()
            if not key or len(key) > 128:
                raise c.SkillError("bad_input", "--dedupe-key 要是 1～128 字元")
            body["dedupe_key"] = key
        url = c.api_url("flows")

        if args.dry_run:
            result.update(dry_run=True, request={"method": "POST", "url": url, "body": body},
                          **c.token_status(agent))
            return c.EXIT_OK

        token = c.controller_token(agent)
        # 有去重鍵時重送不會建出第二條流程，逾時、5xx 才重試；沒有去重鍵只在「連線被拒」時重試。
        idempotent = bool(body.get("dedupe_key")) or args.type in SERVER_DEDUPE_TYPES
        res = c.request("POST", url, token, body, idempotent=idempotent)
        status, resp = res["status"], res["body"]
        if status in (200, 201) and isinstance(resp, dict) and resp.get("id"):
            result.update(flow_id=resp["id"], created=bool(resp.get("created", status == 201)),
                          flow_status=resp.get("status"), title=resp.get("title"),
                          current_step=resp.get("current_step"))
            if res["uncertain"]:
                result["retried"] = True
                result["note"] = ("上一次請求的回應遺失後重送；created=false 可能是上一次已經建立的同一條流程。")
            return c.EXIT_OK
        result["error"] = c.error_detail(status, resp)
        return c.EXIT_FAILED

    c.run(handler, result)


if __name__ == "__main__":
    main()
