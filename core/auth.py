"""
Ограничение доступа к приложению и журналирование пользователя запуска
(ТЗ §11).

Учетные записи задаются переменной окружения AUDIT_USERS в формате
«логин:хэш,логин2:хэш2». Хэш генерируется консольной утилитой:

    python -m core.auth hash пароль

Если AUDIT_USERS не задан или пуст, аутентификация отключена — приложение
работает без входа (ручной режим загрузки файлов остается как есть).
Пароли в открытом виде не хранятся: только PBKDF2-HMAC-SHA256 в формате
«итераций$соль$хэш».
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys
import threading
import time
from collections import defaultdict

from core import db

AUDIT_USERS_ENV: str = "AUDIT_USERS"

# Параметры PBKDF2 по умолчанию (новые хэши); при проверке используется
# число итераций из самого хэша, поэтому старые записи остаются читаемыми.
_PBKDF2_ITERATIONS = 200_000
_SALT_BYTES: int = 16

# Защита от перебора пароля: после N неудачных попыток вход блокируется
MAX_LOGIN_ATTEMPTS: int = 5
LOCKOUT_SECONDS: int = 300
MIN_PASSWORD_LENGTH: int = 8

_LOGIN_FAILURES: dict[str, list[float]] = defaultdict(list)
_FAILURES_LOCK = threading.Lock()

# Роли (ТЗ §11)
ROLE_ADMIN: str = "admin"
ROLE_MANAGER: str = "manager"
ROLE_ACCOUNTANT: str = "accountant"

_VALID_ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_MANAGER, ROLE_ACCOUNTANT)

# Матрица прав (ТЗ §11) — единый источник правды, производный от роли:
#   admin       — все флаги True;
#   manager     — видит все базы, правит нормы и сотрудников;
#                 НЕ управляет пользователями и НЕ редактирует базы;
#   accountant  — только свои операции и клиенты (row-level фильтр в отчёте).
PERMISSION_MATRIX: dict[str, dict[str, bool]] = {
    ROLE_ADMIN: {
        "can_manage_users": True,
        "can_manage_norms": True,
        "can_manage_employees": True,
        "can_see_all_bases": True,
        "can_edit_bases": True,
    },
    ROLE_MANAGER: {
        "can_manage_users": False,
        "can_manage_norms": True,
        "can_manage_employees": True,
        "can_see_all_bases": True,
        "can_edit_bases": False,
    },
    ROLE_ACCOUNTANT: {
        "can_manage_users": False,
        "can_manage_norms": False,
        "can_manage_employees": False,
        "can_see_all_bases": False,
        "can_edit_bases": False,
    },
}

DEFAULT_FLAGS: dict[str, bool] = dict(PERMISSION_MATRIX[ROLE_ACCOUNTANT])


def _normalize_url(url: object) -> str:
    """
    Каноническая нормализация URL базы для сопоставления прав доступа.

    Поведение: схема и host приводятся к нижнему регистру; завершающие слэши
    в пути обрезаются; удаляется завершающий сегмент пути `/en` (1С:Фреш
    вставляет его в адресную строку при английском интерфейсе, но OData-данные
    по такому адресу не отдаёт — это не часть адреса базы). Для URL без пути
    результат получает один завершающий слэш (например,
    `https://a.example` -> `https://a.example/`).
    Пустая строка/None -> "".
    """

    s = str(url or "").strip()
    if not s:
        return ""
    s = s.rstrip("/")
    # Приводим схему и хост к нижнему регистру (путь оставляем как есть)
    if "://" in s:
        scheme, rest = s.split("://", 1)
        host, _, tail = rest.partition("/")
        if tail.endswith("/en"):
            tail = tail[:-3]
        elif tail == "en":
            tail = ""
        return f"{scheme.lower()}://{host.lower()}/{tail}"
    return s.lower()


def hash_password(password: str) -> str:
    """
    Хэш пароля в формате «итерации$соль$хэш» (все компоненты в hex)
    """

    salt_hex = secrets.token_hex(_SALT_BYTES)
    digest_hex = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        _PBKDF2_ITERATIONS,
    ).hex()
    return f"{_PBKDF2_ITERATIONS}${salt_hex}${digest_hex}"


def parse_users(raw: str | None) -> dict[str, str]:
    """
    Разбирает строку AUDIT_USERS в словарь {логин: хэш}
    """

    users: dict[str, str] = {}
    if not raw:
        return users

    # Используем for вместо while для большей читаемости и скорости
    for fragment in raw.split(","):
        login, sep, stored = fragment.partition(":")
        if sep and login.strip() and stored.strip():
            users[login.strip().lower()] = stored.strip()
    return users


def _verify_stored_hash(stored: str | None, password: str) -> bool:
    """
    Проверяет пароль по сохранённому хэшу формата «итерации$соль$хэш».
    """

    if not stored:
        hashlib.pbkdf2_hmac("sha256", b"login", b"salt", _PBKDF2_ITERATIONS)
        return False

    try:
        iterations_text, salt_text, digest_text = stored.split("$", 2)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_text),
            max(1, int(iterations_text)),
            dklen=len(bytes.fromhex(digest_text)),
        )
    except ValueError:
        return False

    return hmac.compare_digest(actual, bytes.fromhex(digest_text))


def validate_password(password: str) -> str:
    """
    Проверяет стойкость пароля. Возвращает текст ошибки или "" если всё ок
    """

    if not password:
        return "Введите пароль."
    if len(password) < MIN_PASSWORD_LENGTH:
        return (
            f"Пароль должен быть не короче "
            f"{MIN_PASSWORD_LENGTH} символов."
        )
    return ""


def register_failure(login: str) -> int:
    """
    Фиксирует неудачную попытку входа. Возвращает число попыток подряд
    """

    key = str(login or "").strip().lower()
    now = time.time()
    with _FAILURES_LOCK:
        stale = [t for t in _LOGIN_FAILURES[key] if now - t < LOCKOUT_SECONDS]
        stale.append(now)
        _LOGIN_FAILURES[key] = stale
        return len(stale)


def clear_failures(login: str) -> None:
    """Сбрасывает счётчик неудачных попыток (после успешного входа)"""

    key = str(login or "").strip().lower()
    with _FAILURES_LOCK:
        _LOGIN_FAILURES.pop(key, None)


def login_locked(login: str) -> bool:
    """
    Заблокирован ли вход: не меньше MAX_LOGIN_ATTEMPTS неудачных попыток
    за последние LOCKOUT_SECONDS секунд
    """

    key = str(login or "").strip().lower()
    now = time.time()
    with _FAILURES_LOCK:
        recent = [t for t in _LOGIN_FAILURES.get(key, []) if now - t < LOCKOUT_SECONDS]
        _LOGIN_FAILURES[key] = recent
        return len(recent) >= MAX_LOGIN_ATTEMPTS


def lockout_seconds_left(login: str) -> int:
    """Сколько секунд осталось до снятия блокировки"""

    key = str(login or "").strip().lower()
    now = time.time()
    with _FAILURES_LOCK:
        recent = [t for t in _LOGIN_FAILURES.get(key, []) if now - t < LOCKOUT_SECONDS]
        _LOGIN_FAILURES[key] = recent
        if len(recent) < MAX_LOGIN_ATTEMPTS:
            return 0
        return int(max(0, LOCKOUT_SECONDS - (now - recent[0]))) or 1


def lockout_message(login: str) -> str:
    """Текст ошибки для заблокированного входа"""

    minutes = max(1, round(lockout_seconds_left(login) / 60))
    return (
        "Превышено число попыток. Вход заблокирован "
        f"на {minutes} мин."
    )


def failed_login_error(login: str) -> str:
    """
    Сообщение о неудачной попытке входа и фиксация попытки
    """

    attempts = register_failure(login)
    left = max(0, MAX_LOGIN_ATTEMPTS - attempts)
    if left == 0:
        return lockout_message(login)
    return f"Неверный логин или пароль. Осталось попыток: {left}."


def verify(login: str, password: str) -> bool:
    """
    Проверяет пару логин/пароль

    Источник истины — таблица `users` (роли + доступ к базам). Для обратной
    совместимости, если пользователя нет в БД, выполняется фолбэк на
    переменную окружения AUDIT_USERS (логин:хэш).
    """

    login = str(login).strip().lower()

    user = db.get_user(login)
    if user is not None:
        if not user.get("active", True):
            return False
        return _verify_stored_hash(user.get("password_hash"), password)

    # Фолбэк на env (старые развёртывания без users.json/БД-пользователей)
    stored = parse_users(os.environ.get(AUDIT_USERS_ENV)).get(login)
    return _verify_stored_hash(stored, password)


def auth_enabled() -> bool:
    """
    Аутентификация включена, когда есть пользователи в БД или AUDIT_USERS.
    """

    try:
        if db.list_users():
            return True
    except Exception:
        pass
    return bool(parse_users(os.environ.get(AUDIT_USERS_ENV)))


def get_user(login: str) -> dict | None:
    """
    Пользователь из БД (или запись, построенная из AUDIT_USERS как fallback)
    """

    login_norm = str(login).strip().lower()
    user = db.get_user(login_norm)
    if user is not None:
        return user
    # Fallback для env-пользователей: роль accountant с пустым списком баз —
    # не имеет доступа ни к одной базе (кроме собственных локальных прогонов)
    env_hash = parse_users(os.environ.get(AUDIT_USERS_ENV)).get(login_norm)
    if env_hash:
        return {
            "login": login_norm,
            "role": ROLE_ACCOUNTANT,
            "password_hash": env_hash,
            "allowed_urls": [],
            "active": True,
        }
    return None


def user_role(login: str) -> str:
    """
    Роль пользователя (admin|manager|accountant); по умолчанию — accountant
    """

    user = get_user(login)
    if not user:
        return ROLE_ACCOUNTANT
    role = str(user.get("role") or ROLE_ACCOUNTANT).strip().lower()
    return role if role in _VALID_ROLES else ROLE_ACCOUNTANT


def permission_flags_from_role(role: str) -> dict[str, bool]:
    """Флаги прав для роли в соответствии с PERMISSION_MATRIX (ТЗ §11)."""

    role = str(role or "").strip().lower()
    return dict(PERMISSION_MATRIX.get(role, DEFAULT_FLAGS))


def permission_flags(login: str) -> dict[str, bool]:
    """Флаги прав текущей роли пользователя (для activity routing в UI)."""

    return permission_flags_from_role(user_role(login))


def user_allowed_urls(login: str) -> list[str]:
    """
    Список URL баз, доступных пользователю (для admin и manager — [] = все)
    """

    user = get_user(login)
    if not user:
        return []
    urls = user.get("allowed_urls") or []
    return [_normalize_url(u) for u in urls if _normalize_url(u)]


def user_can_access(login: str, url: object) -> bool:
    """
    Может ли пользователь работать с базой по URL

    - admin и manager (могут видеть все базы) — True.
    - accountant — True, только если нормализованный URL в его списке
    """

    user = get_user(login)
    if not user:
        return False
    if user_role(login) in (ROLE_ADMIN, ROLE_MANAGER):
        return True
    norm = _normalize_url(url)
    if not norm:
        return False
    return norm in user_allowed_urls(login)


def main(argv: list[str]) -> int:
    """
    Консольная утилита: python -m core.auth hash <пароль>
    """

    if len(argv) == 2 and argv[0] == "hash":
        print(hash_password(argv[1]))
        return 0
    print("Использование: python -m core.auth hash <пароль>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
