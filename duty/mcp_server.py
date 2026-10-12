"""MCP-сервер поверх сервисного слоя. Монтируется в /mcp (Streamable HTTP).

Авторизация: заголовок `Authorization: Bearer <личный токен>` (создаётся в веб-интерфейсе или
POST /api/v1/me/tokens). Права те же, что в API: user меняет график, admin — ещё и составы команд.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from . import auth, schemas as S
from .db import session_scope
from .models import User
from .services import DutyService, ServiceError

INSTRUCTIONS = """\
Сервис графика дежурств. Команды (teams) — отдельные графики со своими правилами ротации.
Смена (shift) имеет id, интервал start–end и несколько мест (seats) для дежурных.
Дежурных назначает планировщик: «дольше всех не дежурил», отсутствующие пропускаются.
Ручное назначение закрепляет место (pinned) — планировщик его не трогает.
Перед изменениями, затрагивающими много смен, используйте dry_run=true и покажите пользователю,
что изменится. Даты: 'YYYY-MM-DD' (для отсутствий конец включительно) или ISO 8601 с временем.
"""


def _dump(model) -> Any:
    if isinstance(model, list):
        return [m.model_dump(mode="json") for m in model]
    return model.model_dump(mode="json")


def _header_user() -> User | None:
    headers = get_http_headers(include={"authorization"})
    value = headers.get("authorization", "")
    if not value.lower().startswith("bearer "):
        return None
    with session_scope() as s:
        return auth.resolve_token(s, value[7:].strip())


def _parse_dt(value: str | None, svc: DutyService, end: bool = False) -> datetime | None:
    if not value:
        return None
    return svc.parse_moment(value, end=end)


def build_mcp(svc: DutyService, resolve_user: Callable[[], User | None] = _header_user) -> FastMCP:
    mcp = FastMCP("duty-calendar", instructions=INSTRUCTIONS)

    def user() -> User:
        u = resolve_user()
        if u is None:
            raise PermissionError("Нужен личный токен: Authorization: Bearer <token>")
        return u

    def call(fn):
        try:
            return fn()
        except ServiceError as exc:
            raise ValueError(exc.message) from exc

    # ------------------------------------------------------------ чтение

    @mcp.tool(annotations={"readOnlyHint": True})
    def list_teams() -> list[dict]:
        """Список команд с правилами ротации и составом."""
        user()
        with svc.read() as s:
            return _dump([S.team_out(t) for t in svc.list_teams(s)])

    @mcp.tool(annotations={"readOnlyHint": True})
    def who_is_on_call(team: str, at: str | None = None) -> list[dict]:
        """Кто дежурит в команде сейчас или в момент `at` (ISO 8601)."""
        user()
        with svc.read() as s:
            return call(lambda: _dump([S.shift_out(x) for x in svc.oncall(s, team, _parse_dt(at, svc))]))

    @mcp.tool(annotations={"readOnlyHint": True})
    def get_schedule(
        team: str | None = None, start: str | None = None, end: str | None = None, person: str | None = None
    ) -> list[dict]:
        """Смены за период (по умолчанию ближайшие 14 дней). Фильтры: команда (slug), человек (email)."""
        user()
        st = _parse_dt(start, svc) or datetime.now(UTC)
        en = _parse_dt(end, svc, end=True) or st + timedelta(days=14)
        with svc.read() as s:
            return call(lambda: _dump([S.shift_out(x) for x in svc.list_shifts(s, st, en, team, person)]))

    @mcp.tool(annotations={"readOnlyHint": True})
    def list_absences(
        person: str | None = None, team: str | None = None, start: str | None = None, end: str | None = None
    ) -> list[dict]:
        """Отсутствия (отпуска, больничные) с фильтрами по человеку, команде и периоду."""
        user()
        with svc.read() as s:
            return call(lambda: _dump([
                S.absence_out(a)
                for a in svc.list_absences(s, _parse_dt(start, svc), _parse_dt(end, svc, end=True), person, team)
            ]))

    @mcp.tool(annotations={"readOnlyHint": True})
    def list_holidays(year: int | None = None) -> list[dict]:
        """Производственный календарь: праздники, перенесённые выходные и рабочие дни."""
        user()
        y = year or date.today().year
        with svc.read() as s:
            return _dump([S.holiday_out(h) for h in svc.list_holidays(s, date(y, 1, 1), date(y, 12, 31))])

    # ------------------------------------------------------------ изменения

    @mcp.tool
    def add_absence(
        email: str,
        start: str,
        end: str,
        kind: Literal["vacation", "sick", "dayoff", "other"] = "vacation",
        note: str = "",
        dry_run: bool = False,
    ) -> dict:
        """Добавить отсутствие и перепланировать затронутые смены. dry_run=true — только показать изменения."""
        u = user()

        def run():
            with svc.write(u) as op:
                absence, reports = svc.add_absence(op, email, start, end, kind, note, dry_run)
                return _dump(S.AbsenceResult(absence=S.absence_out(absence) if absence else None,
                                             plans=[S.report_out(r) for r in reports]))

        return call(run)

    @mcp.tool
    def remove_absence(absence_id: int, dry_run: bool = False) -> dict:
        """Удалить отсутствие и перепланировать. dry_run=true — только показать изменения."""
        u = user()

        def run():
            with svc.write(u) as op:
                reports = svc.delete_absence(op, absence_id, dry_run)
                return _dump(S.AbsenceResult(absence=None, plans=[S.report_out(r) for r in reports]))

        return call(run)

    @mcp.tool
    def update_absence(
        absence_id: int,
        start: str | None = None,
        end: str | None = None,
        kind: Literal["vacation", "sick", "dayoff", "other"] | None = None,
        note: str | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Изменить отсутствие (даты, тип, комментарий) и перепланировать. dry_run=true — только показать."""
        u = user()

        def run():
            with svc.write(u) as op:
                absence, reports = svc.update_absence(op, absence_id, start, end, kind, note, dry_run)
                out = _dump(S.AbsenceResult(absence=S.absence_out(absence), plans=[S.report_out(r) for r in reports]))
                if dry_run:
                    op.session.rollback()
                return out

        return call(run)

    @mcp.tool
    def set_manual_shift(team: str, start_date: str, days: int, emails: list[str], force: bool = False) -> dict:
        """Назначить дежурство вручную на N календарных дней начиная с start_date (YYYY-MM-DD) одному или
        нескольким людям. Период вырезается из сетки ротации, соседние смены укорачиваются."""
        u = user()

        def run():
            with svc.write(u) as op:
                shift, report = svc.create_manual_shift(op, team, start_date, days, emails, force)
                return _dump(S.ManualShiftResult(shift=S.shift_out(shift), plan=S.report_out(report)))

        return call(run)

    @mcp.tool
    def delete_manual_shift(shift_id: int) -> dict:
        """Удалить ручную смену — её период вернётся в сетку ротации."""
        u = user()

        def run():
            with svc.write(u) as op:
                return _dump(S.report_out(svc.delete_shift(op, shift_id)))

        return call(run)

    @mcp.tool
    def assign_shift(shift_id: int, email: str, seat: int | None = None, force: bool = False) -> dict:
        """Назначить человека на место в смене (место закрепляется). seat=None — добавить ещё одно место.
        force=true — назначить, даже если человек в отпуске."""
        u = user()

        def run():
            with svc.write(u) as op:
                return _dump(S.shift_out(svc.assign(op, shift_id, email, seat, force)))

        return call(run)

    @mcp.tool
    def free_seat(shift_id: int, seat: int) -> dict:
        """Освободить место в смене; планировщик заполнит его заново."""
        u = user()

        def run():
            with svc.write(u) as op:
                return _dump(S.shift_out(svc.assign(op, shift_id, None, seat)))

        return call(run)

    @mcp.tool
    def unpin_shift(shift_id: int, seat: int = 0) -> dict:
        """Снять закрепление с места — планировщик снова может его менять."""
        u = user()

        def run():
            with svc.write(u) as op:
                return _dump(S.shift_out(svc.unpin(op, shift_id, seat)))

        return call(run)

    @mcp.tool
    def swap_shifts(shift_a: int, shift_b: int, seat_a: int = 0, seat_b: int = 0, force: bool = False) -> list[dict]:
        """Поменять дежурных местами между двумя сменами (обе закрепляются)."""
        u = user()

        def run():
            with svc.write(u) as op:
                a, b = svc.swap(op, shift_a, seat_a, shift_b, seat_b, force)
                return _dump([S.shift_out(a), S.shift_out(b)])

        return call(run)

    @mcp.tool
    def replan(
        team: str, mode: Literal["fix", "rebalance"] = "fix", dry_run: bool = True, freeze_days: int | None = None
    ) -> dict:
        """Перепланировать команду. fix — исправить только сломанные смены; rebalance — пересчитать будущее,
        кроме закреплённых и смен в ближайшие freeze_days дней (по умолчанию — из настроек команды,
        0 — пересчитать всё, кроме уже начавшихся). По умолчанию dry_run=true."""
        u = user()

        def run():
            with svc.write(u) as op:
                return _dump(S.report_out(svc.plan_team(op, team, mode, dry_run, freeze_days)))

        return call(run)

    # ------------------------------------------------------------ состав (admin)

    @mcp.tool
    def add_member(team: str, email: str, name: str | None = None, mattermost_username: str | None = None) -> dict:
        """[admin] Добавить человека в команду (новому человеку нужно имя)."""
        u = user()

        def run():
            with svc.write(u) as op:
                svc.add_member(op, team, email, name, mattermost_username)
                return _dump(S.team_out(svc.get_team(op.session, team)))

        return call(run)

    @mcp.tool
    def set_member_active(team: str, email: str, active: bool) -> dict:
        """[admin] Включить/выключить человека из ротации команды."""
        u = user()

        def run():
            with svc.write(u) as op:
                svc.update_member(op, team, email, active=active)
                return _dump(S.team_out(svc.get_team(op.session, team)))

        return call(run)

    @mcp.tool
    def remove_member(team: str, email: str) -> dict:
        """[admin] Убрать человека из команды."""
        u = user()

        def run():
            with svc.write(u) as op:
                svc.remove_member(op, team, email)
                return _dump(S.team_out(svc.get_team(op.session, team)))

        return call(run)

    return mcp
