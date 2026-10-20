#!/usr/bin/env python3
"""假 agent：用各 agent 的 controller token 直接呼叫 API，跑完整條流程 ①（visit）、②（refund）或 ③（onboarding）。
流程 ③（--flow onboarding）的角色與步驟見 scripts/fake_onboarding.py。

流程 ②（--flow refund）扮演的角色：
- milim：先在模擬通道傳一則客戶訊息（POST /sim/messages）
- cs-agent：啟動流程（crm-intake 的角色）、draft_reply、log_reply
- finance-agent：assess_refund、record_decision
- 客服主管（預設 hinata）：從收件匣決定退款（--refund-decision approve／reject／escalate，--refund-amount 調整金額）、
  確認回覆（--revise N：先退回重擬 N 次）
- 最後 milim 讀模擬對話，確認收到回覆

    ... scripts/fake_agents.py --setup-local --flow refund --refund-amount 1200 --revise 1
    ... scripts/fake_agents.py --setup-local --flow refund --refund-decision escalate
    ... scripts/fake_agents.py --setup-local --flow refund --stall-at decide_refund   # 主管不決定：4 小時後卡住

流程 ①（預設）扮演的角色：
- cs-agent：啟動流程（crm-visit-list 的角色）、步驟 8 close_visit
- admin-agent：步驟 2 draft_vehicle、4 book_vehicle、5 record_trip
- finance-agent：步驟 6 draft_claim
- 業務（預設 mjolmire-businessman）：從收件匣確認派車、送 trip_report
- 業務主管（main）：從收件匣核准報帳
- 財會（luminous）：最後讀收件匣，確認收到入帳通知

只能對 FLOW_CONTROLLER_DISPATCHER=fake 的 controller 使用（正式環境會真的叫起 agent）。

用法（在 controller/backend 底下）：

    # 1) 起一個假派工的 controller
    FLOW_CONTROLLER_DISPATCHER=fake FLOW_CONTROLLER_DB_URL=sqlite:///./data/fake.db \\
      uv run uvicorn --factory app.main:create_app --port 18091

    # 2) 另一個終端機：--setup-local 會直接寫同一個 DB（建 agent、匯入 identities.json、發 token）
    FLOW_CONTROLLER_DB_URL=sqlite:///./data/fake.db \\
      uv run python scripts/fake_agents.py --base-url http://127.0.0.1:18091 --setup-local

    # 錄影／課堂示範：每步停 6 秒，兩次人工確認留給人在前端按（見 controller/deploy/demo/record/）
    ... scripts/fake_agents.py --setup-local --pace 6 --manual-approvals --timeout 900

    # 故意讓 admin-agent 不回報 draft_vehicle：5 分鐘後總覽頁出現「admin-agent 已 N 分鐘沒有回報」
    ... scripts/fake_agents.py --setup-local --stall-at draft_vehicle

    # 卡住後等人在前端按「重試這一步」，重試後繼續跑完（P2.5「看到卡住 → 找到原因 → 重試」）
    ... scripts/fake_agents.py --setup-local --stall-at draft_vehicle --wait-retry --timeout 1800

    # 模擬 ADM 409 時段衝突兩次（回到步驟 2 重查空車），第 3 次才成功
    ... scripts/fake_agents.py --setup-local --conflicts 2

    # 示範模式（課堂 demo，input.demo=true；拜訪排在今天約 20 分鐘後，時限縮短）
    ... scripts/fake_agents.py --setup-local --demo

    # 課堂排練：接手 controller/deploy/demo 的 start 啟動的流程，只扮演助理 agent，
    # 人工確認（講師在前端收件匣按）與業務回報里程（demo 腳本 trip）留給人
    ... scripts/fake_agents.py --attach fl_xxx --assistants-only --timeout 1800

    # 流程 ③ 新人到職（業務部，主管 main 確認收到）；--department 資訊部 會停下並通知 HR（exit 2）
    ... scripts/fake_agents.py --setup-local --flow onboarding
    ... scripts/fake_agents.py --setup-local --flow onboarding --department 資訊部 --department-id 3

token 來源：環境變數 FLOW_CONTROLLER_TOKEN_<AGENT_ID 轉大寫、- 換成 _>（例如
FLOW_CONTROLLER_TOKEN_CS_AGENT），沒有 --setup-local 時一定要先設好。
只用標準函式庫（--setup-local 例外，會 import controller 的程式直接寫 DB）。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
IDENTITIES_FILE = BACKEND_DIR.parent / "deploy" / "identities.json"
TPE = timezone(timedelta(hours=8))
TERMINAL = {"succeeded", "failed", "expired", "cancelled"}
ASSISTANTS = ("cs-agent", "admin-agent", "finance-agent")
ONBOARDING_ASSISTANTS = ("hr-agent", "it-agent", "admin-agent")


def token_env(agent_id: str) -> str:
    return "FLOW_CONTROLLER_TOKEN_" + agent_id.upper().replace("-", "_")


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class Api:
    def __init__(self, base_url: str, verbose: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.verbose = verbose

    def call(self, agent_id: str | None, method: str, path: str, body: dict | None = None,
             params: dict | None = None) -> dict:
        url = f"{self.base_url}/api{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json"}
        if agent_id:
            token = os.environ.get(token_env(agent_id))
            if not token:
                raise SystemExit(f"缺少 {token_env(agent_id)}（或加上 --setup-local）")
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, exc.read().decode(errors="replace")) from exc


def log(actor: str, message: str) -> None:
    print(f"[{datetime.now(TPE).strftime('%H:%M:%S')}] {actor:<22} {message}", flush=True)


# ---------------------------------------------------------------- 本機準備


def setup_local(agent_ids: list[str], simulators: tuple[str, ...] = ()) -> None:
    """直接寫 controller 的 DB：建 5 隻助理 agent、匯入 identities.json（含員工 agent）、發 token。"""
    sys.path.insert(0, str(BACKEND_DIR))
    from app.cli import ASSISTANT_AGENTS, _session, _upsert_agent, import_identities
    from app.config import Settings
    from app.db import utcnow
    from app.models import Agent, AgentToken
    from app.security import new_token, sha256_hex

    settings = Settings.from_env()
    records = json.loads(IDENTITIES_FILE.read_text(encoding="utf-8"))["identities"]
    with _session(settings) as db:
        for agent_id, name, emoji in ASSISTANT_AGENTS:
            _upsert_agent(db, agent_id, display_name=name, emoji=emoji, role="assistant", workspace=None)
        for record in records:
            if db.get(Agent, record["agent_id"]) is None:
                _upsert_agent(db, record["agent_id"], display_name=record["display_name"], emoji=None,
                              role="employee", workspace=None)
        for agent_id in simulators:
            if db.get(Agent, agent_id) is None:
                _upsert_agent(db, agent_id, display_name=agent_id, emoji=None, role="simulator", workspace=None)
        db.commit()
        import_identities(db, records)
        for agent_id in agent_ids:
            raw = new_token("fca")
            db.add(AgentToken(agent_id=agent_id, name="fake_agents", token_hash=sha256_hex(raw),
                              can_simulate=agent_id in simulators, created_at=utcnow()))
            os.environ[token_env(agent_id)] = raw
        db.commit()
    log("setup", f"DB {settings.db_url}：已建立 agent、匯入 {len(records)} 筆 Identity、發 {len(agent_ids)} 個 token")


# ---------------------------------------------------------------- 各角色


def find_approval(api: Api, agent_id: str, flow_id: str) -> tuple[str, str] | None:
    inbox = api.call(agent_id, "GET", "/inbox", params={"unread_only": "true"})
    for item in inbox["items"]:
        if item["kind"] == "approval_request" and item["flow_id"] == flow_id and item.get("link"):
            parsed = urllib.parse.urlparse(item["link"])
            token = urllib.parse.parse_qs(parsed.query)["token"][0]
            return parsed.path.rsplit("/", 1)[-1], token
    return None


def decide(api: Api, agent_id: str, flow_id: str, decision: str, comment: str,
           data: dict | None = None) -> bool:
    found = find_approval(api, agent_id, flow_id)
    if found is None:
        return False
    approval_id, token = found
    page = api.call(None, "GET", f"/approvals/{approval_id}", params={"token": token})
    log(agent_id, f"確認頁：{page['question']}")
    body = {"token": token, "decision": decision, "comment": comment}
    if data:
        body["data"] = data
    api.call(None, "POST", f"/approvals/{approval_id}/decision", body)
    log(agent_id, f"決定：{decision}（{comment}）")
    return True


def report(api: Api, agent_id: str, flow_id: str, step: dict, output: dict, summary: str,
           status: str = "ok", **extra) -> None:
    body = {"status": status, "summary": summary, "output": output, "attempt": step["attempts"], **extra}
    api.call(agent_id, "POST", f"/flows/{flow_id}/steps/{step['name']}/result", body)
    log(agent_id, f"回報 {step['name']}（attempt {step['attempts']}）：{status}，{summary}")


def run(args: argparse.Namespace) -> int:
    return run_refund(args) if args.flow == "refund" else run_visit(args)


# 流程 ① 裡由假 agent（或業務回報事件）推進的步驟；--pace 只對這些步驟放慢
VISIT_ACTOR_STEPS = ("draft_vehicle", "book_vehicle", "await_trip", "record_trip", "draft_claim", "close_visit")
# --manual-approvals 時留給人在前端決定的兩次人工確認
APPROVAL_STEPS = ("confirm_vehicle", "approve_claim")
# --assistants-only 時不扮演的步驟（人工確認、業務回報里程）
HUMAN_STEPS = ("confirm_vehicle", "await_trip", "approve_claim")


def run_visit(args: argparse.Namespace) -> int:
    api = Api(args.base_url)
    records = {r["agent_id"]: r for r in json.loads(IDENTITIES_FILE.read_text(encoding="utf-8"))["identities"]}
    sales_hrm = records[args.sales]["hrm_employee_id"]

    rate = float(os.environ.get("MILEAGE_RATE_TWD_PER_KM", "5"))
    rate = int(rate) if rate.is_integer() else rate

    if args.attach:
        # 接手一條已經啟動的流程（例如課堂 demo 的 start 腳本、或真的 cs-agent 啟動的示範流程）
        flow_id = args.attach
        detail = api.call("cs-agent", "GET", f"/flows/{flow_id}")
        flow_input = detail["state"]["input"]
        start = datetime.fromisoformat(flow_input["visit_start_at"])
        end = datetime.fromisoformat(flow_input["visit_end_at"])
        day = start.astimezone(TPE).date()
        follow_up_id = flow_input["follow_up_id"]
        log("fake_agents", f"接手流程 {flow_id}「{detail['title']}」（目前在 {detail['current_step']}）")
    else:
        if args.demo and not args.visit_date:
            # 示範模式：今天、約 20 分鐘後開始、1 小時（和 controller/deploy/demo 的 prepare 一樣）
            start = (datetime.now(TPE) + timedelta(minutes=20)).replace(second=0, microsecond=0)
            end = start + timedelta(hours=1)
        else:
            day = (datetime.fromisoformat(args.visit_date).date() if args.visit_date
                   else (datetime.now(TPE) + timedelta(days=2)).date())
            start = datetime(day.year, day.month, day.day, 10, 0, tzinfo=TPE)
            end = start + timedelta(hours=2)
        day = start.date()
        follow_up_id = args.follow_up_id or random.randint(100000, 999999)
        flow_input = {
            "follow_up_id": follow_up_id,
            "customer": {"name": "客戶 C（假資料）", "external_id": "C-FAKE-003"},
            "visit_start_at": start.isoformat(),
            "visit_end_at": end.isoformat(),
            "location": "台中 客戶 C 工廠",
            "seats": 2,
            "assignee_hrm_employee_id": sales_hrm,
            "subject": "新品試吃",
        }
        if args.demo:
            flow_input["demo"] = True
        flow = api.call("cs-agent", "POST", "/flows", {"type": "visit", "input": flow_input})
        flow_id = flow["id"]
        log("cs-agent", f"啟動流程 {flow_id}「{flow['title']}」（follow_up_id={follow_up_id}，created={flow['created']}）")

    conflicts_left = args.conflicts
    stall_attempt: int | None = None  # --wait-retry：卡住時的 attempt，重試後 attempt 會 +1
    resumed = False
    waiting_logged: tuple | None = None
    last_stuck: str | None = None
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        detail = api.call("cs-agent", "GET", f"/flows/{flow_id}")
        status = detail["status"]
        if status in TERMINAL:
            break
        name = detail["current_step"]
        step = next(s for s in detail["steps"] if s["name"] == name)
        stuck_code = (detail.get("stuck") or {}).get("code")
        if stuck_code and stuck_code != last_stuck:
            log("controller", f"卡住：{detail['stuck']['reason']}")
        last_stuck = stuck_code
        if detail.get("stuck"):
            if step["status"] == "failed":
                return 1
        if name == args.stall_at and stall_attempt is None:
            log("fake_agents", f"故意不回報「{step['title']}」（{step['assignee']}）。"
                f"超過時限（{step['deadline_at']}）後，總覽頁會顯示卡住。流程 {flow_id}")
            if not args.wait_retry:
                return 0
            stall_attempt = step["attempts"]
            log("fake_agents", "等待在前端按「重試這一步」…（--wait-retry）")
        if stall_attempt is not None and name == args.stall_at and step["attempts"] <= stall_attempt:
            time.sleep(args.poll)
            continue
        if stall_attempt is not None and name == args.stall_at and not resumed:
            resumed = True
            log("fake_agents", f"偵測到重試（attempt {step['attempts']}），繼續跑流程")
        if step["status"] not in ("dispatched", "running", "waiting_approval"):
            time.sleep(args.poll)
            continue
        if (args.manual_approvals and name in APPROVAL_STEPS) or (args.assistants_only and name in HUMAN_STEPS):
            # 人工確認（--manual-approvals／--assistants-only）、業務回報里程（--assistants-only）
            # 交給真人（講師）、錄影腳本或 demo 腳本，這裡只等；放在 --pace 之前，等人時不另外放慢
            if waiting_logged != (name, step["attempts"]):
                waiting_logged = (name, step["attempts"])
                flag = "--assistants-only" if args.assistants_only and name in HUMAN_STEPS else "--manual-approvals"
                log("fake_agents", f"等人處理「{step['title']}」（{flag}）")
            time.sleep(args.poll)
            continue
        if args.pace and name in VISIT_ACTOR_STEPS:
            # 放慢節奏（錄影用）：每一步先停一下再回報；回報前重抓一次，避免這段時間裡步驟已被別人處理
            time.sleep(args.pace)
            fresh = api.call("cs-agent", "GET", f"/flows/{flow_id}")
            if fresh["current_step"] != name or fresh["status"] in TERMINAL:
                continue
            step = next(s for s in fresh["steps"] if s["name"] == name)
            if step["status"] not in ("dispatched", "running", "waiting_approval"):
                continue

        if name == "draft_vehicle":
            vehicle_id = 2 + (args.conflicts - conflicts_left)
            report(api, "admin-agent", flow_id, step, {
                "vehicle_id": vehicle_id, "vehicle_code": f"FAKE-{vehicle_id:03d}", "vehicle_name": "假車輛",
                "seats": 5, "start_at": start.isoformat(), "end_at": end.isoformat(),
                "destination": "台中 客戶 C 工廠", "purpose": "客戶拜訪：客戶 C（假資料）",
            }, f"可用車輛 FAKE-{vehicle_id:03d}")
        elif name == "confirm_vehicle":
            if not decide(api, args.sales, flow_id, "approve", "OK，就這台"):
                time.sleep(args.poll)
        elif name == "book_vehicle":
            draft = detail["state"]["steps"]["draft_vehicle"]
            if conflicts_left > 0:
                conflicts_left -= 1
                report(api, "admin-agent", flow_id, step,
                       {"conflict": True, "http_status": 409, "vehicle_id": draft["vehicle_id"]},
                       "ADM 回 409 時段衝突", status="failed", error="ADM 回 409 時段衝突",
                       error_kind="business")
            else:
                report(api, "admin-agent", flow_id, step, {
                    "booking_id": random.randint(1000, 9999), "vehicle_id": draft["vehicle_id"],
                    "start_at": draft["start_at"], "end_at": draft["end_at"], "created": True,
                }, f"已代訂 {draft['vehicle_code']}")
        elif name == "await_trip":
            pickup = 12000
            api.call(args.sales, "POST", f"/flows/{flow_id}/events", {
                "type": "trip_report", "pickup_odometer": pickup, "return_odometer": pickup + args.km,
                "note": "客戶對新品有興趣，下週報價（假資料）",
            })
            log(args.sales, f"回報取車 {pickup} km、還車 {pickup + args.km} km")
        elif name == "record_trip":
            trip = detail["state"]["steps"]["await_trip"]
            booking = detail["state"]["steps"]["book_vehicle"]
            report(api, "admin-agent", flow_id, step, {
                "booking_id": booking["booking_id"], "pickup_odometer": trip["pickup_odometer"],
                "return_odometer": trip["return_odometer"], "distance_km": trip["distance_km"],
            }, f"已登記取車還車，里程差 {trip['distance_km']} km")
        elif name == "draft_claim":
            km = detail["state"]["steps"]["record_trip"]["distance_km"]
            amount = round(km * rate)
            report(api, "finance-agent", flow_id, step, {
                "distance_km": km, "rate_twd_per_km": rate, "amount_twd": amount, "rate_is_placeholder": True,
                "description": f"{day.isoformat()} 拜訪客戶 C，{km} km（假資料）",
            }, f"報帳草稿 NT${amount}（demo 假費率）")
        elif name == "approve_claim":
            if not decide(api, args.manager, flow_id, "approve", "核准"):
                time.sleep(args.poll)
        elif name == "close_visit":
            report(api, "cs-agent", flow_id, step, {
                "interaction_id": random.randint(1000, 9999), "follow_up_id": follow_up_id,
                "follow_up_status": "completed",
            }, "已記拜訪互動並完成待辦跟進")
        else:
            time.sleep(args.poll)
    else:
        log("fake_agents", f"{args.timeout} 秒內沒有跑完，流程 {flow_id}")
        return 1

    detail = api.call("cs-agent", "GET", f"/flows/{flow_id}")
    log("controller", f"流程 {flow_id} 結束：{detail['status']}")
    for step in detail["steps"]:
        log("controller", f"  {step['name']:<16} {step['status']:<10} {step['summary'] or ''}")
    notices = api.call(args.finance, "GET", "/inbox")["items"]
    for notice in notices:
        if notice["flow_id"] == flow_id:
            log(args.finance, f"收件匣：{notice['title']}｜{notice['body']}")
    return 0 if detail["status"] == "succeeded" else 1


REFUND_ASSESSMENT_AMOUNT = 1500


def run_refund(args: argparse.Namespace) -> int:
    """流程 ②：milim 傳訊 → cs-agent 啟動 → 評估 → 主管決定 → 寫決定備註 → 擬回覆 → 主管確認 → 送出 → 記互動。"""
    api = Api(args.base_url)
    external_id = "C-FAKE-007"
    case_id = args.case_id or f"fake-case-{random.randint(100000, 999999)}"
    text = "上週買的粉圓到貨破損，我要退款（假資料）"
    sent = api.call(args.customer, "POST", "/sim/messages", {
        "target_agent": "cs-agent", "persona": {"external_customer_id": external_id, "display_name": "陳小姐（假資料）"},
        "message": text,
    })
    log(args.customer, f"模擬客戶傳訊：{text}（對話 {sent['conversation_id']}）")
    flow = api.call("cs-agent", "POST", "/flows", {"type": "refund", "input": {
        "case_id": case_id, "customer_id": f"fake-customer-{external_id}", "external_customer_id": external_id,
        "customer_name": "陳小姐（假資料）", "category": "refund", "summary": "粉圓到貨破損，要求退款（假資料）",
    }})
    flow_id = flow["id"]
    log("cs-agent", f"啟動流程 {flow_id}「{flow['title']}」（case_id={case_id}，created={flow['created']}）")

    revisions_left = args.revise
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        detail = api.call("cs-agent", "GET", f"/flows/{flow_id}")
        if detail["status"] in TERMINAL:
            break
        name = detail["current_step"]
        step = next(s for s in detail["steps"] if s["name"] == name)
        if detail.get("stuck") and step["status"] == "failed":
            log("controller", f"卡住：{detail['stuck']['reason']}")
            return 1
        if name == args.stall_at:
            log("fake_agents", f"故意不處理「{step['title']}」（{step['assignee']}），時限 {step['deadline_at']}。"
                f"流程 {flow_id}")
            return 0
        if step["status"] not in ("dispatched", "running", "waiting_approval"):
            time.sleep(args.poll)
            continue
        state = detail["state"]
        decision = (state.get("decisions") or {}).get("decide_refund") or {}
        if name == "assess_refund":
            report(api, "finance-agent", flow_id, step, {
                "note_id": f"fake-note-{random.randint(1000, 9999)}",
                "suggested_amount_twd": REFUND_ASSESSMENT_AMOUNT, "amount_is_estimate": True,
                "basis": "商品單價 NT$1,500、到貨破損（假資料）", "history": "過去沒有退款紀錄（假資料）",
                "recommendation": "refund",
            }, f"推估建議退款 NT${REFUND_ASSESSMENT_AMOUNT}（推估）")
        elif name == "decide_refund":
            data = {"amount_twd": args.refund_amount} if args.refund_amount else None
            if not decide(api, args.cs_manager, flow_id, args.refund_decision, "依評估處理（假資料）", data):
                time.sleep(args.poll)
        elif name == "record_decision":
            report(api, "finance-agent", flow_id, step, {"note_id": f"fake-note-{random.randint(1000, 9999)}"},
                   "已寫入主管決定備註")
        elif name == "draft_reply":
            if decision.get("decision") == "approve":
                reply = (f"陳小姐您好，很抱歉造成困擾。我們將退款 NT${decision['amount_twd']:,} 至原付款方式，"
                         f"約 7 個工作天入帳。（第 {step['attempts']} 版，假資料）")
            else:
                reply = f"陳小姐您好，經確認這次無法辦理退款，造成不便非常抱歉。（第 {step['attempts']} 版，假資料）"
            report(api, "cs-agent", flow_id, step, {"reply_text": reply}, "回覆草稿完成")
        elif name == "review_reply":
            choice = "revise" if revisions_left > 0 else "approve"
            if decide(api, args.cs_manager, flow_id, choice, "語氣再誠懇一點" if choice == "revise" else "可以送出"):
                revisions_left -= choice == "revise"
            else:
                time.sleep(args.poll)
        elif name == "log_reply":
            report(api, "cs-agent", flow_id, step, {
                "interaction_id": f"fake-interaction-{random.randint(1000, 9999)}",
                "follow_up_id": f"fake-follow-up-{random.randint(1000, 9999)}",
            }, "已記回覆互動並建立後續待辦")
        else:
            time.sleep(args.poll)
    else:
        log("fake_agents", f"{args.timeout} 秒內沒有跑完，流程 {flow_id}")
        return 1

    detail = api.call("cs-agent", "GET", f"/flows/{flow_id}")
    log("controller", f"流程 {flow_id} 結束：{detail['status']}（outcome={detail['state'].get('outcome')}）")
    for step in detail["steps"]:
        log("controller", f"  {step['name']:<16} {step['status']:<10} {step['summary'] or ''}")
    convo = api.call(args.customer, "GET", "/sim/messages", params={"external_customer_id": external_id})
    for message in convo["items"]:
        log(args.customer, f"模擬對話（{message['direction']}）：{message['body']}")
    return 0 if detail["status"] == "succeeded" else 1


def run_onboarding(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import fake_onboarding
    from types import SimpleNamespace

    helpers = SimpleNamespace(Api=Api, IDENTITIES_FILE=IDENTITIES_FILE, TPE=TPE, log=log, report=report,
                              decide=decide)
    return fake_onboarding.run(helpers, args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="假 agent：跑完整條流程 ①（visit）、②（--flow refund）或 ③（--flow onboarding）")
    parser.add_argument("--base-url", default=os.environ.get("FLOW_CONTROLLER_URL", "http://127.0.0.1:18090"))
    parser.add_argument("--flow", choices=["visit", "refund", "onboarding"], default="visit", help="要跑哪一條流程")
    parser.add_argument("--setup-local", action="store_true",
                        help="直接寫 FLOW_CONTROLLER_DB_URL 的 DB：建 agent、匯入 identities.json、發 token")
    parser.add_argument("--sales", default="mjolmire-businessman", help="業務的員工 agent id")
    parser.add_argument("--manager", default="main", help="業務主管的員工 agent id")
    parser.add_argument("--finance", default="luminous", help="財會的員工 agent id")
    parser.add_argument("--stall-at", metavar="STEP",
                        help="到這一步就不回報（例如 draft_vehicle），讓總覽出現卡住")
    parser.add_argument("--wait-retry", action="store_true",
                        help="搭配 --stall-at：卡住後不結束，等到在前端按「重試這一步」（attempt 增加）再繼續跑完；"
                             "記得把 --timeout 調大（例如 1800）")
    parser.add_argument("--conflicts", type=int, default=0, help="book_vehicle 先回幾次 409（最多 2 次會自動重查）")
    parser.add_argument("--km", type=int, default=37, help="取車到還車的里程差")
    parser.add_argument("--visit-date", help="拜訪日期 YYYY-MM-DD（預設後天）")
    parser.add_argument("--follow-up-id", type=int, help="CRM 待辦跟進 id（預設隨機，避免去重）")
    parser.add_argument("--demo", action="store_true",
                        help="①：啟動示範模式的流程（input.demo=true；沒給 --visit-date 時拜訪排在今天約 20 分鐘後。"
                             "controller 只接受今天、3 小時內開始且還沒結束的示範拜訪）")
    parser.add_argument("--attach", metavar="FLOW_ID", help="①：不啟動新流程，接手這條已經啟動的流程")
    parser.add_argument("--assistants-only", action="store_true",
                        help="①：只扮演助理 agent，人工確認與業務回報里程留給真人或 demo 腳本（課堂排練用）")
    parser.add_argument("--cs-manager", default="hinata", help="②：客服主管的員工 agent id")
    parser.add_argument("--customer", default="milim", help="②：扮客戶的 agent id（要有模擬權限）")
    parser.add_argument("--refund-decision", choices=["approve", "reject", "escalate"], default="approve",
                        help="②：客服主管的決定")
    parser.add_argument("--refund-amount", type=int, help="②：同意退款時調整的金額（預設用推估金額）")
    parser.add_argument("--revise", type=int, default=0, help="②：回覆先退回重擬幾次")
    parser.add_argument("--case-id", help="②：CRM 案件 id（預設隨機，避免去重）")
    parser.add_argument("--department", default="業務部", help="onboarding：新人的部門名稱")
    parser.add_argument("--department-id", type=int, default=1, help="onboarding：新人的 HRM 部門 id")
    parser.add_argument("--start-date", help="onboarding：到職日 YYYY-MM-DD（預設下週一）")
    parser.add_argument("--employee-id", help="onboarding：新人的 HRM 員工 id（預設隨機，避免去重）")
    parser.add_argument("--it", default="souei", help="onboarding：IT 人員的員工 agent id")
    parser.add_argument("--hr", default="shion", help="onboarding：人資的員工 agent id")
    parser.add_argument("--pace", type=float, default=0,
                        help="①：每個 agent 步驟回報前先等幾秒（錄影、課堂示範用，預設 0）")
    parser.add_argument("--manual-approvals", action="store_true",
                        help="①：兩次人工確認不由假 agent 代按，等人在前端確認頁決定（錄影腳本用）")
    parser.add_argument("--poll", type=float, default=0.3, help="輪詢間隔（秒）")
    parser.add_argument("--timeout", type=float, default=60, help="整條流程最多跑幾秒")
    args = parser.parse_args(argv)
    if args.setup_local:
        if args.flow == "refund":
            setup_local(sorted({*ASSISTANTS, args.cs_manager, args.customer}), simulators=(args.customer,))
        elif args.flow == "onboarding":
            setup_local(sorted({*ONBOARDING_ASSISTANTS, args.it, args.hr, args.manager}))
        else:
            setup_local(sorted({*ASSISTANTS, args.sales, args.manager, args.finance}))
    try:
        if args.flow == "onboarding":
            return run_onboarding(args)
        return run(args)
    except ApiError as exc:
        log("fake_agents", f"API 錯誤：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
