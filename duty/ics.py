"""ICS-лента смен команды — для подписки в любом календаре (Outlook, Thunderbird, телефон)."""

from __future__ import annotations

from datetime import UTC, datetime

from icalendar import Calendar, Event

from .models import Shift, Team


def team_calendar(team: Team, shifts: list[Shift], base_url: str) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//duty-calendar//RU")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", f"Дежурства: {team.name}")
    cal.add("x-wr-timezone", team.timezone)
    now = datetime.now(UTC)
    for sh in shifts:
        people = [a.person.name for a in sorted(sh.assignments, key=lambda a: a.seat) if a.person]
        ev = Event()
        ev.add("uid", f"shift-{sh.id}@duty-calendar")
        ev.add("dtstamp", now)
        ev.add("dtstart", sh.start)
        ev.add("dtend", sh.end)
        ev.add("summary", f"Дежурство {team.name}: " + (", ".join(people) if people else "НЕ НАЗНАЧЕН"))
        ev.add("url", f"{base_url.rstrip('/')}/#shift-{sh.id}")
        ev.add("transp", "TRANSPARENT")  # не блокировать занятость
        cal.add_component(ev)
    return cal.to_ical()
