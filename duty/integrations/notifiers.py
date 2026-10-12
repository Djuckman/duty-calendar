"""Уведомления в мессенджер. Сейчас: Mattermost (incoming webhook)."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

log = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send(self, webhook: str | None, text: str, channel: str | None = None) -> None: ...


class NullNotifier(Notifier):
    def send(self, webhook, text, channel=None):
        return None


class RecordingNotifier(Notifier):
    """Для тестов: копит сообщения."""

    def __init__(self):
        self.messages: list[tuple[str | None, str, str | None]] = []

    def send(self, webhook, text, channel=None):
        self.messages.append((webhook, text, channel))


class MattermostNotifier(Notifier):
    def __init__(self, default_webhook: str = "", username: str = "Дежурства", icon_url: str = "", timeout: float = 10):
        self.default_webhook = default_webhook
        self.username = username
        self.icon_url = icon_url
        self.client = httpx.Client(timeout=timeout)

    def send(self, webhook, text, channel=None):
        url = webhook or self.default_webhook
        if not url:
            return
        payload: dict = {"text": text, "username": self.username}
        if self.icon_url:
            payload["icon_url"] = self.icon_url
        if channel:
            payload["channel"] = channel
        try:
            self.client.post(url, json=payload).raise_for_status()
        except httpx.HTTPError as exc:  # уведомления не должны ломать планирование
            log.warning("mattermost send failed: %s", exc)
