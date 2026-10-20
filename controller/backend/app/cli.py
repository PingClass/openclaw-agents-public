"""管理指令：`python -m app <指令>`（或 `uv run flow-controller <指令>`）。

- init-db                 建立資料表
- create-user             建立或更新 admin／viewer 帳號
- seed-agents             建立 5 隻助理 agent 的資料
- upsert-agent            新增或更新單一 agent（例如員工 agent、扮客戶的 milim）
- issue-agent-token       發 agent token（只印出一次，資料庫只存 SHA-256）
- revoke-agent-token      停用某隻 agent 的所有 token
- import-identities       從 JSON 匯入或更新 Identity（以 hrm_employee_id 對應）
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import init_db, make_engine, make_session_factory, utcnow
from app.models import ROLE_ADMIN, ROLE_VIEWER, Agent, AgentToken, Identity, User
from app.sanitize import is_sensitive_key
from app.security import hash_password, new_token, sha256_hex

ASSISTANT_AGENTS = [
    ("cs-agent", "客服助理", "💬"),
    ("admin-agent", "行政總務助理", "🗂️"),
    ("finance-agent", "財會助理", "💰"),
    ("it-agent", "IT 服務台助理", "🛠️"),
    ("hr-agent", "人資營運助理", "👥"),
]

IDENTITY_FIELDS = (
    "employee_number",
    "display_name",
    "line_user_id",
    "adm_user_id",
    "crm_user_id",
    "role",
    "department",
    "agent_id",
)


def _session(settings: Settings) -> Session:
    engine = make_engine(settings.db_url)
    init_db(engine)
    return make_session_factory(engine)()


def cmd_init_db(settings: Settings, _args: argparse.Namespace) -> int:
    added = init_db(make_engine(settings.db_url))
    if added:
        print(f"已替既有資料表補上欄位：{', '.join(added)}")
    print("資料表已建立")
    return 0


def cmd_create_user(settings: Settings, args: argparse.Namespace) -> int:
    password = args.password or os.environ.get("FLOW_CONTROLLER_NEW_PASSWORD")
    if not password:
        password = getpass.getpass("密碼：")
    with _session(settings) as db:
        user = db.scalar(select(User).where(User.username == args.username))
        if user is None:
            user = User(username=args.username)
            db.add(user)
        user.password_hash = hash_password(password)
        user.role = args.role
        user.display_name = args.display_name or user.display_name or args.username
        user.disabled = False
        if args.identity:
            identity = db.scalar(select(Identity).where(Identity.hrm_employee_id == args.identity))
            if identity is None:
                print(f"找不到 Identity：{args.identity}", file=sys.stderr)
                return 1
            user.identity_id = identity.id
        db.commit()
        print(f"帳號 {user.username}（{user.role}）已建立或更新")
    return 0


def _upsert_agent(db: Session, agent_id: str, *, display_name: str | None, emoji: str | None,
                  role: str | None, workspace: str | None) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None:
        agent = Agent(id=agent_id, role=role or "assistant",
                      workspace=workspace or f"~/.openclaw/workspace-{agent_id}")
        db.add(agent)
    if display_name is not None:
        agent.display_name = display_name
    if emoji is not None:
        agent.emoji = emoji
    if role is not None:
        agent.role = role
    if workspace is not None:
        agent.workspace = workspace
    agent.enabled = True
    return agent


def cmd_seed_agents(settings: Settings, _args: argparse.Namespace) -> int:
    with _session(settings) as db:
        for agent_id, name, emoji in ASSISTANT_AGENTS:
            _upsert_agent(db, agent_id, display_name=name, emoji=emoji, role="assistant",
                          workspace=None)
        db.commit()
    print("已建立 5 隻助理 agent")
    return 0


def cmd_upsert_agent(settings: Settings, args: argparse.Namespace) -> int:
    with _session(settings) as db:
        agent = _upsert_agent(db, args.id, display_name=args.display_name, emoji=args.emoji,
                              role=args.role, workspace=args.workspace)
        db.commit()
        print(f"agent {agent.id}（{agent.role}）已建立或更新")
    return 0


def cmd_issue_token(settings: Settings, args: argparse.Namespace) -> int:
    with _session(settings) as db:
        agent = db.get(Agent, args.agent)
        if agent is None:
            print(f"找不到 agent {args.agent}，請先 seed-agents 或 upsert-agent", file=sys.stderr)
            return 1
        raw = new_token("fca")
        db.add(
            AgentToken(
                agent_id=agent.id,
                name=args.name or "",
                token_hash=sha256_hex(raw),
                can_simulate=args.can_simulate,
                created_at=utcnow(),
            )
        )
        db.commit()
    # token 只印這一次，請存進 ~/.openclaw/secrets/freeforjoy.env
    print(raw)
    return 0


def cmd_revoke_token(settings: Settings, args: argparse.Namespace) -> int:
    with _session(settings) as db:
        tokens = db.scalars(
            select(AgentToken).where(AgentToken.agent_id == args.agent, AgentToken.revoked_at.is_(None))
        ).all()
        for token in tokens:
            token.revoked_at = utcnow()
        db.commit()
    print(f"已停用 {len(tokens)} 個 token")
    return 0


def import_identities(db: Session, records: list[dict]) -> tuple[int, int]:
    created = updated = 0
    for record in records:
        key = str(record.get("hrm_employee_id") or "").strip()
        if not key:
            raise ValueError(f"缺少 hrm_employee_id：{record}")
        identity = db.scalar(select(Identity).where(Identity.hrm_employee_id == key))
        if identity is None:
            identity = Identity(hrm_employee_id=key, created_at=utcnow())
            db.add(identity)
            created += 1
        else:
            updated += 1
        for field in IDENTITY_FIELDS:
            if field in record and not is_sensitive_key(field):
                value = record[field]
                setattr(identity, field, "" if value is None and field in ("display_name", "role", "department")
                        else (str(value) if value is not None else None))
        identity.updated_at = utcnow()
    db.commit()
    return created, updated


def cmd_import_identities(settings: Settings, args: argparse.Namespace) -> int:
    raw = Path(args.file).read_text(encoding="utf-8") if args.file != "-" else sys.stdin.read()
    records = json.loads(raw)
    if isinstance(records, dict):
        records = records.get("identities", [])
    with _session(settings) as db:
        created, updated = import_identities(db, records)
        agents = 0
        if args.with_agents:
            # 扮演這些人的員工 agent（role=employee），已存在的只更新角色、不動 display_name
            for record in records:
                agent_id = record.get("agent_id")
                if not agent_id:
                    continue
                existing = db.get(Agent, agent_id)
                _upsert_agent(db, agent_id, display_name=None if existing else record.get("display_name"),
                              emoji=None, role="employee", workspace=None)
                agents += 1
            db.commit()
    # 電話、地址等欄位不匯入
    print(f"新增 {created} 筆，更新 {updated} 筆" + (f"；員工 agent {agents} 隻" if args.with_agents else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flow-controller", description="Workflow Controller 管理指令")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="建立資料表").set_defaults(func=cmd_init_db)

    p = sub.add_parser("create-user", help="建立或更新帳號")
    p.add_argument("username")
    p.add_argument("--role", choices=[ROLE_ADMIN, ROLE_VIEWER], default=ROLE_VIEWER)
    p.add_argument("--password", help="不建議在指令列給；可用 FLOW_CONTROLLER_NEW_PASSWORD 或互動輸入")
    p.add_argument("--display-name")
    p.add_argument("--identity", help="對應的 hrm_employee_id（看得到自己收件匣的確認連結）")
    p.set_defaults(func=cmd_create_user)

    sub.add_parser("seed-agents", help="建立 5 隻助理 agent").set_defaults(func=cmd_seed_agents)

    p = sub.add_parser("upsert-agent", help="新增或更新 agent")
    p.add_argument("id")
    p.add_argument("--display-name")
    p.add_argument("--emoji")
    p.add_argument("--role", choices=["assistant", "employee", "simulator"])
    p.add_argument("--workspace")
    p.set_defaults(func=cmd_upsert_agent)

    p = sub.add_parser("issue-agent-token", help="發 agent token（只印一次）")
    p.add_argument("agent")
    p.add_argument("--name", help="備註，例如 2026-10 發")
    p.add_argument("--can-simulate", action="store_true", help="允許呼叫 /api/sim/messages")
    p.set_defaults(func=cmd_issue_token)

    p = sub.add_parser("revoke-agent-token", help="停用 agent 的所有 token")
    p.add_argument("agent")
    p.set_defaults(func=cmd_revoke_token)

    p = sub.add_parser("import-identities", help="從 JSON 匯入 Identity（- 代表 stdin）")
    p.add_argument("file")
    p.add_argument("--with-agents", action="store_true",
                   help="一併建立 agent_id 對應的員工 agent（role=employee）")
    p.set_defaults(func=cmd_import_identities)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(Settings.from_env(), args)


if __name__ == "__main__":
    raise SystemExit(main())
