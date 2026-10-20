"""資料庫連線與共用型別。"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from sqlalchemy import DateTime, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """SQLite 不保存時區：寫入時轉成 UTC naive，讀出時補回 UTC。"""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime 必須帶時區")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):  # noqa: ANN001
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


def make_engine(db_url: str) -> Engine:
    connect_args: dict = {}
    if db_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        path = db_url.removeprefix("sqlite:///")
        if path and path != ":memory:" and not db_url.startswith("sqlite:///:memory:"):
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
    engine = create_engine(db_url, connect_args=connect_args, future=True)

    if db_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


# 既有資料表新增的欄位（沒有導入 Alembic；create_all 不會替既有的表加欄位）。
# 只做「加欄位」這種不會破壞資料的變更：(表, 欄位, SQL 型別與預設值)
ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("identities", "department", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("approvals", "fields", "JSON NOT NULL DEFAULT '[]'"),
    ("identities", "confirm_mode", "VARCHAR(8) NOT NULL DEFAULT 'agent'"),
]


def _add_missing_columns(engine: Engine) -> list[str]:
    added: list[str] = []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in ADDED_COLUMNS:
            if table not in tables:
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                added.append(f"{table}.{column}")
    return added


def init_db(engine: Engine) -> list[str]:
    """建立資料表，並替既有的表補上新欄位。回傳補上的欄位（給管理指令顯示）。"""
    from app import models  # noqa: F401  確保模型已註冊

    added = _add_missing_columns(engine)
    Base.metadata.create_all(engine)
    return added
