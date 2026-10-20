#!/usr/bin/env python3
"""flow-report：把 controller 派來的步驟結果回報給 Workflow Controller（計畫 5.1）。

    python3 flow_report.py --flow <flow_id> --step <step> --status ok|failed|needs_human \
        [--error-kind business|system] --summary "<一句話>" --output-file <JSON 檔或 -> \
        [--attempt <n>] [--error "<原因>"] [--agent <id>] [--dry-run]

對應 controller 的 POST /api/flows/{flow_id}/steps/{step}/result（body：status、summary、output、
error、error_kind、attempt、revision）。token 用 FLOW_CONTROLLER_TOKEN_<AGENT>。

stdout 一律一行 JSON：
  成功  {"ok": true, "flow_id", "step", "flow_status", "current_step", ...}，exit 0
  失敗  {"ok": false, "error": {"http_status", "code", "message", "hint", "retryable"}}，
        controller 拒絕或連不上 exit 1；參數、設定、輸入檔錯誤 exit 2
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flow_common as c  # noqa: E402

MAX_TEXT = 2000
# 這些 409 可能代表「這份報告其實已經被接受了」（例如上一次的回應在路上遺失），要查稽核紀錄確認。
MAYBE_ALREADY_REPORTED = {"step_not_current", "step_not_running", "flow_finished", "stale_attempt"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="回報工作流步驟結果給 controller")
    p.add_argument("--flow", required=True, help="步驟指令裡的 flow_id")
    p.add_argument("--step", required=True, help="步驟指令裡的 step")
    p.add_argument("--status", required=True, choices=["ok", "failed", "needs_human"])
    p.add_argument("--error-kind", choices=["business", "system"],
                   help="status=failed 時：business（業務錯誤，例如 409 時段衝突）或 system（連線失敗、5xx）")
    p.add_argument("--summary", required=True, help="一句話摘要（給人看）")
    p.add_argument("--output-file", help="output JSON 檔路徑，- 代表 stdin；status=ok 時必填")
    p.add_argument("--error", help="失敗原因（預設用 summary）")
    p.add_argument("--attempt", type=int, help="步驟指令裡的 attempt（建議帶上，過期的回報會被拒絕）")
    p.add_argument("--revision", type=int, help="只在要求流程版本一致時使用（一般不用帶）")
    p.add_argument("--agent", help="agent id（預設讀 OPENCLAW_AGENT_ID）")
    p.add_argument("--dry-run", action="store_true", help="不連網，只顯示會送出的請求")
    return p


def clip(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def already_reported(url: str, token: str, agent: str, step: str, status: str,
                     attempt: int | None) -> dict | None:
    """查流程的稽核紀錄：這一步「這次派工」最後一筆回報，是否就是這個 agent 送的相同 status。

    只比對同一個 attempt（有帶 --attempt 用它，沒帶就用流程目前這一步的 attempts），
    避免把先前某次派工（例如 attempt 1 回報 failed 後被重派）的舊回報誤認成這一次已經送到。
    """
    try:
        res = c.request("GET", url, token, idempotent=True)
    except c.Unreachable:
        return None
    body = res["body"]
    if res["status"] != 200 or not isinstance(body, dict):
        return None
    if attempt is None:
        for row in body.get("steps") or []:
            if isinstance(row, dict) and row.get("name") == step:
                attempt = row.get("attempts")
                break
    if attempt is None:
        return None
    last = None
    for entry in body.get("audit") or []:
        if not isinstance(entry, dict) or entry.get("action") != "step.reported":
            continue
        detail = entry.get("detail") or {}
        if detail.get("step") == step and detail.get("attempt") == attempt:
            last = entry
    if last is None:
        return None
    detail = last.get("detail") or {}
    if last.get("actor") == f"agent:{agent}" and detail.get("status") == status:
        return body
    return None


def main(argv: list | None = None) -> None:
    c.load_env_file()
    args = build_parser().parse_args(argv)
    result: dict = {"command": "flow-report", "flow_id": args.flow, "step": args.step,
                    "status": args.status}

    def handler() -> int:
        agent = c.resolve_agent(args.agent)
        result["agent"] = agent
        if args.status == "ok" and args.output_file is None:
            raise c.SkillError("bad_input", "status=ok 時要用 --output-file 提供 output JSON（- 代表 stdin）")
        if args.error_kind and args.status != "failed":
            raise c.SkillError("bad_input", "--error-kind 只能和 --status failed 一起用")
        output = c.read_json_object(args.output_file, "output JSON", default={})
        body = {"status": args.status, "summary": clip(args.summary), "output": output}
        if args.status == "failed":
            body["error"] = clip(args.error) or clip(args.summary)
            if args.error_kind:
                body["error_kind"] = args.error_kind
        elif args.error:
            body["error"] = clip(args.error)
        if args.attempt is not None:
            body["attempt"] = args.attempt
        if args.revision is not None:
            body["revision"] = args.revision
        url = c.api_url("flows", args.flow, "steps", args.step, "result")

        if args.dry_run:
            result.update(dry_run=True, request={"method": "POST", "url": url, "body": body},
                          **c.token_status(agent))
            return c.EXIT_OK

        token = c.controller_token(agent)
        # 回報可以安全重送：controller 只接受一次，重複的會回 409，下面再查稽核紀錄確認。
        delays = c.retry_delays()
        conflicts = 0
        uncertain = False
        while True:
            res = c.request("POST", url, token, body, idempotent=True)
            uncertain = uncertain or res["uncertain"]
            status, data = res["status"], res["body"]
            if 200 <= status < 300 and isinstance(data, dict):
                result.update(flow_status=data.get("status"), current_step=data.get("current_step"),
                              revision=data.get("revision"))
                if uncertain or conflicts:
                    result["retried"] = True
                return c.EXIT_OK
            err = c.error_detail(status, data)
            # revision_conflict 而且呼叫端沒指定 revision：是和其他更新同時發生（交易已回滾），等一下重送。
            if (err["code"] == "revision_conflict" and args.revision is None
                    and conflicts < len(delays)):
                c.SLEEP(delays[conflicts])
                conflicts += 1
                continue
            if status == 409 and err["code"] in MAYBE_ALREADY_REPORTED:
                flow = already_reported(c.api_url("flows", args.flow), token, agent, args.step,
                                        args.status, args.attempt)
                if flow is not None:
                    result.update(already_reported=True, flow_status=flow.get("status"),
                                  current_step=flow.get("current_step"), revision=flow.get("revision"),
                                  note="這份回報先前已被 controller 接受（可能是上一次的回應遺失），不需要再送。")
                    return c.EXIT_OK
            if err["code"] == "revision_conflict":
                if args.revision is not None:
                    err["hint"] = "--revision 和流程目前的版本不同。一般回報不用帶 --revision，拿掉後再送。"
                    err["retryable"] = False
                elif conflicts:
                    err["message"] += f"（已重送 {conflicts} 次仍衝突）"
            result["error"] = err
            return c.EXIT_FAILED

    c.run(handler, result)


if __name__ == "__main__":
    main()
