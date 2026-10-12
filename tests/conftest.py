from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from duty.config import Settings
from duty.integrations.notifiers import RecordingNotifier
from duty.main import build_service, create_app


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 1, 9, 0, tzinfo=UTC))


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
def settings():
    return Settings(
        database_url="sqlite://",
        admin_username="admin",
        admin_password="admin-pass-123",
        scheduler_enabled=False,
        mattermost_default_webhook="http://mm.local/hooks/x",
        _env_file=None,
    )


@pytest.fixture
def service(settings, notifier, clock):
    return build_service(settings, notifier=notifier, clock=clock)


@pytest.fixture
def app(settings, service):
    return create_app(settings, service=service, start_scheduler=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(client):
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-pass-123"})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture
def team(admin, service):
    with service.write() as op:
        service.sync_holidays(op, [2026, 2027])
    r = admin.post("/api/v1/teams", json={
        "slug": "sec", "name": "SEC", "shift_length_days": 7, "handoff_time": "10:00",
        "anchor_date": "2026-10-05", "horizon_days": 42,
    })
    assert r.status_code == 201, r.text
    for email, name, mm in [("a@x.ru", "Аня", "anya"), ("b@x.ru", "Боря", "borya"), ("c@x.ru", "Вика", "vika")]:
        r = admin.post("/api/v1/teams/sec/members", json={"email": email, "name": name, "mattermost_username": mm})
        assert r.status_code == 201, r.text
    admin.post("/api/v1/teams/sec/plan", params={"mode": "rebalance"})
    return "sec"
