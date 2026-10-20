#!/usr/bin/env python3
"""流程 ① 步驟 6（controller 步驟 draft_claim）：依里程差 × 費率產生交通費報帳草稿。

契約：controller/backend/docs/flows/visit.md 的「draft_claim」。

- 只產生草稿，交給 controller 保存（flow state）；**不寫入任何系統**，金額要等業務主管核准才算數。
- 費率只讀環境變數 MILEAGE_RATE_TWD_PER_KM（由財會提供，不寫死）。沒設定或格式不對 → needs_human，不猜。
- 金額＝里程差 × 費率，四捨五入到元（ROUND_HALF_UP：0.5 進位，不用銀行家捨入）。
- 里程差不是正整數、或超過 2000 km → needs_human。

用法：
    mileage_claim.py --message-file <controller 步驟指令檔，或 - 代表 stdin> [--report] [--dry-run] [--agent <id>]
    mileage_claim.py --input-file <只有輸入 JSON 的檔案> [--flow <flow_id> --step draft_claim --attempt <n>] ...

沒有 --report 時只印出結果 JSON（含要給 flow-report 的 status、summary、output），由 agent 自己呼叫 flow-report；
有 --report 時腳本直接呼叫共用 skill 的 flow_report.py 回報。--dry-run 一律不回報、不連網。

只用 Python 標準函式庫。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

SKILL = "mileage-claim-draft"
STEP = "draft_claim"
RATE_ENV = "MILEAGE_RATE_TWD_PER_KM"
PLACEHOLDER_ENV = "MILEAGE_RATE_IS_PLACEHOLDER"
DEFAULT_ENV_FILE = "~/.openclaw/secrets/freeforjoy.env"
DEFAULT_FLOW_REPORT = "~/.openclaw/skills/flow-report/scripts/flow_report.py"
DEFAULT_AGENT = "finance-agent"

# 合理範圍：超出就交給人判斷（controller 的 trip_report 也拒絕超過 2000 km 的里程差）
MAX_DISTANCE_KM = 2000
MAX_RATE_TWD_PER_KM = Decimal("100")

DRAFT_MARK = "【草稿・待主管核准】"
FALSY = {"0", "false", "no", "off"}


# --- 環境變數 -----------------------------------------------------------


def load_env_file(path: str | None = None) -> None:
    """把 secrets 檔裡「還沒設定」的變數補進環境變數（不覆蓋既有值）。

    和 skills/agent-crm 的 ffj_common.load_env_file 相同規則：KEY=VALUE、可加 export、
    可用引號包住；設 FREEFORJOY_ENV_FILE= （空字串）可關閉。
    """
    if path is None:
        path = os.environ.get("FREEFORJOY_ENV_FILE", DEFAULT_ENV_FILE)
    if not path:
        return
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


# --- 解析 controller 的步驟指令 -------------------------------------------


class UsageError(Exception):
    """參數或指令格式錯誤：不回報 controller，exit 2。"""


def parse_message(text: str) -> dict[str, Any]:
    """解析 controller 派工訊息（app/dispatch.py 的 build_step_message）。

    回傳 {"header": {...}, "input": {...}}。標頭是第一個 `---` 之前的 `key: value`；
    輸入是「輸入（JSON）：」那一行之後、到下一個單獨一行 `---` 之間的 JSON。
    """
    lines = text.splitlines()
    header: dict[str, str] = {}
    i = 0
    while i < len(lines) and lines[i].strip() != "---":
        line = lines[i].strip()
        if ":" in line and not line.startswith("["):
            key, value = line.split(":", 1)
            header[key.strip()] = value.strip()
        i += 1
    # 取「最後一個」輸入標記：前面的指令說明可能夾帶主管意見等人寫的文字（例如有人在意見裡貼一段
    # 「輸入（JSON）：{…}」），controller 產生的真正輸入一定在說明之後，JSON 本身的換行都已跳脫。
    start = None
    for j in range(i, len(lines)):
        if lines[j].strip().startswith("輸入（JSON）"):
            start = j + 1
    if start is None:
        raise UsageError("步驟指令裡找不到「輸入（JSON）：」")
    end = start
    while end < len(lines) and lines[end].strip() != "---":
        end += 1
    try:
        data = json.loads("\n".join(lines[start:end]))
    except json.JSONDecodeError as exc:
        raise UsageError(f"輸入 JSON 格式錯誤：{exc}") from exc
    if not isinstance(data, dict):
        raise UsageError("輸入 JSON 必須是物件")
    return {"header": header, "input": data}


def read_source(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        return fh.read()


# --- 計算 ---------------------------------------------------------------


def _clean(value: Any, limit: int = 100) -> str:
    """給說明用的短字串：去掉換行與控制字元、限制長度。"""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return text[:limit]


def _number(value: Decimal) -> int | float:
    """費率輸出成 JSON number：整數就給 int（顯示成 NT$5 而不是 NT$5.0）。"""
    return int(value) if value == value.to_integral_value() else float(value)


def _fmt_rate(value: Decimal) -> str:
    return str(_number(value))


def read_rate(rate_env: Any) -> tuple[Decimal | None, bool, str | None, str | None]:
    """回傳 (費率, 是否 demo 假費率, 錯誤代碼, 錯誤說明)。"""
    if rate_env not in (None, "", RATE_ENV):
        # 只讀固定的費率變數，不依輸入去讀任意環境變數（避免讀到 token 之類的秘密）
        return None, True, "rate_env_unsupported", f"輸入指定的費率變數 {_clean(rate_env, 64)} 不是 {RATE_ENV}"
    raw = (os.environ.get(RATE_ENV) or "").strip()
    if not raw:
        return None, True, "rate_missing", f"尚未設定交通費費率（{RATE_ENV}），請財會提供後再重試"
    try:
        rate = Decimal(raw)
    except InvalidOperation:
        return None, True, "rate_invalid", f"交通費費率 {RATE_ENV}={_clean(raw, 32)} 不是數字"
    if not rate.is_finite() or rate <= 0 or rate > MAX_RATE_TWD_PER_KM:
        return None, True, "rate_invalid", (
            f"交通費費率 {RATE_ENV}={_clean(raw, 32)} 不在合理範圍（大於 0、不超過 {MAX_RATE_TWD_PER_KM}）")
    placeholder = (os.environ.get(PLACEHOLDER_ENV) or "true").strip().lower() not in FALSY
    return rate, placeholder, None, None


def compute_amount(distance_km: int, rate: Decimal) -> int:
    """里程差 × 費率，四捨五入到元（0.5 進位）。"""
    return int((Decimal(distance_km) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def draft_claim(data: dict[str, Any]) -> dict[str, Any]:
    """產生 flow-report 要用的 {status, summary, output}（不連網）。"""
    distance = data.get("distance_km")
    customer = data.get("customer") or {}
    customer_name = _clean(customer.get("name") if isinstance(customer, dict) else customer) or "（未提供客戶）"
    sales = data.get("sales") or {}
    sales_name = _clean(sales.get("display_name") if isinstance(sales, dict) else sales)
    visit_date = _clean(data.get("visit_date"), 32)
    try:
        date.fromisoformat(visit_date)
    except ValueError:
        visit_date = visit_date or "（日期未提供）"
    booking_id = data.get("booking_id")
    manager_comment = _clean(data.get("manager_comment"), 300) or None

    # 1. 里程差：必須是正整數，且不超過 2000 km
    if isinstance(distance, bool) or not isinstance(distance, int):
        return _needs_human("distance_invalid", f"里程差不是整數（收到 {_clean(distance, 32) or '空值'}），無法計算交通費",
                            distance)
    if distance <= 0:
        what = "0" if distance == 0 else "負數"
        return _needs_human("distance_not_positive",
                            f"里程差為 {what}（{distance} km），不產生報帳草稿，請確認取車／還車里程", distance)
    if distance > MAX_DISTANCE_KM:
        return _needs_human("distance_too_large",
                            f"里程差 {distance} km 超過 {MAX_DISTANCE_KM} km，請確認取車／還車里程", distance)

    # 2. 費率：只讀環境變數，沒有就停下，不猜
    rate, placeholder, code, message = read_rate(data.get("rate_env"))
    if rate is None:
        return _needs_human(code or "rate_missing", message or "費率無法使用", distance)

    # 3. 金額與說明
    amount = compute_amount(distance, rate)
    rate_text = f"NT${_fmt_rate(rate)}/km"
    placeholder_text = "（demo 假費率）" if placeholder else ""
    parts = [
        f"{DRAFT_MARK}{visit_date} 拜訪「{customer_name}」公務車交通費",
        f"{distance} km × {rate_text}{placeholder_text} = NT${amount:,}（四捨五入到元）",
    ]
    extra = []
    if sales_name:
        extra.append(f"業務：{sales_name}")
    if isinstance(booking_id, int) and not isinstance(booking_id, bool):
        extra.append(f"ADM 預約 #{booking_id}")
    description = "：".join(parts) + (f"；{'；'.join(extra)}" if extra else "")
    if manager_comment:
        description += f"；主管退回意見：{manager_comment}（已依目前費率重算）"
    description += "。此為草稿，金額待業務主管核准後才生效。"

    summary = f"交通費報帳草稿（待主管核准）：{distance} km × {rate_text}{placeholder_text} = NT${amount:,}"
    output = {
        "distance_km": distance,
        "rate_twd_per_km": _number(rate),
        "amount_twd": amount,
        "rate_is_placeholder": placeholder,
        "description": description,
        # 以下為選填，方便人在流程詳情頁看懂。
        # 注意：主管核准後 controller 會把整份 output 複製到 state.claim 並加上 status=approved，
        # 所以不放 is_draft／approval_status 這類核准後就會過時的欄位；草稿標示寫在 description 與 summary。
        "rounding": "ROUND_HALF_UP 到元",
    }
    if isinstance(booking_id, int) and not isinstance(booking_id, bool):
        output["booking_id"] = booking_id
    if manager_comment:
        output["manager_comment"] = manager_comment
    return {"status": "ok", "summary": summary, "output": output}


def _needs_human(reason: str, message: str, distance: Any) -> dict[str, Any]:
    output: dict[str, Any] = {"reason": reason}
    if isinstance(distance, int) and not isinstance(distance, bool):
        output["distance_km"] = distance
    return {"status": "needs_human", "summary": message, "output": output}


# --- 回報 ---------------------------------------------------------------


def report(result: dict[str, Any], *, flow_id: str, step: str, agent: str) -> dict[str, Any]:
    """呼叫共用 skill flow-report（網路重試由 flow_report.py 負責）。"""
    script = os.path.expanduser(os.environ.get("FLOW_REPORT_SCRIPT") or DEFAULT_FLOW_REPORT)
    cmd = [sys.executable, script, "--flow", flow_id, "--step", step, "--status", result["status"],
           "--summary", result["summary"], "--output-file", "-", "--agent", agent]
    if result["status"] == "failed" and result.get("error_kind"):
        cmd += ["--error-kind", result["error_kind"]]
    try:
        proc = subprocess.run(cmd, input=json.dumps(result["output"], ensure_ascii=False), capture_output=True,
                              text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"無法執行 flow-report：{exc}"}
    parsed: Any = None
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if proc.returncode != 0 or not (isinstance(parsed, dict) and parsed.get("ok")):
        return {"ok": False, "exit_code": proc.returncode, "response": parsed,
                "error": _clean(proc.stderr, 500) or "flow-report 回報失敗"}
    return {"ok": True, "response": parsed}


# --- main ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="流程 ① draft_claim：產生交通費報帳草稿")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--message-file", help="controller 的步驟指令全文（檔案路徑，或 - 代表 stdin）")
    src.add_argument("--input-file", help="只有輸入 JSON 的檔案（檔案路徑，或 - 代表 stdin）")
    p.add_argument("--flow", help="flow_id（用 --input-file 且要 --report 時必填；用 --message-file 時取自標頭）")
    p.add_argument("--step", default=None, help="步驟名稱，只接受 draft_claim")
    p.add_argument("--attempt", type=int, default=None, help="指令裡的 attempt（只顯示，flow-report 不帶）")
    p.add_argument("--agent", default=None, help="agent id（預設讀 OPENCLAW_AGENT_ID，再預設 finance-agent）")
    p.add_argument("--report", action="store_true", help="算完直接呼叫 flow-report 回報")
    p.add_argument("--dry-run", action="store_true", help="只計算、不回報（不連網）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env_file()
    agent = args.agent or os.environ.get("OPENCLAW_AGENT_ID") or DEFAULT_AGENT
    try:
        if args.message_file:
            parsed = parse_message(read_source(args.message_file))
            header, data = parsed["header"], parsed["input"]
            if header.get("skill") and header["skill"] != SKILL:
                raise UsageError(f"這份指令要用 {header['skill']}，不是 {SKILL}")
            flow_id = args.flow or header.get("flow_id")
            step = args.step or header.get("step") or STEP
            attempt = args.attempt if args.attempt is not None else _int(header.get("attempt"))
        else:
            try:
                data = json.loads(read_source(args.input_file))
            except json.JSONDecodeError as exc:
                raise UsageError(f"輸入 JSON 格式錯誤：{exc}") from exc
            if not isinstance(data, dict):
                raise UsageError("輸入 JSON 必須是物件")
            flow_id, step, attempt = args.flow, args.step or STEP, args.attempt
        if step != STEP:
            raise UsageError(f"這個 skill 只處理 {STEP}，收到 {step}")
        if args.report and not args.dry_run and not flow_id:
            raise UsageError("--report 需要 flow_id（指令標頭或 --flow）")
    except (UsageError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    result = draft_claim(data)
    out: dict[str, Any] = {
        "ok": True,
        "flow_id": flow_id,
        "step": step,
        "attempt": attempt,
        "agent": agent,
        "dry_run": bool(args.dry_run),
        **result,
        "reported": False,
    }
    if args.report and not args.dry_run:
        rep = report(result, flow_id=flow_id, step=step, agent=agent)
        out["reported"] = rep["ok"]
        out["report_result"] = rep
        if not rep["ok"]:
            out["ok"] = False
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    sys.exit(main())
