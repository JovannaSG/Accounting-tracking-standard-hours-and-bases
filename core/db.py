import base64
import sqlite3
import json
import os
import secrets
from datetime import datetime

# Позволяем тестам использовать временный файл через переменную окружения
_DB_PATH = os.environ.get(
    "AUDIT_DB_PATH",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "norm_hours.db"
    )
)

# Путь к конфигу пользователей (роли + доступ к базам)
# По умолчанию — users.json в корне проекта
# Опционально переопределяется переменной окружения AUDIT_USERS_CONFIG
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USERS_CONFIG_PATH = os.environ.get(
    "AUDIT_USERS_CONFIG",
    os.path.join(_PROJECT_ROOT, "users.json")
)

# Шифрование паролей клиентских баз. Ключ — переменная окружения
# AUDIT_DB_SECRET_KEY (base64 от 32 случайных байт). Без ключа пароли
# хранятся как раньше, в открытом виде (обратная совместимость).
_SECRET_KEY_ENV = "AUDIT_DB_SECRET_KEY"
_ENC_PREFIX = "enc:v1:"
_SECRET_CACHE: dict[str, object] = {}


def _get_fernet():
    """
    Возвращает объект Fernet по ключу из окружения или None.
    Ключ кэшируется; библиотека cryptography опциональна — если её нет,
    шифрование отключается (пароль хранится как раньше).
    """

    if "fernet" in _SECRET_CACHE:
        return _SECRET_CACHE["fernet"]

    fernet = None
    raw_key = (os.environ.get(_SECRET_KEY_ENV) or "").strip()
    if raw_key:
        try:
            from cryptography.fernet import Fernet

            fernet = Fernet(raw_key.encode("utf-8"))
        except Exception:
            fernet = None
    _SECRET_CACHE["fernet"] = fernet
    return fernet


def generate_secret_key() -> str:
    """
    Генерирует новый ключ шифрования (base64 от 32 случайных байт).
    """

    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _encrypt_password(password: str) -> str:
    """
    Шифрует пароль, если ключ задан. Без ключа возвращает как есть.
    """

    raw = str(password or "")
    if not raw:
        return raw
    fernet = _get_fernet()
    if fernet is None:
        return raw
    token = fernet.encrypt(raw.encode("utf-8")).decode("utf-8")
    return _ENC_PREFIX + token


def _decrypt_password(stored: str) -> str:
    """
    Расшифровывает пароль. Записи в открытом виде (legacy, без префикса)
    и нерасшифрованные токены (если ключ пропал) возвращаются как есть.
    """

    raw = str(stored or "")
    if not raw.startswith(_ENC_PREFIX):
        return raw
    fernet = _get_fernet()
    if fernet is None:
        return ""
    token = raw[len(_ENC_PREFIX):]
    try:
        return fernet.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


def init_db():
    """
    Создает таблицы, если их нет, и добавляет недостающие колонки.

    Таблица `users` хранит роли и доступ к базам (ТЗ §11). Если таблица
    пуста — засеивается из конфига users.json.

    Таблица `bases` — клиентские базы 1С:Фреш (одна база = один клиент).
    Дополнительные поля:
      - sno   — система налогообложения клиента (заполняется вручную,
                т.к. регистр СНО в OData-составе обычно не публикуется);
      - group — группа клиентов для отборов по группе.

    Таблица `norms` — регистр норм трудозатрат (ТЗ §4.1): ключ вида документа,
    категория, наименование, норма в минутах и нормочасах, коэффициент
    сложности, привязка к СНО (NULL = универсальная норма), срок действия.

    Таблица `employees` — соответствие «Сотрудник аутсорсера → Пользователь 1С»
    (ТЗ §3.3): ключ — полное ФИО сотрудника, `user_1c` — необязательный алиас
    для ручной привязки технических имён из 1С («Е_Кирищёнок» и т.п.).
    """

    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    # WAL: многие читатели одновременно с одним писателем
    # (несколько сессий Streamlit / процессов)
    # synchronous=NORMAL безопасен в WAL-режиме и
    # сильно снижает оверхед по диску.
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            login TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            allowed_urls TEXT,
            created_at TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    cursor.execute("PRAGMA table_info(users)")
    existing_users = {row[1] for row in cursor.fetchall()}
    if "employee_full_name" not in existing_users:
        cursor.execute("ALTER TABLE users ADD COLUMN employee_full_name TEXT")
    # Базы клиентов (данные для OData). Одна база = один клиент.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            login TEXT,
            password TEXT,
            sno TEXT,
            "group" TEXT,
            created_at TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    cursor.execute("PRAGMA table_info(bases)")
    existing = {row[1] for row in cursor.fetchall()}
    if "sno" not in existing:
        cursor.execute("ALTER TABLE bases ADD COLUMN sno TEXT")
    if "group" not in existing:
        cursor.execute('ALTER TABLE bases ADD COLUMN "group" TEXT')

    # Регистр норм трудозатрат (ТЗ §4.1)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS norms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_type TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            entity TEXT NOT NULL,
            unit TEXT,
            norm_min REAL NOT NULL,
            norm_hours REAL NOT NULL,
            coeff REAL NOT NULL DEFAULT 1.00,
            sno TEXT,
            date_from TEXT,
            date_to TEXT,
            comment TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            UNIQUE (doc_type)
        )
    """)

    # Соответствие «Сотрудник аутсорсера -> Пользователь 1С» (ТЗ §3.3)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL UNIQUE,
            user_1c TEXT,
            role TEXT,
            hours_per_month REAL NOT NULL DEFAULT 130.0,
            comment TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    _migrate_employees_legacy_schema(cursor)

    conn.commit()

    # Сид первого набора пользователей, если БД пуста
    cursor.execute("SELECT COUNT(*) FROM users")
    count = cursor.fetchone()[0]
    if count == 0:
        _seed_users_from_config(cursor)
        conn.commit()

    conn.close()


def load_users_config() -> dict:
    """
    Читает users.json в {login: {...или строка хэша}}. Если файла нет — {}.

    Для совместимости, когда конфиг называется user.json (опечатка в одну
    букву), используется он как фолбэк.
    """

    candidates = [USERS_CONFIG_PATH]
    if os.path.basename(USERS_CONFIG_PATH) != "user.json":
        candidates.append(os.path.join(os.path.dirname(USERS_CONFIG_PATH), "user.json"))

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        return data if isinstance(data, dict) else {}
    return {}


def _seed_users_from_config(cursor) -> None:
    """
    Заполняет пустую таблицу users из users.json
    """

    config = load_users_config()

    for login, spec in config.items():
        if not isinstance(spec, dict) or not login.strip():
            continue
        role = str(spec.get("role") or "accountant").strip().lower()

        pwd_hash = str(
            spec.get("password_hash")
            or spec.get("password")
            or ""
        ).strip()
        if not pwd_hash:
            continue

        allowed = spec.get("allowed_urls") or []
        insert_user(
            cursor,
            login=login.strip().lower(),
            role=role,
            password_hash=pwd_hash,
            allowed_urls=allowed if isinstance(allowed, list) else [],
            employee_full_name=spec.get("employee_full_name"),
        )


def insert_user(
    cursor,
    login: str,
    role: str,
    password_hash: str,
    allowed_urls: list,
    employee_full_name: str | None = None,
) -> None:
    """
    Вставляет пользователя. SQLite поддерживает UPSERT начиная с 3.24;
    для совместимости используем INSERT OR REPLACE
    """

    cursor.execute(
        "INSERT OR REPLACE INTO users "
        "(login, role, password_hash, allowed_urls, created_at, active, "
        "employee_full_name) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (
            login.strip().lower(),
            role,
            password_hash,
            json.dumps(list(allowed_urls or []), ensure_ascii=False),
            datetime.now().isoformat(timespec="seconds"),
            (employee_full_name or None),
        ),
    )


def upsert_user(
    login: str,
    role: str,
    password_hash: str,
    allowed_urls: list,
    active: bool = True,
    employee_full_name: str | None = None,
) -> None:
    """
    Сохраняет/обновляет пользователя в БД (используется при загрузке из конфига)
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO users (login, role, password_hash, allowed_urls, created_at, "
        "active, employee_full_name) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(login) DO UPDATE SET "
        "role=excluded.role, password_hash=excluded.password_hash, "
        "allowed_urls=excluded.allowed_urls, active=excluded.active, "
        "employee_full_name=excluded.employee_full_name",
        (
            login.strip().lower(),
            role,
            password_hash,
            json.dumps(list(allowed_urls or []), ensure_ascii=False),
            datetime.now().isoformat(timespec="seconds"),
            int(bool(active)),
            (employee_full_name or None),
        ),
    )
    conn.commit()
    conn.close()


def get_user(login: str) -> dict | None:
    """
    Возвращает пользователя по логину или None
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT login, role, password_hash, allowed_urls, active, "
        "employee_full_name "
        "FROM users WHERE login = ?",
        (str(login).strip().lower(),),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    urls = []
    try:
        parsed = json.loads(row[3]) if row[3] else []
        if isinstance(parsed, list):
            urls = parsed
    except (TypeError, ValueError):
        urls = []
    return {
        "login": row[0],
        "role": row[1],
        "password_hash": row[2],
        "allowed_urls": urls,
        "active": bool(row[4]),
        "employee_full_name": row[5],
    }


def list_users() -> list[dict]:
    """
    Возвращает список всех пользователей (без конфиденциальной части)
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT login, role, allowed_urls, active, employee_full_name "
        "FROM users ORDER BY login")
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "login": r[0],
            "role": r[1],
            "allowed_urls": _parse_urls(r[2]),
            "active": bool(r[3]),
            "employee_full_name": r[4],
        }
        for r in rows
    ]


def delete_user(login: str) -> bool:
    """
    Удаляет пользователя из БД. Возвращает True, если запись существовала.
    """

    login = str(login).strip().lower()
    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE login = ?", (login,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


# =============================== БАЗЫ КЛИЕНТОВ ==============================
def list_bases(active_only: bool = False) -> list[dict]:
    """
    Базы клиентов (таблица bases). Пароли возвращаются: они нужны
    для подключения к OData при сборе данных.
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    sql: str = (
        "SELECT id, name, url, login, password, sno, \"group\", "
        "created_at, active FROM bases"
    )
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY name COLLATE NOCASE"
    cursor.execute(sql)
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "name": r[1],
            "url": r[2],
            "login": r[3],
            "password": _decrypt_password(r[4]),
            "sno": r[5],
            "group": r[6],
            "created_at": r[7],
            "active": bool(r[8]),
        }
        for r in rows
    ]


def get_base(url: str) -> dict | None:
    """
    Возвращает базу по (нормализованному) URL или None
    """

    from core.auth import _normalize_url

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, url, login, password, sno, \"group\", "
        "created_at, active "
        "FROM bases WHERE url = ?",
        (_normalize_url(url),),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "url": row[2],
        "login": row[3],
        "password": _decrypt_password(row[4]),
        "sno": row[5],
        "group": row[6],
        "created_at": row[7],
        "active": bool(row[8]),
    }


def insert_base(
    name: str,
    url: str,
    login: str,
    password: str,
    sno: str | None = None,
    group: str | None = None,
    active: bool = True,
) -> dict | None:
    """
    Добавляет базу. URL нормализуется (_normalize_url) и должен быть уникальным.
    Возвращает None при совпадении URL с существующей записью.
    """

    from core.auth import _normalize_url

    norm = _normalize_url(url)
    if get_base(norm) is not None:
        return None
    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO bases (name, url, login, password, sno, \"group\", "
        "created_at, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(name or "").strip(),
            norm,
            str(login or "").strip(),
            _encrypt_password(password),
            (str(sno).strip() if sno else None),
            (str(group).strip() if group else None),
            datetime.now().isoformat(timespec="seconds"),
            int(bool(active)),
        ),
    )
    conn.commit()
    base = get_base(norm)
    conn.close()
    return base


def update_base(
    base_id: int,
    name: str | None = None,
    url: str | None = None,
    login: str | None = None,
    password: str | None = None,
    sno: str | None = None,
    group: str | None = None,
    active: bool | None = None,
) -> bool:
    """
    Обновляет поля базы. Пустой password — без изменений пароля.
    Возвращает False, если записи нет или новый URL уже занят.
    """

    from core.auth import _normalize_url

    base = get_base_by_id(base_id)
    if base is None:
        return False

    new_url = base["url"]
    if url is not None:
        new_url = _normalize_url(url)
        if new_url != base["url"] and get_base(new_url) is not None:
            return False

    new_name = base["name"] if name is None else str(name or "").strip()
    new_login = base["login"] if login is None else str(login or "").strip()
    stored_raw = _raw_password_by_id(base_id) or ""
    if password:
        new_pass_enc = _encrypt_password(str(password))
    elif stored_raw.startswith(_ENC_PREFIX):
        # Ключ не задан или токен не читается — сохраняем как есть
        new_pass_enc = stored_raw
    else:
        new_pass_enc = _encrypt_password(base["password"] or "")
    new_sno = base["sno"] if sno is None else (str(sno).strip() if sno else None)
    new_group = base["group"] if group is None else (str(group).strip() if group else None)
    new_active = base["active"] if active is None else bool(active)

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE bases SET name=?, url=?, login=?, password=?, sno=?, "
        "\"group\"=?, active=? WHERE id=?",
        (new_name, new_url, new_login, new_pass_enc, new_sno, new_group,
         int(new_active), base_id),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def _raw_password_by_id(base_id: int) -> str | None:
    """Возвращает пароль из БД как есть (без расшифровки)"""

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("SELECT password FROM bases WHERE id = ?", (base_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def get_base_by_id(base_id: int) -> dict | None:
    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, url, login, password, sno, \"group\", "
        "created_at, active "
        "FROM bases WHERE id = ?",
        (base_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "url": row[2],
        "login": row[3],
        "password": _decrypt_password(row[4]),
        "sno": row[5],
        "group": row[6],
        "created_at": row[7],
        "active": bool(row[8]),
    }


def delete_base(base_id: int) -> bool:
    """
    Удаляет базу из БД. Возвращает True, если запись существовала.
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM bases WHERE id = ?", (base_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def merge_bases(db_bases: list[dict], file_entries: list[dict]) -> list[dict]:
    """
    Объединяет базы из БД (таблица bases) и из client_databases.json,
    дедуплицируя по нормализованному URL. База из БД приоритетнее файла.
    Возвращает список словарей с ключами {name, url, login, password}.
    """

    from core.auth import _normalize_url

    merged: list[dict] = []
    seen: set[str] = set()
    for entry in db_bases or []:
        url = _normalize_url(entry.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        merged.append({
            "name": str(entry.get("name") or "").strip(),
            "url": url,
            "login": str(entry.get("login") or "").strip(),
            "password": str(entry.get("password") or ""),
        })
    for entry in file_entries or []:
        url = _normalize_url(entry.get("url") or "")
        if url in seen:
            continue
        seen.add(url)
        merged.append({
            "name": str(entry.get("name") or "").strip(),
            "url": url,
            "login": str(entry.get("login") or "").strip(),
            "password": str(entry.get("password") or ""),
        })
    return merged


# =============================== НОРМЫ =====================================

def list_norms(category: str | None = None, active_only: bool = True) -> list[dict]:
    """
    Возвращает нормы трудозатрат. При category — только этой категории.
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    sql = "SELECT id, doc_type, category, title, entity, unit, norm_min, " \
          "norm_hours, coeff, sno, date_from, date_to, comment, sort_order, active " \
          "FROM norms"
    cond: list[str] = []
    params: list = []
    if category:
        cond.append("category = ?")
        params.append(category)
    if active_only:
        cond.append("active = 1")
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY sort_order, doc_type"
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    conn.close()
    return [_norm_row(r) for r in rows]


def get_norm(doc_type: str | None = None, norm_id: int | None = None) -> dict | None:
    """
    Норма по ключу вида документа (doc_type) или по id
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    if norm_id is not None:
        cursor.execute(
            "SELECT id, doc_type, category, title, entity, unit, norm_min, "
            "norm_hours, coeff, sno, date_from, date_to, comment, sort_order, active "
            "FROM norms WHERE id = ?",
            (norm_id,),
        )
    else:
        cursor.execute(
            "SELECT id, doc_type, category, title, entity, unit, norm_min, "
            "norm_hours, coeff, sno, date_from, date_to, comment, sort_order, active "
            "FROM norms WHERE doc_type = ?",
            (doc_type,),
        )
    row = cursor.fetchone()
    conn.close()
    return _norm_row(row) if row else None


def upsert_norm(
    doc_type: str,
    category: str,
    title: str,
    entity: str,
    unit: str | None = None,
    norm_min: float | None = None,
    norm_hours: float | None = None,
    coeff: float = 1.00,
    sno: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    comment: str | None = None,
    sort_order: int = 0,
    active: bool = True,
) -> dict:
    """
    Сохраняет/обновляет норму по doc_type. Если указан только один из
    norm_min/norm_hours — второй пересчитывается из него (60 мин = 1 час).
    """

    if norm_hours is None and norm_min is not None:
        norm_hours = round(float(norm_min) / 60.0, 6)
    if norm_min is None and norm_hours is not None:
        norm_min = round(float(norm_hours) * 60.0, 3)
    if norm_min is None:
        norm_min = 0.0
    if norm_hours is None:
        norm_hours = 0.0

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO norms (doc_type, category, title, entity, unit, norm_min, "
        "norm_hours, coeff, sno, date_from, date_to, comment, sort_order, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(doc_type) DO UPDATE SET "
        "category=excluded.category, title=excluded.title, entity=excluded.entity, "
        "unit=excluded.unit, norm_min=excluded.norm_min, "
        "norm_hours=excluded.norm_hours, coeff=excluded.coeff, sno=excluded.sno, "
        "date_from=excluded.date_from, date_to=excluded.date_to, "
        "comment=excluded.comment, sort_order=excluded.sort_order, "
        "active=excluded.active",
        (
            str(doc_type).strip(),
            str(category or "").strip(),
            str(title or "").strip(),
            str(entity or "").strip(),
            str(unit or "").strip(),
            float(norm_min),
            float(norm_hours),
            float(coeff),
            (str(sno).strip() if sno else None),
            date_from,
            date_to,
            comment,
            int(sort_order),
            int(bool(active)),
        ),
    )
    conn.commit()
    conn.close()
    return get_norm(doc_type=doc_type)


def delete_norm(doc_type: str) -> bool:
    """
    Удаляет норму по ключу вида документа. Возвращает True при удалении.
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM norms WHERE doc_type = ?", (str(doc_type).strip(),))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def count_norms() -> int:
    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM norms")
    n = cursor.fetchone()[0]
    conn.close()
    return int(n)


def _norm_row(row) -> dict:
    return {
        "id": row[0],
        "doc_type": row[1],
        "category": row[2],
        "title": row[3],
        "entity": row[4],
        "unit": row[5],
        "norm_min": row[6],
        "norm_hours": row[7],
        "coeff": row[8],
        "sno": row[9],
        "date_from": row[10],
        "date_to": row[11],
        "comment": row[12],
        "sort_order": row[13],
        "active": bool(row[14]),
    }


# ============================ СОТРУДНИКИ ===================================

def _migrate_employees_legacy_schema(cursor) -> None:
    """
    Переводит таблицу employees из старой схемы (ключ user_1c) в новую
    (ключ full_name, user_1c — необязательный алиас для технических имён 1С).

    Вызывается из init_db при каждом подключении; при уже новой схеме — no-op.
    Старые записи сохраняются, пустые user_1c схлопываются в NULL,
    дубли full_name (другой регистр) сводятся к одной записи.
    """

    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='employees'"
    )
    if cursor.fetchone() is None:
        return

    # Старая схема имеет UNIQUE-индекс на user_1c (автосозданный sqlite_autoindex)
    cursor.execute("PRAGMA index_list(employees)")
    legacy = False
    for idx_row in cursor.fetchall():
        idx_name = idx_row[1]
        cursor.execute(f'PRAGMA index_info("{idx_name}")')
        cols = [r[2] for r in cursor.fetchall()]
        if idx_name.startswith("sqlite_autoindex") and cols == ["user_1c"]:
            legacy = True
            break
    if not legacy:
        return

    cursor.execute("""
        CREATE TABLE employees_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL UNIQUE,
            user_1c TEXT,
            role TEXT,
            hours_per_month REAL NOT NULL DEFAULT 130.0,
            comment TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    cursor.execute("""
        INSERT INTO employees_new
            (full_name, user_1c, role, hours_per_month, comment, active)
        SELECT full_name, NULLIF(TRIM(user_1c), ''), role, hours_per_month,
               comment, active
        FROM employees
        GROUP BY full_name COLLATE NOCASE
    """)
    cursor.execute("DROP TABLE employees")
    cursor.execute("ALTER TABLE employees_new RENAME TO employees")

def list_employees(active_only: bool = True) -> list[dict]:
    """
    Сотрудники аутсорсера (таблица employees). Ключ — ФИО,
    user_1c — необязательный алиас для технических имён 1С.
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    sql = ("SELECT id, user_1c, full_name, role, hours_per_month, comment, active "
           "FROM employees")
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY full_name COLLATE NOCASE"
    cursor.execute(sql)
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "user_1c": r[1],
            "full_name": r[2],
            "role": r[3],
            "hours_per_month": r[4],
            "comment": r[5],
            "active": bool(r[6]),
        }
        for r in rows
    ]


def get_employee(
    full_name: str | None = None,
    user_1c: str | None = None,
    emp_id: int | None = None,
) -> dict | None:
    """
    Возвращает сотрудника по ФИО, алиасу пользователя 1С или id (приоритет:
    emp_id > full_name > user_1c). Сравнение без учёта регистра.
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, user_1c, full_name, role, hours_per_month, comment, active "
        "FROM employees"
    )
    rows = cursor.fetchall()
    conn.close()

    def _norm(v: object) -> str:
        return str(v or "").strip().lower()

    row = None
    if emp_id is not None:
        for r in rows:
            if r[0] == emp_id:
                row = r
                break
    elif full_name is not None:
        needle = _norm(full_name)
        for r in rows:
            if _norm(r[2]) == needle:
                row = r
                break
    elif user_1c is not None:
        needle = _norm(user_1c)
        for r in rows:
            if _norm(r[1]) == needle:
                row = r
                break
    elif rows:
        row = rows[0]

    if row is None:
        return None
    return {
        "id": row[0],
        "user_1c": row[1],
        "full_name": row[2],
        "role": row[3],
        "hours_per_month": row[4],
        "comment": row[5],
        "active": bool(row[6]),
    }


def upsert_employee(
    full_name: str,
    user_1c: str | None = None,
    role: str | None = None,
    hours_per_month: float = 130.0,
    comment: str | None = None,
    active: bool = True,
) -> dict:
    """
    Сохраняет/обновляет сотрудника по ФИО (ключ). user_1c — опциональный
    алиас технического имени из 1С; пустой/null не перетирает существующий.
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    user_1c_val = str(user_1c).strip() if user_1c else None
    cursor.execute(
        "INSERT INTO employees (user_1c, full_name, role, hours_per_month, "
        "comment, active) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(full_name) DO UPDATE SET "
        "user_1c=COALESCE(excluded.user_1c, employees.user_1c), "
        "role=excluded.role, "
        "hours_per_month=excluded.hours_per_month, comment=excluded.comment, "
        "active=excluded.active",
        (
            user_1c_val,
            str(full_name).strip(),
            (str(role or "").strip() if role else None),
            float(hours_per_month),
            comment,
            int(bool(active)),
        ),
    )
    conn.commit()
    conn.close()
    return get_employee(full_name=str(full_name).strip())


def delete_employee(emp_id: int) -> bool:
    """
    Удаляет сотрудника. Возвращает True при удалении.
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM employees WHERE id = ?", (emp_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def _parse_urls(raw) -> list:
    try:
        parsed = json.loads(raw) if raw else []
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []
