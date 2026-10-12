"""Публикация смен во внешний календарь (Exchange и т.п.).

Сервис помечает смену `publish_dirty=True` при любом изменении (создание, смена дежурного,
удаление). Фоновая задача отдаёт такие смены в `CalendarPublisher`, сохраняет возвращённый
`external_id` и снимает флаг. Удалённые смены (deleted=True) передаются в `unpublish`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishTeam:
    slug: str
    name: str
    external_calendar: str | None  # напр. имя календаря в ящике Exchange
    timezone: str


@dataclass(frozen=True)
class PublishShift:
    shift_id: int
    start: datetime
    end: datetime
    person_email: str | None  # None = смена не назначена
    person_name: str | None
    external_id: str | None  # id ранее созданного события
    url: str  # ссылка на смену в веб-интерфейсе


class CalendarPublisher(ABC):
    name: str

    @abstractmethod
    def publish(self, team: PublishTeam, shift: PublishShift) -> str | None:
        """Создать или обновить событие. Вернуть id события во внешней системе."""

    @abstractmethod
    def unpublish(self, team: PublishTeam, external_id: str) -> None:
        """Удалить/отменить событие."""


class NullPublisher(CalendarPublisher):
    """Ничего не публикует (по умолчанию)."""

    name = "none"

    def publish(self, team, shift):
        return shift.external_id

    def unpublish(self, team, external_id):
        return None


class LogPublisher(CalendarPublisher):
    """Пишет в лог, что было бы опубликовано. Удобно для отладки."""

    name = "log"

    def publish(self, team, shift):
        log.info("publish %s shift=%s %s..%s -> %s", team.slug, shift.shift_id, shift.start, shift.end, shift.person_email)
        return shift.external_id or f"log-{shift.shift_id}"

    def unpublish(self, team, external_id):
        log.info("unpublish %s %s", team.slug, external_id)


def get_publisher(name: str) -> CalendarPublisher:
    registry: dict[str, type[CalendarPublisher]] = {NullPublisher.name: NullPublisher, LogPublisher.name: LogPublisher}
    # from .exchange import ExchangePublisher
    # registry[ExchangePublisher.name] = ExchangePublisher
    try:
        return registry[name]()
    except KeyError:
        raise ValueError(f"неизвестный публикатор календаря {name!r}: {', '.join(registry)}") from None
