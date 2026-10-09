import json
import os
import sqlite3
import tempfile
from unittest import mock

import pytest

from core import auth, db


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


def _write_users_config(tmp, payload):
    path = os.path.join(tmp, "users.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return path


def test_seed_skips_placeholder_hash_and_warns(clean_db, monkeypatch, capsys):
    """Плейсхолдер из users.example.json не должен создавать нелогинябельного юзера."""
    tmp = tempfile.mkdtemp(prefix="seed_placeholder_")
    monkeypatch.setattr(
        db,
        "USERS_CONFIG_PATH",
        _write_users_config(
            tmp,
            {"admin": {"role": "admin", "password": "ЗАМЕНИТЕ_НА_ХЭШ"}},
        ),
    )

    db.init_db()

    assert db.get_user("admin") is None
    warning = capsys.readouterr().err
    assert "admin" in warning
    assert "core.auth hash" in warning


def test_seed_creates_user_from_real_hash_and_login_works(clean_db, monkeypatch):
    """Хэш из core.auth.hash_password() принимается, и вход по нему проходит."""
    tmp = tempfile.mkdtemp(prefix="seed_real_hash_")
    monkeypatch.setattr(
        db,
        "USERS_CONFIG_PATH",
        _write_users_config(
            tmp,
            {
                "admin": {
                    "role": "admin",
                    "password_hash": auth.hash_password("СекретныйПароль123"),
                }
            },
        ),
    )

    db.init_db()

    row = db.get_user("admin")
    assert row is not None
    assert row["role"] == "admin"
    assert auth.verify("admin", "СекретныйПароль123")
    assert not auth.verify("admin", "неверный")


def test_seed_accepts_users_json_with_utf8_bom(clean_db, monkeypatch):
    """BOM от «Блокнота»/PowerShell не должен ломать посев пользователей."""
    tmp = tempfile.mkdtemp(prefix="seed_bom_")
    path = os.path.join(tmp, "users.json")
    with open(path, "w", encoding="utf-8-sig") as f:
        json.dump(
            {"admin": {"role": "admin", "password_hash": auth.hash_password("ПарольСBom1")}},
            f,
            ensure_ascii=False,
        )
    monkeypatch.setattr(db, "USERS_CONFIG_PATH", path)

    db.init_db()

    assert db.get_user("admin") is not None
    assert auth.verify("admin", "ПарольСBom1")


def test_reseed_does_not_overwrite_existing_user(clean_db, monkeypatch):
    """users.json применяется только пока таблица users пуста."""
    tmp = tempfile.mkdtemp(prefix="seed_once_")
    monkeypatch.setattr(
        db,
        "USERS_CONFIG_PATH",
        _write_users_config(
            tmp,
            {"admin": {"role": "admin", "password_hash": auth.hash_password("ПервыйПароль1")}},
        ),
    )
    db.init_db()
    first_hash = db.get_user("admin")["password_hash"]

    monkeypatch.setattr(
        db,
        "USERS_CONFIG_PATH",
        _write_users_config(
            tmp,
            {"admin": {"role": "admin", "password_hash": auth.hash_password("ВторойПароль2")}},
        ),
    )
    db.init_db()

    assert db.get_user("admin")["password_hash"] == first_hash
    assert auth.verify("admin", "ПервыйПароль1")
    assert not auth.verify("admin", "ВторойПароль2")


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


# ============ УДАЛЕНИЕ НЕИСПОЛЬЗУЕМОЙ КОЛОНКИ "group" У БАЗ ================

def _bases_columns() -> list[str]:
    conn = sqlite3.connect(db._DB_PATH)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(bases)")]
    finally:
        conn.close()


def test_bases_has_no_group_column(clean_db):
    """Мёртвое поле «Группа клиентов» убрано из схемы (YAGNI)."""
    assert "group" not in _bases_columns()
    base = db.insert_base("Клиент", "https://msk1.1cfresh.com/a/ea/1", "u", "p")
    assert "group" not in base


def test_init_db_migrates_legacy_bases_dropping_group(clean_db):
    """Старая БД с колонкой group мигрируется: колонка уходит, данные целы."""
    base = db.insert_base(
        "Старый клиент", "https://msk1.1cfresh.com/a/ea/8", "u", "p",
        sno="УСН",
    )
    conn = sqlite3.connect(db._DB_PATH)
    conn.execute('ALTER TABLE bases ADD COLUMN "group" TEXT')
    conn.execute(
        'UPDATE bases SET "group"=? WHERE id=?', ("Группа А", base["id"])
    )
    conn.commit()
    conn.close()
    assert "group" in _bases_columns()

    db.init_db()

    if db.sqlite3.sqlite_version_info >= (3, 35, 0):
        assert "group" not in _bases_columns()
    # На старой SQLite колонка может остаться — это не ломает работу,
    # но код её больше не читает и не пишет.
    got = db.get_base_by_id(base["id"])
    assert got["name"] == "Старый клиент"
    assert got["sno"] == "УСН"
    assert "group" not in got


def test_legacy_group_data_dropped_is_not_silently_reused(clean_db):
    """После миграции прежнее значение группы не всплывает нигде."""
    base = db.insert_base(
        "Клиент", "https://msk1.1cfresh.com/a/ea/9", "u", "p", sno="ОСНО"
    )
    conn = sqlite3.connect(db._DB_PATH)
    conn.execute('ALTER TABLE bases ADD COLUMN "group" TEXT')
    conn.execute(
        'UPDATE bases SET "group"=? WHERE id=?', ("Группа Б", base["id"])
    )
    conn.commit()
    conn.close()

    db.init_db()
    db.update_base(base["id"], name="Клиент переименован")

    got = db.get_base_by_id(base["id"])
    assert got["name"] == "Клиент переименован"
    assert got["sno"] == "ОСНО"
    assert "group" not in got
    assert db.list_bases()[0]["sno"] == "ОСНО"


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
    )
    assert base is not None
    assert base["sno"] == "УСН Доходы"

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


def test_base_password_token_unreadable_with_other_key(clean_db, monkeypatch):
    """
    Пароль зашифрован другим ключом -> SecretKeyError с понятным текстом.

    Раньше здесь возвращалась пустая строка, и подключение к 1С уходило с
    пустым паролем: ошибка выглядела как «пароль потерялся», хотя ключ просто
    не тот. Теперь тишины нет, а токен в БД остаётся нетронутым.
    """
    pytest.importorskip("cryptography")
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv(db._SECRET_KEY_ENV, key)
    db._SECRET_CACHE.clear()
    base = db.insert_base("А", "https://x.example/a", "u", "pw")
    token = _raw_password(base["id"])

    monkeypatch.setenv(db._SECRET_KEY_ENV, Fernet.generate_key().decode())
    db._SECRET_CACHE.clear()
    with pytest.raises(db.SecretKeyError):
        db.get_base_by_id(base["id"])
    # Токен не перетирается при обновлении других полей
    db.update_base(base["id"], sno="ОСН")
    assert _raw_password(base["id"]) == token
    db._SECRET_CACHE.clear()


def test_base_password_untouched_when_no_key(clean_db, monkeypatch):
    """Без ключа legacy-пароль в открытом виде читается как есть."""
    monkeypatch.delenv(db._SECRET_KEY_ENV, raising=False)
    db._SECRET_CACHE.clear()
    base = db.insert_base("А", "https://plain.example/a", "u", "legacy-pass")
    assert _raw_password(base["id"]) == "legacy-pass"
    assert db.get_base_by_id(base["id"])["password"] == "legacy-pass"
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


def test_broken_secret_key_raises_instead_of_silent_plaintext(
    clean_db, monkeypatch
):
    """
    Ключ задан, но неверен -> SecretKeyError, а не тихое отключение
    шифрования (иначе пароли сохранились бы в открытом виде).
    """

    monkeypatch.setenv(db._SECRET_KEY_ENV, "не-fernet-ключ")
    db._SECRET_CACHE.clear()
    assert db._secret_key_is_broken() is True
    with pytest.raises(db.SecretKeyError):
        db._get_fernet()
    with pytest.raises(db.SecretKeyError):
        db.insert_base("А", "https://broken.example/a", "u", "pw")
    # ничего не записано: подмена пароля пустым недопустима
    assert db.get_base("https://broken.example/a") is None
    db._SECRET_CACHE.clear()


def test_broken_secret_key_stops_import(clean_db, monkeypatch):
    """
    Импорт не должен частично записать базы с открытыми паролями,
    если ключ задан, но неверен.
    """

    monkeypatch.setenv(db._SECRET_KEY_ENV, "битый")
    db._SECRET_CACHE.clear()
    report = db.import_bases([
        {"name": "А", "url": "https://a.example/a", "login": "u", "password": "p"},
    ])
    assert report["added"] == 0
    assert report["errors"]
    assert db.get_base("https://a.example/a") is None
    db._SECRET_CACHE.clear()


def test_import_bases_dedup_and_report(clean_db):
    """
    Дубли по URL и внутри файла, и с уже существующей базой: без дублей
    в БД, всё попадает в отчёт. Запись без URL -> invalid.
    """

    db.insert_base("СТЕПП", "https://msk1.1cfresh.com/a/ea/3504241", "l", "p")
    entries = [
        {"name": "1;А", "url": "https://msk1.1cfresh.com/a/ea/1000001",
         "login": "u", "password": "p1"},
        {"name": "1;А (дубль в файле)",
         "url": "https://msk1.1cfresh.com/a/ea/1000001",
         "login": "u", "password": "p1"},
        {"name": "2;Б", "url": " https://msk1.1cfresh.com/a/ea/1000002/ ",
         "login": "u", "password": "p2"},
        {"name": "СТЕПП (уже есть)",
         "url": "https://msk1.1cfresh.com/a/ea/3504241",
         "login": "other", "password": "other"},
        {"name": "123", "url": "", "login": "", "password": ""},
    ]
    report = db.import_bases(entries)
    assert report["added"] == 2
    assert report["invalid"] == 1
    assert report["skipped"] == 2
    assert db.get_base("https://msk1.1cfresh.com/a/ea/1000001")["name"] == "1;А"
    # префиксы имён из файла сохраняются как есть
    assert db.get_base("https://msk1.1cfresh.com/a/ea/1000002")["name"] == "2;Б"
    # существующая база не перезаписана
    assert db.get_base("https://msk1.1cfresh.com/a/ea/3504241")["login"] == "l"


def test_import_bases_is_idempotent(clean_db):
    entries = [
        {"name": "1;А", "url": "https://msk1.1cfresh.com/a/ea/2000001",
         "login": "u", "password": "p1"},
    ]
    first = db.import_bases(entries)
    count_after_first = len(db.list_bases())
    second = db.import_bases(entries)
    assert first["added"] == 1
    assert second["added"] == 0
    assert second["skipped"] == 1
    assert len(db.list_bases()) == count_after_first


def test_import_encrypts_passwords_with_key(clean_db, monkeypatch):
    """С ключом пароль в БД лежит зашифрованным, наружу отдаётся открытым."""

    pytest.importorskip("cryptography")
    monkeypatch.setenv(db._SECRET_KEY_ENV, db.generate_secret_key())
    db._SECRET_CACHE.clear()
    base = db.insert_base("А", "https://enc.example/a", "u", "secret-pw")

    raw = db.get_base_by_id(base["id"])["password"]
    assert raw == "secret-pw"  # list_bases/get_base расшифровывают

    conn = sqlite3.connect(db._DB_PATH)
    stored = conn.execute(
        "SELECT password FROM bases WHERE id = ?", (base["id"],)
    ).fetchone()[0]
    conn.close()
    assert stored.startswith(db._ENC_PREFIX)
    assert "secret-pw" not in stored
    db._SECRET_CACHE.clear()


def test_load_client_databases_json_csv_and_bom(clean_db):
    """Формат: JSON-массив, JSON с обёрткой, CSV с русскими заголовками, BOM."""

    payload = [
        {"name": "1;А", "url": "https://msk1.1cfresh.com/a/ea/1000001",
         "login": "u", "password": "p1"},
        {"name": "no-url", "url": "", "login": "", "password": ""},
    ]
    text = json.dumps(payload, ensure_ascii=False)

    rows = db.load_client_databases(text)
    assert len(rows) == 2
    assert rows[0]["valid"] is True and rows[1]["valid"] is False

    # BOM (файл из Windows) не должен ломать разбор
    assert db.load_client_databases("﻿" + text)[0]["url"] == (
        "https://msk1.1cfresh.com/a/ea/1000001"
    )
    # и в виде байтов
    assert len(db.load_client_databases(("﻿" + text).encode("utf-8"))) == 2

    # обёртка {"databases": [...]}
    wrapped = json.dumps({"databases": payload}, ensure_ascii=False)
    assert len(db.load_client_databases(wrapped)) == 2

    # CSV с русскими заголовками
    csv_text = ("название,ссылка,логин,пароль\n"
                "1;А,https://msk1.1cfresh.com/a/ea/1000001,u,p1\n")
    csv_rows = db.load_client_databases(csv_text)
    assert len(csv_rows) == 1
    assert csv_rows[0]["name"] == "1;А"
    assert csv_rows[0]["url"] == "https://msk1.1cfresh.com/a/ea/1000001"
    assert csv_rows[0]["password"] == "p1"

    # неизвестный формат/пустой файл
    assert db.load_client_databases("") == []
    with pytest.raises(ValueError):
        db.load_client_databases("[{не json}]")
    with pytest.raises(ValueError):
        db.load_client_databases("колонка1,колонка2\n1,2\n")


def test_upsert_employee_idempotent_does_not_create_users(clean_db):
    """
    9 фамилий из docs/Список сотрудников.docx: повторный импорт не
    плодит дубли и не заводит учётные записи — users создаются отдельно.
    """

    surnames = ["Шмаракова", "Османова", "Зверева", "Сагирова", "Кузьменко",
                "Реутова", "Архипова", "Радаева", "Яблонцева"]
    for name in surnames:
        db.upsert_employee(full_name=name, role="бухгалтер")
    assert len(db.list_employees()) == len(surnames)

    for name in surnames:  # второй проход
        db.upsert_employee(full_name=name, role="бухгалтер")
    assert len(db.list_employees()) == len(surnames)
    assert {e["full_name"] for e in db.list_employees()} == set(surnames)

    # сотрудники не равны учётным записям
    linked = [u for u in db.list_users() if u.get("employee_full_name")]
    assert linked == []

# ============ СКИМ-ДРИФТ: init_db() МОЖЕТ ТОЛЬКО ДОБАВЛЯТЬ ================

def _schema_snapshot(path: str) -> dict:
    """{таблица: {столбец: (type, notnull, default_value, pk)}}."""
    conn = sqlite3.connect(path)
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            table: {
                row[1]: (row[2], row[3], row[4], row[5])
                for row in conn.execute(f'PRAGMA table_info("{table}")')
            }
            for table in tables
        }
    finally:
        conn.close()


def test_init_db_is_additive_only(monkeypatch):
    """init_db() имеет право только ДОБАВЛЯТЬ столбцы.

    Обобщает test_users_employee_full_name_additive_migration и
    test_base_active_doc_types_additive_migration на все таблицы сразу:
    после многократных запусков ни одна таблица не исчезает и ни один
    столбец не удаляется и не меняет type/NOT NULL/default/pk. Отдельно
    проверяется идемпотентность: на 2-м и 3-м запуске схема уже не растёт.
    """
    tmp = tempfile.mkdtemp(prefix="schema_drift_")
    path = os.path.join(tmp, "drift.db")
    monkeypatch.setattr(db, "_DB_PATH", path)

    db.init_db()
    before = _schema_snapshot(path)
    assert {"users", "bases", "norms", "employees"} <= set(before), (
        f"ожидались все таблицы, есть: {sorted(before)}"
    )

    db.init_db()
    second = _schema_snapshot(path)
    db.init_db()
    third = _schema_snapshot(path)

    # 1) Ничего не удалено и не изменено — только аддитивность.
    for table, cols in before.items():
        assert table in second, f"таблица {table} исчезла при миграции"
        for name, meta in cols.items():
            assert name in second[table], f"столбец {table}.{name} удалён"
            assert second[table][name] == meta, (
                f"{table}.{name} изменён: {meta} -> {second[table][name]}"
            )
    # 2) Идемпотентность: столбцы не добавляются на каждом запуске.
    assert second == third, "миграции не идемпотентны: схема продолжает расти"


def test_init_db_preserves_legacy_norms_data(monkeypatch):
    """Старая таблица norms доращивается, а её данные не трогаются."""
    tmp = tempfile.mkdtemp(prefix="norms_legacy_")
    path = os.path.join(tmp, "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE norms ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "doc_type TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL, "
        "entity TEXT NOT NULL, unit TEXT, norm_min REAL NOT NULL, "
        "norm_hours REAL NOT NULL, coeff REAL NOT NULL DEFAULT 1.00, "
        "sno TEXT, date_from TEXT, date_to TEXT, comment TEXT, "
        "sort_order INTEGER NOT NULL DEFAULT 0, "
        "active INTEGER NOT NULL DEFAULT 1, UNIQUE (doc_type))"
    )
    conn.execute(
        "INSERT INTO norms (doc_type, category, title, entity, "
        "norm_min, norm_hours, coeff) "
        "VALUES ('bank_incoming', 'Банк', 'Поступление', 'Document_X', "
        "60.0, 1.0, 1.25)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "_DB_PATH", path)
    db.init_db()

    added = ("is_discovered", "discovered_at", "has_responsible_key",
             "has_author_key", "has_operation_type", "variant_rules_json",
             "ref_base")
    snap = _schema_snapshot(path)["norms"]
    for column in added:
        assert column in snap, f"миграция не добавила столбец {column}"

    norm = db.get_norm(doc_type="bank_incoming")
    assert norm["title"] == "Поступление"
    assert norm["norm_hours"] == 1.0
    assert norm["coeff"] == 1.25
    assert norm["is_discovered"] is False
    assert norm["discovered_at"] is None
    assert norm["ref_base"] is None


def test_norm_row_projection_exposes_discovery_columns(clean_db):
    """get_norm/list_norms отдают все столбцы проекции согласованно.

    Регрессия на позиционный сдвиг: раньше _norm_row собирал словарь по
    row[0]..row[14], и любое добавление столбца в SELECT молча смещало бы
    соответствие (тот же класс ошибки, что при удалении bases.group).
    """
    db.insert_discovered_norm(
        doc_type="projection_check", category="К", title="Проверка",
        entity="Document_Проверка", ref_base="https://donor/1",
        has_author_key=True,
    )
    from_db = db.get_norm(doc_type="projection_check")
    from_list = next(
        n for n in db.list_norms(active_only=False)
        if n["doc_type"] == "projection_check"
    )

    assert set(from_db) == set(db._NORMS_COLUMNS) == set(from_list)
    for column in db._NORMS_COLUMNS:
        assert from_db[column] == from_list[column], (
            f"расхождение в столбце {column}: "
            f"{from_db[column]!r} != {from_list[column]!r}"
        )
    # Ключевые значения не сместились на соседние столбцы.
    assert from_db["title"] == "Проверка"
    assert from_db["ref_base"] == "https://donor/1"
    assert from_db["has_author_key"] is True
    assert from_db["has_responsible_key"] is False
    assert from_db["is_discovered"] is True
    assert from_db["active"] is True
    assert isinstance(from_db["norm_hours"], float)


# ============ insert_discovered_norm(): «ПРОПУСТИТЬ, ЕСТЬ» ================

def test_insert_discovered_norm_creates_net_new(clean_db):
    created = db.insert_discovered_norm(
        doc_type="brand_new", category="Категория", title="Новый вид",
        entity="Document_Новый", ref_base="https://donor/a/1",
        has_responsible_key=True, has_operation_type=True,
    )
    assert created is True

    norm = db.get_norm(doc_type="brand_new")
    assert norm["title"] == "Новый вид"
    assert norm["entity"] == "Document_Новый"
    assert norm["ref_base"] == "https://donor/a/1"
    assert norm["is_discovered"] is True
    assert norm["has_responsible_key"] is True
    assert norm["has_author_key"] is False
    assert norm["has_operation_type"] is True
    # Нет нормы -> 0.0: find_missing_norms() считает нулевую норму «не заданной».
    assert norm["norm_hours"] == 0.0
    assert norm["norm_min"] == 0.0
    assert norm["active"] is True
    assert norm["variant_rules_json"] == "{}"
    # UTC ISO8601
    assert norm["discovered_at"].endswith("+00:00")


def test_insert_discovered_norm_skips_existing_preserving_admin_values(clean_db):
    """Главный инвариант: сканер НИКОГДА не перезаписывает админские значения.

    У insert_discovered_norm намеренно НЕТ параметров norm_hours/norm_min/coeff:
    сканер не имеет права приносить значения норм. Даже при существующей строке
    (которую upsert_norm перезаписал бы целиком) вставка пропускается.
    """
    db.upsert_norm(
        doc_type="keeper", category="К", title="Админский заголовок",
        entity="Document_Keep", norm_hours=1.5, coeff=2.0,
        comment="настроен вручную",
    )
    assert "norm_hours" not in db.insert_discovered_norm.__code__.co_varnames

    created = db.insert_discovered_norm(
        doc_type="keeper", category="Чужая категория",
        title="Заголовок сканера", entity="Document_Keep",
        ref_base="https://donor/other",
    )

    assert created is False, "существующий doc_type должен быть пропущен"
    norm = db.get_norm(doc_type="keeper")
    assert norm["title"] == "Админский заголовок"
    assert norm["category"] == "К"
    assert norm["norm_hours"] == 1.5
    assert norm["coeff"] == 2.0
    assert norm["comment"] == "настроен вручную"
    # ref_base не переписан: приоритет у первого обнаружившего донора.
    assert norm["ref_base"] is None
    assert norm["is_discovered"] is False


def test_insert_discovered_norm_empty_title_falls_back_to_doc_type(clean_db):
    """Пустой title подменяется на doc_type.

    find_missing_norms() сопоставляет norm['title'] с полем «Вид документа»,
    поэтому пустой заголовок сделал бы строку невидимой для предупреждения.
    """
    db.insert_discovered_norm(doc_type="title_missing", title="   ")
    assert db.get_norm(doc_type="title_missing")["title"] == "title_missing"

    db.insert_discovered_norm(doc_type="title_missing2")
    assert db.get_norm(doc_type="title_missing2")["title"] == "title_missing2"


def test_insert_discovered_norm_requires_doc_type(clean_db):
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            db.insert_discovered_norm(doc_type=bad)


def test_insert_discovered_norm_rejects_malformed_json(clean_db):
    """Битый variant_rules_json не должен доходить до СУБД."""
    for bad in ('{"broken"', '"строка"', "123", "{oops}"):
        with pytest.raises(ValueError):
            db.insert_discovered_norm(doc_type="bad_json", variant_rules_json=bad)
    assert db.get_norm(doc_type="bad_json") is None

    # Валидный JSON принимается и сохраняется как есть.
    db.insert_discovered_norm(
        doc_type="good_json", variant_rules_json='{"terms": ["x"], "to": "y"}'
    )
    saved = db.get_norm(doc_type="good_json")["variant_rules_json"]
    assert json.loads(saved) == {"terms": ["x"], "to": "y"}


def test_insert_discovered_norm_is_idempotent(clean_db):
    """Повторный вызов не создаёт второй строки и не трогает первую."""
    first = db.insert_discovered_norm(doc_type="once", ref_base="https://donor/1")
    second = db.insert_discovered_norm(doc_type="once", ref_base="https://donor/2")
    assert first is True
    assert second is False

    rows = [n for n in db.list_norms(active_only=False) if n["doc_type"] == "once"]
    assert len(rows) == 1
    assert rows[0]["ref_base"] == "https://donor/1"
