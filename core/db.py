import base64
import sqlite3
import json
import os
import secrets
import sys
from datetime import datetime, timezone

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
#
# Если ключ ЗАДАН, но непригоден, шифрование не отключается молча: это
# привело бы к записи паролей в открытом виде и к «пустым» паролям при
# чтении. Вместо этого поднимается SecretKeyError с указанием причины.
_SECRET_KEY_ENV = "AUDIT_DB_SECRET_KEY"
_ENC_PREFIX = "enc:v1:"
_SECRET_CACHE: dict[str, object] = {}


class SecretKeyError(RuntimeError):
    """
    AUDIT_DB_SECRET_KEY задан, но не является корректным ключом Fernet.
    """


def _get_fernet():
    """
    Возвращает объект Fernet по ключу из окружения или None (ключ не задан).

    Если ключ задан, но непригоден (опечатка, не base64 от 32 байт, не
    установлен cryptography), поднимает SecretKeyError. Молча выключать
    шифрование нельзя: молчаливый откат записал бы пароли открытым текстом
    и превратил бы опечатку в ключе в трудно диагностируемую потерю паролей.
    Ключ кэшируется вместе с ошибкой, чтобы не перебирать его на каждый вызов.
    """

    if "fernet" in _SECRET_CACHE:
        cached = _SECRET_CACHE["fernet"]
        if isinstance(cached, SecretKeyError):
            raise cached
        return cached

    raw_key = (os.environ.get(_SECRET_KEY_ENV) or "").strip()
    if not raw_key:
        _SECRET_CACHE["fernet"] = None
        return None

    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        err = SecretKeyError(
            f"{_SECRET_KEY_ENV} задан, но не установлен пакет cryptography — "
            "зашифровать пароли нечем. Установите cryptography "
            "(pip install cryptography) или уберите ключ из окружения."
        )
        err.__cause__ = e
        _SECRET_CACHE["fernet"] = err
        raise err

    try:
        fernet = Fernet(raw_key.encode("utf-8"))
    except Exception as e:
        err = SecretKeyError(
            f"{_SECRET_KEY_ENV} не является корректным ключом Fernet: {e}. "
            "Ожидается base64 от 32 случайных байт, например "
            "'python -c \"import base64,secrets; "
            "print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())\"'. "
            "Пока ключ неверен, приложение не сохранит пароли в открытом виде."
        )
        err.__cause__ = e
        _SECRET_CACHE["fernet"] = err
        raise err

    _SECRET_CACHE["fernet"] = fernet
    return fernet


def _secret_key_is_broken() -> bool:
    """True, если ключ задан, но непригоден (без исключения наружу)."""

    try:
        _get_fernet()
    except SecretKeyError:
        return True
    return False


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
    Расшифровывает пароль.

    Записи в открытом виде (legacy, без префикса) возвращаются как есть —
    это позволяет включить ключ на базе, где часть паролей ещё не
    пересохранена.

    Если значение зашифровано, а ключ не задан, пароль недоступен: раньше
    здесь возвращалась пустая строка, и ошибка выглядела как «пароль базы
    потерялся». Теперь при непригодном ключе поднимается SecretKeyError с
    причиной, а при отсутствии ключа — тот же класс ошибки с понятным текстом.
    """

    raw = str(stored or "")
    if not raw.startswith(_ENC_PREFIX):
        return raw
    fernet = _get_fernet()  # SecretKeyError, если ключ задан, но неверен
    if fernet is None:
        return ""
    token = raw[len(_ENC_PREFIX):]
    try:
        return fernet.decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception as e:
        raise SecretKeyError(
            f"Пароль базы зашифрован, но не расшифровывается ({e}). "
            "Похоже, значение в другой БД было сохранено с другим "
            f"{_SECRET_KEY_ENV}."
        ) from e


def init_db():
    """
    Создает таблицы, если их нет, и добавляет недостающие колонки.

    Таблица `users` хранит роли и доступ к базам (ТЗ §11). Если таблица
    пуста — засеивается из конфига users.json.

    Таблица `bases` — клиентские базы 1С:Фреш (одна база = один клиент).
    Дополнительные поля:
      - sno   — система налогообложения клиента (заполняется вручную,
                т.к. регистр СНО в OData-составе обычно не публикуется);
      - active_doc_types — JSON-список ключей видов документов, которые
                выгружаются по этой базе; NULL или пустой список означает
                «выгружать все виды из реестра doc_types.json».

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
            active_doc_types TEXT,
            created_at TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    cursor.execute("PRAGMA table_info(bases)")
    existing = {row[1] for row in cursor.fetchall()}
    if "sno" not in existing:
        cursor.execute("ALTER TABLE bases ADD COLUMN sno TEXT")
    if "active_doc_types" not in existing:
        cursor.execute("ALTER TABLE bases ADD COLUMN active_doc_types TEXT")
    if "group" in existing:
        # Колонка была неиспользуемой: значение сохранялось, но нигде не
        # участвовало в расчётах и не попадало в отчёт. Убрана как
        # мёртвая функциональность (YAGNI) — вернуть при появлении
        # группировки по группам клиентов можно из истории Git.
        # DROP COLUMN появился в SQLite 3.35.0; на более старой версии
        # колонка просто остаётся неиспользуемой.
        try:
            cursor.execute('ALTER TABLE bases DROP COLUMN "group"')
        except sqlite3.OperationalError:
            pass

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
    cursor.execute("PRAGMA table_info(norms)")
    existing_norms = {row[1] for row in cursor.fetchall()}
    if "is_discovered" not in existing_norms:
        cursor.execute("ALTER TABLE norms ADD COLUMN is_discovered INTEGER NOT NULL DEFAULT 0")
    if "discovered_at" not in existing_norms:
        cursor.execute("ALTER TABLE norms ADD COLUMN discovered_at TEXT")
    if "has_responsible_key" not in existing_norms:
        cursor.execute("ALTER TABLE norms ADD COLUMN has_responsible_key INTEGER NOT NULL DEFAULT 0")
    if "has_author_key" not in existing_norms:
        cursor.execute("ALTER TABLE norms ADD COLUMN has_author_key INTEGER NOT NULL DEFAULT 0")
    if "has_operation_type" not in existing_norms:
        cursor.execute("ALTER TABLE norms ADD COLUMN has_operation_type INTEGER NOT NULL DEFAULT 0")
    if "variant_rules_json" not in existing_norms:
        cursor.execute("ALTER TABLE norms ADD COLUMN variant_rules_json TEXT")
    if "ref_base" not in existing_norms:
        # Донорская база, из которой впервые увиден вида документа
        # (только URL). Аддон к is_discovered/discovered_at — без flags JSON,
        # чтобы не было двух конкурирующих представлений одного признака.
        cursor.execute("ALTER TABLE norms ADD COLUMN ref_base TEXT")

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
            # utf-8-sig: файл часто правят в «Блокноте» или через PowerShell
            # 5.1, и такой редактор добавляет BOM, который utf-8 не читает.
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        return data if isinstance(data, dict) else {}
    return {}


def _looks_like_pbkdf2(value: str) -> bool:
    """
    Проверяет, что строка похожа на хэш приложения, а не на плейсхолдер.

    Формат ровно тот, что выдаёт `core.auth.hash_password()`:
    `<итерации>$<соль hex>$<дайджест hex>`. Проверка нужна, чтобы шаблон
    users.json не создавал учётную запись, в которую невозможно войти:
    `core.auth._verify_stored_hash()` вернёт False на любом другом формате.
    """

    parts = str(value or "").split("$")
    if len(parts) != 3:
        return False
    iterations, salt, digest = parts
    hex_digits = "0123456789abcdefABCDEF"
    return (
        iterations.isdigit()
        and int(iterations) >= 1000
        and len(salt) >= 16
        and len(digest) == 64
        and all(c in hex_digits for c in salt + digest)
    )


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
        if not _looks_like_pbkdf2(pwd_hash):
            print(
                f"[users.json] пропущен '{login.strip().lower()}': ожидается "
                f"хэш вида <итерации>$<соль>$<дайджест> — сгенерируйте его "
                f"командой 'python -m core.auth hash <пароль>'; получено: "
                f"{pwd_hash[:24]}",
                file=sys.stderr,
            )
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
            "allowed_urls": _parse_json_list(r[2]),
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
_BASES_SELECT = (
    "id, name, url, login, password, sno, active_doc_types, "
    "created_at, active"
)


def _base_row(row) -> dict:
    """
    Строка таблицы bases -> словарь. Пароль расшифровывается,
    active_doc_types разбирается из JSON в список ключей видов документов
    (пустой список = выгружать все виды).
    """

    return {
        "id": row[0],
        "name": row[1],
        "url": row[2],
        "login": row[3],
        "password": _decrypt_password(row[4]),
        "sno": row[5],
        "active_doc_types": _parse_json_list(row[6]),
        "created_at": row[7],
        "active": bool(row[8]),
    }


def list_bases(active_only: bool = False) -> list[dict]:
    """
    Базы клиентов (таблица bases). Пароли возвращаются: они нужны
    для подключения к OData при сборе данных.
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    sql = f"SELECT {_BASES_SELECT} FROM bases"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY name COLLATE NOCASE"
    cursor.execute(sql)
    rows = cursor.fetchall()
    conn.close()
    return [_base_row(r) for r in rows]


def get_base(url: str) -> dict | None:
    """
    Возвращает базу по (нормализованному) URL или None
    """

    from core.auth import _normalize_url

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {_BASES_SELECT} FROM bases WHERE url = ?",
        (_normalize_url(url),),
    )
    row = cursor.fetchone()
    conn.close()
    return _base_row(row) if row else None


def insert_base(
    name: str,
    url: str,
    login: str,
    password: str,
    sno: str | None = None,
    active_doc_types: list[str] | None = None,
    active: bool = True,
) -> dict | None:
    """
    Добавляет базу. URL нормализуется (_normalize_url) и должен быть уникальным.
    ``active_doc_types`` — ключи видов документов, выгружаемых по этой базе;
    пустой список означает «выгружать все виды из реестра».
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
        "INSERT INTO bases (name, url, login, password, sno, "
        "active_doc_types, created_at, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(name or "").strip(),
            norm,
            str(login or "").strip(),
            _encrypt_password(password),
            (str(sno).strip() if sno else None),
            _dump_doc_types(active_doc_types),
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
    active_doc_types: list[str] | None = None,
    active: bool | None = None,
) -> bool:
    """
    Обновляет поля базы. Пустой password — без изменений пароля.
    ``active_doc_types=None`` — без изменений; ``[]`` — выгружать все виды.
    Возвращает False, если записи нет или новый URL уже занят.
    """

    from core.auth import _normalize_url

    try:
        base = get_base_by_id(base_id)
    except SecretKeyError:
        # Пароль в БД зашифрован не тем ключом. Остальные поля править можно:
        # токен останется нетронутым (stored_raw ниже), иначе администратор не
        # смог бы починить СНО или виды документов, не зная ключа.
        base = _raw_base_by_id(base_id)
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
    new_types = (
        _dump_doc_types(base["active_doc_types"])
        if active_doc_types is None
        else _dump_doc_types(active_doc_types)
    )
    new_active = base["active"] if active is None else bool(active)

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE bases SET name=?, url=?, login=?, password=?, sno=?, "
        "active_doc_types=?, active=? WHERE id=?",
        (new_name, new_url, new_login, new_pass_enc, new_sno,
         new_types, int(new_active), base_id),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def _dump_doc_types(keys) -> str | None:
    """Сериализует список ключей видов документов для колонки TEXT."""

    clean = [str(k).strip() for k in (keys or []) if str(k or "").strip()]
    return json.dumps(clean, ensure_ascii=False) if clean else None


def _raw_base_by_id(base_id: int):
    """
    Возвращает словарь полей bases по id с паролем как в БД (без
    расшифровки) — нужен, чтобы править остальные поля, когда ключ
    шифрования не подходит к уже сохранённым токенам.
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {_BASES_SELECT} FROM bases WHERE id = ?", (base_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "url": row[2],
        "login": row[3],
        "password": row[4],  # токен как есть, без расшифровки
        "sno": row[5],
        "active_doc_types": _parse_json_list(row[6]),
        "created_at": row[7],
        "active": bool(row[8]),
    }


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
        f"SELECT {_BASES_SELECT} FROM bases WHERE id = ?",
        (base_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return _base_row(row) if row else None


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


# ============================ ИМПОРТ БАЗ ===================================
# client_databases.json (или .csv) — выгрузка 1С из клиентских баз. Формат
# каждой записи: {"name": ..., "url": ..., "login": ..., "password": ...}.
# Файл содержит учётные данные и НИКОГДА не коммитится (см. .gitignore).
_IMPORT_FIELD_ALIASES = {
    "name": ("name", "название", "имя", "база", "наименование"),
    "url": ("url", "ссылка", "адрес", "адресикс", "server"),
    "login": ("login", "логин", "пользователь", "user", "username"),
    "password": ("password", "пароль", "pass"),
}


def _clean_str(value, key: str) -> str:
    """Мелкая очистка значения из файла: пробелы, NBSP, пустые -> ''. """

    text = str(value if value is not None else "")
    return text.replace("\xa0", " ").strip()


def _normalize_row_key(value) -> str:
    """
    Приводит заголовок столбца к каноническому виду для сопоставления
    с полями импорта: регистр, пробелы, «ё» и лишние символы игнорируются.
    """

    text = _clean_str(value, "key").lower().replace("ё", "е")
    return "".join(ch for ch in text if ch.isalnum())


def _map_import_field(header) -> str | None:
    """
    Возвращает каноническое поле импорта по заголовку столбца
    или None, если заголовок не распознан.
    """

    key = _normalize_row_key(header)
    if not key:
        return None
    for field, aliases in _IMPORT_FIELD_ALIASES.items():
        if key in {_normalize_row_key(a) for a in aliases}:
            return field
    return None


def _normalize_import_row(record) -> dict:
    """
    Приводит одну запись файла к виду {name, url, login, password}.
    URL нормализуется (_normalize_url). Запись без URL невалидна.
    """
    from core.auth import _normalize_url

    if not isinstance(record, dict):
        return {"name": "", "url": "", "login": "",
                "password": "", "valid": False}
    url = _normalize_url(_clean_str(record.get("url"), "url"))
    name = _clean_str(record.get("name"), "name")
    if not url:
        return {
            "name": name or "(без URL)",
            "url": "",
            "login": _clean_str(record.get("login"), "login"),
            "password": str(record.get("password") or ""),
            "valid": False,
        }
    if not name:
        name = "База " + url.split("/")[-1]
    return {
        "name": name,
        "url": url,
        "login": _clean_str(record.get("login"), "login"),
        "password": str(record.get("password") or "").strip(),
        "valid": True,
    }


def load_client_databases(data) -> list[dict]:
    """
    Разбирает содержимое файла клиентских баз в нормализованные записи
    {name, url, login, password}. ``data`` — байты или строка файла.
    Поддерживается JSON (список объектов либо объект с одним списком
    записей) и CSV с заголовками на русском или английском.
    Записи без URL отбрасываются (помечаются valid=False).
    """
    if isinstance(data, (bytes, bytearray)):
        text = bytes(data).decode("utf-8-sig")
    else:
        text = str(data)
    text = text.lstrip("\ufeff").strip()
    if not text:
        return []

    records: list
    if text.lstrip().startswith(("[", "{")):
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Некорректный JSON: {e}") from e
        if isinstance(raw, dict):
            for key in ("databases", "bases", "clients", "items", "rows"):
                if isinstance(raw.get(key), list):
                    raw = raw[key]
                    break
            else:
                raw = [raw]
        if not isinstance(raw, list):
            raise ValueError("Ожидался список баз (массив JSON)")
        records = raw
    else:
        import csv
        import io as _io
        reader = csv.DictReader(_io.StringIO(text))
        field_map: dict[str, str] = {}
        for header in (reader.fieldnames or []):
            field = _map_import_field(header)
            if field and field not in field_map.values():
                field_map[header] = field
        if not field_map:
            raise ValueError(
                "Не найдено ни одного нужного столбца. Ожидаются "
                "название/name, ссылка/url, логин/login, пароль/password."
            )
        records = [
            {field: row.get(header) for header, field in field_map.items()}
            for row in reader
        ]

    out = []
    for record in records:
        if not isinstance(record, dict):
            continue
        mapped: dict = {}
        for field, aliases in _IMPORT_FIELD_ALIASES.items():
            for alias in aliases:
                key = _normalize_row_key(alias)
                match = next(
                    (k for k in record if _normalize_row_key(k) == key), None
                )
                if match is not None:
                    mapped[field] = record[match]
                    break
        out.append(_normalize_import_row(mapped))
    return out


def import_bases(entries: list[dict], active: bool = True) -> dict:
    """
    Импортирует записи файла клиентских баз в таблицу bases.
    Существующие базы (по нормализованному URL) НЕ обновляются и НЕ
    дублируются — они попадают в skipped. Записи без URL — в invalid.
    Возвращает отчёт: {added, skipped, invalid, added_rows, errors}.
    """
    report: dict = {
        "added": 0,
        "skipped": 0,
        "invalid": 0,
        "added_rows": [],
        "errors": [],
    }
    seen: set[str] = set()
    for raw in entries or []:
        entry = _normalize_import_row(raw)
        if not entry["valid"]:
            report["invalid"] += 1
            report["errors"].append(f"{entry['name'] or '?'}: не указан URL")
            continue
        url = entry["url"]
        if url in seen:
            report["skipped"] += 1
            continue
        seen.add(url)
        if get_base(url) is not None:
            report["skipped"] += 1
            continue
        if _secret_key_is_broken():
            report["errors"].append(
                f"{_SECRET_KEY_ENV} задан, но неверен — импорт остановлен, "
                "чтобы не сохранить пароли в открытом виде. Исправьте ключ."
            )
            break
        created = insert_base(
            name=entry["name"],
            url=url,
            login=entry["login"],
            password=entry["password"],
            sno=None,
            active_doc_types=None,
            active=active,
        )
        if created is None:
            report["skipped"] += 1
        else:
            report["added"] += 1
            report["added_rows"].append(created)
    return report


# =============================== НОРМЫ =====================================

# Единая проекция столбцов norms для чтения.
#
# _norm_row строит словарь через zip с этим кортежем, а не по позиционным
# индексам: раньше всё читалось явным списком из 14 столбцов, и любое
# добавление столбца без параллельной правки _norm_row молча смещало
# соответствие (тот же класс ошибки, что произошёл при удалении bases.group).
_NORMS_COLUMNS: tuple[str, ...] = (
    "id", "doc_type", "category", "title", "entity", "unit",
    "norm_min", "norm_hours", "coeff", "sno", "date_from", "date_to",
    "comment", "sort_order", "active",
    # Столбцы обнаружения реестра (аддитивные миграции ниже в init_db())
    "ref_base", "is_discovered", "discovered_at",
    "has_responsible_key", "has_author_key", "has_operation_type",
    "variant_rules_json",
)

# Признаки обнаружения: приводятся к bool при чтении.
_NORMS_BOOL_COLUMNS: tuple[str, ...] = (
    "active", "is_discovered",
    "has_responsible_key", "has_author_key", "has_operation_type",
)


def list_norms(category: str | None = None, active_only: bool = True) -> list[dict]:
    """
    Возвращает нормы трудозатрат. При category — только этой категории.
    """

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    sql = "SELECT " + ", ".join(_NORMS_COLUMNS) + " FROM norms"
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
    _cols = "SELECT " + ", ".join(_NORMS_COLUMNS) + " FROM norms"
    if norm_id is not None:
        cursor.execute(_cols + " WHERE id = ?", (norm_id,))
    else:
        cursor.execute(_cols + " WHERE doc_type = ?", (str(doc_type).strip(),))
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


def insert_discovered_norm(
    *,
    doc_type: str,
    category: str = "",
    title: str = "",
    entity: str = "",
    unit: str | None = None,
    ref_base: str | None = None,
    discovered_at: str | None = None,
    variant_rules_json: str = "{}",
    has_responsible_key: bool = False,
    has_author_key: bool = False,
    has_operation_type: bool = False,
) -> bool:
    """
    Вставляет впервые обнаруженный в реестре вида документа.

    Семантика — строго «пропустить, если есть» (ON CONFLICT DO NOTHING):
    задача сканера — показать и добавить только новое, никогда не перезаписывая
    заголовок или нормочасы, выставленные администратором вручную. Поэтому
    здесь НЕ используется upsert_norm, который по doc_type всё перезаписывает.

    Возвращает True, если строка создана, и False, если такой doc_type уже
    существовал и вставка была пропущена.

    Пустой title подменяется на doc_type: find_missing_norms() ищет норму по
    title, поэтому пустой заголовок сделал бы строку невидимой для
    предупреждения об отсутствии норм.

    Норма создаётся с norm_hours = 0.0: find_missing_norms() считает нулевую
    норму «не заданной», поэтому строка корректно попадёт в предупреждение,
    а не будет молча участвовать в расчёте с нулевыми часами.
    """

    key = str(doc_type or "").strip()
    if not key:
        raise ValueError("insert_discovered_norm: doc_type обязателен")

    try:
        parsed = json.loads(variant_rules_json) if variant_rules_json else {}
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"insert_discovered_norm: variant_rules_json не является JSON: {e}"
        ) from e
    if not isinstance(parsed, (dict, list)):
        raise ValueError(
            "insert_discovered_norm: variant_rules_json должен быть "
            f"объектом или массивом, получен {type(parsed).__name__}"
        )
    vjson = json.dumps(parsed, ensure_ascii=False)

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO norms ("
        "doc_type, category, title, entity, unit, "
        "norm_min, norm_hours, coeff, sno, date_from, date_to, "
        "comment, sort_order, active, "
        "ref_base, is_discovered, discovered_at, "
        "has_responsible_key, has_author_key, has_operation_type, "
        "variant_rules_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(doc_type) DO NOTHING",
        (
            key,
            str(category or "").strip(),
            str(title or "").strip() or key,
            str(entity or "").strip(),
            str(unit).strip() if unit else None,
            0.0,
            0.0,
            1.00,
            None,
            None,
            None,
            None,
            0,
            1,
            (str(ref_base).strip() if ref_base else None),
            1,
            discovered_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            int(bool(has_responsible_key)),
            int(bool(has_author_key)),
            int(bool(has_operation_type)),
            vjson,
        ),
    )
    inserted = cursor.rowcount == 1
    conn.commit()
    conn.close()
    return inserted


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
    """Словарь нормы из строки SELECT.

    Позиционируется через _NORMS_COLUMNS, а не по «row[0] .. row[14]»:
    если добавить столбец в проекцию, словарь обновится сам и не сдвинется.
    """
    if row is None:
        return {}
    data = dict(zip(_NORMS_COLUMNS, row))
    for col in _NORMS_BOOL_COLUMNS:
        data[col] = bool(data.get(col))
    return data


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


def update_employee(
    emp_id: int,
    full_name: str | None = None,
    user_1c: str | None = None,
    role: str | None = None,
    hours_per_month: float | None = None,
    comment: str | None = None,
    active: bool | None = None,
) -> bool:
    """
    Обновляет сотрудника по emp_id. ФИО — изменяемое поле (у человека может
    смениться имя), поэтому адресация идёт по id, а не по имени: key full_name
    остаётся UNIQUE только для защиты от дублей.

    None означает «оставить поле без изменений».

    Переименование каскадно правит users.employee_full_name — это мягкий
    внешний ключ «учётная запись → сотрудник» (ТЗ §11). Без каскада
    бухгалтер потерял бы привязку и увидел пустой отчёт. Обе правки идут
    в одной транзакции: при конфликте UNIQUE откатывается и employees,
    и users.

    Возвращает False, если записи нет, ФИО пустое или новое имя уже занято.
    """

    emp = get_employee(emp_id=emp_id)
    if emp is None:
        return False

    new_name = emp["full_name"] if full_name is None else str(full_name).strip()
    if not new_name:
        return False
    new_user_1c = (
        emp["user_1c"] if user_1c is None
        else (str(user_1c).strip() or None)
    )
    new_role = emp["role"] if role is None else (str(role).strip() or None)
    new_hours = (
        float(emp["hours_per_month"]) if hours_per_month is None
        else float(hours_per_month)
    )
    new_comment = emp["comment"] if comment is None else comment
    new_active = emp["active"] if active is None else bool(active)
    old_name = str(emp["full_name"] or "")
    renamed = new_name.strip().lower() != old_name.strip().lower()

    init_db()
    conn = sqlite3.connect(_DB_PATH, timeout=30.0)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE employees SET user_1c=?, full_name=?, role=?, "
            "hours_per_month=?, comment=?, active=? WHERE id=?",
            (
                new_user_1c, new_name, new_role, new_hours, new_comment,
                int(new_active), emp_id,
            ),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            return False
        if renamed:
            # Сопоставление регистронезависимое, но делаем его в Python,
            # а не через COLLATE NOCASE: встроенная коллация SQLite
            # сворачивает регистр только для ASCII, поэтому «ИВАНОВА АННА»
            # и «Иванова Анна» она считает разными строками.
            old_key = old_name.strip().lower()
            cursor.execute(
                "SELECT login, employee_full_name FROM users "
                "WHERE employee_full_name IS NOT NULL"
            )
            for login, linked in cursor.fetchall():
                if str(linked or "").strip().lower() == old_key:
                    cursor.execute(
                        "UPDATE users SET employee_full_name = ? WHERE login = ?",
                        (new_name, login),
                    )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Дубль ФИО: откатываем и employees, и users, иначе правка
        # сотрудника применилась бы, а привязка учётной записи — нет.
        conn.rollback()
        return False
    finally:
        conn.close()


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


def _parse_json_list(raw) -> list:
    """
    Разбирает JSON-массив из TEXT-колонки. NULL/мусор -> пустой список.
    Используется для users.allowed_urls и bases.active_doc_types.
    """

    try:
        parsed = json.loads(raw) if raw else []
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []
