from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


def names(shifts):
    return [[s["person"]["name"] if s["person"] else None for s in sh["seats"]] for sh in shifts]


def get_shifts(client, team="sec"):
    r = client.get("/api/v1/shifts", params={"team": team, "start": "2026-10-01T00:00:00Z",
                                             "end": "2026-11-20T00:00:00Z"})
    assert r.status_code == 200, r.text
    return r.json()


def test_requires_login(client):
    assert client.get("/api/v1/teams").status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 403


def test_rotation_created(team, admin):
    shifts = get_shifts(admin)
    assert names(shifts)[:6] == [["Аня"], ["Боря"], ["Вика"], ["Аня"], ["Боря"], ["Вика"]]
    assert shifts[0]["start"].startswith("2026-10-05T07:00:00")


def test_absence_dry_run_then_apply(team, admin, notifier):
    body = {"email": "b@x.ru", "start": "2026-10-13", "end": "2026-10-14", "kind": "vacation"}
    r = admin.post("/api/v1/absences", params={"dry_run": True}, json=body)
    assert r.status_code == 200, r.text
    plan = r.json()["plans"][0]
    assert r.json()["absence"] is None
    assert [(c["old"]["name"], c["new"]["name"], c["reason"]) for c in plan["changes"]] == [("Боря", "Вика", "absent")]
    assert names(get_shifts(admin))[1] == ["Боря"]  # dry run ничего не поменял

    notifier.messages.clear()
    r = admin.post("/api/v1/absences", json=body)
    assert r.status_code == 200
    assert names(get_shifts(admin))[1] == ["Вика"]
    assert any("@borya → @vika" in m[1] for m in notifier.messages)

    absence_id = r.json()["absence"]["id"]
    r = admin.delete(f"/api/v1/absences/{absence_id}", params={"dry_run": True})
    assert r.json()["plans"][0]["changes"] == []  # fix-режим не возвращает Борю: смена валидна у Вики


def test_manual_assign_pins_and_checks_absence(team, admin):
    shifts = get_shifts(admin)
    sid = shifts[0]["id"]
    admin.post("/api/v1/absences", json={"email": "c@x.ru", "start": "2026-10-06", "end": "2026-10-07"})
    r = admin.post(f"/api/v1/shifts/{sid}/assign", json={"email": "c@x.ru", "seat": 0})
    assert r.status_code == 409 and "отсутствует" in r.json()["detail"]
    r = admin.post(f"/api/v1/shifts/{sid}/assign", json={"email": "c@x.ru", "seat": 0, "force": True})
    assert r.status_code == 200
    assert r.json()["seats"][0] == {"seat": 0, "person": r.json()["seats"][0]["person"], "pinned": True}
    # закреплённое место планировщик не трогает, но сообщает конфликт
    plan = admin.post("/api/v1/teams/sec/plan", params={"dry_run": True}).json()
    assert any(c["reason"] == "absent" and c["shift_id"] == sid for c in plan["conflicts"])
    # открепляем — планировщик переназначает
    r = admin.post(f"/api/v1/shifts/{sid}/unpin", json={"seat": 0})
    assert r.json()["seats"][0]["person"]["name"] != "Вика"


def test_assign_extra_seat_and_free_it(team, admin):
    sid = get_shifts(admin)[0]["id"]
    r = admin.post(f"/api/v1/shifts/{sid}/assign", json={"email": "b@x.ru"})  # seat=None → доп. место
    assert [s["person"]["name"] for s in r.json()["seats"]] == ["Аня", "Боря"]
    r = admin.post(f"/api/v1/shifts/{sid}/assign", json={"email": "a@x.ru", "seat": 1})
    assert r.status_code == 409  # Аня уже в смене
    r = admin.post(f"/api/v1/shifts/{sid}/assign", json={"email": None, "seat": 1})
    assert len(r.json()["seats"]) == 1


def test_swap(team, admin):
    s = get_shifts(admin)
    r = admin.post("/api/v1/shifts/swap", json={"shift_a": s[0]["id"], "shift_b": s[1]["id"]})
    assert r.status_code == 200, r.text
    assert names(get_shifts(admin))[:2] == [["Боря"], ["Аня"]]
    log = admin.get(f"/api/v1/shifts/{s[0]['id']}/log").json()
    assert log[-1]["reason"] == "swap" and log[-1]["actor"] == "admin"


def test_two_people_per_shift(team, admin):
    r = admin.patch("/api/v1/teams/sec", json={"people_per_shift": 2})
    assert r.status_code == 200, r.text
    shifts = get_shifts(admin)
    assert all(len(sh["seats"]) == 2 for sh in shifts)
    assert all(len({p for p in n}) == 2 for n in names(shifts))


def test_change_rotation_length_regenerates_grid(team, admin):
    r = admin.patch("/api/v1/teams/sec", json={"shift_length_days": 2})
    assert r.status_code == 200
    shifts = get_shifts(admin)
    lengths = {(datetime.fromisoformat(s["end"]) - datetime.fromisoformat(s["start"])).days for s in shifts}
    assert lengths == {2}


def test_split_policy_not_implemented(team, admin):
    r = admin.patch("/api/v1/teams/sec", json={"absence_policy": "split"})
    assert r.status_code == 422 and "split" in r.json()["detail"]
    assert admin.get("/api/v1/teams/sec").json()["absence_policy"] == "whole"


def test_daily_business_days_skip_holidays(admin, service):
    with service.write() as op:
        service.sync_holidays(op, [2026])
    r = admin.post("/api/v1/teams", json={"slug": "ops", "shift_length_days": 1, "weekdays": "12345",
                                          "skip_holidays": True, "anchor_date": "2026-11-02", "horizon_days": 60})
    assert r.status_code == 201, r.text
    admin.post("/api/v1/teams/ops/members", json={"email": "a@x.ru", "name": "Аня"})
    r = admin.get("/api/v1/shifts", params={"team": "ops", "start": "2026-11-02T00:00:00Z",
                                            "end": "2026-11-09T00:00:00Z"})
    days = [datetime.fromisoformat(s["start"]).day for s in r.json()]
    assert days == [2, 3, 5, 6]  # 4 ноября — праздник, выходные пропущены


def test_roles(team, admin, client):
    r = admin.post("/api/v1/users", json={"username": "ivan", "password": "ivan-pass-1", "role": "user"})
    assert r.status_code == 201
    admin.post("/api/v1/auth/logout")
    r = client.post("/api/v1/auth/login", json={"username": "ivan", "password": "ivan-pass-1"})
    assert r.status_code == 200
    assert client.post("/api/v1/teams", json={"slug": "x2"}).status_code == 403
    assert client.post("/api/v1/teams/sec/members", json={"email": "z@x.ru", "name": "Z"}).status_code == 403
    r = client.post("/api/v1/absences", json={"email": "a@x.ru", "start": "2026-12-01", "end": "2026-12-02"})
    assert r.status_code == 200
    assert r.json()["absence"]["created_by"] == "ivan"


def test_personal_token_and_ics(team, admin, client):
    r = admin.post("/api/v1/me/tokens", json={"name": "mcp"})
    raw = r.json()["token"]
    client.cookies.clear()
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {raw}"}).json()["username"] == "admin"
    r = client.get("/api/v1/teams/sec/calendar.ics", params={"token": raw})
    assert r.status_code == 200 and b"BEGIN:VEVENT" in r.content and "Дежурство SEC".encode() in r.content
    assert client.get("/api/v1/teams/sec/calendar.ics").status_code == 401


def test_calendar_endpoint_has_holidays(team, admin):
    r = admin.get("/api/v1/calendar", params={"start": "2026-11-01T00:00:00Z", "end": "2026-11-30T00:00:00Z"})
    body = r.json()
    assert any(h["day"] == "2026-11-04" for h in body["holidays"])
    assert body["shifts"]


def test_announce_and_publish(team, admin, service, clock, notifier):
    clock.now = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    notifier.messages.clear()
    assert service.announce_tick() == 1
    assert "началась смена" in notifier.messages[-1][1] and "@anya" in notifier.messages[-1][1]
    assert service.announce_tick() == 0  # повторно не объявляем
    assert service.publish_pending() > 0
    assert service.publish_pending() == 0


def test_mcp_requires_token(client):
    r = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401


def test_business_days_two_day_rotation_after_adding_people_one_by_one(admin, service, clock):
    """Сценарий из жалобы: 2-дневная ротация по будням, люди добавляются по одному."""
    clock.now = datetime(2026, 10, 12, 18, 26, tzinfo=UTC)
    with service.write() as op:
        service.sync_holidays(op, [2026])
    r = admin.post("/api/v1/teams", json={"slug": "ops2", "shift_length_days": 2, "weekdays": "12345",
                                          "skip_holidays": True, "handoff_time": "11:00",
                                          "anchor_date": "2026-10-12", "horizon_days": 35})
    assert r.status_code == 201, r.text
    for login in ["djuckman", "roma", "egor", "enot"]:
        admin.post("/api/v1/teams/ops2/members", json={"email": f"{login}@x.ru", "name": login})
    shifts = admin.get("/api/v1/shifts", params={"team": "ops2", "start": "2026-10-12T00:00:00Z",
                                                 "end": "2026-11-15T00:00:00Z"}).json()
    msk = ZoneInfo("Europe/Moscow")
    rows = []
    for sh in shifts:
        st = datetime.fromisoformat(sh["start"]).astimezone(msk)
        en = datetime.fromisoformat(sh["end"]).astimezone(msk)
        rows.append((st, en, sh["seats"][0]["person"]["name"]))
    # ни одна смена не захватывает выходные и 4 ноября
    for st, en, _ in rows:
        day = st.date()
        while day < en.date():
            assert day.isoweekday() <= 5 and day != date(2026, 11, 4), (st, en)
            day += timedelta(days=1)
    # смены не пересекаются
    for (s1, e1, _), (s2, e2, _) in zip(rows, rows[1:]):
        assert e1 <= s2
    # текущую (уже начавшуюся) смену не трогаем, дальше — честная очередь без повторов подряд
    people = [p for _, _, p in rows]
    assert people[0] == "djuckman"
    groups = []
    for (st, en, p) in rows:  # склеиваем куски одной смены, разорванные выходными/праздником (пт + пн)
        if groups and groups[-1][1] == p and groups[-1][2] < st:
            groups[-1] = (groups[-1][0], p, en)
        else:
            groups.append((st, p, en))
    seq = [p for _, p, _ in groups]
    assert all(a != b for a, b in zip(seq, seq[1:])), seq
    assert seq[1:5] and len(set(seq[1:5])) == 4, seq


def _rows(admin, team, start="2026-10-01T00:00:00Z", end="2026-11-20T00:00:00Z"):
    msk = ZoneInfo("Europe/Moscow")
    out = []
    for sh in admin.get("/api/v1/shifts", params={"team": team, "start": start, "end": end}).json():
        out.append((datetime.fromisoformat(sh["start"]).astimezone(msk).strftime("%d.%m"),
                    datetime.fromisoformat(sh["end"]).astimezone(msk).strftime("%d.%m"),
                    [s["person"]["name"] if s["person"] else None for s in sh["seats"]], sh["manual"], sh["id"]))
    return out


def test_manual_shift_one_day_inside_week(team, admin):
    # неделя 12.10–19.10 у Бори; вручную ставим Аню на 14.10 (1 день)
    r = admin.post("/api/v1/teams/sec/shifts", json={"start_date": "2026-10-14", "days": 1, "emails": ["a@x.ru"]})
    assert r.status_code == 201, r.text
    rows = [x[:4] for x in _rows(admin, "sec", end="2026-10-18T00:00:00Z")]
    assert rows[1:] == [("12.10", "14.10", ["Боря"], False), ("14.10", "15.10", ["Аня"], True),
                        ("15.10", "19.10", ["Боря"], False)]
    # пересекающуюся ручную смену создать нельзя
    r = admin.post("/api/v1/teams/sec/shifts", json={"start_date": "2026-10-13", "days": 2, "emails": ["b@x.ru"]})
    assert r.status_code == 409
    # удаление ручной смены возвращает период в сетку
    manual_id = [x for x in _rows(admin, "sec") if x[3]][0][4]
    assert admin.delete(f"/api/v1/shifts/{manual_id}").status_code == 200
    rows = [x[:4] for x in _rows(admin, "sec", end="2026-10-18T00:00:00Z")]
    assert rows[1:] == [("12.10", "19.10", ["Боря"], False)]
    # обычную смену удалить нельзя
    grid_id = _rows(admin, "sec")[0][4]
    assert admin.delete(f"/api/v1/shifts/{grid_id}").status_code == 422


def test_manual_shift_several_people_and_absence_check(team, admin):
    admin.post("/api/v1/absences", json={"email": "c@x.ru", "start": "2026-10-20", "end": "2026-10-21"})
    body = {"start_date": "2026-10-20", "days": 2, "emails": ["b@x.ru", "c@x.ru"]}
    r = admin.post("/api/v1/teams/sec/shifts", json=body)
    assert r.status_code == 409 and "отсутствует" in r.json()["detail"]
    r = admin.post("/api/v1/teams/sec/shifts", json={**body, "force": True})
    assert r.status_code == 201
    assert [s["person"]["name"] for s in r.json()["shift"]["seats"]] == ["Боря", "Вика"]


def test_update_absence(team, admin):
    r = admin.post("/api/v1/absences", json={"email": "b@x.ru", "start": "2026-11-20", "end": "2026-11-21"})
    aid = r.json()["absence"]["id"]
    # переносим на неделю Бори (13.10) — сначала предпросмотр
    r = admin.patch(f"/api/v1/absences/{aid}", params={"dry_run": True}, json={"start": "2026-10-13", "end": "2026-10-14"})
    assert r.status_code == 200
    assert r.json()["plans"][0]["changes"][0]["reason"] == "absent"
    assert admin.get("/api/v1/absences").json()[0]["start"].startswith("2026-11-19")  # не изменилось
    r = admin.patch(f"/api/v1/absences/{aid}", json={"start": "2026-10-13", "end": "2026-10-14", "note": "перенос"})
    assert r.json()["absence"]["note"] == "перенос"
    assert names(get_shifts(admin))[1] == ["Вика"]


def test_auto_migration_adds_column(tmp_path):
    import sqlite3

    from duty.db import init_db

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE shifts (id INTEGER PRIMARY KEY, team_id INTEGER, start DATETIME, \"end\" DATETIME, "
                "deleted BOOLEAN, external_id VARCHAR(512), publish_dirty BOOLEAN, updated_at DATETIME)")
    con.execute("INSERT INTO shifts (id, team_id, deleted, publish_dirty) VALUES (1, 1, 0, 0)")
    con.commit()
    con.close()
    init_db(f"sqlite:///{path}")
    con = sqlite3.connect(path)
    assert con.execute("SELECT manual FROM shifts WHERE id = 1").fetchone() == (0,)
