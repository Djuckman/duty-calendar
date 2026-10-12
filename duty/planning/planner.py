"""Планировщик назначений.

Чистая логика без БД: на вход сетка смен, существующие смены, участники, отсутствия и история,
на выход — назначения и список изменений. Сервисный слой применяет результат к БД.

В каждой смене `seats` мест (people_per_shift команды): все дежурные равноправные
и выбираются из одной очереди; один человек не может занимать два места в одной смене.

Интерфейс `Planner` имеет две реализации (поле команды `absence_policy`):
  * `whole` — WholeShiftPlanner: если отсутствие задевает хоть часть смены, место
    в смене целиком переходит другому участнику (реализовано);
  * `split` — SplitShiftPlanner: смена делится по границам отсутствия, на время отсутствия
    назначается замена (заготовка).

Правило выбора: из доступных берём того, кто дольше всех не дежурил в этой команде;
при равенстве — у кого меньше смен за период истории, затем по порядку в списке участников. Отсутствующий пропускает очередь и
остаётся первым, поэтому после возвращения берёт ближайшую свободную смену.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from .slots import Slot

Mode = Literal["fix", "rebalance"]
EPOCH = datetime.min.replace(tzinfo=UTC)


class PlanningNotImplemented(NotImplementedError):
    """Выбранная стратегия планирования ещё не реализована."""


# ---------------------------------------------------------------- входные данные


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and self.end > start


@dataclass(frozen=True)
class MemberInfo:
    person_id: int
    position: int
    active: bool = True


@dataclass(frozen=True)
class ExistingSeat:
    seat: int
    person_id: int | None
    pinned: bool = False


@dataclass(frozen=True)
class ExistingShift:
    id: int
    start: datetime
    end: datetime
    seats: tuple[ExistingSeat, ...] = ()

    @classmethod
    def single(cls, id: int, start: datetime, end: datetime, person_id: int | None, pinned: bool = False):
        return cls(id, start, end, (ExistingSeat(0, person_id, pinned),))


@dataclass
class PlanInput:
    now: datetime
    slots: list[Slot]  # сетка смен в окне планирования
    existing: list[ExistingShift]  # смены команды, пересекающиеся с окном
    members: list[MemberInfo]
    seats: int = 1  # дежурных на смену
    absences: dict[int, list[Interval]] = field(default_factory=dict)
    last_duty: dict[int, datetime] = field(default_factory=dict)  # начало последней смены до окна
    duty_count: dict[int, int] = field(default_factory=dict)  # сколько смен было за период истории
    mode: Mode = "fix"
    freeze_until: datetime | None = None  # rebalance не трогает смены, начинающиеся раньше


# ---------------------------------------------------------------- результат


@dataclass
class PlannedSeat:
    seat: int
    person_id: int | None
    pinned: bool


@dataclass
class PlannedShift:
    start: datetime
    end: datetime
    existing_id: int | None
    seats: list[PlannedSeat]

    @property
    def people(self) -> list[int | None]:
        return [s.person_id for s in self.seats]


@dataclass
class Change:
    start: datetime
    end: datetime
    existing_id: int | None
    seat: int
    old_person_id: int | None
    new_person_id: int | None
    reason: str  # new | absent | inactive | unfilled | rebalance | removed


@dataclass
class Conflict:
    existing_id: int | None
    start: datetime
    end: datetime
    seat: int
    person_id: int | None
    reason: str  # absent | inactive | unassigned
    detail: str = ""


@dataclass
class PlanResult:
    shifts: list[PlannedShift] = field(default_factory=list)
    deletions: list[int] = field(default_factory=list)  # id существующих смен, выпавших из сетки
    changes: list[Change] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)


# ---------------------------------------------------------------- интерфейс


class Planner(ABC):
    name: str

    @abstractmethod
    def plan(self, data: PlanInput) -> PlanResult: ...


class BasePlanner(Planner):
    """Общие части: правило выбора и проверки доступности."""

    @staticmethod
    def absence_overlap(data: PlanInput, person_id: int, start: datetime, end: datetime) -> list[Interval]:
        return [a for a in data.absences.get(person_id, []) if a.overlaps(start, end)]

    def unavailability(
        self, data: PlanInput, members: dict[int, MemberInfo], person_id: int | None, start: datetime, end: datetime
    ) -> str | None:
        """Причина, по которой человек не может взять смену, или None."""
        if person_id is None:
            return "unassigned"
        m = members.get(person_id)
        if m is None or not m.active:
            return "inactive"
        if self.absence_overlap(data, person_id, start, end):
            return "absent"
        return None

    def pick(
        self,
        data: PlanInput,
        members: dict[int, MemberInfo],
        last: dict[int, datetime],
        count: dict[int, int],
        start: datetime,
        end: datetime,
        exclude: set[int],
        prefer: list[int] | None = None,
    ) -> int | None:
        # Продолжение смены, разорванной выходными: тот же человек, если может.
        for pid in prefer or []:
            if pid not in exclude and self.unavailability(data, members, pid, start, end) is None:
                return pid
        candidates = [
            m
            for m in members.values()
            if m.person_id not in exclude and self.unavailability(data, members, m.person_id, start, end) is None
        ]
        if not candidates:
            return None
        # дольше всех не дежурил → меньше смен за период → порядок в списке
        candidates.sort(key=lambda m: (last.get(m.person_id, EPOCH), count.get(m.person_id, 0), m.position))
        return candidates[0].person_id


class WholeShiftPlanner(BasePlanner):
    """Отпуск, задевающий хоть часть смены, — место в смене целиком уходит другому участнику."""

    name = "whole"

    def plan(self, data: PlanInput) -> PlanResult:
        res = PlanResult()
        members = {m.person_id: m for m in data.members}
        last = dict(data.last_duty)
        count = dict(data.duty_count)
        group_people: dict[int, list[int]] = {}  # group -> кто дежурил в предыдущем куске этой смены
        now = data.now

        existing_by_start = {e.start: e for e in data.existing}
        slot_starts = {s.start for s in data.slots}

        # Смены, выпавшие из сетки (поменялись правила): будущие без закреплений — удаляем.
        for e in data.existing:
            if e.start not in slot_starts and e.start > now and not any(s.pinned for s in e.seats):
                res.deletions.append(e.id)
                for s in e.seats:
                    if s.person_id is not None:
                        res.changes.append(Change(e.start, e.end, e.id, s.seat, s.person_id, None, "removed"))

        # Обход: сетка + закреплённые/начавшиеся смены вне сетки.
        items: dict[datetime, tuple[Slot, ExistingShift | None, bool]] = {
            s.start: (s, existing_by_start.get(s.start), True) for s in data.slots
        }
        for e in data.existing:
            if e.start not in items and (e.start <= now or any(s.pinned for s in e.seats)):
                items[e.start] = (Slot(e.start, e.end), e, False)

        for start in sorted(items):
            slot, ex, on_grid = items[start]
            end = slot.end if on_grid else ex.end  # при смене длины ротации конец берём из новой сетки
            started = start <= now
            frozen = data.freeze_until is not None and start < data.freeze_until
            old_seats = {s.seat: s for s in (ex.seats if ex else ())}
            n_seats = data.seats if on_grid else 0
            seat_ids = sorted(set(range(n_seats)) | set(old_seats))

            planned: dict[int, PlannedSeat] = {}
            taken: set[int] = set()
            to_fill: list[tuple[int, int | None, str | None]] = []  # (seat, old_person, reason)

            # 1) Неприкосновенные места: закреплённые и занятые в уже начавшейся смене.
            for seat in seat_ids:
                old = old_seats.get(seat)
                if old is None:
                    continue
                if old.pinned or (started and old.person_id is not None):
                    reason = self.unavailability(data, members, old.person_id, start, end)
                    if end > now and reason is not None:
                        res.conflicts.append(Conflict(ex.id, start, end, seat, old.person_id, reason))
                    planned[seat] = PlannedSeat(seat, old.person_id, old.pinned)
                    if old.person_id is not None:
                        taken.add(old.person_id)

            # 2) Остальные: лишние места удаляем, валидные оставляем, прочие — в очередь на заполнение.
            for seat in seat_ids:
                if seat in planned:
                    continue
                old = old_seats.get(seat)
                old_pid = old.person_id if old else None
                if seat >= n_seats:  # мест стало меньше
                    if old_pid is not None:
                        res.changes.append(Change(start, end, ex.id, seat, old_pid, None, "removed"))
                    continue
                if old is None:
                    to_fill.append((seat, None, None))
                    continue
                reason = self.unavailability(data, members, old_pid, start, end)
                if reason is None and old_pid in taken:
                    reason = "duplicate"
                if reason is None and (data.mode == "fix" or frozen):
                    planned[seat] = PlannedSeat(seat, old_pid, False)
                    taken.add(old_pid)
                else:
                    to_fill.append((seat, old_pid, reason or "rebalance"))

            # 3) Заполняем по правилу «дольше всех не дежурил».
            for seat, old_pid, reason in to_fill:
                prefer = group_people.get(slot.group, []) if (on_grid and slot.group is not None) else []
                new = self.pick(data, members, last, count, start, end, exclude=taken, prefer=prefer)
                planned[seat] = PlannedSeat(seat, new, False)
                if new is not None:
                    taken.add(new)
                if reason is None:  # места раньше не было
                    res.changes.append(Change(start, end, ex.id if ex else None, seat, None, new, "new"))
                elif new != old_pid:
                    why = {"unassigned": "unfilled", "duplicate": "rebalance"}.get(reason, reason)
                    res.changes.append(Change(start, end, ex.id if ex else None, seat, old_pid, new, why))
                if new is None:
                    res.conflicts.append(
                        Conflict(ex.id if ex else None, start, end, seat, None, "unassigned", "нет доступных участников")
                    )

            prev_group = set(group_people.get(slot.group, [])) if slot.group is not None else set()
            for pid in taken:
                last[pid] = max(last.get(pid, EPOCH), start)
                if pid not in prev_group:  # кусок той же смены не считаем отдельной сменой
                    count[pid] = count.get(pid, 0) + 1
            if on_grid and slot.group is not None:
                group_people[slot.group] = [planned[k].person_id for k in sorted(planned)
                                            if planned[k].person_id is not None]
            res.shifts.append(
                PlannedShift(start, end, ex.id if ex else None, [planned[k] for k in sorted(planned)])
            )

        return res


class SplitShiftPlanner(BasePlanner):
    """Заготовка: деление смены по границам отсутствия.

    Задуманный алгоритм (для доработки):
      1. Обход сетки как в WholeShiftPlanner.
      2. Если у дежурного отсутствие задевает часть смены, разбить его место на сегменты по
         границам отсутствия (с округлением до handoff_time): основному оставить сегменты вне
         отсутствия, на сегменты внутри выбрать замену через self.pick().
      3. Хранить сегменты как подзаписи места (понадобится таблица overrides: shift_id, seat,
         start, end, person_id) — основное назначение при этом не меняется.
      4. Счётчик «дольше всех не дежурил» обновлять только для основного дежурного.
    """

    name = "split"

    def plan(self, data: PlanInput) -> PlanResult:
        raise PlanningNotImplemented(
            "Стратегия 'split' (деление смены при частичном отсутствии) ещё не реализована. "
            "Используйте absence_policy='whole'."
        )


PLANNERS: dict[str, type[Planner]] = {
    WholeShiftPlanner.name: WholeShiftPlanner,
    SplitShiftPlanner.name: SplitShiftPlanner,
}


def get_planner(policy: str) -> Planner:
    try:
        return PLANNERS[policy]()
    except KeyError:
        raise ValueError(f"неизвестная стратегия {policy!r}, доступны: {', '.join(PLANNERS)}") from None
