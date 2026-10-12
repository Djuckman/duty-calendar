"""Генерация сетки смен по правилам ротации команды."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class RotationRule:
    timezone: str
    shift_length_days: int  # 1, 2, 7, ...
    handoff_time: str  # "HH:MM" в локальном времени команды
    anchor_date: date
    weekdays: str = "1234567"  # ISO-дни недели, в которые смена может начинаться
    skip_holidays: bool = False

    def __post_init__(self):
        if self.shift_length_days < 1:
            raise ValueError("shift_length_days должен быть >= 1")
        parse_hhmm(self.handoff_time)
        if not self.weekdays or any(c not in "1234567" for c in self.weekdays):
            raise ValueError("weekdays: строка из цифр 1-7 (1 = понедельник)")
        ZoneInfo(self.timezone)


@dataclass(frozen=True)
class Slot:
    start: datetime  # aware UTC
    end: datetime  # aware UTC


def parse_hhmm(value: str) -> time:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"время должно быть в формате HH:MM, получено {value!r}") from exc


def generate_slots(
    rule: RotationRule,
    window_start: datetime,
    window_end: datetime,
    day_kind: Callable[[date], str | None] | None = None,
) -> list[Slot]:
    """Смены, пересекающиеся с окном [window_start, window_end).

    Сетка: смена k начинается в день anchor_date + k * shift_length_days в handoff_time
    по локальному времени команды и длится shift_length_days суток (по «настенным» часам,
    поэтому переходы на летнее время не сдвигают передачу смены).

    Фильтры применяются к дню начала смены:
      * день недели должен входить в weekdays;
      * при skip_holidays праздники и перенесённые выходные пропускаются, а перенесённые
        рабочие дни (рабочая суббота) наоборот разрешены, даже если их нет в weekdays.
    day_kind(day) возвращает тип дня по производственному календарю:
    'holiday' | 'dayoff' | 'workday' | 'short' | None.
    Для дежурств «только по рабочим дням» используйте shift_length_days = 1, weekdays = 12345.
    """
    tz = ZoneInfo(rule.timezone)
    handoff = parse_hhmm(rule.handoff_time)
    n = rule.shift_length_days
    allowed = {int(c) for c in rule.weekdays}

    local_start_day = window_start.astimezone(tz).date()
    k = (local_start_day - rule.anchor_date).days // n - 1  # с запасом в одну смену назад
    k = max(k, 0)

    slots: list[Slot] = []
    while True:
        day = rule.anchor_date + timedelta(days=k * n)
        start = datetime.combine(day, handoff, tz).astimezone(UTC)
        if start >= window_end:
            break
        end = datetime.combine(day + timedelta(days=n), handoff, tz).astimezone(UTC)
        k += 1
        if end <= window_start:
            continue
        if day_allowed(rule, day, allowed, day_kind):
            slots.append(Slot(start, end))
    return slots


def day_allowed(
    rule: RotationRule,
    day: date,
    allowed: set[int] | None = None,
    day_kind: Callable[[date], str | None] | None = None,
) -> bool:
    allowed = allowed if allowed is not None else {int(c) for c in rule.weekdays}
    kind = day_kind(day) if (rule.skip_holidays and day_kind) else None
    if kind == "workday":
        return True
    if day.isoweekday() not in allowed:
        return False
    return kind not in ("holiday", "dayoff")
