from datetime import date

import httpx

from duty.integrations.holidays import IsDayOffProvider, LibraryHolidayProvider


def test_library_provider_ru():
    items = {e.day: e for e in LibraryHolidayProvider().fetch(2026, "RU")}
    assert items[date(2026, 11, 4)].name == "День народного единства"
    assert items[date(2026, 1, 7)].kind == "holiday"


def _year_codes(year: int, overrides: dict[date, str]) -> str:
    d, out = date(year, 1, 1), []
    while d.year == year:
        out.append(overrides.get(d, "1" if d.isoweekday() >= 6 else "0"))
        d = date.fromordinal(d.toordinal() + 1)
    return "".join(out)


def test_isdayoff_provider_transfers():
    overrides = {
        date(2026, 11, 4): "1",  # праздник
        date(2026, 11, 3): "2",  # сокращённый
        date(2026, 1, 9): "1",  # перенесённый выходной (пятница)
        date(2026, 11, 7): "0",  # рабочая суббота
    }
    body = _year_codes(2026, overrides)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["year"] == "2026"
        return httpx.Response(200, text=body)

    provider = IsDayOffProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    items = {e.day: e.kind for e in provider.fetch(2026, "RU")}
    assert items[date(2026, 11, 4)] == "holiday"
    assert items[date(2026, 11, 3)] == "short"
    assert items[date(2026, 1, 9)] == "dayoff"
    assert items[date(2026, 11, 7)] == "workday"
    assert date(2026, 11, 8) not in items  # обычное воскресенье не храним
