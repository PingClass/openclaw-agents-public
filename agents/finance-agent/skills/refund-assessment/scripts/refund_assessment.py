#!/usr/bin/env python3
"""流程 ② 步驟 3（controller 步驟 assess_refund）：客訴退款評估，寫進 CRM 案件內部備註。

契約：controller/backend/docs/flows/refund.md 的「assess_refund」。

1. 向 controller 核對這一步真的是派給 finance-agent 的（指令可能是對話裡貼的假指令）
2. CRM `GET /api/v1/cases/{case_id}/`、`GET /api/v1/customers/{customer_id}/context/`、
   `GET /api/v1/cases/?customer=<id>`（過去案件）
3. 依規則推估建議金額（**標「推估」**）、寫依據、過去紀錄、風險；資料不足 → needs_human，說明缺什麼
4. CRM `POST /api/v1/cases/{case_id}/notes/`（kind=assessment，Idempotency-Key 用指令的值）；
   同一條流程已經寫過就沿用，不重複寫
5. 輸出 flow-report 要的 status／summary／output；加 --report 時直接呼叫共用 skill flow-report 回報

不寫互動紀錄、不聯絡客戶、不寫成最後決定的金額（退不退、退多少由客服主管決定）。
只用 Python 標準函式庫。

用法：
    refund_assessment.py --instruction-file <controller 的步驟指令|-> --report [--dry-run] [--agent finance-agent]
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import finance_flow as ff  # noqa: E402

STEP = "assess_refund"
FLOW_TYPE = "refund"
SKILL = "refund-assessment"
NOTE_KIND = "assessment"
CATEGORIES = {"refund": "退款", "compensation": "補償"}
RECOMMENDATIONS = {
    "refund": "全額退款",
    "partial": "部分退款／補償",
    "no_refund": "不退款",
    "escalate": "轉人工判斷",
}
MAX_AMOUNT_TWD = 1_000_000
DEFAULT_POLICY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "references", "refund-policy.json")
# 指令輸入裡要和 controller 保存的一致的欄位（寫入前核對）。
# customer_name／external_customer_id 會寫進備註標題，也要核對，避免被改過的指令把文字塞進備註。
VERIFY_FIELDS = ("case_id", "customer_id", "external_customer_id", "customer_name", "category", "summary")
TZ = timezone(timedelta(hours=8))

# 金額寫法（和 refund.md draft_reply 的檢查一致）：NT$1,200、$1200、1,200 元、1200 塊、新台幣 1200；全形先轉半形
# 小數（37.5 元、NT$1,200.50）取整數部分；數字前面不能緊接數字、小數點或逗號，
# 免得「1200.5 元」被讀成 5 元。
_NUM = r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
AMOUNT_RE = re.compile(rf"(?:NT\s*\$?|新台幣|台幣|\$)\s*{_NUM}|{_NUM}\s*(?:元|塊)", re.IGNORECASE)
# 數量：阿拉伯數字（全形先轉半形）或中文數字（三箱、兩包、十二袋、十幾箱、二十幾包），後面接單位。
# 「第三箱」「第 3 箱」是序數，不算數量。前面緊接數字、小數點或中文數字時不從中間開始讀
# （免得「九千九百九十九箱」被讀成「千九百九十九」、「1.5 箱」被讀成 5 箱、「第十三箱」被讀成 3 箱）。
QTY_UNITS = "包|箱|袋|盒|件"
_CN_NUM_CHARS = "零〇一二兩三四五六七八九十百千幾"
QTY_RE = re.compile(rf"(?<![\d.第{_CN_NUM_CHARS}])(?<!第\s)"
                    rf"(\d{{1,4}}|[{_CN_NUM_CHARS}]{{1,8}})\s*({QTY_UNITS})")
CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000}
# 和數量同一句的這些字代表「受影響的數量」（破損、少送……），有多個數量時優先取這個
DAMAGE_WORDS = ("破", "壞", "損", "少送", "少了", "短少", "缺", "漏", "退冰", "融化", "結塊", "發霉", "變質",
                "過期", "爛", "沒送", "沒收到", "沒到", "不見", "壓扁", "不能用")
CLAUSE_SPLIT = re.compile(r"[，,。．.；;、！!？?\n]")
ORDER_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,5}-\d{4,})(?!\d)")
# 客戶聲稱「主管已同意」之類的話：只記成風險，絕不當成核准
CLAIM_WORDS = ("主管已經同意", "主管同意", "主管說", "經理已經", "老闆已經", "已經核准", "直接退", "不用評估", "不用再審")
LOSS_WORDS = ("損失", "停賣", "停業", "營業", "賠償損失")
LEVELS = {"低": 0, "中": 1, "高": 2}


# --- 文字工具 -------------------------------------------------------------


def norm(text) -> str:
    """全形轉半形（ＮＴ＄１,２００ → NT$1,200），去掉控制字元。"""
    if not isinstance(text, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", unicodedata.normalize("NFKC", text))


def clean(value, limit: int = 100) -> str:
    """給備註用的短字串：不放「核定」字樣（評估不是最後決定）。"""
    text = re.sub(r"\s+", " ", norm(str(value or ""))).strip()
    return text.replace("核定", "決定")[:limit]


def twd(amount: int) -> str:
    return f"NT${amount:,}"


def amounts_in(text: str) -> list:
    out = []
    for m in AMOUNT_RE.finditer(norm(text)):
        raw = m.group(1) or m.group(2)
        value = int(raw.replace(",", ""))
        if 0 < value <= MAX_AMOUNT_TWD and value not in out:
            out.append(value)
    return out


def cn_number(text: str) -> tuple | None:
    """中文數字 → (數值, 是否約略)。「十幾」「二十幾」以下限推估（11、21）；只有「幾」「好幾」沒有下限，不算。"""
    if text.isdigit():
        return int(text), False
    # 「幾箱」「好幾箱」沒有下限；「幾十箱」「幾百包」是「好幾個十／百」，下限不明確，也不算
    if "幾" in text and (text.startswith("幾") or not any(u in text for u in CN_UNITS)):
        return None
    total, current = 0, None
    for ch in text:
        if ch in CN_DIGITS:
            current = CN_DIGITS[ch]
        elif ch == "幾":
            current = 1
        elif ch in CN_UNITS:
            total += (1 if current is None else current) * CN_UNITS[ch]
            current = None
    total += current or 0
    return (total, "幾" in text) if total > 0 else None


def quantities_in(text: str) -> list:
    """文字裡的數量：[{qty, unit, raw, approx, damage}]，依出現順序。

    damage：和數量同一句（以標點分句）裡有破損／少送之類的字；「訂了 10 包其中 3 包破掉」這種同一句有多個數量時，
    只算離那個字最近的數量。
    """
    # 只做全形轉半形，不用 norm()：norm 會把換行變成空白，主旨、說明、summary 之間就不分句了
    text = unicodedata.normalize("NFKC", text if isinstance(text, str) else "")
    found = []
    for m in QTY_RE.finditer(text):
        parsed = cn_number(m.group(1))
        if not parsed or parsed[0] <= 0:
            continue
        start = max([0] + [x.end() for x in CLAUSE_SPLIT.finditer(text, 0, m.start())])
        nxt = CLAUSE_SPLIT.search(text, m.end())
        end = nxt.start() if nxt else len(text)
        distance = None
        for word in DAMAGE_WORDS:
            for w in re.finditer(re.escape(word), text[start:end]):
                pos = start + w.start()
                gap = m.start() - (pos + len(word)) if pos < m.start() else pos - m.end()
                distance = gap if distance is None else min(distance, gap)
        found.append({"qty": parsed[0], "unit": m.group(2), "raw": m.group(0), "approx": parsed[1],
                      "start": m.start(), "clause": (start, end), "distance": distance})
    # 同一句有多個數量時，破損字只算給最近的那一個
    for item in found:
        rivals = [o for o in found if o["clause"] == item["clause"] and o["distance"] is not None]
        item["damage"] = item["distance"] is not None and item["distance"] == min(o["distance"] for o in rivals)
    return found


def pick_quantity(text: str) -> tuple:
    """回傳 (數量 dict 或 None, 說明文字或 None, 是否列為風險)。

    規則：只有一種數量 → 用它；有多種 → 優先取和破損／少送同一句的數量（最近的那個），
    沒有這種句子就用第一個數量，並列為風險。
    """
    found = quantities_in(text)
    if not found:
        return None, None, False
    distinct = []
    for item in found:
        if (item["qty"], item["unit"]) not in [(d["qty"], d["unit"]) for d in distinct]:
            distinct.append(item)
    if len(distinct) == 1:
        return distinct[0], None, False
    listed = "、".join(f"{d['qty']} {d['unit']}" for d in distinct)
    damaged = [d for d in found if d["damage"]]
    if damaged:
        chosen = min(damaged, key=lambda d: (d["distance"], d["start"]))
        if len({(d["qty"], d["unit"]) for d in damaged}) == 1:
            return chosen, f"提到多個數量（{listed}），以破損／少送的 {chosen['qty']} {chosen['unit']}推估", False
        return (chosen, f"提到多個破損／少送數量（{listed}），以最接近描述的 {chosen['qty']} {chosen['unit']}推估，需確認",
                True)
    first = distinct[0]
    return first, f"提到多個數量（{listed}），看不出哪個是受影響的數量，以第一個 {first['qty']} {first['unit']}推估", True


def local_day(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    local = parsed.astimezone(TZ)
    return f"{local.month}/{local.day}"


# --- 價目表與政策 ---------------------------------------------------------


def load_policy() -> dict:
    """價目表與退款政策（JSON）。預設是 references/refund-policy.json（demo 假價格）。"""
    path = os.path.expanduser(ff.env("REFUND_POLICY_FILE") or DEFAULT_POLICY_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ff.ConfigError(f"讀不到退款政策檔 {path}：{exc}") from exc
    policy = data.get("policy") if isinstance(data, dict) else None
    products = data.get("products") if isinstance(data, dict) else None
    if not isinstance(policy, dict) or not isinstance(products, list):
        raise ff.ConfigError(f"退款政策檔 {path} 格式不對（要有 policy 與 products）")
    for key in ("refund_ratio", "compensation_ratio"):
        ratio = policy.get(key)
        if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 <= ratio <= 1:
            raise ff.ConfigError(f"退款政策檔 {path} 的 {key} 要是 0～1 的數字")
    return {
        "is_placeholder": data.get("is_placeholder", True) is not False,
        "refund_ratio": Decimal(str(policy["refund_ratio"])),
        "compensation_ratio": Decimal(str(policy["compensation_ratio"])),
        "medium_amount_twd": int(policy.get("medium_amount_twd") or 10_000),
        "escalate_amount_twd": int(policy.get("escalate_amount_twd") or 50_000),
        "repeat_cases_escalate": int(policy.get("repeat_cases_escalate") or 2),
        "products": [p for p in products if isinstance(p, dict) and isinstance(p.get("name"), str)
                     and isinstance(p.get("unit_price_twd"), int) and p["unit_price_twd"] > 0],
    }


def match_product(text: str, products: list) -> dict | None:
    """在文字裡找價目表的商品：名稱或別名，長的優先（「黑糖粉圓（6kg 箱裝）」先於「黑糖粉圓」）。"""
    compact = re.sub(r"\s+", "", norm(text))
    candidates = []
    for product in products:
        for name in [product["name"], *(product.get("aliases") or [])]:
            key = re.sub(r"\s+", "", norm(name))
            if key:
                candidates.append((len(key), key, product))
    for _length, key, product in sorted(candidates, key=lambda c: -c[0]):
        if key in compact:
            return product
    return None


# --- 評估（不連網，測試直接呼叫）-----------------------------------------


def assess(inp: dict, case: dict, context: dict, past_cases: list, policy: dict) -> dict:
    """回傳 {"missing": [...]}（資料不足）或評估結果 dict。"""
    category = inp["category"]
    case_text = "\n".join(norm(case.get(k)) for k in ("subject", "description")) + "\n" + norm(inp.get("summary"))
    orders = sorted(set(ORDER_RE.findall(case_text)))
    interactions = [i for i in (context.get("interactions") or []) if isinstance(i, dict)]
    related = [i for i in interactions if orders and any(o in norm(i.get("summary")) for o in orders)]

    missing: list = []
    if not (norm(case.get("subject")).strip() or norm(case.get("description")).strip()):
        missing.append("案件內容（CRM 案件的主旨與說明都是空的）")

    risks: list = []
    assumptions: list = []
    product = match_product(case_text, policy["products"])

    # 1. 訂單金額：案件內容 → 提到同一訂單的互動紀錄 → 價目表
    base = None
    source = None
    found = amounts_in(case_text)
    if not found:
        for row in related:
            found += [a for a in amounts_in(row.get("summary")) if a not in found]
        if found:
            source = "提到同一訂單的互動紀錄"
    else:
        source = "案件內容"
    if found:
        base = max(found)
        if len(found) > 1:
            risks.append(("中", f"提到多個金額（{'、'.join(twd(a) for a in sorted(found))}），以最高的 {twd(base)} 為上限推估"))
    elif product:
        # 數量另外用保留換行的文字解析（norm 會把說明裡的換行變成空白，分句就不準了）
        qty_text = "\n".join(unicodedata.normalize("NFKC", v) for v in
                             (case.get("subject"), case.get("description"), inp.get("summary")) if isinstance(v, str))
        picked, note, note_is_risk = pick_quantity(qty_text)
        qty = picked["qty"] if picked else 1
        if not picked:
            assumptions.append(f"沒有寫數量，以 1 {product.get('unit') or '單位'}推估")
            risks.append(("中", "數量未提供"))
        elif picked["approx"]:
            assumptions.append(f"數量寫「{clean(picked['raw'], 20)}」，以下限 {qty} {picked['unit']}推估")
            risks.append(("中", "數量是約略的說法"))
        # 只是挑了破損／少送那個數量不算風險；挑不出來或有多個破損數量才列為風險
        if note and note_is_risk:
            risks.append(("中", note))
        elif note:
            assumptions.append(note)
        base = product["unit_price_twd"] * qty
        tag = "（demo 假價格）" if policy["is_placeholder"] else ""
        source = f"價目表 {product['name'][:40]} {twd(product['unit_price_twd'])}{tag} × {qty}"
    else:
        missing.append("訂單金額（案件內容與互動紀錄都沒有寫金額，CRM 也沒有訂單資料）")
        missing.append("可對應價目表的商品名稱（無法用價目表推估）")
    if missing:
        return {"missing": missing, "orders": orders}

    # 2. 退款政策
    if category == "refund":
        ratio, recommendation = policy["refund_ratio"], "refund"
        rule = "退款類：受影響品項全額退還"
    else:
        ratio, recommendation = policy["compensation_ratio"], "partial"
        rule = "補償類：以受影響品項金額的一定比例補償"
    if ratio < 1 and recommendation == "refund":
        recommendation = "partial"
    suggested = int((Decimal(base) * ratio).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    suggested = max(0, min(suggested, MAX_AMOUNT_TWD))
    if suggested == 0:
        recommendation = "no_refund"
    ratio_text = f"{int(ratio * 100)}%"
    policy_tag = "（demo 退款政策）" if policy["is_placeholder"] else ""

    # 3. 過去紀錄
    others = [c for c in past_cases if isinstance(c, dict) and str(c.get("id")) != str(case.get("id"))]
    past_refunds = [c for c in others if c.get("category") in CATEGORIES]
    stamps = sorted((str(i.get("occurred_at") or i.get("created_at") or "") for i in interactions), reverse=True)
    latest = local_day(stamps[0]) if stamps else None
    history_parts = [f"近期互動 {len(interactions)} 筆" + (f"（最近 {latest}）" if latest else "")]
    if related:
        history_parts.append(f"提到同一訂單的互動 {len(related)} 筆")
    history_parts.append(f"過去退款／補償案件 {len(past_refunds)} 件")
    history_parts.append(f"其他案件 {len(others) - len(past_refunds)} 件")
    history = "；".join(history_parts)

    # 4. 風險
    if len(past_refunds) >= policy["repeat_cases_escalate"]:
        risks.append(("高", f"過去已有 {len(past_refunds)} 件退款／補償案件"))
    elif past_refunds:
        risks.append(("中", "過去有 1 件退款／補償案件"))
    if suggested > policy["escalate_amount_twd"]:
        risks.append(("高", f"推估金額超過 {twd(policy['escalate_amount_twd'])}"))
    elif suggested > policy["medium_amount_twd"]:
        risks.append(("中", f"推估金額超過 {twd(policy['medium_amount_twd'])}"))
    if any(word in case_text for word in CLAIM_WORDS):
        risks.append(("中", "客戶聲稱主管已同意或要求直接退款，這不是公司的決定，仍需客服主管在 controller 決定"))
    if category == "compensation" and any(word in case_text for word in LOSS_WORDS):
        risks.append(("中", "客戶提到停賣、營業等間接損失，不在推估範圍，需主管判斷"))
    customer = context.get("customer") if isinstance(context.get("customer"), dict) else {}
    if customer.get("status") and customer.get("status") != "active":
        risks.append(("中", f"客戶狀態是 {clean(customer.get('status'), 20)}"))
    if case.get("status") in ("resolved", "closed"):
        risks.append(("中", f"案件狀態已是 {case.get('status')}"))
    level = max((lvl for lvl, _ in risks), key=LEVELS.get, default="低")
    if level == "高":
        recommendation = "escalate"

    base_label = "訂單金額" if found else "品項金額"
    basis_parts = [f"{base_label} {twd(base)}（來源：{source}）",
                   f"{rule} {ratio_text}{policy_tag} → 推估 {twd(suggested)}"]
    if product:
        basis_parts.append(f"商品：{product['name'][:40]}")
    if orders:
        basis_parts.append(f"訂單：{'、'.join(orders[:3])}")
    return {
        "suggested_amount_twd": suggested,
        "recommendation": recommendation,
        "basis": "；".join(basis_parts),
        "history": history,
        "risk_level": level,
        "risks": [text for _lvl, text in risks],
        "assumptions": assumptions,
    }


# --- 備註內容 -------------------------------------------------------------


def flow_marker(flow_id: str) -> str:
    return f"流程：{flow_id}（finance-agent／{SKILL}）"


def note_body(inp: dict, flow_id: str, ev: dict) -> str:
    """寫進 CaseNote kind=assessment 的內容。只用輸入與 CRM 資料組成（重試時一字不差）。

    不放電話、地址，也不引用客戶原文（避免把個資或客戶自稱的「已核准」抄進評估）。
    """
    who = clean(inp.get("customer_name") or inp.get("external_customer_id") or "客戶", 40)
    lines = [
        f"【退款評估・推估】客戶「{who}」的{CATEGORIES[inp['category']]}案件",
        f"建議金額（推估）：{twd(ev['suggested_amount_twd'])}",
        f"建議處理：{RECOMMENDATIONS[ev['recommendation']]}（{ev['recommendation']}）",
        f"依據：{ev['basis']}",
        f"過去紀錄：{ev['history']}",
        f"風險：{ev['risk_level']}" + (f"｜{'；'.join(ev['risks'])}" if ev["risks"] else ""),
    ]
    if ev["assumptions"]:
        lines.append(f"假設：{'；'.join(ev['assumptions'])}")
    lines += [
        "說明：以上金額是財會的推估，只供客服主管決定時參考；退不退、退多少由客服主管決定，不得對客戶承諾。",
        flow_marker(flow_id),
    ]
    return "\n".join(lines)


_LINE = {
    "suggested": re.compile(r"^建議金額（推估）：NT\$([\d,]+)\s*$", re.M),
    "recommendation": re.compile(r"^建議處理：.*（(refund|partial|no_refund|escalate)）\s*$", re.M),
    "basis": re.compile(r"^依據：(.*)$", re.M),
    "history": re.compile(r"^過去紀錄：(.*)$", re.M),
    "risk": re.compile(r"^風險：(.*)$", re.M),
}


def parse_note(body: str) -> dict:
    """從已經寫進 CRM 的評估備註取回欄位（沿用既有備註時，回報的數字以 CRM 上那一筆為準）。"""
    out: dict = {}
    for name, regex in _LINE.items():
        m = regex.search(body or "")
        if m:
            out[name] = m.group(1).strip()
    if "suggested" in out:
        out["suggested"] = int(out["suggested"].replace(",", ""))
    return out


# --- 主流程 ---------------------------------------------------------------


def check_input(inp: dict) -> None:
    missing = [k for k in ("case_id", "customer_id", "category", "summary")
               if inp.get(k) in (None, "") or (isinstance(inp.get(k), str) and not inp[k].strip())]
    if missing:
        raise ff.StepFailure(ff.result("needs_human", f"輸入缺少 {', '.join(missing)}，無法推估退款評估",
                                       {"reason": "input_missing", "missing": missing}))
    if inp.get("category") not in CATEGORIES:
        raise ff.StepFailure(ff.result(
            "needs_human", f"案件分類 {clean(inp.get('category'), 20)} 不是 refund／compensation，不做退款推估",
            {"reason": "category_invalid"}))


def read_case(crm: ff.Crm, inp: dict) -> tuple:
    case_id, customer_id = str(inp["case_id"]), str(inp["customer_id"])
    status, case = crm.get(f"/v1/cases/{ff.quote(case_id)}/")
    if status != 200 or not isinstance(case, dict):
        raise ff.crm_failure(status, case, f"讀取案件 {case_id}")
    if str(case.get("customer")) != customer_id:
        raise ff.StepFailure(ff.result(
            "needs_human", f"案件 {case_id} 的客戶和流程輸入的 customer_id 不同，不推估，請人工確認",
            {"reason": "case_customer_mismatch"}))
    status, context = crm.get(f"/v1/customers/{ff.quote(customer_id)}/context/")
    if status != 200 or not isinstance(context, dict):
        raise ff.crm_failure(status, context, f"讀取客戶 {customer_id} 的完整資料")
    past = crm.paged("/v1/cases/?" + urllib.parse.urlencode({"customer": customer_id}), "讀取客戶的過去案件")
    return case, context, past


def handle(ctx: dict, args) -> dict:
    inp = ctx["input"]
    check_input(inp)
    if args.dry_run:
        policy = load_policy()
        step = ff.result("ok", "dry-run：不連網、不寫入、不回報（建議金額會標「推估」）", {})
        step["plan"] = [
            "GET controller /api/flows/{flow_id}（核對步驟與輸入）",
            f"GET /api/v1/cases/{inp['case_id']}/",
            f"GET /api/v1/customers/{inp['customer_id']}/context/",
            f"GET /api/v1/cases/?customer={inp['customer_id']}",
            f"GET /api/v1/cases/{inp['case_id']}/notes/?kind=assessment（同一流程寫過就沿用）",
            f"POST /api/v1/cases/{inp['case_id']}/notes/ kind=assessment（Idempotency-Key: {ctx['idempotency_key']}）",
            f"價目表／退款政策：{'demo 假資料' if policy['is_placeholder'] else '正式資料'}",
        ]
        return step

    ff.verify_dispatch(ctx, VERIFY_FIELDS)
    policy = load_policy()
    crm = ff.Crm()
    case, context, past = read_case(crm, inp)
    ev = assess(inp, case, context, past, policy)
    if "missing" in ev:
        return ff.result(
            "needs_human",
            f"資料不足，無法推估退款金額：缺少{'、'.join(ev['missing'])}。請客服在 CRM 案件補上後，由 admin 重試這一步",
            {"reason": "insufficient_data", "missing": ev["missing"]})

    body = note_body(inp, ctx["flow_id"], ev)
    marker = flow_marker(ctx["flow_id"])
    note, created = ff.write_note_once(crm, str(inp["case_id"]), NOTE_KIND, body, ctx["idempotency_key"],
                                       lambda n: marker in (n.get("body") or ""))
    stored = parse_note(note.get("body") or body)
    suggested = stored.get("suggested", ev["suggested_amount_twd"])
    recommendation = stored.get("recommendation", ev["recommendation"])
    output = {
        "note_id": str(note["id"]),
        "suggested_amount_twd": suggested,
        "amount_is_estimate": True,
        "basis": stored.get("basis", ev["basis"]),
        "history": stored.get("history", ev["history"]),
        "recommendation": recommendation,
        "risk": stored.get("risk", ev["risk_level"]),
    }
    again = "" if created else "（同一流程先前已寫過，沿用那一筆）"
    summary = (f"退款評估已寫入案件內部備註{again}：建議 {twd(suggested)}（推估），"
               f"{RECOMMENDATIONS.get(recommendation, recommendation)}，風險{output['risk'].split('｜')[0]}")
    return ff.result("ok", summary, output)


def main(argv=None) -> None:
    ff.run_step(STEP, FLOW_TYPE, "流程 ② assess_refund：客訴退款評估（推估），寫入 CRM 案件內部備註", handle, argv)


if __name__ == "__main__":
    main()
