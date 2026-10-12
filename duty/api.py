"""REST API /api/v1."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from . import auth, schemas as S
from .db import session_scope
from .ics import team_calendar
from .models import User
from .services import DutyService, ServiceError

SESSION_COOKIE = "duty_session"

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------- зависимости


def get_service(request: Request) -> DutyService:
    return request.app.state.service


def _raw_token(request: Request, authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.cookies.get(SESSION_COOKIE)


def optional_user(request: Request, authorization: Annotated[str | None, Header()] = None) -> User | None:
    raw = _raw_token(request, authorization)
    if not raw:
        return None
    with session_scope() as s:
        return auth.resolve_token(s, raw)


def current_user(user: Annotated[User | None, Depends(optional_user)]) -> User:
    if user is None:
        raise HTTPException(401, "нужно войти", headers={"WWW-Authenticate": "Bearer"})
    return user


def admin_user(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(403, "нужны права администратора")
    return user


Svc = Annotated[DutyService, Depends(get_service)]
CurrentUser = Annotated[User, Depends(current_user)]
AdminUser = Annotated[User, Depends(admin_user)]


def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


def _range(start: datetime | None, end: datetime | None, default_days: int = 31) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    start = start or (now - timedelta(days=1))
    end = end or (start + timedelta(days=default_days))
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return start, end


# ---------------------------------------------------------------- вход


@router.post("/auth/login", response_model=S.UserOut, tags=["auth"])
def login(data: S.LoginIn, response: Response, svc: Svc):
    with svc.write() as op:
        user, raw = svc.login(op, data.username, data.password)
        out = S.user_out(user)
    response.set_cookie(SESSION_COOKIE, raw, httponly=True, samesite="lax", secure=svc.settings.cookie_secure,
                        max_age=svc.settings.session_ttl_hours * 3600)
    return out


@router.post("/auth/logout", status_code=204, tags=["auth"])
def logout(request: Request, response: Response, svc: Svc):
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        with svc.write() as op:
            svc.logout(op, raw)
    response.delete_cookie(SESSION_COOKIE)


@router.get("/me", response_model=S.UserOut, tags=["auth"])
def me(user: CurrentUser):
    return S.user_out(user)


@router.get("/me/tokens", response_model=list[S.TokenOut], tags=["auth"])
def my_tokens(user: CurrentUser, svc: Svc):
    with svc.write(user) as op:
        return [S.TokenOut.model_validate(t) for t in svc.list_tokens(op)]


@router.post("/me/tokens", response_model=S.TokenCreated, tags=["auth"])
def create_token(data: S.TokenIn, user: CurrentUser, svc: Svc):
    with svc.write(user) as op:
        tok, raw = svc.create_token(op, data.name, data.days)
        return S.token_created(tok, raw)


@router.delete("/me/tokens/{token_id}", status_code=204, tags=["auth"])
def revoke_token(token_id: int, user: CurrentUser, svc: Svc):
    with svc.write(user) as op:
        svc.revoke_token(op, token_id)


# ---------------------------------------------------------------- пользователи (admin)


@router.get("/users", response_model=list[S.UserOut], tags=["users"])
def list_users(user: AdminUser, svc: Svc):
    with svc.write(user) as op:
        return [S.user_out(u) for u in svc.list_users(op)]


@router.post("/users", response_model=S.UserOut, status_code=201, tags=["users"])
def create_user(data: S.UserIn, user: AdminUser, svc: Svc):
    with svc.write(user) as op:
        return S.user_out(svc.create_user(op, data.username, data.password, data.role, data.display_name))


@router.patch("/users/{username}", response_model=S.UserOut, tags=["users"])
def update_user(username: str, data: S.UserPatch, user: CurrentUser, svc: Svc):
    with svc.write(user) as op:
        return S.user_out(svc.update_user(op, username, **data.model_dump(exclude_unset=True)))


# ---------------------------------------------------------------- команды


@router.get("/teams", response_model=list[S.TeamOut], tags=["teams"])
def list_teams(user: CurrentUser, svc: Svc):
    with svc.read() as s:
        return [S.team_out(t) for t in svc.list_teams(s)]


@router.post("/teams", response_model=S.TeamOut, status_code=201, tags=["teams"])
def create_team(data: S.TeamIn, user: AdminUser, svc: Svc):
    with svc.write(user) as op:
        t = svc.create_team(op, data.slug, data.model_dump(exclude={"slug"}, exclude_none=True))
        return S.team_out(t)


@router.get("/teams/{slug}", response_model=S.TeamOut, tags=["teams"])
def get_team(slug: str, user: CurrentUser, svc: Svc):
    with svc.read() as s:
        return S.team_out(svc.get_team(s, slug))


@router.patch("/teams/{slug}", response_model=S.TeamOut, tags=["teams"])
def update_team(slug: str, data: S.TeamFields, user: AdminUser, svc: Svc):
    with svc.write(user) as op:
        return S.team_out(svc.update_team(op, slug, data.model_dump(exclude_unset=True)))


@router.delete("/teams/{slug}", status_code=204, tags=["teams"])
def delete_team(slug: str, user: AdminUser, svc: Svc):
    with svc.write(user) as op:
        svc.delete_team(op, slug)


@router.post("/teams/{slug}/members", response_model=S.TeamOut, status_code=201, tags=["teams"])
def add_member(slug: str, data: S.MemberIn, user: AdminUser, svc: Svc):
    with svc.write(user) as op:
        svc.add_member(op, slug, data.email, data.name, data.mattermost_username, data.position, data.active)
        return S.team_out(svc.get_team(op.session, slug))


@router.put("/teams/{slug}/members", response_model=S.TeamOut, tags=["teams"])
def reorder_members(slug: str, emails: list[str], user: AdminUser, svc: Svc):
    with svc.write(user) as op:
        svc.reorder_members(op, slug, emails)
        return S.team_out(svc.get_team(op.session, slug))


@router.patch("/teams/{slug}/members/{email}", response_model=S.TeamOut, tags=["teams"])
def update_member(slug: str, email: str, data: S.MemberPatch, user: AdminUser, svc: Svc):
    with svc.write(user) as op:
        svc.update_member(op, slug, email, data.active, data.position)
        return S.team_out(svc.get_team(op.session, slug))


@router.delete("/teams/{slug}/members/{email}", response_model=S.TeamOut, tags=["teams"])
def remove_member(slug: str, email: str, user: AdminUser, svc: Svc):
    with svc.write(user) as op:
        svc.remove_member(op, slug, email)
        return S.team_out(svc.get_team(op.session, slug))


@router.get("/teams/{slug}/oncall", response_model=list[S.ShiftOut], tags=["shifts"])
def oncall(slug: str, user: CurrentUser, svc: Svc, at: datetime | None = None):
    with svc.read() as s:
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        return [S.shift_out(sh) for sh in svc.oncall(s, slug, at)]


@router.post("/teams/{slug}/plan", response_model=S.PlanReportOut, tags=["shifts"])
def plan_team(slug: str, user: CurrentUser, svc: Svc, mode: str = "fix", dry_run: bool = False,
              freeze_days: int | None = None):
    with svc.write(user) as op:
        return S.report_out(svc.plan_team(op, slug, mode, dry_run, freeze_days))


@router.get("/teams/{slug}/calendar.ics", tags=["shifts"])
def team_ics(slug: str, request: Request, svc: Svc, token: str | None = None,
             authorization: Annotated[str | None, Header()] = None):
    """Подписка на смены команды. Для календарных клиентов токен передаётся в ?token=… (личный токен)."""
    with session_scope() as s:
        user = auth.resolve_token(s, token or _raw_token(request, authorization))
    if user is None:
        raise HTTPException(401, "нужен токен")
    now = datetime.now(UTC)
    with svc.read() as s:
        team = svc.get_team(s, slug)
        shifts = svc.list_shifts(s, now - timedelta(days=30), now + timedelta(days=team.horizon_days + 7), team_slug=slug)
        body = team_calendar(team, shifts, svc.settings.public_url)
    return Response(body, media_type="text/calendar; charset=utf-8")


# ---------------------------------------------------------------- люди


@router.get("/people", response_model=list[S.PersonOut], tags=["people"])
def list_people(user: CurrentUser, svc: Svc):
    with svc.read() as s:
        return [S.PersonOut.model_validate(p) for p in svc.list_people(s)]


@router.post("/people", response_model=S.PersonOut, tags=["people"])
def upsert_person(data: S.PersonIn, user: AdminUser, svc: Svc):
    with svc.write(user) as op:
        return S.PersonOut.model_validate(svc.upsert_person(op, data.email, data.name, data.mattermost_username))


# ---------------------------------------------------------------- смены


@router.get("/shifts", response_model=list[S.ShiftOut], tags=["shifts"])
def list_shifts(user: CurrentUser, svc: Svc, team: str | None = None, person: str | None = None,
                start: datetime | None = None, end: datetime | None = None):
    st, en = _range(start, end)
    with svc.read() as s:
        return [S.shift_out(sh) for sh in svc.list_shifts(s, st, en, team, person)]


@router.get("/shifts/{shift_id}", response_model=S.ShiftOut, tags=["shifts"])
def get_shift(shift_id: int, user: CurrentUser, svc: Svc):
    with svc.read() as s:
        return S.shift_out(svc.get_shift(s, shift_id))


@router.get("/shifts/{shift_id}/log", response_model=list[S.ShiftLogOut], tags=["shifts"])
def shift_log(shift_id: int, user: CurrentUser, svc: Svc):
    with svc.read() as s:
        return S.shift_log_out(svc.shift_log(s, shift_id))


@router.post("/shifts/{shift_id}/assign", response_model=S.ShiftOut, tags=["shifts"])
def assign(shift_id: int, data: S.AssignIn, user: CurrentUser, svc: Svc):
    with svc.write(user) as op:
        return S.shift_out(svc.assign(op, shift_id, data.email, data.seat, data.force))


@router.post("/teams/{slug}/shifts", response_model=S.ManualShiftResult, status_code=201, tags=["shifts"])
def create_manual_shift(slug: str, data: S.ManualShiftIn, user: CurrentUser, svc: Svc):
    """Ручное дежурство на N календарных дней (вырезается из сетки ротации)."""
    with svc.write(user) as op:
        shift, report = svc.create_manual_shift(op, slug, data.start_date, data.days, data.emails, data.force)
        return S.ManualShiftResult(shift=S.shift_out(shift), plan=S.report_out(report))


@router.delete("/shifts/{shift_id}", response_model=S.PlanReportOut, tags=["shifts"])
def delete_shift(shift_id: int, user: CurrentUser, svc: Svc):
    """Удалить ручную смену — период вернётся в сетку ротации."""
    with svc.write(user) as op:
        return S.report_out(svc.delete_shift(op, shift_id))


@router.post("/shifts/{shift_id}/unpin", response_model=S.ShiftOut, tags=["shifts"])
def unpin(shift_id: int, data: S.UnpinIn, user: CurrentUser, svc: Svc):
    with svc.write(user) as op:
        return S.shift_out(svc.unpin(op, shift_id, data.seat))


@router.post("/shifts/swap", response_model=list[S.ShiftOut], tags=["shifts"])
def swap(data: S.SwapIn, user: CurrentUser, svc: Svc):
    with svc.write(user) as op:
        a, b = svc.swap(op, data.shift_a, data.seat_a, data.shift_b, data.seat_b, data.force)
        return [S.shift_out(a), S.shift_out(b)]


# ---------------------------------------------------------------- отсутствия


@router.get("/absences", response_model=list[S.AbsenceOut], tags=["absences"])
def list_absences(user: CurrentUser, svc: Svc, person: str | None = None, team: str | None = None,
                  start: datetime | None = None, end: datetime | None = None):
    with svc.read() as s:
        return [S.absence_out(a) for a in svc.list_absences(s, start, end, person, team)]


@router.post("/absences", response_model=S.AbsenceResult, tags=["absences"])
def add_absence(data: S.AbsenceIn, user: CurrentUser, svc: Svc, dry_run: bool = False):
    with svc.write(user) as op:
        absence, reports = svc.add_absence(op, data.email, data.start, data.end, data.kind, data.note, dry_run)
        return S.AbsenceResult(absence=S.absence_out(absence) if absence else None,
                               plans=[S.report_out(r) for r in reports])


@router.patch("/absences/{absence_id}", response_model=S.AbsenceResult, tags=["absences"])
def update_absence(absence_id: int, data: S.AbsencePatch, user: CurrentUser, svc: Svc, dry_run: bool = False):
    with svc.write(user) as op:
        absence, reports = svc.update_absence(op, absence_id, data.start, data.end, data.kind, data.note, dry_run)
        out = S.AbsenceResult(absence=S.absence_out(absence), plans=[S.report_out(r) for r in reports])
        if dry_run:
            op.session.rollback()
        return out


@router.delete("/absences/{absence_id}", response_model=S.AbsenceResult, tags=["absences"])
def delete_absence(absence_id: int, user: CurrentUser, svc: Svc, dry_run: bool = False):
    with svc.write(user) as op:
        reports = svc.delete_absence(op, absence_id, dry_run)
        return S.AbsenceResult(absence=None, plans=[S.report_out(r) for r in reports])


# ---------------------------------------------------------------- праздники


@router.get("/holidays", response_model=list[S.HolidayOut], tags=["holidays"])
def list_holidays(user: CurrentUser, svc: Svc, start: date | None = None, end: date | None = None):
    today = date.today()
    start = start or date(today.year, 1, 1)
    end = end or date(today.year, 12, 31)
    with svc.read() as s:
        return [S.holiday_out(h) for h in svc.list_holidays(s, start, end)]


@router.put("/holidays", response_model=S.HolidayOut, tags=["holidays"])
def set_holiday(data: S.HolidayIn, user: AdminUser, svc: Svc):
    with svc.write(user) as op:
        return S.holiday_out(svc.set_holiday(op, data.day, data.name, data.kind))


@router.delete("/holidays/{day}", status_code=204, tags=["holidays"])
def delete_holiday(day: date, user: AdminUser, svc: Svc):
    with svc.write(user) as op:
        svc.delete_holiday(op, day)


@router.post("/holidays/sync", tags=["holidays"])
def sync_holidays(user: AdminUser, svc: Svc, year: Annotated[list[int] | None, Query()] = None):
    years = year or [date.today().year, date.today().year + 1]
    with svc.write(user) as op:
        n = svc.sync_holidays(op, years)
        svc._replan_holiday_teams(op)
    return {"loaded": n, "years": years, "provider": svc.holiday_provider.name}


# ---------------------------------------------------------------- календарь для веба


@router.get("/calendar", response_model=S.CalendarOut, tags=["calendar"])
def calendar(user: CurrentUser, svc: Svc, start: datetime, end: datetime,
             teams: Annotated[list[str] | None, Query()] = None):
    st, en = _range(start, end)
    with svc.read() as s:
        shifts = svc.list_shifts(s, st, en)
        if teams:
            shifts = [sh for sh in shifts if sh.team.slug in teams]
        absences = []
        seen = set()
        for slug in teams or [t.slug for t in svc.list_teams(s)]:
            for a in svc.list_absences(s, st, en, team_slug=slug):
                if a.id not in seen:
                    seen.add(a.id)
                    absences.append(a)
        holidays = svc.list_holidays(s, (st - timedelta(days=1)).date(), en.date())
        return S.CalendarOut(
            shifts=[S.shift_out(sh) for sh in shifts],
            absences=[S.absence_out(a) for a in absences],
            holidays=[S.holiday_out(h) for h in holidays],
        )


# ---------------------------------------------------------------- служебное


@router.post("/admin/run-cycle", tags=["admin"])
def run_cycle(user: AdminUser, svc: Svc):
    """Запустить фоновый цикл сейчас: праздники, внешние отсутствия, планирование, публикация."""
    svc.background_cycle()
    return {"ok": True}
