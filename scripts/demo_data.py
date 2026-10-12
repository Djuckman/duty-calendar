"""Заполнить запущенный сервис демо-данными через REST API.

    python scripts/demo_data.py http://localhost:8000 admin <пароль>
"""

import sys
from datetime import date, timedelta

import httpx


def main(base: str, username: str, password: str) -> None:
    c = httpx.Client(base_url=base.rstrip("/") + "/api/v1", timeout=30)
    c.post("/auth/login", json={"username": username, "password": password}).raise_for_status()
    c.post("/holidays/sync")
    today = date.today()
    monday = today - timedelta(days=today.weekday())

    teams = [
        {"slug": "sec", "name": "SEC", "shift_length_days": 7, "people_per_shift": 1, "handoff_time": "10:00",
         "anchor_date": str(monday - timedelta(weeks=2))},
        {"slug": "backend", "name": "Backend", "shift_length_days": 2, "people_per_shift": 2, "handoff_time": "09:00",
         "anchor_date": str(monday - timedelta(weeks=2))},
    ]
    members = {
        "sec": [("ivanov@example.ru", "Иванов Иван", "ivanov"), ("petrova@example.ru", "Петрова Анна", "petrova"),
                ("sidorov@example.ru", "Сидоров Пётр", "sidorov"), ("kim@example.ru", "Ким Алексей", "kim")],
        "backend": [("orlov@example.ru", "Орлов Дмитрий", "orlov"), ("belova@example.ru", "Белова Мария", "belova"),
                    ("zaitsev@example.ru", "Зайцев Олег", "zaitsev"), ("kim@example.ru", "Ким Алексей", "kim"),
                    ("frolova@example.ru", "Фролова Ольга", "frolova")],
    }
    for t in teams:
        r = c.post("/teams", json=t)
        if r.status_code not in (201, 409):
            r.raise_for_status()
        for email, name, mm in members[t["slug"]]:
            c.post(f"/teams/{t['slug']}/members", json={"email": email, "name": name, "mattermost_username": mm})
        c.post(f"/teams/{t['slug']}/plan", params={"mode": "rebalance"})

    absences = [
        ("petrova@example.ru", monday + timedelta(days=8), monday + timedelta(days=18), "vacation", "Отпуск"),
        ("orlov@example.ru", monday + timedelta(days=3), monday + timedelta(days=4), "sick", ""),
        ("kim@example.ru", monday + timedelta(days=22), monday + timedelta(days=26), "dayoff", "Отгулы"),
    ]
    for email, s, e, kind, note in absences:
        c.post("/absences", json={"email": email, "start": str(s), "end": str(e), "kind": kind, "note": note})
    print("ok")


if __name__ == "__main__":
    main(*sys.argv[1:4])
