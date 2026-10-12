"""Заготовка интеграции с Microsoft Exchange (on-prem) через EWS. НЕ РЕАЛИЗОВАНО.

План доработки (библиотека exchangelib, `pip install exchangelib`):

Настройки (добавить в config.Settings):
    exchange_server="mail.company.ru", exchange_username="DOMAIN\\\\duty-bot",
    exchange_password=..., exchange_email="duty-bot@company.ru", exchange_auth_type="NTLM",
    exchange_free_busy_status="Free"  # чтобы недельная смена не блокировала календарь дежурного

ExchangePublisher(CalendarPublisher):
    account = Account(exchange_email, config=Configuration(server=..., credentials=Credentials(...),
                      auth_type=NTLM), autodiscover=False, access_type=DELEGATE)
    folder  = календарь с именем team.external_calendar (найти через account.msg_folder_root.walk()
              среди папок Calendar; если нет — Calendar(parent=account.calendar, name=...).save())
    publish:
        если shift.external_id: item = account.fetch(ids=[(external_id, None)]), иначе CalendarItem(folder=folder)
        item.subject = f"Дежурство {team.name}: {shift.person_name or 'НЕ НАЗНАЧЕН'}"
        item.start/end = EWSDateTime.from_datetime(...)
        item.required_attendees = [shift.person_email] if shift.person_email else []
        item.legacy_free_busy_status = exchange_free_busy_status
        создание: item.save(send_meeting_invitations=SEND_TO_ALL_AND_SAVE_COPY)
        изменение: item.save(update_fields=[...], send_meeting_invitations=SEND_TO_CHANGED_AND_SAVE_COPY)
        вернуть item.id
    unpublish:
        item.delete(send_meeting_cancellations=SEND_TO_ALL_AND_SAVE_COPY)

ExchangeFreeBusySource(AbsenceSource):  # отпуска по статусу «Нет на месте»
    views = account.protocol.get_free_busy_info(
        accounts=[(email, "Required", False) for email in emails], start=..., end=..., requested_view="DetailedMerged")
    для каждого view: события с busy_type == "OOF" -> ExternalAbsence(email, ev.start, ev.end)
    (порядок views совпадает с порядком emails; соседние интервалы склеить)

После реализации зарегистрировать классы в publishers.get_publisher() и absences.get_absence_sources().
"""

from __future__ import annotations

from datetime import datetime

from .absences import AbsenceSource, ExternalAbsence
from .publishers import CalendarPublisher, PublishShift, PublishTeam


class ExchangePublisher(CalendarPublisher):
    name = "exchange"

    def publish(self, team: PublishTeam, shift: PublishShift) -> str | None:
        raise NotImplementedError("Публикация в Exchange ещё не реализована, см. docstring модуля")

    def unpublish(self, team: PublishTeam, external_id: str) -> None:
        raise NotImplementedError("Публикация в Exchange ещё не реализована, см. docstring модуля")


class ExchangeFreeBusySource(AbsenceSource):
    name = "exchange"

    def fetch(self, emails: list[str], start: datetime, end: datetime) -> list[ExternalAbsence]:
        raise NotImplementedError("Чтение отпусков из Exchange ещё не реализовано, см. docstring модуля")
