#!/usr/bin/env python3
"""測試用的 flow-report 替身：照主 session 定的呼叫介面解析參數，POST 到（假）controller。

    flow_report.py --flow <id> --step <step> --status ok|failed|needs_human [--error-kind business|system]
                   --summary "<一句話>" --output-file <檔案|-> [--attempt <n>] [--error <原因>] [--agent <id>]

（和共用 skill skills/flow-report/scripts/flow_report.py 的參數一致。）

成功印出 {"ok": true, "flow_id", "step", "flow_status"}；失敗 exit 1。
每次被呼叫都把 argv 記到 FAKE_FLOW_REPORT_LOG（若有設定）。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--flow", required=True)
    parser.add_argument("--step", required=True)
    parser.add_argument("--status", required=True, choices=["ok", "failed", "needs_human"])
    parser.add_argument("--error-kind", choices=["business", "system"])
    parser.add_argument("--summary", required=True)
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--attempt", type=int)
    parser.add_argument("--error")
    parser.add_argument("--agent")
    args = parser.parse_args()
    log = os.environ.get("FAKE_FLOW_REPORT_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\n")
    raw = sys.stdin.read() if args.output_file == "-" else open(args.output_file, encoding="utf-8").read()
    output = json.loads(raw) if raw.strip() else {}
    agent = args.agent or os.environ.get("OPENCLAW_AGENT_ID") or ""
    token = os.environ.get("FLOW_CONTROLLER_TOKEN_" + agent.upper().replace("-", "_"), "")
    body = {"status": args.status, "summary": args.summary, "output": output}
    if args.status != "ok":
        body["error"] = args.error or args.summary
    if args.attempt is not None:
        body["attempt"] = args.attempt
    if args.error_kind:
        body["error_kind"] = args.error_kind
    url = f"{os.environ['FLOW_CONTROLLER_URL'].rstrip('/')}/api/flows/{args.flow}/steps/{args.step}/result"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            reply = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        print(json.dumps({"ok": False, "http_status": exc.code,
                          "error": exc.read().decode("utf-8", "replace")[:300]}))
        return 1
    print(json.dumps({"ok": True, "flow_id": args.flow, "step": args.step,
                      "flow_status": reply.get("status")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
