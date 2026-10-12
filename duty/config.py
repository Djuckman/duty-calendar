"""Настройки сервиса. Читаются из переменных окружения с префиксом DUTY_ или из файла .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DUTY_", env_file=".env", extra="ignore")

    # --- Общее ---
    database_url: str = "sqlite:///./duty.db"
    timezone: str = "Europe/Moscow"  # пояс по умолчанию: для дат отпусков без времени и новых команд
    public_url: str = "http://localhost:8000"  # для ссылок в сообщениях

    # --- Доступ ---
    admin_username: str = "admin"  # первый админ создаётся при старте, если пользователей ещё нет
    admin_password: str = ""  # пусто = не создавать
    session_ttl_hours: int = 12
    cookie_secure: bool = False  # True, если сервис за HTTPS

    # --- Фоновые задачи ---
    scheduler_enabled: bool = True
    plan_interval_minutes: int = 30  # продление горизонта, синк отпусков, публикация в календарь
    tick_interval_minutes: int = 5  # объявления о начале смены

    # --- Праздники ---
    holiday_provider: str = "library"  # library (офлайн, пакет holidays) | isdayoff (isdayoff.ru, с переносами)
    holiday_country: str = "RU"

    # --- Публикация смен во внешний календарь (Exchange и т.п.) ---
    calendar_publisher: str = "none"  # none | log   (exchange — будет добавлен)

    # --- Mattermost ---
    mattermost_default_webhook: str = ""  # если у команды не задан свой webhook
    mattermost_username: str = "Дежурства"
    mattermost_icon_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
