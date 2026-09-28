import pandas as pd
import pytest

from core import db
from core.employees import (
    employee_scope_names,
    filter_by_employee,
    linked_employee,
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