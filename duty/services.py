"""Сервисный слой: все операции над графиком. Используется REST API, MCP и фоновыми задачами.

Все изменения идут через `DutyService.write()`: глобальная блокировка (один экземпляр сервиса),
транзакция и отправка накопленных уведомлений после коммита.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import auth
from .config import Settings
from .db import session_scope
from .formatting import REASONS, fmt_dt, fmt_range, mention, mentions
from .integrations.absences import AbsenceSource
from .integrations.holidays import HolidayProvider
from .integrations.notifiers import Notifier
from .integrations.publishers import Attendee, CalendarPublisher, PublishShift, PublishTeam
from .models import (
    Absence,
    ApiToken,
    Holiday,
    Membership,
    Person,
    Shift,
    ShiftAssignment,
    ShiftLog,
    Team,
    User,
    utcnow,
)
from .planning.planner import (
    PLANNERS,
    ExistingSeat,
    ExistingShift,
    Interval,
    MemberInfo,
    PlanInput,
    PlanningNotImplemented,
    PlanResult,
    get_planner,
)
from .planning.slots import RotationRule, generate_slots

log = logging.getLogger(__name__)

HISTORY_DAYS = 180
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
ABSENCE_KINDS = ("vacation", "sick", "dayoff", "other")
HOLIDAY_KINDS = ("holiday", "dayoff", "workday", "short")


# ---------------------------------------------------------------- ошибки


class ServiceError(Exception):
    status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(ServiceError):
    status = 404


class Forbidden(ServiceError):
    status = 403


class Invalid(ServiceError):
    status = 422


class ConflictError(ServiceError):
    status = 409


# ---------------------------------------------------------------- DTO результатов


@dataclass
class ChangeView:
    shift_id: int | None
    start: datetime
    end: datetime
    seat: int
    old: Person | None
    new: Person | None
    reason: str


@dataclass
class ConflictView:
    shift_id: int | None
    start: datetime
    end: datetime
    seat: int
    person: Person | None
    reason: str
    detail: str = ""


@dataclass
class PlanReport:
    team_slug: str
    team_name: str
    mode: str
    dry_run: bool
    changes: list[ChangeView] = field(default_factory=list)
    conflicts: list[ConflictView] = field(default_factory=list)
    deleted: int = 0


@dataclass
class Op:
    session: Session
    actor: User | None  # None = система (фоновые задачи)
    outbox: list[tuple[str | None, str, str | None]] = field(default_factory=list)

    @property
    def actor_name(self) -> str:
        return self.actor.username if self.actor else "system"


def norm_email(email: str) -> str:
    return email.strip().lower()


def rule_for(team: Team) -> RotationRule:
    return RotationRule(
        timezone=team.timezone,
        shift_length_days=team.shift_length_days,
        handoff_time=team.handoff_time,
        anchor_date=team.anchor_date,
        weekdays=team.weekdays,
        skip_holidays=team.skip_holidays,
    )


class DutyService:
    def __init__(
        self,
        settings: Settings,
        notifier: Notifier,
        publisher: CalendarPublisher,
        holiday_provider: HolidayProvider,
        absence_sources: list[AbsenceSource] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.notifier = notifier
        self.publisher = publisher
        self.holiday_provider = holiday_provider
        self.absence_sources = absence_sources or []
        self.clock = clock or utcnow
        self.lock = threading.RLock()
        self._notified_conflicts: set[tuple] = set()

    def now(self) -> datetime:
        return self.clock()

    # ------------------------------------------------------------ транзакции

    @contextmanager
    def write(self, actor: User | None = None) -> Iterator[Op]:
        with self.lock:
            with session_scope() as s:
                op = Op(s, actor)
                yield op
            for webhook, text, channel in op.outbox:
                self.notifier.send(webhook, text, channel)

    @contextmanager
    def read(self) -> Iterator[Session]:
        with session_scope() as s:
            yield s

    @staticmethod
    def _require_admin(op: Op) -> None:
        if op.actor is not None and op.actor.role != "admin":
            raise Forbidden("нужны права администратора")

    # ------------------------------------------------------------ поиск

    @staticmethod
    def get_team(s: Session, slug: str) -> Team:
        team = s.scalar(select(Team).where(Team.slug == slug))
        if team is None:
            raise NotFound(f"команда {slug!r} не найдена")
        return team

    @staticmethod
    def get_person(s: Session, email: str) -> Person:
        person = s.scalar(select(Person).where(Person.email == norm_email(email)))
        if person is None:
            raise NotFound(f"человек {email!r} не найден")
        return person

    @staticmethod
    def get_shift(s: Session, shift_id: int) -> Shift:
        shift = s.get(Shift, shift_id)
        if shift is None or shift.deleted:
            raise NotFound(f"смена {shift_id} не найдена")
        return shift

    @staticmethod
    def _membership(team: Team, person_id: int) -> Membership | None:
        return next((m for m in team.memberships if m.person_id == person_id), None)

    # ------------------------------------------------------------ команды

    TEAM_FIELDS = (
        "name",
        "description",
        "timezone",
        "shift_length_days",
        "people_per_shift",
        "handoff_time",
        "anchor_date",
        "weekdays",
        "skip_holidays",
        "horizon_days",
        "freeze_days",
        "absence_policy",
        "notify_days",
        "mattermost_webhook",
        "mattermost_channel",
        "external_calendar",
    )

    def list_teams(self, s: Session) -> list[Team]:
        return list(s.scalars(select(Team).order_by(Team.name)))

    def _validate_team(self, team: Team) -> None:
        try:
            rule_for(team)
        except Exception as exc:  # noqa: BLE001
            raise Invalid(f"правила ротации: {exc}") from exc
        if not 1 <= team.people_per_shift <= 20:
            raise Invalid("people_per_shift: от 1 до 20")
        if not 1 <= team.horizon_days <= 366:
            raise Invalid("horizon_days: от 1 до 366")
        if team.freeze_days < 0 or team.notify_days < 0:
            raise Invalid("freeze_days и notify_days не могут быть отрицательными")
        if team.absence_policy not in PLANNERS:
            raise Invalid(f"absence_policy: одно из {', '.join(PLANNERS)}")

    def create_team(self, op: Op, slug: str, data: dict) -> Team:
        self._require_admin(op)
        s = op.session
        if not SLUG_RE.match(slug or ""):
            raise Invalid("slug: латиница в нижнем регистре, цифры и дефис, 2–63 символа")
        if s.scalar(select(Team.id).where(Team.slug == slug)) is not None:
            raise ConflictError(f"команда {slug!r} уже существует")
        values = {k: v for k, v in data.items() if k in self.TEAM_FIELDS and v is not None}
        values.setdefault("name", slug)
        values.setdefault("timezone", self.settings.timezone)
        values.setdefault("anchor_date", self.now().astimezone(ZoneInfo(values["timezone"])).date())
        team = Team(slug=slug, **values)
        for k in self.TEAM_FIELDS:  # заполнить дефолты колонок до валидации
            if getattr(team, k) is None:
                default = Team.__table__.c[k].default
                if default is not None:
                    setattr(team, k, default.arg)
        self._validate_team(team)
        s.add(team)
        s.flush()
        self._replan(op, team, "fix")
        return team

    def update_team(self, op: Op, slug: str, data: dict) -> Team:
        self._require_admin(op)
        team = self.get_team(op.session, slug)
        for k, v in data.items():
            if k in self.TEAM_FIELDS:
                setattr(team, k, v)
        self._validate_team(team)
        op.session.flush()
        self._replan(op, team, "fix")
        return team

    def delete_team(self, op: Op, slug: str) -> None:
        self._require_admin(op)
        s = op.session
        team = self.get_team(s, slug)
        for sh in s.scalars(select(Shift).where(Shift.team_id == team.id, Shift.external_id.is_not(None))):
            try:
                self.publisher.unpublish(self._publish_team(team), sh.external_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("unpublish on team delete failed: %s", exc)
        s.delete(team)

    # ------------------------------------------------------------ люди и состав

    def list_people(self, s: Session) -> list[Person]:
        return list(s.scalars(select(Person).order_by(Person.name)))

    def upsert_person(
        self, op: Op, email: str, name: str | None = None, mattermost_username: str | None = None
    ) -> Person:
        self._require_admin(op)
        s = op.session
        email = norm_email(email)
        if "@" not in email:
            raise Invalid("некорректный email")
        person = s.scalar(select(Person).where(Person.email == email))
        if person is None:
            if not name:
                raise Invalid(f"{email}: нового человека нужно создать с именем (name)")
            person = Person(email=email, name=name, mattermost_username=mattermost_username or None)
            s.add(person)
        else:
            if name:
                person.name = name
            if mattermost_username is not None:
                person.mattermost_username = mattermost_username or None
        s.flush()
        return person

    def add_member(
        self,
        op: Op,
        slug: str,
        email: str,
        name: str | None = None,
        mattermost_username: str | None = None,
        position: int | None = None,
        active: bool = True,
    ) -> Membership:
        self._require_admin(op)
        team = self.get_team(op.session, slug)
        person = self.upsert_person(op, email, name, mattermost_username)
        if self._membership(team, person.id) is not None:
            raise ConflictError(f"{person.email} уже в команде {slug}")
        if position is None:
            position = max((m.position for m in team.memberships), default=-1) + 1
        m = Membership(team=team, person=person, position=position, active=active)
        op.session.add(m)
        op.session.flush()
        # Новый человек должен войти в очередь сразу: пересчитываем будущее (кроме закреплённых и начавшихся).
        self._replan(op, team, "rebalance" if active else "fix", freeze_days=0)
        return m

    def update_member(self, op: Op, slug: str, email: str, active: bool | None = None, position: int | None = None):
        self._require_admin(op)
        team = self.get_team(op.session, slug)
        person = self.get_person(op.session, email)
        m = self._membership(team, person.id)
        if m is None:
            raise NotFound(f"{email} не состоит в команде {slug}")
        activated = active is True and not m.active
        if active is not None:
            m.active = active
        if position is not None:
            m.position = position
        op.session.flush()
        if activated:
            self._replan(op, team, "rebalance", freeze_days=0)
        else:
            self._replan(op, team, "fix")
        return m

    def remove_member(self, op: Op, slug: str, email: str) -> None:
        self._require_admin(op)
        team = self.get_team(op.session, slug)
        person = self.get_person(op.session, email)
        m = self._membership(team, person.id)
        if m is None:
            raise NotFound(f"{email} не состоит в команде {slug}")
        team.memberships.remove(m)
        op.session.flush()
        self._replan(op, team, "fix")

    def reorder_members(self, op: Op, slug: str, emails: list[str]) -> list[Membership]:
        self._require_admin(op)
        team = self.get_team(op.session, slug)
        by_email = {m.person.email: m for m in team.memberships}
        wanted = [norm_email(e) for e in emails]
        if sorted(wanted) != sorted(by_email):
            raise Invalid("нужно перечислить ровно всех участников команды")
        for i, e in enumerate(wanted):
            by_email[e].position = i
        op.session.flush()
        return sorted(team.memberships, key=lambda m: m.position)

    # ------------------------------------------------------------ пользователи и токены

    def list_users(self, op: Op) -> list[User]:
        self._require_admin(op)
        return list(op.session.scalars(select(User).order_by(User.username)))

    def create_user(self, op: Op, username: str, password: str, role: str = "user", display_name: str = "") -> User:
        self._require_admin(op)
        s = op.session
        if role not in auth.ROLES:
            raise Invalid(f"role: одно из {', '.join(auth.ROLES)}")
        if len(password) < 8:
            raise Invalid("пароль: минимум 8 символов")
        if s.scalar(select(User.id).where(User.username == username)) is not None:
            raise ConflictError(f"пользователь {username!r} уже существует")
        user = User(username=username, display_name=display_name or username, role=role,
                    password_hash=auth.hash_password(password))
        s.add(user)
        s.flush()
        return user

    def update_user(
        self,
        op: Op,
        username: str,
        role: str | None = None,
        active: bool | None = None,
        password: str | None = None,
        display_name: str | None = None,
    ) -> User:
        s = op.session
        user = s.scalar(select(User).where(User.username == username))
        if user is None:
            raise NotFound(f"пользователь {username!r} не найден")
        is_self = op.actor is not None and op.actor.id == user.id
        if not is_self or role is not None or active is not None:
            self._require_admin(op)
        if role is not None:
            if role not in auth.ROLES:
                raise Invalid(f"role: одно из {', '.join(auth.ROLES)}")
            user.role = role
        if active is not None:
            user.active = active
        if password is not None:
            if len(password) < 8:
                raise Invalid("пароль: минимум 8 символов")
            user.password_hash = auth.hash_password(password)
        if display_name is not None:
            user.display_name = display_name
        return user

    def login(self, op: Op, username: str, password: str) -> tuple[User, str]:
        user = auth.authenticate(op.session, username, password)
        if user is None:
            raise Forbidden("неверный логин или пароль")
        _, raw = auth.issue_token(op.session, user, "session", "web",
                                  ttl=timedelta(hours=self.settings.session_ttl_hours))
        return user, raw

    def logout(self, op: Op, raw: str) -> None:
        tok = op.session.scalar(select(ApiToken).where(ApiToken.token_hash == auth.hash_token(raw)))
        if tok is not None:
            op.session.delete(tok)

    def create_token(self, op: Op, name: str, days: int | None = None) -> tuple[ApiToken, str]:
        if op.actor is None:
            raise Forbidden("нужен пользователь")
        ttl = timedelta(days=days) if days else None
        return auth.issue_token(op.session, op.actor, "personal", name or "token", ttl)

    def list_tokens(self, op: Op) -> list[ApiToken]:
        if op.actor is None:
            return []
        return list(op.session.scalars(
            select(ApiToken).where(ApiToken.user_id == op.actor.id, ApiToken.kind == "personal")
        ))

    def revoke_token(self, op: Op, token_id: int) -> None:
        tok = op.session.get(ApiToken, token_id)
        if tok is None:
            raise NotFound("токен не найден")
        if op.actor is not None and tok.user_id != op.actor.id:
            self._require_admin(op)
        op.session.delete(tok)

    # ------------------------------------------------------------ смены: чтение

    def list_shifts(
        self,
        s: Session,
        start: datetime,
        end: datetime,
        team_slug: str | None = None,
        person_email: str | None = None,
    ) -> list[Shift]:
        q = (
            select(Shift)
            .options(selectinload(Shift.assignments).selectinload(ShiftAssignment.person), selectinload(Shift.team))
            .where(Shift.deleted.is_(False), Shift.start < end, Shift.end > start)
            .order_by(Shift.start)
        )
        if team_slug:
            q = q.where(Shift.team_id == self.get_team(s, team_slug).id)
        shifts = list(s.scalars(q))
        if person_email:
            pid = self.get_person(s, person_email).id
            shifts = [sh for sh in shifts if any(a.person_id == pid for a in sh.assignments)]
        return shifts

    def oncall(self, s: Session, slug: str, at: datetime | None = None) -> list[Shift]:
        """Смены команды, идущие в момент `at` (обычно одна)."""
        at = at or self.now()
        return self.list_shifts(s, at, at + timedelta(microseconds=1), team_slug=slug)

    def shift_log(self, s: Session, shift_id: int) -> list[ShiftLog]:
        return list(s.scalars(select(ShiftLog).where(ShiftLog.shift_id == shift_id).order_by(ShiftLog.at)))

    # ------------------------------------------------------------ смены: ручные правки

    def _check_editable(self, shift: Shift) -> None:
        if shift.end <= self.now():
            raise Invalid("смена уже закончилась, её нельзя менять")

    def _absences_of(self, s: Session, person_id: int, start: datetime, end: datetime) -> list[Absence]:
        return list(s.scalars(select(Absence).where(
            Absence.person_id == person_id, Absence.start < end, Absence.end > start
        )))

    def _log(self, op: Op, shift: Shift, seat: int, old: int | None, new: int | None, reason: str) -> None:
        op.session.add(ShiftLog(shift_id=shift.id, seat=seat, old_person_id=old, new_person_id=new,
                                reason=reason, actor=op.actor_name))

    def assign(
        self, op: Op, shift_id: int, email: str | None, seat: int | None = None, force: bool = False
    ) -> Shift:
        """Назначить человека на место в смене (место закрепляется) или освободить место (email=None).

        seat=None при назначении — добавить ещё одно место сверх people_per_shift."""
        s = op.session
        shift = self.get_shift(s, shift_id)
        self._check_editable(shift)
        team = shift.team
        seats = {a.seat: a for a in shift.assignments}

        if email is None:
            if seat is None or seat not in seats:
                raise Invalid("укажите занятое место (seat), которое нужно освободить")
            a = seats[seat]
            old = a.person_id
            if seat >= team.people_per_shift:
                shift.assignments.remove(a)
            else:
                a.person_id, a.pinned = None, False
            self._log(op, shift, seat, old, None, "manual")
            shift.publish_dirty = True
            s.flush()
            self._replan(op, team, "fix", notify=False)
            self._notify_manual(op, team, shift, f"освободил место ({mention(s.get(Person, old) if old else None)})")
            return shift

        person = self.get_person(s, email)
        if self._membership(team, person.id) is None:
            raise Invalid(f"{person.email} не состоит в команде {team.slug}")
        if any(a.person_id == person.id and a.seat != seat for a in shift.assignments):
            raise ConflictError(f"{person.name} уже дежурит в этой смене")
        if not force:
            absent = self._absences_of(s, person.id, shift.start, shift.end)
            if absent:
                raise ConflictError(
                    f"{person.name} отсутствует в это время ({fmt_range(absent[0].start, absent[0].end, team.timezone)}). "
                    "Чтобы назначить всё равно, повторите с force=true"
                )
        if seat is None:
            seat = max(seats, default=-1) + 1
        a = seats.get(seat)
        if a is None:
            a = ShiftAssignment(seat=seat)
            shift.assignments.append(a)
        old = a.person_id
        a.person_id, a.pinned = person.id, True
        self._log(op, shift, seat, old, person.id, "manual")
        shift.publish_dirty = True
        s.flush()
        old_p = s.get(Person, old) if old else None
        what = f"назначил {mention(person)}" + (f" вместо {mention(old_p)}" if old_p and old_p.id != person.id else "")
        self._notify_manual(op, team, shift, what)
        return shift

    def unpin(self, op: Op, shift_id: int, seat: int) -> Shift:
        s = op.session
        shift = self.get_shift(s, shift_id)
        self._check_editable(shift)
        a = next((a for a in shift.assignments if a.seat == seat), None)
        if a is None:
            raise NotFound(f"в смене нет места {seat}")
        a.pinned = False
        s.flush()
        self._replan(op, shift.team, "fix")
        return shift

    def swap(self, op: Op, a_id: int, a_seat: int, b_id: int, b_seat: int, force: bool = False) -> tuple[Shift, Shift]:
        s = op.session
        sa, sb = self.get_shift(s, a_id), self.get_shift(s, b_id)
        if sa.id == sb.id:
            raise Invalid("для обмена нужны две разные смены")
        for sh in (sa, sb):
            self._check_editable(sh)
        aa = next((x for x in sa.assignments if x.seat == a_seat), None)
        ab = next((x for x in sb.assignments if x.seat == b_seat), None)
        if aa is None or ab is None or aa.person_id is None or ab.person_id is None:
            raise Invalid("оба места должны быть заняты")
        pa, pb = aa.person_id, ab.person_id
        if any(x.person_id == pa for x in sb.assignments if x is not ab):
            raise ConflictError("первый дежурный уже есть во второй смене")
        if any(x.person_id == pb for x in sa.assignments if x is not aa):
            raise ConflictError("второй дежурный уже есть в первой смене")
        if not force:
            for pid, sh in ((pa, sb), (pb, sa)):
                if self._absences_of(s, pid, sh.start, sh.end):
                    name = s.get(Person, pid).name
                    raise ConflictError(f"{name} отсутствует во время смены {sh.id}. Повторите с force=true")
        aa.person_id, ab.person_id = pb, pa
        aa.pinned = ab.pinned = True
        self._log(op, sa, a_seat, pa, pb, "swap")
        self._log(op, sb, b_seat, pb, pa, "swap")
        sa.publish_dirty = sb.publish_dirty = True
        s.flush()
        p_a, p_b = s.get(Person, pa), s.get(Person, pb)
        team = sa.team
        text = (
            f"🔁 **{team.name}**: {op.actor_name} поменял смены {mention(p_a)} и {mention(p_b)}\n"
            f"• {fmt_range(sa.start, sa.end, team.timezone)}: теперь {mention(p_b)}\n"
            f"• {fmt_range(sb.start, sb.end, sb.team.timezone)}: теперь {mention(p_a)}"
        )
        op.outbox.append((team.mattermost_webhook, text, team.mattermost_channel))
        return sa, sb

    def _notify_manual(self, op: Op, team: Team, shift: Shift, what: str) -> None:
        if shift.start > self.now() + timedelta(days=team.notify_days):
            return
        text = f"✋ **{team.name}**: {op.actor_name} {what} на смену {fmt_range(shift.start, shift.end, team.timezone)}"
        op.outbox.append((team.mattermost_webhook, text, team.mattermost_channel))

    # ------------------------------------------------------------ планирование

    def plan_team(
        self, op: Op, slug: str, mode: str = "fix", dry_run: bool = False, freeze_days: int | None = None
    ) -> PlanReport:
        """freeze_days — для rebalance: не трогать смены, начинающиеся в ближайшие N дней
        (по умолчанию значение из настроек команды)."""
        if mode not in ("fix", "rebalance"):
            raise Invalid("mode: fix | rebalance")
        if freeze_days is not None and freeze_days < 0:
            raise Invalid("freeze_days не может быть отрицательным")
        team = self.get_team(op.session, slug)
        if dry_run:
            result, by_id = self._compute(op.session, team, mode, freeze_days=freeze_days)
            return self._report(op.session, team, result, mode, True, {})
        return self._replan(op, team, mode, freeze_days=freeze_days)

    def plan_all(self, op: Op) -> list[PlanReport]:
        reports = []
        for team in self.list_teams(op.session):
            try:
                reports.append(self._replan(op, team, "fix"))
            except ServiceError as exc:
                log.warning("planning %s failed: %s", team.slug, exc.message)
        return reports

    def day_kinds(self, s: Session, start: date, end: date) -> dict[date, str]:
        rows = s.execute(select(Holiday.day, Holiday.kind).where(
            Holiday.country == self.settings.holiday_country, Holiday.day >= start, Holiday.day <= end
        ))
        return {d: k for d, k in rows}

    def _compute(
        self,
        s: Session,
        team: Team,
        mode: str,
        extra_absences: dict[int, list[Interval]] | None = None,
        skip_absence_ids: set[int] | None = None,
        freeze_days: int | None = None,
    ) -> tuple[PlanResult, dict[int, Shift]]:
        now = self.now()
        window_end = now + timedelta(days=team.horizon_days)
        # производственный календарь нужен от даты отсчёта: по нему нумеруются дежурные дни
        kinds = self.day_kinds(s, min(team.anchor_date, now.date()) - timedelta(days=1), window_end.date())
        slots = generate_slots(rule_for(team), now, window_end, kinds.get)

        shifts = list(s.scalars(
            select(Shift).options(selectinload(Shift.assignments))
            .where(Shift.team_id == team.id, Shift.deleted.is_(False), Shift.end > now, Shift.start < window_end)
        ))
        existing = [
            ExistingShift(sh.id, sh.start, sh.end, tuple(ExistingSeat(a.seat, a.person_id, a.pinned) for a in sh.assignments))
            for sh in shifts
        ]
        members = [MemberInfo(m.person_id, m.position, m.active) for m in team.memberships]
        pids = [m.person_id for m in members]

        absences: dict[int, list[Interval]] = {}
        if pids:
            q = select(Absence).where(Absence.person_id.in_(pids), Absence.end > now - timedelta(days=1),
                                      Absence.start < window_end)
            for a in s.scalars(q):
                if skip_absence_ids and a.id in skip_absence_ids:
                    continue
                absences.setdefault(a.person_id, []).append(Interval(a.start, a.end))
        for pid, items in (extra_absences or {}).items():
            absences.setdefault(pid, []).extend(items)

        last: dict[int, datetime] = {}
        count: dict[int, int] = {}
        hist = s.execute(
            select(ShiftAssignment.person_id, Shift.start).join(Shift)
            .where(Shift.team_id == team.id, Shift.deleted.is_(False), Shift.end <= now,
                   Shift.start >= now - timedelta(days=HISTORY_DAYS), ShiftAssignment.person_id.is_not(None))
        )
        for pid, start in hist:
            last[pid] = max(last.get(pid, start), start)
            count[pid] = count.get(pid, 0) + 1

        data = PlanInput(
            now=now,
            slots=slots,
            existing=existing,
            members=members,
            seats=team.people_per_shift,
            absences=absences,
            last_duty=last,
            duty_count=count,
            mode=mode,  # type: ignore[arg-type]
            freeze_until=now + timedelta(days=team.freeze_days if freeze_days is None else freeze_days),
        )
        try:
            result = get_planner(team.absence_policy).plan(data)
        except PlanningNotImplemented as exc:
            raise Invalid(str(exc)) from exc
        return result, {sh.id: sh for sh in shifts}

    def _apply(self, op: Op, team: Team, result: PlanResult, by_id: dict[int, Shift]) -> dict[datetime, Shift]:
        s = op.session
        reasons = {(c.start, c.seat): c.reason for c in result.changes}
        by_start: dict[datetime, Shift] = {}

        for sid in result.deletions:
            sh = by_id[sid]
            for a in sh.assignments:
                if a.person_id is not None:
                    self._log(op, sh, a.seat, a.person_id, None, "removed")
            if sh.external_id:
                sh.deleted = True
                sh.publish_dirty = True
            else:
                s.delete(sh)
        s.flush()

        for ps in result.shifts:
            created = False
            if ps.existing_id is not None:
                sh = by_id[ps.existing_id]
            else:
                sh = s.scalar(select(Shift).where(Shift.team_id == team.id, Shift.start == ps.start))
                if sh is None:
                    sh = Shift(team_id=team.id, start=ps.start, end=ps.end)
                    s.add(sh)
                else:  # ранее удалённая смена на том же месте сетки
                    sh.deleted = False
                    sh.end = ps.end
                created = True
                s.flush()
            by_start[ps.start] = sh

            current = {a.seat: a for a in sh.assignments}
            desired = {p.seat: p for p in ps.seats}
            changed = created
            if sh.end != ps.end:
                sh.end = ps.end
                changed = True
            for seat, p in desired.items():
                a = current.get(seat)
                if a is None:
                    a = ShiftAssignment(seat=seat, person_id=p.person_id, pinned=p.pinned)
                    sh.assignments.append(a)
                    changed = True
                    if p.person_id is not None:
                        self._log(op, sh, seat, None, p.person_id, reasons.get((ps.start, seat), "new"))
                elif a.person_id != p.person_id or a.pinned != p.pinned:
                    if a.person_id != p.person_id:
                        self._log(op, sh, seat, a.person_id, p.person_id, reasons.get((ps.start, seat), "rebalance"))
                        changed = True
                    a.person_id, a.pinned = p.person_id, p.pinned
            for seat, a in current.items():
                if seat not in desired:
                    if a.person_id is not None:
                        self._log(op, sh, seat, a.person_id, None, "removed")
                    sh.assignments.remove(a)
                    changed = True
            if changed:
                sh.publish_dirty = True
        s.flush()
        return by_start

    def _replan(
        self, op: Op, team: Team, mode: str = "fix", notify: bool = True, freeze_days: int | None = None
    ) -> PlanReport:
        result, by_id = self._compute(op.session, team, mode, freeze_days=freeze_days)
        by_start = self._apply(op, team, result, by_id)
        report = self._report(op.session, team, result, mode, False, by_start)
        if notify:
            self._notify_plan(op, team, report)
        return report

    def _report(
        self, s: Session, team: Team, result: PlanResult, mode: str, dry_run: bool, by_start: dict[datetime, Shift]
    ) -> PlanReport:
        def person(pid):
            return s.get(Person, pid) if pid is not None else None

        def sid(existing_id, start):
            if existing_id is not None:
                return existing_id
            sh = by_start.get(start)
            return sh.id if sh else None

        return PlanReport(
            team_slug=team.slug,
            team_name=team.name,
            mode=mode,
            dry_run=dry_run,
            changes=[
                ChangeView(sid(c.existing_id, c.start), c.start, c.end, c.seat, person(c.old_person_id),
                           person(c.new_person_id), c.reason)
                for c in result.changes
            ],
            conflicts=[
                ConflictView(sid(c.existing_id, c.start), c.start, c.end, c.seat, person(c.person_id), c.reason, c.detail)
                for c in result.conflicts
            ],
            deleted=len(result.deletions),
        )

    def _notify_plan(self, op: Op, team: Team, report: PlanReport) -> None:
        horizon = self.now() + timedelta(days=team.notify_days)
        lines = []
        for c in report.changes:
            if c.reason == "new" or c.start >= horizon:
                continue
            lines.append(f"• {fmt_range(c.start, c.end, team.timezone)}: {mention(c.old)} → {mention(c.new)} "
                         f"({REASONS.get(c.reason, c.reason)})")
        has_people = any(m.active for m in team.memberships)
        for c in report.conflicts:
            if c.start >= horizon or not has_people:
                continue
            key = (team.id, c.start, c.seat, c.reason, c.person.id if c.person else None)
            if key in self._notified_conflicts:
                continue
            self._notified_conflicts.add(key)
            if c.reason == "unassigned":
                lines.append(f"⚠️ {fmt_range(c.start, c.end, team.timezone)}: некому дежурить (место {c.seat + 1})")
            else:
                lines.append(f"⚠️ {fmt_range(c.start, c.end, team.timezone)}: {mention(c.person)} — "
                             f"{REASONS.get(c.reason, c.reason)}, смена закреплена или уже идёт, нужна ручная замена")
        if lines:
            text = f"📅 **{team.name}**: изменения в графике дежурств\n" + "\n".join(lines)
            op.outbox.append((team.mattermost_webhook, text, team.mattermost_channel))

    # ------------------------------------------------------------ отсутствия

    def parse_moment(self, value: str | datetime | date, end: bool = False) -> datetime:
        """Дата 'YYYY-MM-DD' → полночь по поясу сервиса (для end — полночь следующего дня, т.е. включительно).
        Дата-время без пояса → пояс сервиса."""
        tz = ZoneInfo(self.settings.timezone)
        if isinstance(value, str):
            v = value.strip()
            try:
                value = date.fromisoformat(v) if len(v) == 10 else datetime.fromisoformat(v)
            except ValueError as exc:
                raise Invalid(f"не удалось разобрать дату {value!r}") from exc
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=tz)
        day = value + timedelta(days=1) if end else value
        return datetime.combine(day, time(0), tz)

    def list_absences(
        self,
        s: Session,
        start: datetime | None = None,
        end: datetime | None = None,
        email: str | None = None,
        team_slug: str | None = None,
    ) -> list[Absence]:
        q = select(Absence).options(selectinload(Absence.person)).order_by(Absence.start)
        if start:
            q = q.where(Absence.end > start)
        if end:
            q = q.where(Absence.start < end)
        if email:
            q = q.where(Absence.person_id == self.get_person(s, email).id)
        if team_slug:
            team = self.get_team(s, team_slug)
            q = q.where(Absence.person_id.in_([m.person_id for m in team.memberships]))
        return list(s.scalars(q))

    def _teams_of(self, s: Session, person_id: int) -> list[Team]:
        return list(s.scalars(select(Team).join(Membership).where(Membership.person_id == person_id)))

    def add_absence(
        self,
        op: Op,
        email: str,
        start: str | datetime | date,
        end: str | datetime | date,
        kind: str = "vacation",
        note: str = "",
        dry_run: bool = False,
    ) -> tuple[Absence | None, list[PlanReport]]:
        s = op.session
        person = self.get_person(s, email)
        st, en = self.parse_moment(start), self.parse_moment(end, end=True)
        if en <= st:
            raise Invalid("конец отсутствия должен быть позже начала")
        if kind not in ABSENCE_KINDS:
            raise Invalid(f"kind: одно из {', '.join(ABSENCE_KINDS)}")
        teams = self._teams_of(s, person.id)
        if dry_run:
            extra = {person.id: [Interval(st, en)]}
            reports = []
            for t in teams:
                result, _ = self._compute(s, t, "fix", extra_absences=extra)
                reports.append(self._report(s, t, result, "fix", True, {}))
            return None, reports
        absence = Absence(person_id=person.id, start=st, end=en, kind=kind, note=note, source="manual",
                          created_by=op.actor_name)
        s.add(absence)
        s.flush()
        return absence, [self._replan(op, t, "fix") for t in teams]

    def delete_absence(self, op: Op, absence_id: int, dry_run: bool = False) -> list[PlanReport]:
        s = op.session
        absence = s.get(Absence, absence_id)
        if absence is None:
            raise NotFound(f"отсутствие {absence_id} не найдено")
        teams = self._teams_of(s, absence.person_id)
        if dry_run:
            reports = []
            for t in teams:
                result, _ = self._compute(s, t, "fix", skip_absence_ids={absence_id})
                reports.append(self._report(s, t, result, "fix", True, {}))
            return reports
        s.delete(absence)
        s.flush()
        return [self._replan(op, t, "fix") for t in teams]

    def sync_external_absences(self, op: Op) -> int:
        """Забрать отсутствия из внешних источников и заменить ими прежние записи этих источников."""
        if not self.absence_sources:
            return 0
        s = op.session
        people = {p.email: p for p in s.scalars(select(Person).join(Membership).distinct())}
        if not people:
            return 0
        now = self.now()
        horizon = max((t.horizon_days for t in self.list_teams(s)), default=56)
        w_start, w_end = now - timedelta(days=1), now + timedelta(days=horizon)
        total = 0
        for src in self.absence_sources:
            try:
                items = src.fetch(list(people), w_start, w_end)
            except Exception as exc:  # noqa: BLE001
                log.warning("absence source %s failed: %s", src.name, exc)
                continue
            for old in s.scalars(select(Absence).where(Absence.source == src.name, Absence.end > w_start,
                                                         Absence.start < w_end)):
                s.delete(old)
            for it in items:
                p = people.get(norm_email(it.email))
                if p is None or it.end <= it.start:
                    continue
                s.add(Absence(person_id=p.id, start=it.start, end=it.end, kind=it.kind, note=it.note,
                              source=src.name, created_by=src.name))
                total += 1
        s.flush()
        return total

    # ------------------------------------------------------------ праздники

    def list_holidays(self, s: Session, start: date, end: date) -> list[Holiday]:
        return list(s.scalars(select(Holiday).where(
            Holiday.country == self.settings.holiday_country, Holiday.day >= start, Holiday.day <= end
        ).order_by(Holiday.day)))

    def holidays_loaded(self, s: Session, year: int) -> bool:
        return s.scalar(select(Holiday.id).where(
            Holiday.country == self.settings.holiday_country, Holiday.source != "manual",
            Holiday.day >= date(year, 1, 1), Holiday.day <= date(year, 12, 31),
        ).limit(1)) is not None

    def sync_holidays(self, op: Op, years: list[int]) -> int:
        s = op.session
        country = self.settings.holiday_country
        total = 0
        for year in years:
            entries = self.holiday_provider.fetch(year, country)
            y0, y1 = date(year, 1, 1), date(year, 12, 31)
            rows = list(s.scalars(select(Holiday).where(Holiday.country == country, Holiday.day >= y0, Holiday.day <= y1)))
            manual = {h.day for h in rows if h.source == "manual"}
            for h in rows:
                if h.source != "manual":
                    s.delete(h)
            s.flush()
            for e in entries:
                if e.day in manual:
                    continue
                s.add(Holiday(country=country, day=e.day, name=e.name, kind=e.kind, source=self.holiday_provider.name))
                total += 1
        s.flush()
        return total

    def set_holiday(self, op: Op, day: date, name: str, kind: str = "holiday") -> Holiday:
        self._require_admin(op)
        if kind not in HOLIDAY_KINDS:
            raise Invalid(f"kind: одно из {', '.join(HOLIDAY_KINDS)}")
        s = op.session
        h = s.scalar(select(Holiday).where(Holiday.country == self.settings.holiday_country, Holiday.day == day))
        if h is None:
            h = Holiday(country=self.settings.holiday_country, day=day)
            s.add(h)
        h.name, h.kind, h.source = name, kind, "manual"
        s.flush()
        self._replan_holiday_teams(op)
        return h

    def delete_holiday(self, op: Op, day: date) -> None:
        self._require_admin(op)
        s = op.session
        h = s.scalar(select(Holiday).where(Holiday.country == self.settings.holiday_country, Holiday.day == day))
        if h is None:
            raise NotFound("такого дня нет в календаре")
        s.delete(h)
        s.flush()
        self._replan_holiday_teams(op)

    def _replan_holiday_teams(self, op: Op) -> None:
        for team in self.list_teams(op.session):
            if team.skip_holidays:
                self._replan(op, team, "fix")

    # ------------------------------------------------------------ публикация и объявления

    def _publish_team(self, team: Team) -> PublishTeam:
        return PublishTeam(team.slug, team.name, team.external_calendar, team.timezone)

    def publish_pending(self) -> int:
        """Отдать изменённые смены во внешний календарь. Возвращает число обработанных смен."""
        done = 0
        with self.write() as op:
            s = op.session
            now = self.now()
            dirty = list(s.scalars(
                select(Shift).options(selectinload(Shift.assignments).selectinload(ShiftAssignment.person),
                                      selectinload(Shift.team))
                .where(Shift.publish_dirty.is_(True))
            ))
            for sh in dirty:
                team = sh.team
                pteam = self._publish_team(team)
                try:
                    if sh.deleted:
                        if sh.external_id:
                            self.publisher.unpublish(pteam, sh.external_id)
                        s.delete(sh)
                    elif sh.end <= now - timedelta(days=1):
                        sh.publish_dirty = False  # прошлое не публикуем
                    else:
                        attendees = tuple(Attendee(a.person.email, a.person.name) for a in sh.assignments if a.person)
                        url = f"{self.settings.public_url.rstrip('/')}/#shift-{sh.id}"
                        sh.external_id = self.publisher.publish(
                            pteam, PublishShift(sh.id, sh.start, sh.end, attendees, team.people_per_shift,
                                                sh.external_id, url)
                        )
                        sh.publish_dirty = False
                    done += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("publish shift %s failed: %s", sh.id, exc)
        return done

    def announce_tick(self) -> int:
        """Сообщить в Mattermost о начавшихся сменах. Возвращает число объявлений."""
        sent = 0
        with self.write() as op:
            s = op.session
            now = self.now()
            for team in self.list_teams(s):
                current = self.oncall(s, team.slug, now)
                if not current:
                    continue
                sh = current[0]
                if team.last_announced_shift_id == sh.id:
                    continue
                team.last_announced_shift_id = sh.id
                people = [a.person for a in sh.assignments]
                nxt = s.scalar(
                    select(Shift).options(selectinload(Shift.assignments).selectinload(ShiftAssignment.person))
                    .where(Shift.team_id == team.id, Shift.deleted.is_(False), Shift.start >= sh.end)
                    .order_by(Shift.start).limit(1)
                )
                text = f"🟢 **{team.name}**: началась смена до {fmt_dt(sh.end, team.timezone)}. Дежурит: {mentions(people)}"
                if nxt is not None:
                    text += (f"\nСледующая смена с {fmt_dt(nxt.start, team.timezone)}: "
                             f"{mentions([a.person for a in nxt.assignments])}")
                op.outbox.append((team.mattermost_webhook, text, team.mattermost_channel))
                sent += 1
        return sent

    def background_cycle(self) -> None:
        """Периодическая задача: праздники → внешние отсутствия → планирование → публикация."""
        year = self.now().year
        with self.write() as op:
            missing = [y for y in (year, year + 1) if not self.holidays_loaded(op.session, y)]
            if missing:
                try:
                    self.sync_holidays(op, missing)
                except Exception as exc:  # noqa: BLE001
                    log.warning("holiday sync failed: %s", exc)
        with self.write() as op:
            self.sync_external_absences(op)
            self.plan_all(op)
        self.publish_pending()
