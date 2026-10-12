"""Подключение к БД. Все даты хранятся в UTC без таймзоны, наружу отдаются как aware-UTC."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not allowed, use timezone-aware values")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def init_db(url: str) -> Engine:
    global _engine, _SessionLocal
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
    _engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _fk_on(dbapi_conn, _):  # pragma: no cover - простая настройка
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    from . import models  # noqa: F401  регистрируем модели

    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    _drop_shift_unique(_engine)
    _SessionLocal = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def _add_missing_columns(engine: Engine) -> None:
    """Мини-миграция: добавить в существующие таблицы колонки, появившиеся в моделях.
    (Полноценные миграции — Alembic — когда схема начнёт меняться сложнее.)"""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl_type = col.type.compile(dialect=engine.dialect)
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if isinstance(default, bool):
                    default_sql = " DEFAULT " + ("TRUE" if default else "FALSE") if engine.dialect.name != "sqlite" \
                        else f" DEFAULT {int(default)}"
                elif isinstance(default, (int, float)):
                    default_sql = f" DEFAULT {default}"
                elif isinstance(default, str):
                    default_sql = " DEFAULT '" + default.replace("'", "''") + "'"
                else:
                    default_sql = ""
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl_type}{default_sql}'))


def _drop_shift_unique(engine: Engine) -> None:
    """Мини-миграция: снять UNIQUE(team_id, start) с shifts (ручные дежурства — слои поверх сетки)."""
    from sqlalchemy import inspect, text
    from sqlalchemy.schema import CreateIndex, CreateTable

    insp = inspect(engine)
    if not insp.has_table("shifts"):
        return
    uniques = [u for u in insp.get_unique_constraints("shifts") if sorted(u["column_names"]) == ["start", "team_id"]]
    if not uniques:
        return
    table = Base.metadata.tables["shifts"]
    if engine.dialect.name != "sqlite":
        with engine.begin() as conn:
            for u in uniques:
                conn.execute(text(f'ALTER TABLE shifts DROP CONSTRAINT "{u["name"]}"'))
        return
    # SQLite не умеет DROP CONSTRAINT: пересоздаём таблицу
    cols = ", ".join(f'"{c.name}"' for c in table.columns)
    ddl = str(CreateTable(table).compile(dialect=engine.dialect)).replace("CREATE TABLE shifts", "CREATE TABLE shifts_new", 1)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql(ddl)
        conn.exec_driver_sql(f"INSERT INTO shifts_new ({cols}) SELECT {cols} FROM shifts")
        conn.exec_driver_sql("DROP TABLE shifts")
        conn.exec_driver_sql("ALTER TABLE shifts_new RENAME TO shifts")
        for idx in table.indexes:
            conn.exec_driver_sql(str(CreateIndex(idx).compile(dialect=engine.dialect)))
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def get_engine() -> Engine:
    assert _engine is not None, "init_db() не вызван"
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    assert _SessionLocal is not None, "init_db() не вызван"
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
