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
    group: int | None = None  # номер ротации: куски одной смены, разорванные выходными, имеют общий group


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

    Смена длится shift_length_days *дежурных* дней и передаётся в handoff_time по локальному
    времени команды (по «настенным» часам, поэтому переход на летнее время не сдвигает передачу).

    Дежурные дни:
      * день недели входит в weekdays;
      * при skip_holidays праздники и перенесённые выходные не дежурные, а перенесённые рабочие
        дни (рабочая суббота) — дежурные, даже если их нет в weekdays.
    day_kind(day) возвращает тип дня по производственному календарю:
    'holiday' | 'dayoff' | 'workday' | 'short' | None.

    Если дежурные все дни подряд (weekdays=1234567, skip_holidays=False) — обычная сетка
    24/7: смена k начинается в anchor_date + k * shift_length_days.
    Иначе дежурные дни нумеруются от anchor_date и режутся по shift_length_days штук; смена
    никогда не захватывает недежурные дни: если её дни разорваны выходными/праздником, она
    состоит из нескольких кусков с общим group (планировщик ставит на них одного человека).
    Пример: 2 дня, пн–пт → [пн–вт] [ср–чт] [пт + пн] [вт–ср] ...
    """
    tz = ZoneInfo(rule.timezone)
    handoff = parse_hhmm(rule.handoff_time)
    n = rule.shift_length_days
    allowed = {int(c) for c in rule.weekdays}

    def at(day: date) -> datetime:
        return datetime.combine(day, handoff, tz).astimezone(UTC)

    slots: list[Slot] = []

    if allowed == set(range(1, 8)) and not rule.skip_holidays:
        local_start_day = window_start.astimezone(tz).date()
        k = max((local_start_day - rule.anchor_date).days // n - 1, 0)  # с запасом в одну смену назад
        while True:
            day = rule.anchor_date + timedelta(days=k * n)
            start = at(day)
            if start >= window_end:
                break
            end = at(day + timedelta(days=n))
            if end > window_start:
                slots.append(Slot(start, end, k))
            k += 1
        return slots

    # Нумерация дежурных дней от anchor_date и нарезка на куски без недежурных дней.
    last_day = window_end.astimezone(tz).date() + timedelta(days=1)
    idx = 0
    seg_first: date | None = None
    seg_last: date | None = None
    seg_group = -1

    def flush():
        if seg_first is not None:
            start, end = at(seg_first), at(seg_last + timedelta(days=1))
            if end > window_start and start < window_end:
                slots.append(Slot(start, end, seg_group))

    day = rule.anchor_date
    while day <= last_day:
        if day_allowed(rule, day, allowed, day_kind):
            group = idx // n
            idx += 1
            if seg_first is not None and group == seg_group and seg_last == day - timedelta(days=1):
                seg_last = day
            else:
                flush()
                seg_first, seg_last, seg_group = day, day, group
        day += timedelta(days=1)
    flush()
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
