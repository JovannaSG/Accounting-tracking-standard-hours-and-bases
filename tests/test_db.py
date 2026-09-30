import json
import os
import sqlite3
import tempfile
from unittest import mock

import pytest

from core import db


def test_users_config_falls_back_to_user_json(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="cfg_test_")
    monkeypatch.setattr(db, "USERS_CONFIG_PATH", os.path.join(tmp, "users.json"))
    cfg_path = os.path.join(tmp, "user.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"admin": {"role": "admin", "password_hash": "h"}}, f)
    cfg = db.load_users_config()
    assert list(cfg) == ["admin"]

    # Приоритет у users.json, если оба файла существуют
    with open(os.path.join(tmp, "users.json"), "w", encoding="utf-8") as f:
        json.dump({"boss": {"role": "admin", "password_hash": "h"}}, f)
    cfg = db.load_users_config()
    assert list(cfg) == ["boss"]


def test_no_users_config_returns_empty(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="cfg_test_")
    monkeypatch.setattr(db, "USERS_CONFIG_PATH", os.path.join(tmp, "users.json"))
    assert db.load_users_config() == {}


def test_users_employee_full_name_additive_migration(monkeypatch):
    """Старый DDL без employee_full_name доращивается миграцией в init_db()."""
    tmp = tempfile.mkdtemp(prefix="users_schema_")
    legacy = os.path.join(tmp, "legacy.db")
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE users ("
        "login TEXT PRIMARY KEY, role TEXT NOT NULL,"
        "password_hash TEXT NOT NULL, allowed_urls TEXT,"
        "created_at TEXT, active INTEGER NOT NULL DEFAULT 1)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "_DB_PATH", legacy)
    db.init_db()

    conn = sqlite3.connect(legacy)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    finally:
        conn.close()
    assert "employee_full_name" in cols


def test_user_employee_full_name_roundtrip(clean_db):
    db.upsert_user(
        "acc1", "accountant", "h", [],
        active=True, employee_full_name="Иванова Анна",
    )
    row = db.get_user("acc1")
    assert row["employee_full_name"] == "Иванова Анна"

    listed = next(u for u in db.list_users() if u["login"] == "acc1")
    assert listed["employee_full_name"] == "Иванова Анна"

    # Привязка сбрасывается при переводе на роль без row-level фильтра
    db.upsert_user("acc1", "admin", "h", [], active=True, employee_full_name=None)
    assert db.get_user("acc1")["employee_full_name"] is None


@pytest.fixture
def clean_db():
    """Пустая тестовая БД (conftest чистит файл между тестами)."""
    db.init_db()
    yield
    db.init_db()


def test_norm_roundtrip(clean_db):
    norm = db.upsert_norm(
        doc_type="bank_incoming",
        category="Банк и касса",
        title="Поступление на расчетный счет",
        entity="Document_ПоступлениеНаРасчетныйСчет",
        unit="операция",
        norm_min=2.5,
        coeff=1.00,
    )
    assert norm["norm_hours"] == pytest.approx(round(2.5 / 60.0, 6))
    got = db.get_norm(doc_type="bank_incoming")
    assert got["title"] == "Поступление на расчетный счет"
    assert got["category"] == "Банк и касса"

    # UPSERT с новыми значениями
    db.upsert_norm(
        doc_type="bank_incoming",
        category="Банк и касса",
        title="Поступление на расчетный счет",
        entity="Document_ПоступлениеНаРасчетныйСчет",
        norm_hours=0.10,
    )
    got = db.get_norm(doc_type="bank_incoming")
    assert got["norm_hours"] == pytest.approx(0.10)
    assert db.count_norms() == 1


def test_norm_delete(clean_db):
    db.upsert_norm(doc_type="payment_order", category="Банк и касса",
                   title="Платёжное поручение", entity="Document_ПлатежноеПоручение")
    assert db.delete_norm("payment_order") is True
    assert db.get_norm(doc_type="payment_order") is None
    assert db.delete_norm("payment_order") is False


def test_employee_roundtrip(clean_db):
    emp = db.upsert_employee(
        full_name="Иванова Анна Александровна",
        user_1c="Иванова А.А.",
        role="Бухгалтер по первичке",
        hours_per_month=130.0,
    )
    assert emp["role"] == "Бухгалтер по первичке"
    got = db.get_employee(full_name="иванова анна александровна")
    assert got["full_name"] == "Иванова Анна Александровна"
    assert got["user_1c"] == "Иванова А.А."

    # Обновление по ключу ФИО
    db.upsert_employee(full_name="Иванова Анна Александровна",
                       hours_per_month=140.0)
    assert db.get_employee(full_name="Иванова Анна Александровна")[
        "hours_per_month"] == 140.0

    assert db.delete_employee(emp["id"]) is True
    assert db.get_employee(full_name="Иванова Анна Александровна") is None


def test_employee_lookup_by_alias(clean_db):
    db.upsert_employee(
        full_name="Кирищёнок Евгений",
        user_1c="Е_Кирищёнок",
        role="Бухгалтер",
        hours_per_month=130.0,
    )
    # По алиасу user_1c (без учёта регистра)
    got = db.get_employee(user_1c="е_кирищёнок")
    assert got is not None
    assert got["full_name"] == "Кирищёнок Евгений"

    # upsert без алиаса не затирает существующий
    db.upsert_employee(full_name="Кирищёнок Евгений", role="Главный бухгалтер")
    got = db.get_employee(user_1c="Е_Кирищёнок")
    assert got["role"] == "Главный бухгалтер"
    assert got["user_1c"] == "Е_Кирищёнок"


def test_employee_dup_alias_keeps_single_row(clean_db):
    db.upsert_employee(full_name="Кирищёнок Евгений", user_1c="Е_Кирищёнок")
    db.upsert_employee(full_name="Кирищёнок Евгений", user_1c="ДругойАлиас")
    # Ключ — ФИО: одна запись, алиас обновлён
    assert len(db.list_employees(active_only=False)) == 1
    assert db.get_employee(user_1c="ДругойАлиас")["full_name"] == "Кирищёнок Евгений"


def test_employee_legacy_schema_migrated(clean_db):
    """Старая БД (ключ user_1c) после init_db переходит на ключ full_name."""
    import sqlite3
    conn = sqlite3.connect(db._DB_PATH)
    conn.execute("DROP TABLE IF EXISTS employees")
    conn.execute("""
        CREATE TABLE employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_1c TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            role TEXT,
            hours_per_month REAL NOT NULL DEFAULT 130.0,
            comment TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute(
        "INSERT INTO employees (user_1c, full_name, role, hours_per_month) "
        "VALUES (?, ?, ?, ?)",
        ("Иванова А.А.", "Иванова Анна", "Бухгалтер", 130.0),
    )
    conn.execute(
        "INSERT INTO employees (user_1c, full_name, role, hours_per_month) "
        "VALUES (?, ?, ?, ?)",
        ("Е_Кирищёнок", "Кирищёнок Евгений", "Бухгалтер", 130.0),
    )
    conn.commit()
    conn.close()

    db.init_db()  # запускает миграцию

    rows = db.list_employees(active_only=False)
    assert {e["full_name"] for e in rows} == {
        "Иванова Анна", "Кирищёнок Евгений",
    }
    assert {e["user_1c"] for e in rows} == {"Иванова А.А.", "Е_Кирищёнок"}
    assert db.get_employee(full_name="Иванова Анна")["user_1c"] == "Иванова А.А."
    assert db.get_employee(user_1c="Е_Кирищёнок")["full_name"] == "Кирищёнок Евгений"


def test_base_insert_with_sno(clean_db):
    base = db.insert_base(
        name="АГЕНТСТВО ИНВЕСТ-АВИА",
        url="https://msk1.1cfresh.com/a/ea/1119958",
        login="odata.user",
        password="secret",
        sno="УСН Доходы",
        group="Группа А",
    )
    assert base is not None
    assert base["sno"] == "УСН Доходы"
    assert base["group"] == "Группа А"

    # Дубликат URL отклоняется
    dup = db.insert_base("Дубль", "https://msk1.1cfresh.com/a/ea/1119958",
                         "u", "p")
    assert dup is None

    # Обновление СНО
    db.update_base(base["id"], sno="ОСН")
    assert db.get_base_by_id(base["id"])["sno"] == "ОСН"


def _raw_password(base_id: int) -> str:
    conn = sqlite3.connect(db._DB_PATH)
    row = conn.execute(
        "SELECT password FROM bases WHERE id = ?", (base_id,)
    ).fetchone()
    conn.close()
    return row[0]


# ===================== ОБНОВЛЕНИЕ СОТРУДНИКА ПО ID =========================

def test_update_employee_by_id(clean_db):
    """ФИО изменяемо, поэтому правка адресуется по id, а не по имени."""
    emp = db.upsert_employee(
        full_name="Иванова Анна", user_1c="Иванова А.А.", role="Бухгалтер",
    )
    assert db.update_employee(emp["id"], full_name="Иванова Анна Петровна") is True

    got = db.get_employee(emp_id=emp["id"])
    assert got["full_name"] == "Иванова Анна Петровна"
    # id не меняется, алиас и роль сохраняются (None = не трогать)
    assert got["id"] == emp["id"]
    assert got["user_1c"] == "Иванова А.А."
    assert got["role"] == "Бухгалтер"
    assert db.get_employee(full_name="Иванова Анна") is None


def test_update_employee_none_leaves_fields_untouched(clean_db):
    emp = db.upsert_employee(
        full_name="Петрова Е.В.", user_1c="Е_Петрова", role="Бухгалтер",
        hours_per_month=100.0,
    )
    assert db.update_employee(emp["id"], comment="заметка") is True
    got = db.get_employee(emp_id=emp["id"])
    assert got["full_name"] == "Петрова Е.В."
    assert got["user_1c"] == "Е_Петрова"
    assert got["hours_per_month"] == pytest.approx(100.0)
    assert got["comment"] == "заметка"


def test_update_employee_can_deactivate_and_clear_alias(clean_db):
    emp = db.upsert_employee(full_name="Сидоров С.С.", user_1c="С_Сидоров")
    assert db.update_employee(
        emp["id"], user_1c="", active=False,
    ) is True
    got = db.get_employee(emp_id=emp["id"])
    assert got["user_1c"] is None
    assert got["active"] is False
    assert db.list_employees(active_only=True) == []


def test_update_employee_duplicate_name_rejected_and_rolled_back(clean_db):
    """Дубль ФИО -> False; откат не оставляет «отравленное» соединение."""
    first = db.upsert_employee(full_name="Иванова Анна")
    second = db.upsert_employee(full_name="Петрова Елена")

    assert db.update_employee(second["id"], full_name="Иванова Анна") is False
    # Запись не изменилась
    assert db.get_employee(emp_id=second["id"])["full_name"] == "Петрова Елена"
    # Первая запись на месте
    assert db.get_employee(emp_id=first["id"])["full_name"] == "Иванова Анна"
    # Соединение переиспользуемо: следующий вызов в том же процессе работает
    assert db.update_employee(first["id"], role="Бухгалтер") is True
    assert db.get_employee(emp_id=first["id"])["role"] == "Бухгалтер"


def test_update_employee_cascades_to_user_link(clean_db):
    """Переименование не должно осиротить бухгалтера (ТЗ §11)."""
    emp = db.upsert_employee(full_name="Иванова Анна")
    db.upsert_user(
        login="buh1", role="accountant", password_hash="h", allowed_urls=[],
        employee_full_name="Иванова Анна",
    )
    assert db.update_employee(emp["id"], full_name="Иванова Анна Петровна") is True
    assert db.get_user("buh1")["employee_full_name"] == "Иванова Анна Петровна"


def test_update_employee_cascade_is_case_insensitive(clean_db):
    emp = db.upsert_employee(full_name="Иванова Анна")
    db.upsert_user(
        login="buh2", role="accountant", password_hash="h", allowed_urls=[],
        employee_full_name="ИВАНОВА АННА",
    )
    assert db.update_employee(emp["id"], full_name="Иванова А.П.") is True
    assert db.get_user("buh2")["employee_full_name"] == "Иванова А.П."


def test_update_employee_cascade_rolled_back_with_employee(clean_db):
    """Конфликт UNIQUE откатывает и employees, и users в одной транзакции."""
    keeper = db.upsert_employee(full_name="Иванова Анна")
    other = db.upsert_employee(full_name="Петрова Елена")
    db.upsert_user(
        login="buh3", role="accountant", password_hash="h", allowed_urls=[],
        employee_full_name="Петрова Елена",
    )
    assert db.update_employee(other["id"], full_name="Иванова Анна") is False
    assert db.get_user("buh3")["employee_full_name"] == "Петрова Елена"
    assert db.get_employee(emp_id=keeper["id"])["full_name"] == "Иванова Анна"


def test_update_employee_no_cascade_when_name_unchanged(clean_db):
    """Смена только роли не должна трогать users."""
    emp = db.upsert_employee(full_name="Иванова Анна")
    db.upsert_user(
        login="buh4", role="accountant", password_hash="h", allowed_urls=[],
        employee_full_name="Иванова Анна",
    )
    assert db.update_employee(
        emp["id"], full_name="  иванова анна  ", role="Главный бухгалтер",
    ) is True
    assert db.get_user("buh4")["employee_full_name"] == "Иванова Анна"
    assert db.get_employee(emp_id=emp["id"])["role"] == "Главный бухгалтер"


def test_update_employee_rejects_bad_arguments(clean_db):
    emp = db.upsert_employee(full_name="Иванова Анна")
    assert db.update_employee(999999, full_name="Нет Такого") is False
    assert db.update_employee(emp["id"], full_name="   ") is False
    assert db.get_employee(emp_id=emp["id"])["full_name"] == "Иванова Анна"


def test_update_employee_closes_connection_on_every_path(clean_db):
    """Все ветки (успех, дубль ФИО, несуществующий id) закрывают соединение:
    иначе update_employee копил бы незакрытые соединения SQLite."""

    emp = db.upsert_employee(full_name="Иванова Анна")
    other = db.upsert_employee(full_name="Петрова Елена")

    real_connect = sqlite3.connect
    opened = []

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    with mock.patch.object(db.sqlite3, "connect", tracking_connect):
        assert db.update_employee(emp["id"], role="Бухгалтер") is True
        assert db.update_employee(
            other["id"], full_name="Иванова Анна"
        ) is False  # дубль ФИО -> IntegrityError

    assert opened, "ожидалось хотя бы одно открытое соединение"
    for conn in opened:
        # закрытое соединение падает на execute с ProgrammingError
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


# ==================== ВИДЫ ДОКУМЕНТОВ НА УРОВНЕ БАЗЫ ======================

def test_base_active_doc_types_defaults_to_all(clean_db):
    """Пустое значение = выгружать все виды из doc_types.json."""
    base = db.insert_base("Клиент", "https://msk1.1cfresh.com/a/ea/1", "u", "p")
    assert base["active_doc_types"] == []
    assert db.get_base_by_id(base["id"])["active_doc_types"] == []
    assert db.list_bases()[0]["active_doc_types"] == []


def test_base_active_doc_types_roundtrip(clean_db):
    base = db.insert_base(
        "Клиент", "https://msk1.1cfresh.com/a/ea/1", "u", "p",
        active_doc_types=["bank_incoming", "payment_order"],
    )
    assert base["active_doc_types"] == ["bank_incoming", "payment_order"]

    # Через get_base (по URL) и list_bases — тоже разобранный список
    assert db.get_base("https://msk1.1cfresh.com/a/ea/1")["active_doc_types"] == [
        "bank_incoming", "payment_order",
    ]
    assert db.list_bases()[0]["active_doc_types"] == [
        "bank_incoming", "payment_order",
    ]

    assert db.update_base(base["id"], active_doc_types=["goods_incoming"]) is True
    assert db.get_base_by_id(base["id"])["active_doc_types"] == ["goods_incoming"]

    # Пустой список — снять ограничение, а не «без изменений»
    assert db.update_base(base["id"], active_doc_types=[]) is True
    assert db.get_base_by_id(base["id"])["active_doc_types"] == []


def test_update_base_none_keeps_doc_types(clean_db):
    base = db.insert_base(
        "Клиент", "https://msk1.1cfresh.com/a/ea/1", "u", "p",
        active_doc_types=["bank_incoming"],
    )
    assert db.update_base(base["id"], name="Клиент 2") is True
    assert db.get_base_by_id(base["id"])["active_doc_types"] == ["bank_incoming"]
    assert db.get_base_by_id(base["id"])["name"] == "Клиент 2"


def test_base_active_doc_types_additive_migration(monkeypatch):
    """Старая БД без active_doc_types доращивается в init_db()."""
    tmp = tempfile.mkdtemp(prefix="bases_schema_")
    legacy = os.path.join(tmp, "legacy.db")
    monkeypatch.setattr(db, "_DB_PATH", legacy)

    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE bases ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
        "url TEXT NOT NULL UNIQUE, login TEXT, password TEXT,"
        "created_at TEXT, active INTEGER NOT NULL DEFAULT 1)"
    )
    conn.execute(
        "INSERT INTO bases (name, url, active) VALUES (?, ?, 1)",
        ("Старый клиент", "https://msk1.1cfresh.com/a/ea/9"),
    )
    conn.commit()
    conn.close()

    db.init_db()

    got = db.get_base("https://msk1.1cfresh.com/a/ea/9")
    assert got is not None
    assert got["name"] == "Старый клиент"
    assert got["active_doc_types"] == []
    # Миграция аддитивная: можно сразу записать значения
    assert db.update_base(got["id"], active_doc_types=["cash_income"]) is True
    assert db.get_base_by_id(got["id"])["active_doc_types"] == ["cash_income"]


def test_base_active_doc_types_corrupt_json_reads_as_empty(clean_db):
    base = db.insert_base("Клиент", "https://msk1.1cfresh.com/a/ea/1", "u", "p")
    conn = sqlite3.connect(db._DB_PATH)
    conn.execute(
        "UPDATE bases SET active_doc_types = ? WHERE id = ?",
        ("не json", base["id"]),
    )
    conn.commit()
    conn.close()
    assert db.get_base_by_id(base["id"])["active_doc_types"] == []


def test_base_doc_types_stored_as_json_text(clean_db):
    base = db.insert_base(
        "Клиент", "https://msk1.1cfresh.com/a/ea/1", "u", "p",
        active_doc_types=["bank_incoming"],
    )
    conn = sqlite3.connect(db._DB_PATH)
    raw = conn.execute(
        "SELECT active_doc_types FROM bases WHERE id = ?", (base["id"],)
    ).fetchone()[0]
    conn.close()
    assert json.loads(raw) == ["bank_incoming"]


def test_base_password_plaintext_without_key(clean_db, monkeypatch):
    monkeypatch.delenv(db._SECRET_KEY_ENV, raising=False)
    db._SECRET_CACHE.clear()
    base = db.insert_base("А", "https://x.example/a", "u", "plain-pass")
    assert _raw_password(base["id"]) == "plain-pass"
    assert db.get_base_by_id(base["id"])["password"] == "plain-pass"
    assert db.list_bases()[0]["password"] == "plain-pass"
    db._SECRET_CACHE.clear()


def test_base_password_encrypted_with_key(clean_db, monkeypatch):
    cryptography = pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv(db._SECRET_KEY_ENV, key)
    db._SECRET_CACHE.clear()

    base = db.insert_base("А", "https://x.example/a", "u", "super-secret")
    stored = _raw_password(base["id"])
    assert stored.startswith("enc:v1:")
    assert "super-secret" not in stored
    # Чтение возвращает открытый пароль
    assert db.get_base_by_id(base["id"])["password"] == "super-secret"
    assert db.get_base("https://x.example/a")["password"] == "super-secret"
    assert db.list_bases()[0]["password"] == "super-secret"

    # Обновление без нового пароля сохраняет токен, не ломая доступ
    db.update_base(base["id"], sno="ОСН")
    assert db.get_base_by_id(base["id"])["password"] == "super-secret"

    # Обновление с новым паролем перешифровывает
    db.update_base(base["id"], password="brand-new")
    assert db.get_base_by_id(base["id"])["password"] == "brand-new"
    assert "brand-new" not in _raw_password(base["id"])
    db._SECRET_CACHE.clear()


def test_base_password_legacy_plaintext_still_readable(clean_db, monkeypatch):
    """Записи без префикса читаются как раньше — даже если ключ задан."""
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet

    # Ключа нет — пишем в открытом виде
    monkeypatch.delenv(db._SECRET_KEY_ENV, raising=False)
    db._SECRET_CACHE.clear()
    base = db.insert_base("А", "https://x.example/a", "u", "legacy-pass")

    # Появился ключ — legacy-запись всё ещё читается и перешифруется
    monkeypatch.setenv(db._SECRET_KEY_ENV, Fernet.generate_key().decode())
    db._SECRET_CACHE.clear()
    assert db.get_base_by_id(base["id"])["password"] == "legacy-pass"
    db.update_base(base["id"], sno="УСН")
    assert _raw_password(base["id"]).startswith("enc:v1:")
    assert db.get_base_by_id(base["id"])["password"] == "legacy-pass"
    db._SECRET_CACHE.clear()


def test_base_password_token_unreadable_without_key(clean_db, monkeypatch):
    """Потерянный ключ не приводит к падению — пароль пустой, токен цел."""
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv(db._SECRET_KEY_ENV, key)
    db._SECRET_CACHE.clear()
    base = db.insert_base("А", "https://x.example/a", "u", "pw")
    token = _raw_password(base["id"])

    monkeypatch.setenv(db._SECRET_KEY_ENV, Fernet.generate_key().decode())
    db._SECRET_CACHE.clear()
    assert db.get_base_by_id(base["id"])["password"] == ""
    # Токен не перетирается при обновлении других полей
    db.update_base(base["id"], sno="ОСН")
    assert _raw_password(base["id"]) == token
    db._SECRET_CACHE.clear()


def test_generate_secret_key_is_usable(clean_db, monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet

    monkeypatch.setenv(db._SECRET_KEY_ENV, db.generate_secret_key())
    db._SECRET_CACHE.clear()
    assert db._get_fernet() is not None
    base = db.insert_base("А", "https://x.example/a", "u", "pw")
    assert db.get_base_by_id(base["id"])["password"] == "pw"
    db._SECRET_CACHE.clear()