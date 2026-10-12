import asyncio
import json

from fastmcp import Client
from sqlalchemy import select

from duty.db import session_scope
from duty.mcp_server import build_mcp
from duty.models import User


def _admin():
    with session_scope() as s:
        return s.scalar(select(User).where(User.username == "admin"))


def _data(result):
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def test_mcp_tools(team, service):
    mcp = build_mcp(service, resolve_user=_admin)

    async def scenario():
        async with Client(mcp) as c:
            tools = {t.name for t in await c.list_tools()}
            assert {"who_is_on_call", "get_schedule", "add_absence", "assign_shift", "swap_shifts", "replan"} <= tools

            sched = _data(await c.call_tool("get_schedule", {"team": "sec", "start": "2026-10-05", "end": "2026-10-25"}))
            assert [s["seats"][0]["person"]["name"] for s in sched] == ["Аня", "Боря", "Вика"]

            dry = _data(await c.call_tool("add_absence", {"email": "a@x.ru", "start": "2026-10-06",
                                                          "end": "2026-10-07", "dry_run": True}))
            assert dry["absence"] is None
            assert dry["plans"][0]["changes"][0]["new"]["name"] == "Боря"

            plan = _data(await c.call_tool("replan", {"team": "sec", "mode": "rebalance"}))
            assert plan["dry_run"] is True

            batch = _data(await c.call_tool("plan_batch", {"operations": [
                {"op": "set_period", "team": "sec", "start": "2026-10-20", "end": "2026-10-22", "emails": ["a@x.ru"]},
            ]}))
            assert batch["dry_run"] is True and batch["changes"]
            per = _data(await c.call_tool("set_period", {"team": "sec", "start": "2026-10-20", "end": "2026-10-22"}))
            assert per["dry_run"] is True and per["people"] == []

            err = await c.call_tool("assign_shift", {"shift_id": 99999, "email": "a@x.ru"}, raise_on_error=False)
            assert err.is_error

    asyncio.run(scenario())


def test_mcp_without_user_is_denied(team, service):
    mcp = build_mcp(service, resolve_user=lambda: None)

    async def scenario():
        async with Client(mcp) as c:
            res = await c.call_tool("list_teams", {}, raise_on_error=False)
            assert res.is_error

    asyncio.run(scenario())
