"""對外通道（計畫 9.5）：LINE 接上前，客戶訊息與回覆都記在 controller 的模擬通道。

- 收：`POST /sim/messages`（扮客戶的 milim）→ 記一筆 direction=in，轉給 cs-agent。
- 送：流程的「送出回覆」步驟（例如 ② 的 deliver_reply）→ 記一筆 direction=out。
  該客戶有模擬對話時記在同一個 conversation_id，`GET /sim/messages` 讀得到。

controller 只記錄、轉送；**不會因為訊息內容建立或決定任何人工確認**（例如客戶說「主管已經同意」）。
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CHANNEL_SIM, DIRECTION_IN, DIRECTION_OUT, ChannelMessage


def conversation_id(target_agent: str, external_customer_id: str) -> str:
    """同一位模擬客戶與同一隻 agent 的對話 id，也是派工時沿用的 OpenClaw session id。"""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", external_customer_id)[:64]
    return f"sim-{target_agent}-{safe}"


def record_inbound(db: Session, *, now: datetime, agent_id: str, external_customer_id: str,
                   display_name: str, body: str, created_by: str) -> ChannelMessage:
    message = ChannelMessage(
        channel=CHANNEL_SIM,
        direction=DIRECTION_IN,
        conversation_id=conversation_id(agent_id, external_customer_id),
        agent_id=agent_id,
        external_customer_id=external_customer_id,
        display_name=display_name,
        body=body,
        created_by=created_by,
        created_at=now,
    )
    db.add(message)
    db.flush()
    return message


def find_conversation(db: Session, *, agent_id: str, external_customer_id: str | None) -> str | None:
    """這位客戶和這隻 agent 有沒有模擬對話（收過 /sim/messages）。"""
    if not external_customer_id:
        return None
    return db.scalar(
        select(ChannelMessage.conversation_id)
        .where(ChannelMessage.direction == DIRECTION_IN, ChannelMessage.agent_id == agent_id,
               ChannelMessage.external_customer_id == external_customer_id)
        .order_by(ChannelMessage.created_at.desc())
        .limit(1)
    )


def record_outbound(db: Session, *, now: datetime, flow_id: str | None, agent_id: str,
                    external_customer_id: str | None, body: str, created_by: str) -> ChannelMessage:
    message = ChannelMessage(
        channel=CHANNEL_SIM,
        direction=DIRECTION_OUT,
        conversation_id=find_conversation(db, agent_id=agent_id, external_customer_id=external_customer_id),
        agent_id=agent_id,
        external_customer_id=external_customer_id,
        body=body,
        flow_id=flow_id,
        created_by=created_by,
        created_at=now,
    )
    db.add(message)
    db.flush()
    return message
