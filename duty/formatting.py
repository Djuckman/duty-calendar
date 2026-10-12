"""Форматирование для сообщений в мессенджер."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .models import Person

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

REASONS = {
    "new": "новая смена",
    "absent": "отсутствие",
    "inactive": "выключен из ротации",
    "unfilled": "место было свободно",
    "rebalance": "перебалансировка",
    "removed": "смена удалена",
    "manual": "вручную",
    "swap": "обмен",
    "unassigned": "некому дежурить",
}


def fmt_dt(dt: datetime, tz: str) -> str:
    local = dt.astimezone(ZoneInfo(tz))
    return f"{WEEKDAYS[local.weekday()]} {local:%d.%m %H:%M}"


def fmt_range(start: datetime, end: datetime, tz: str) -> str:
    return f"{fmt_dt(start, tz)} – {fmt_dt(end, tz)}"


def mention(person: Person | None) -> str:
    if person is None:
        return "—"
    if person.mattermost_username:
        return f"@{person.mattermost_username}"
    return person.name


def mentions(people: list[Person | None]) -> str:
    real = [p for p in people if p is not None]
    return ", ".join(mention(p) for p in real) if real else "никто"
