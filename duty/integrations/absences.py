"""Внешние источники отсутствий (отпусков).

Сейчас отсутствия вводятся вручную (API / MCP / веб) и хранятся с source='manual'.
Чтобы подключить внешний источник (Outlook free/busy, HR-система, 1С, Excel), нужно
реализовать `AbsenceSource` и зарегистрировать его в `get_absence_sources()`.
При синхронизации записи этого источника в окне полностью заменяются свежими данными,
ручные записи не трогаются.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ExternalAbsence:
    email: str
    start: datetime  # aware
    end: datetime  # aware
    kind: str = "vacation"  # vacation | sick | dayoff | other
    note: str = ""


class AbsenceSource(ABC):
    name: str  # попадёт в Absence.source

    @abstractmethod
    def fetch(self, emails: list[str], start: datetime, end: datetime) -> list[ExternalAbsence]:
        """Все отсутствия указанных людей, пересекающиеся с [start, end)."""


def get_absence_sources(names: list[str]) -> list[AbsenceSource]:
    """Фабрика по списку имён из настроек. Пока внешних источников нет."""
    registry: dict[str, type[AbsenceSource]] = {}
    # from .exchange import ExchangeFreeBusySource
    # registry[ExchangeFreeBusySource.name] = ExchangeFreeBusySource
    sources = []
    for n in names:
        if n not in registry:
            raise ValueError(f"неизвестный источник отсутствий {n!r}")
        sources.append(registry[n]())
    return sources
