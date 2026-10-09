import pandas as pd
import pytest

from core import db
from core.employees import (
    employee_scope_names,
    filter_by_employee,
    linked_employee,
    resolve_employee_smart,
    unmapped_users,
)


@pytest.fixture
def clean_db():
    """Пустая тестовая БД (conftest чистит файл между тестами)."""
    db.init_db()
    yield
    db.init_db()


def test_linked_employee(clean_db):
    db.upsert_employee(full_name="Иванова Анна", user_1c="Иванова А.А.")
    db.upsert_user(
        "acc", "accountant", "h", [],
        employee_full_name="Иванова Анна",
    )
    emp = linked_employee("acc")
    assert emp is not None
    assert emp["full_name"] == "Иванова Анна"
    assert linked_employee("ghost") is None


def test_employee_scope_names_include_alias(clean_db):
    db.upsert_employee(full_name="Иванова Анна", user_1c="Иванова А.А.")
    scope = employee_scope_names("Иванова Анна")
    assert scope == {"Иванова Анна", "Иванова А.А."}


def test_filter_by_employee_matches_full_name_and_alias(clean_db):
    db.upsert_employee(full_name="Иванова Анна", user_1c="Иванова А.А.")
    df = pd.DataFrame({
        "Клиент": ["ООО Альфа", "ООО Бета", "ООО Альфа"],
        "Ответственный сотрудник": ["Иванова Анна", "Петров П.П.", "Иванова А.А."],
        "Количество операций": [1, 1, 1],
    })
    out = filter_by_employee(df, "Иванова Анна")
    assert list(out["Клиент"]) == ["ООО Альфа", "ООО Альфа"]


def test_filter_without_binding_returns_empty_schema(clean_db):
    df = pd.DataFrame({
        "Клиент": ["ООО Альфа"],
        "Ответственный сотрудник": ["Иванова Анна"],
        "Количество операций": [1],
    })
    out = filter_by_employee(df, None)
    assert out.empty
    assert list(out.columns) == list(df.columns)


def test_filter_ignores_other_employees_rows(clean_db):
    db.upsert_employee(full_name="Петров Пётр")
    df = pd.DataFrame({
        "Ответственный сотрудник": ["Петров П.П."],
        "Количество операций": [7],
    })
    out = filter_by_employee(df, "Петров Пётр")
    assert out.empty

# ================= КАСКАДНЫЙ РЕЗОЛВЕР (Smart Fallback) =====================

def test_smart_resolve_exact_match():
    """Точное совпадение по ФИО и по алиасу user_1c; пустой ввод — None."""
    emps = [
        {"id": 1, "full_name": "Иванова Анна", "user_1c": "Иванова А.А."},
        {"id": 2, "full_name": "Петров Пётр", "user_1c": None},
    ]
    assert resolve_employee_smart("Иванова Анна", emps)["id"] == 1
    assert resolve_employee_smart("Иванова А.А.", emps)["id"] == 1
    assert resolve_employee_smart("", emps) is None
    assert resolve_employee_smart(None, emps) is None


def test_smart_resolve_substring_match():
    """Правило A: «Yablontseva» находит «Elena Yablontseva»."""
    emps = [{"id": 1, "full_name": "Elena Yablontseva", "user_1c": None}]
    assert resolve_employee_smart("Yablontseva", emps)["id"] == 1
    assert resolve_employee_smart("Elena", emps)["id"] == 1


def test_smart_resolve_collision_returns_none():
    """Collision Guard: неоднозначная фамилия -> None (в «Не распределено»)."""
    emps = [
        {"id": 1, "full_name": "Ivanov Petr", "user_1c": None},
        {"id": 2, "full_name": "Ivanov Ivan", "user_1c": None},
    ]
    assert resolve_employee_smart("Ivanov", emps) is None


def test_smart_resolve_case_insensitive():
    emps = [{"id": 1, "full_name": "Иванова Анна", "user_1c": None}]
    assert resolve_employee_smart("иванова анна", emps)["id"] == 1
    assert resolve_employee_smart("ИВАНОВА АННА", emps)["id"] == 1
    assert resolve_employee_smart("  Иванова Анна  ", emps)["id"] == 1


def test_smart_resolve_surname_token():
    """Правило B: перестановка слов ловится по токену-фамилии."""
    emps = [{"id": 1, "full_name": "Yablontseva Elena", "user_1c": None}]
    # Цельной подстроки нет — срабатывает именно токен, а не правило A.
    assert resolve_employee_smart("Elena Yablontseva", emps)["id"] == 1


def test_unmapped_users_uses_smart_resolution(clean_db):
    """Список «Не распределены» согласован с каскадным матчингом отчёта."""
    db.upsert_employee(full_name="Elena Yablontseva", user_1c=None)
    assert unmapped_users(
        ["Elena Yablontseva", "Yablontseva", "Unknown X"]
    ) == ["Unknown X"]


def test_smart_resolve_common_first_name_does_not_collide():
    """Общее имя («Elena») не должно конфликтовать с чужой фамилией.

    Правило B требует присутствия ВСЕХ значимых токенов БД: у «Ivanova
    Elena» в строке 1С нет «ivanova», поэтому она не кандидат, и коллизия
    не возникает — «Elena Yablontseva» сопоставляется без алиаса user_1c.
    """
    emps = [
        {"id": 1, "full_name": "Yablontseva Elena", "user_1c": None},
        {"id": 2, "full_name": "Ivanova Elena", "user_1c": None},
    ]
    assert resolve_employee_smart("Elena Yablontseva", emps)["id"] == 1
