from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, UTCDateTime


def utcnow() -> datetime:
    return datetime.now(UTC)


class Team(Base):
    """Команда = отдельный календарь дежурств со своими правилами ротации."""

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")

    # --- Правила ротации ---
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    shift_length_days: Mapped[int] = mapped_column(Integer, default=7)  # 1 = сутки, 2 = двое суток, 7 = неделя
    people_per_shift: Mapped[int] = mapped_column(Integer, default=1)  # сколько дежурных одновременно
    handoff_time: Mapped[str] = mapped_column(String(5), default="10:00")  # локальное время передачи смены
    anchor_date: Mapped[date] = mapped_column(Date)  # от этой даты отсчитывается сетка смен
    # Фильтры начала смены (полезны для посуточных дежурств «только в рабочие дни»):
    weekdays: Mapped[str] = mapped_column(String(7), default="1234567")  # ISO-дни, в которые смена может начинаться
    skip_holidays: Mapped[bool] = mapped_column(Boolean, default=False)  # не начинать смены в праздники/выходные по произв. календарю
    horizon_days: Mapped[int] = mapped_column(Integer, default=56)  # на сколько дней вперёд планировать
    freeze_days: Mapped[int] = mapped_column(Integer, default=0)  # rebalance не трогает ближайшие N дней
    absence_policy: Mapped[str] = mapped_column(String(20), default="whole")  # whole | split
    notify_days: Mapped[int] = mapped_column(Integer, default=14)  # о перестановках дальше N дней не сообщаем

    # --- Интеграции ---
    mattermost_webhook: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mattermost_channel: Mapped[str | None] = mapped_column(String(200), nullable=True)
    external_calendar: Mapped[str | None] = mapped_column(String(300), nullable=True)  # напр. имя календаря в Exchange

    last_announced_shift_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="team", cascade="all, delete-orphan", order_by="Membership.position"
    )


class Person(Base):
    """Человек, который может дежурить. Ключ — email (совпадает с почтой в Exchange)."""

    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    mattermost_username: Mapped[str | None] = mapped_column(String(100), nullable=True)

    memberships: Mapped[list[Membership]] = relationship(back_populates="person", cascade="all, delete-orphan")


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("team_id", "person_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)  # место в начальной очереди
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # участвует в ротации

    team: Mapped[Team] = relationship(back_populates="memberships")
    person: Mapped[Person] = relationship(back_populates="memberships")


class Absence(Base):
    """Отпуск/больничный/отгул. Интервал [start, end)."""

    __tablename__ = "absences"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    start: Mapped[datetime] = mapped_column(UTCDateTime)
    end: Mapped[datetime] = mapped_column(UTCDateTime)
    kind: Mapped[str] = mapped_column(String(20), default="vacation")  # vacation | sick | dayoff | other
    source: Mapped[str] = mapped_column(String(50), default="manual")  # manual | имя внешнего источника
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    person: Mapped[Person] = relationship()


class Shift(Base):
    """Смена дежурства (интервал [start, end)). Дежурные — в ShiftAssignment, по одному на место (seat)."""

    __tablename__ = "shifts"
    __table_args__ = (UniqueConstraint("team_id", "start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    start: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    end: Mapped[datetime] = mapped_column(UTCDateTime)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)  # удалена, ждёт снятия из внешнего календаря
    manual: Mapped[bool] = mapped_column(Boolean, default=False)  # период задан вручную, а не сеткой ротации

    # Публикация во внешний календарь (Exchange и т.п.): одно событие на смену, все дежурные — участники
    external_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    publish_dirty: Mapped[bool] = mapped_column(Boolean, default=True)

    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    team: Mapped[Team] = relationship()
    assignments: Mapped[list[ShiftAssignment]] = relationship(
        back_populates="shift", cascade="all, delete-orphan", order_by="ShiftAssignment.seat"
    )


class ShiftAssignment(Base):
    """Место дежурного в смене. person_id=None — место не занято."""

    __tablename__ = "shift_assignments"
    __table_args__ = (UniqueConstraint("shift_id", "seat"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id", ondelete="CASCADE"), index=True)
    seat: Mapped[int] = mapped_column(Integer, default=0)
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), nullable=True, index=True
    )
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)  # назначено вручную, планировщик не трогает

    shift: Mapped[Shift] = relationship(back_populates="assignments")
    person: Mapped[Person | None] = relationship()


class ShiftLog(Base):
    """Журнал изменений назначений."""

    __tablename__ = "shift_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id", ondelete="CASCADE"), index=True)
    seat: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    old_person_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_person_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(50))
    actor: Mapped[str] = mapped_column(String(100), default="planner")


class Holiday(Base):
    """Производственный календарь: праздники, перенесённые выходные и рабочие дни."""

    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("country", "day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    country: Mapped[str] = mapped_column(String(8), default="RU")
    day: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(20), default="holiday")  # holiday | dayoff | workday | short
    source: Mapped[str] = mapped_column(String(50), default="library")  # library | isdayoff | manual


class User(Base):
    """Пользователь сервиса. role: user (меняет график) | admin (ещё и настройки)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str | None] = mapped_column(String(300), nullable=True)  # None = вход только внешним бэкендом
    role: Mapped[str] = mapped_column(String(20), default="user")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    auth_source: Mapped[str] = mapped_column(String(50), default="local")  # local | ldap (в будущем)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ApiToken(Base):
    """Токен доступа. kind: session (вход в веб) | personal (API/MCP/ICS). Храним только хеш."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="personal")
    name: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    user: Mapped[User] = relationship()
