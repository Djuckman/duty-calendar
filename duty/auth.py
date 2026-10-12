"""Пользователи, пароли и токены.

Роли: `user` — меняет график и отсутствия; `admin` — ещё команды, состав, пользователи.
Бэкенды входа реализуют `AuthBackend`; сейчас есть локальный (пароль в БД),
для AD/LDAP оставлена заготовка `LdapAuthBackend`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ApiToken, User, utcnow

ROLES = ("user", "admin")
_PBKDF2_ITERATIONS = 310_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ITERATIONS).hex()
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
    return hmac.compare_digest(calc, digest)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_token(
    session: Session, user: User, kind: str, name: str = "", ttl: timedelta | None = None
) -> tuple[ApiToken, str]:
    raw = ("dts_" if kind == "session" else "dtp_") + secrets.token_urlsafe(32)
    tok = ApiToken(
        user_id=user.id,
        token_hash=hash_token(raw),
        kind=kind,
        name=name,
        expires_at=(utcnow() + ttl) if ttl else None,
    )
    session.add(tok)
    session.flush()
    return tok, raw


def resolve_token(session: Session, raw: str | None, now: datetime | None = None) -> User | None:
    if not raw:
        return None
    now = now or utcnow()
    tok = session.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(raw)))
    if tok is None or (tok.expires_at is not None and tok.expires_at <= now):
        return None
    user = tok.user
    if not user.active:
        return None
    if tok.last_used_at is None or (now - tok.last_used_at) > timedelta(minutes=5):
        tok.last_used_at = now
    return user


# ---------------------------------------------------------------- бэкенды входа


class AuthBackend(ABC):
    name: str

    @abstractmethod
    def authenticate(self, session: Session, username: str, password: str) -> User | None: ...


class LocalAuthBackend(AuthBackend):
    name = "local"

    def authenticate(self, session, username, password):
        user = session.scalar(select(User).where(User.username == username, User.auth_source == "local"))
        if user and user.active and verify_password(password, user.password_hash):
            return user
        return None


class LdapAuthBackend(AuthBackend):
    """Заготовка входа через AD/LDAP (ldap3): bind пользователем, проверка членства в группах
    `duty-users` → role=user, `duty-admins` → role=admin, создание/обновление User с auth_source='ldap'."""

    name = "ldap"

    def authenticate(self, session, username, password):
        raise NotImplementedError("Вход через LDAP ещё не реализован")


def authenticate(session: Session, username: str, password: str, backends: list[AuthBackend] | None = None):
    for backend in backends or [LocalAuthBackend()]:
        user = backend.authenticate(session, username, password)
        if user is not None:
            return user
    return None


def ensure_admin(session: Session, username: str, password: str) -> None:
    """Создать первого админа, если пользователей ещё нет."""
    if not password:
        return
    if session.scalar(select(User.id).limit(1)) is not None:
        return
    session.add(User(username=username, display_name="Администратор", password_hash=hash_password(password), role="admin"))
