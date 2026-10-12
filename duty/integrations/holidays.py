"""Производственный календарь (праздники РФ).

Интерфейс `HolidayProvider` и две реализации:
  * LibraryHolidayProvider — офлайн, пакет `holidays`. Знает официальные праздники,
    но НЕ знает перенесённые выходные/рабочие дни (их утверждают постановлением каждый год).
  * IsDayOffProvider — isdayoff.ru, учитывает переносы и сокращённые дни. Нужен доступ в интернет.
Записи с source='manual' (добавленные через API) имеют приоритет над провайдером.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, timedelta

import holidays as holidays_lib
import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HolidayEntry:
    day: date
    name: str
    kind: str  # holiday | dayoff | workday | short


class HolidayProvider(ABC):
    name: str

    @abstractmethod
    def fetch(self, year: int, country: str) -> list[HolidayEntry]: ...


class LibraryHolidayProvider(HolidayProvider):
    name = "library"

    def fetch(self, year: int, country: str) -> list[HolidayEntry]:
        lang = "ru" if country.upper() == "RU" else None
        data = holidays_lib.country_holidays(country.upper(), years=year, language=lang)
        return [HolidayEntry(d, n, "holiday") for d, n in sorted(data.items())]


class IsDayOffProvider(HolidayProvider):
    """https://isdayoff.ru/api/getdata?year=YYYY&cc=ru&pre=1 → строка кодов по дням года:
    0 — рабочий, 1 — нерабочий, 2 — сокращённый предпраздничный, 4 — рабочий (особый режим)."""

    name = "isdayoff"
    url = "https://isdayoff.ru/api/getdata"

    def __init__(self, client: httpx.Client | None = None, timeout: float = 10.0):
        self.client = client or httpx.Client(timeout=timeout)

    def fetch(self, year: int, country: str) -> list[HolidayEntry]:
        resp = self.client.get(self.url, params={"year": year, "cc": country.lower(), "pre": 1})
        resp.raise_for_status()
        codes = resp.text.strip()
        names = {e.day: e.name for e in LibraryHolidayProvider().fetch(year, country)}
        result: list[HolidayEntry] = []
        day = date(year, 1, 1)
        for code in codes:
            weekend = day.isoweekday() >= 6
            if day in names and code == "1":
                result.append(HolidayEntry(day, names[day], "holiday"))
            elif code == "1" and not weekend:
                result.append(HolidayEntry(day, "Перенесённый выходной", "dayoff"))
            elif code in ("0", "4") and weekend:
                result.append(HolidayEntry(day, "Рабочий день (перенос)", "workday"))
            elif code == "2":
                result.append(HolidayEntry(day, "Сокращённый предпраздничный день", "short"))
            day += timedelta(days=1)
        return result


def get_holiday_provider(name: str) -> HolidayProvider:
    providers: dict[str, type[HolidayProvider]] = {
        LibraryHolidayProvider.name: LibraryHolidayProvider,
        IsDayOffProvider.name: IsDayOffProvider,
    }
    try:
        return providers[name]()
    except KeyError:
        raise ValueError(f"неизвестный провайдер праздников {name!r}: {', '.join(providers)}") from None
