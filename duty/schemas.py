"""Pydantic-схемы API и функции сериализации (используются и REST, и MCP)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import Absence, ApiToken, Holiday, Membership, Person, Shift, ShiftLog, Team, User
from .services import ChangeView, ConflictView, PlanReport


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- люди и команды


class PersonOut(ORM):
    id: int
    email: str
    name: str
    mattermost_username: str | None = None


class PersonIn(BaseModel):
    email: str
    name: str | None = None
    mattermost_username: str | None = None


class MemberOut(BaseModel):
    email: str
    name: str
    mattermost_username: str | None
    position: int
    active: bool


class MemberIn(PersonIn):
    position: int | None = None
    active: bool = True


class MemberPatch(BaseModel):
    active: bool | None = None
    position: int | None = None


class TeamFields(BaseModel):
    name: str | None = None
    description: str | None = None
    timezone: str | None = None
    shift_length_days: int | None = Field(None, description="Длина смены в днях: 1, 2, 7…")
    people_per_shift: int | None = Field(None, description="Сколько дежурных одновременно")
    handoff_time: str | None = Field(None, description="Время передачи смены HH:MM (локальное)")
    anchor_date: date | None = Field(None, description="Дата, от которой отсчитывается сетка смен")
    weekdays: str | None = Field(None, description="ISO-дни начала смены, напр. 12345")
    skip_holidays: bool | None = Field(None, description="Не начинать смены в праздники и выходные по произв. календарю")
    horizon_days: int | None = None
    freeze_days: int | None = None
    absence_policy: Literal["whole", "split"] | None = None
    notify_days: int | None = None
    mattermost_webhook: str | None = None
    mattermost_channel: str | None = None
    external_calendar: str | None = None


class TeamIn(TeamFields):
    slug: str


class TeamOut(ORM):
    slug: str
    name: str
    description: str
    timezone: str
    shift_length_days: int
    people_per_shift: int
    handoff_time: str
    anchor_date: date
    weekdays: str
    skip_holidays: bool
    horizon_days: int
    freeze_days: int
    absence_policy: str
    notify_days: int
    mattermost_webhook: str | None
    mattermost_channel: str | None
    external_calendar: str | None
    members: list[MemberOut] = []


def member_out(m: Membership) -> MemberOut:
    return MemberOut(email=m.person.email, name=m.person.name, mattermost_username=m.person.mattermost_username,
                     position=m.position, active=m.active)


def team_out(t: Team) -> TeamOut:
    out = TeamOut.model_validate(t)
    out.members = [member_out(m) for m in sorted(t.memberships, key=lambda m: m.position)]
    return out


# ---------------------------------------------------------------- смены


class SeatOut(BaseModel):
    seat: int
    person: PersonOut | None
    pinned: bool


class ShiftOut(BaseModel):
    id: int
    team: str
    team_name: str
    start: datetime
    end: datetime
    people_per_shift: int
    manual: bool = False
    custom_period: bool = False  # даты смены по графику изменены вручную
    slot_start: datetime | None = None
    seats: list[SeatOut]


def shift_out(sh: Shift) -> ShiftOut:
    return ShiftOut(
        id=sh.id,
        team=sh.team.slug,
        team_name=sh.team.name,
        start=sh.start,
        end=sh.end,
        people_per_shift=sh.team.people_per_shift,
        manual=sh.manual,
        custom_period=sh.custom_period,
        slot_start=sh.slot_start,
        seats=[
            SeatOut(seat=a.seat, person=PersonOut.model_validate(a.person) if a.person else None, pinned=a.pinned)
            for a in sorted(sh.assignments, key=lambda a: a.seat)
        ],
    )


class AssignIn(BaseModel):
    email: str | None = Field(None, description="Кого назначить; null — освободить место")
    seat: int | None = Field(None, description="Номер места (с 0); null при назначении — добавить ещё одно место")
    force: bool = Field(False, description="Назначить, даже если человек отсутствует")


class ManualShiftIn(BaseModel):
    start_date: date = Field(description="Первый день дежурства")
    days: int = Field(1, ge=1, le=366, description="Сколько календарных дней")
    emails: list[str] = Field(description="Кто дежурит (один или несколько)")
    force: bool = Field(False, description="Назначить, даже если кто-то отсутствует")


class ShiftPeriodIn(BaseModel):
    start: datetime = Field(description="Новое начало (ISO 8601)")
    end: datetime = Field(description="Новый конец (ISO 8601)")
    force: bool = Field(False, description="Сохранить, даже если кто-то из дежурных отсутствует")


class UnpinIn(BaseModel):
    seat: int = 0


class SwapIn(BaseModel):
    shift_a: int
    seat_a: int = 0
    shift_b: int
    seat_b: int = 0
    force: bool = False


class ShiftLogOut(ORM):
    at: datetime
    seat: int
    old_person_id: int | None
    new_person_id: int | None
    reason: str
    actor: str


def shift_log_out(rows: list[ShiftLog]) -> list[ShiftLogOut]:
    return [ShiftLogOut.model_validate(r) for r in rows]


# ---------------------------------------------------------------- планирование


class ChangeOut(BaseModel):
    shift_id: int | None
    start: datetime
    end: datetime
    seat: int
    old: PersonOut | None
    new: PersonOut | None
    reason: str


class ConflictOut(BaseModel):
    shift_id: int | None
    start: datetime
    end: datetime
    seat: int
    person: PersonOut | None
    reason: str
    detail: str


class PlanReportOut(BaseModel):
    team: str
    team_name: str
    mode: str
    dry_run: bool
    changes: list[ChangeOut]
    conflicts: list[ConflictOut]
    deleted: int


def _p(p: Person | None) -> PersonOut | None:
    return PersonOut.model_validate(p) if p else None


def report_out(r: PlanReport) -> PlanReportOut:
    return PlanReportOut(
        team=r.team_slug,
        team_name=r.team_name,
        mode=r.mode,
        dry_run=r.dry_run,
        changes=[ChangeOut(shift_id=c.shift_id, start=c.start, end=c.end, seat=c.seat, old=_p(c.old), new=_p(c.new),
                           reason=c.reason) for c in r.changes],
        conflicts=[ConflictOut(shift_id=c.shift_id, start=c.start, end=c.end, seat=c.seat, person=_p(c.person),
                               reason=c.reason, detail=c.detail) for c in r.conflicts],
        deleted=r.deleted,
    )


# ---------------------------------------------------------------- отсутствия и праздники


class AbsenceIn(BaseModel):
    email: str
    start: str = Field(description="YYYY-MM-DD (с начала дня) или дата-время ISO 8601")
    end: str = Field(description="YYYY-MM-DD (включительно) или дата-время ISO 8601 (не включительно)")
    kind: Literal["vacation", "sick", "dayoff", "other"] = "vacation"
    note: str = ""


class AbsencePatch(BaseModel):
    start: str | None = None
    end: str | None = None
    kind: Literal["vacation", "sick", "dayoff", "other"] | None = None
    note: str | None = None


class AbsenceOut(BaseModel):
    id: int
    person: PersonOut
    start: datetime
    end: datetime
    all_day: bool = False  # целые дни по поясу сервиса
    start_day: date | None = None  # первый день (если all_day)
    end_day: date | None = None  # последний день включительно (если all_day)
    kind: str
    source: str
    note: str
    created_by: str


def absence_out(a: Absence) -> AbsenceOut:
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from .config import get_settings

    tz = ZoneInfo(get_settings().timezone)
    ls, le = a.start.astimezone(tz), a.end.astimezone(tz)
    all_day = ls.time() == le.time() == datetime.min.time()
    return AbsenceOut(id=a.id, person=PersonOut.model_validate(a.person), start=a.start, end=a.end,
                      all_day=all_day, start_day=ls.date() if all_day else None,
                      end_day=(le.date() - timedelta(days=1)) if all_day else None,
                      kind=a.kind, source=a.source, note=a.note, created_by=a.created_by)


class AbsenceResult(BaseModel):
    absence: AbsenceOut | None
    plans: list[PlanReportOut]


class HolidayOut(ORM):
    day: date
    name: str
    kind: str
    source: str


class HolidayIn(BaseModel):
    day: date
    name: str
    kind: Literal["holiday", "dayoff", "workday", "short"] = "holiday"


class EventIn(BaseModel):
    title: str
    start: str = Field(description="YYYY-MM-DD (с начала дня) или дата-время ISO 8601")
    end: str = Field(description="YYYY-MM-DD (включительно) или дата-время ISO 8601 (не включительно)")
    description: str = ""
    color: str = "#e5a50a"
    team: str | None = Field(None, description="slug команды; null — событие для всех")
    emails: list[str] = Field(default_factory=list, description="Участники события")


class EventPatch(BaseModel):
    title: str | None = None
    start: str | None = None
    end: str | None = None
    description: str | None = None
    color: str | None = None
    team: str | None = None
    emails: list[str] | None = None


class EventOut(BaseModel):
    id: int
    title: str
    description: str
    start: datetime
    end: datetime
    all_day: bool
    start_day: date | None
    end_day: date | None
    color: str
    team: str | None
    people: list[PersonOut] = []
    created_by: str


def _days(start: datetime, end: datetime):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from .config import get_settings

    tz = ZoneInfo(get_settings().timezone)
    ls, le = start.astimezone(tz), end.astimezone(tz)
    all_day = ls.time() == le.time() == datetime.min.time()
    return all_day, (ls.date() if all_day else None), ((le.date() - timedelta(days=1)) if all_day else None)


def event_out(e) -> EventOut:
    all_day, sd, ed = _days(e.start, e.end)
    return EventOut(id=e.id, title=e.title, description=e.description, start=e.start, end=e.end, all_day=all_day,
                    start_day=sd, end_day=ed, color=e.color, team=e.team.slug if e.team else None,
                    people=[PersonOut.model_validate(p) for p in e.people], created_by=e.created_by)


class CalendarOut(BaseModel):
    shifts: list[ShiftOut]
    absences: list[AbsenceOut]
    holidays: list[HolidayOut]
    events: list[EventOut] = []
    cancelled: list[ShiftOut] = []  # снятые вручную смены по графику (можно вернуть)


# ---------------------------------------------------------------- пользователи


class UserOut(ORM):
    id: int
    username: str
    display_name: str
    role: str
    active: bool
    auth_source: str


class UserIn(BaseModel):
    username: str
    password: str
    role: Literal["user", "admin"] = "user"
    display_name: str = ""


class UserPatch(BaseModel):
    role: Literal["user", "admin"] | None = None
    active: bool | None = None
    password: str | None = None
    display_name: str | None = None


class LoginIn(BaseModel):
    username: str
    password: str


class TokenIn(BaseModel):
    name: str = "token"
    days: int | None = Field(None, description="Срок жизни в днях; null — бессрочно")


class TokenOut(ORM):
    id: int
    name: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None


class TokenCreated(TokenOut):
    token: str = Field(description="Показывается один раз")


def token_created(t: ApiToken, raw: str) -> TokenCreated:
    return TokenCreated(id=t.id, name=t.name, created_at=t.created_at, expires_at=t.expires_at,
                        last_used_at=t.last_used_at, token=raw)


def user_out(u: User) -> UserOut:
    return UserOut.model_validate(u)


def holiday_out(h: Holiday) -> HolidayOut:
    return HolidayOut.model_validate(h)


__all__ = [n for n in dir() if not n.startswith("_")] + ["ChangeView", "ConflictView"]
