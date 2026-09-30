import pandas as pd
import pytest

from core import db
from core.calculator import (
    build_report,
    build_employee_load,
    find_missing_norms,
    summarize_totals,
)


@pytest.fixture
def with_norms_and_employees():
    db.init_db()
    db.upsert_norm(doc_type="bank_incoming", category="Банк и касса",
                   title="Поступление на расчетный счет",
                   entity="Document_ПоступлениеНаРасчетныйСчет",
                   norm_hours=0.040, coeff=1.00)
    db.upsert_norm(doc_type="advance_report", category="Первичные документы",
                   title="Авансовый отчет",
                   entity="Document_АвансовыйОтчет",
                   norm_hours=0.250, coeff=1.00)
    db.upsert_employee(user_1c="Иванова А.А.", full_name="Иванова Анна",
                       role="Бухгалтер", hours_per_month=130.0)
    yield


def _df(rows):
    return pd.DataFrame(rows, columns=[
        "Клиент", "ИНН", "Система налогообложения", "Период", "Вид документа",
        "Вид операции", "Количество операций", "Ответственный сотрудник",
        "Роль сотрудника", "Норма на операцию", "Коэффициент сложности",
        "Трудозатраты, нормочасы", "Комментарий",
    ])


def _row(**overrides):
    base = {
        "Клиент": "ООО Альфа", "ИНН": "123", "Система налогообложения": "",
        "Период": "01.2026", "Вид документа": "Поступление на расчетный счет",
        "Вид операции": "Оплата от покупателя", "Количество операций": 1,
        "Ответственный сотрудник": "Иванова А.А.", "Роль сотрудника": "",
        "Норма на операцию": None, "Коэффициент сложности": None,
        "Трудозатраты, нормочасы": None, "Комментарий": "",
    }
    base.update(overrides)
    return base


def test_build_report_sums_hours(with_norms_and_employees):
    raw = _df([
        _row(),
        _row(),
        _row(**{"Вид документа": "Авансовый отчет", "Вид операции": "До 10 чеков",
                "Ответственный сотрудник": "Петров П.П."}),
    ])

    report = build_report(raw)
    # 2 × поступление (0,04) + 1 × авансовый отчёт (0,25) = 0,33
    assert report["Трудозатраты, нормочасы"].sum() == pytest.approx(0.33)

    # Иванова — распределена, Петров — «Не распределено»
    grouped = report.groupby("Ответственный сотрудник",
                             dropna=False).size().to_dict()
    assert "Иванова Анна" in grouped
    assert "Не распределено" in grouped

    totals = summarize_totals(report)
    assert totals["total_operations"] == 3
    assert totals["total_hours"] == pytest.approx(0.33)
    assert totals["by_client"]["ООО Альфа"]["operations"] == 3


def test_report_keeps_sno_and_inn_columns(with_norms_and_employees):
    raw = _df([
        _row(**{"Система налогообложения": "УСН «Доходы»", "ИНН": "7700000001"}),
        _row(**{
            "Система налогообложения": "УСН «Доходы»", "ИНН": "7700000001",
            "Вид документа": "Авансовый отчет", "Вид операции": "До 10 чеков",
            "Ответственный сотрудник": "Петров П.П.",
        }),
    ])
    report = build_report(raw)
    assert "Система налогообложения" in report.columns
    assert "ИНН" in report.columns
    assert "Период" in report.columns
    assert set(report["Система налогообложения"]) == {"УСН «Доходы»"}
    assert set(report["ИНН"]) == {"7700000001"}


def test_grouping_by_sno(with_norms_and_employees):
    raw = _df([
        _row(**{"Система налогообложения": "УСН «Доходы»"}),
        _row(**{
            "Система налогообложения": "УСН «Доходы минус расходы»",
            "Вид документа": "Авансовый отчет", "Вид операции": "До 10 чеков",
            "Ответственный сотрудник": "Петров П.П.",
        }),
    ])
    report = build_report(raw, grouping=["Клиент", "Система налогообложения"])
    assert report["Система налогообложения"].nunique() == 2
    # Сгруппировано по Клиент + СНО: детальные колонки схлопываются,
    # остаются ключи группировки + агрегаты
    for col in ("Клиент", "Система налогообложения", "ИНН", "Период",
                "Количество операций", "Роль сотрудника", "Норма на операцию",
                "Коэффициент сложности", "Трудозатраты, нормочасы",
                "Комментарий"):
        assert col in report.columns
    assert len(report) == 2


def test_empty_report(with_norms_and_employees):
    empty = pd.DataFrame(columns=[
        "Клиент", "ИНН", "Система налогообложения", "Период", "Вид документа",
        "Вид операции", "Количество операций", "Ответственный сотрудник",
        "Роль сотрудника", "Норма на операцию", "Коэффициент сложности",
        "Трудозатраты, нормочасы", "Комментарий",
    ])
    report = build_report(empty)
    assert report.empty
    totals = summarize_totals(report)
    assert totals["total_operations"] == 0
    assert totals["total_hours"] == 0.0


def test_detail_grouping_empty_keeps_rows(with_norms_and_employees):
    raw = _df([
        _row(),
        _row(),
        _row(**{"Вид документа": "Авансовый отчет"}),
    ])
    detail = build_report(raw, grouping=[])
    assert len(detail) == len(raw)
    assert list(detail.columns) == [
        "Клиент", "ИНН", "Система налогообложения", "Период", "Вид документа",
        "Вид операции", "Количество операций", "Ответственный сотрудник",
        "Роль сотрудника", "Норма на операцию", "Коэффициент сложности",
        "Трудозатраты, нормочасы", "Комментарий",
    ]


def test_employee_load(with_norms_and_employees):
    raw = _df([
        _row(**{"Система налогообложения": "УСН «Доходы»"}),
    ])
    report = build_report(raw)
    load = build_employee_load(report)
    row = load.iloc[0]
    assert row["Сотрудник"] == "Иванова Анна"
    assert row["Нормочасы"] == pytest.approx(0.04)
    expected = round(0.04 / 130.0 * 100.0, 2)
    assert row["Доля загрузки, %"] == pytest.approx(expected)


def test_variant_norms_match_by_title(with_norms_and_employees):
    db.upsert_norm(
        doc_type="advance_report", category="Первичные документы",
        title="Авансовый отчет до 10 чеков",
        entity="Document_АвансовыйОтчет", norm_hours=0.250, coeff=1.00,
    )
    db.upsert_norm(
        doc_type="advance_report_travel", category="Первичные документы",
        title="Авансовый отчет до 10 чеков с ГСМ или командировкой",
        entity="Document_АвансовыйОтчет", norm_hours=0.375, coeff=1.00,
    )
    raw = _df([
        _row(**{
            "Вид документа": "Авансовый отчет до 10 чеков с ГСМ или командировкой",
            "Вид операции": "Командировочные расходы",
            "Ответственный сотрудник": "Петров П.П.",
        }),
        _row(**{
            "Вид документа": "Авансовый отчет до 10 чеков",
            "Вид операции": "Прочие расходы",
            "Ответственный сотрудник": "Петров П.П.",
        }),
    ])
    report = build_report(raw)
    # 0,375 + 0,25 — нормы двух вариантов по title
    assert report["Трудозатраты, нормочасы"].sum() == pytest.approx(0.625)


def test_default_sort_order_tz_9_2(with_norms_and_employees):
    """Клиент ▲, Вид документа ▲, операций ▼, трудозатраты ▼ (ТЗ §9.2)."""
    raw = _df([
        _row(**{"Клиент": "ООО Бета", "Вид документа": "Авансовый отчет",
                "Количество операций": 3, "Трудозатраты, нормочасы": 0.30}),
        _row(**{"Клиент": "ООО Альфа", "Вид документа": "Поступление",
                "Количество операций": 1, "Трудозатраты, нормочасы": 0.01}),
        _row(**{"Клиент": "ООО Альфа", "Вид документа": "Авансовый отчет",
                "Количество операций": 5, "Трудозатраты, нормочасы": 0.50}),
        _row(**{"Клиент": "ООО Альфа", "Вид документа": "Авансовый отчет",
                "Количество операций": 5, "Трудозатраты, нормочасы": 0.50}),
    ])
    report = build_report(raw)
    assert list(report["Клиент"]) == [
        "ООО Альфа", "ООО Альфа", "ООО Бета",
    ]
    # Внутри клиента — вид документа по алфавиту
    assert list(report["Вид документа"]) == [
        "Авансовый отчет", "Поступление", "Авансовый отчет",
    ]
    # Одинаковые строки схлопнулись группировкой: 5 + 5 = 10 операций
    assert list(report["Количество операций"]) == [10, 1, 3]

    # Без группировки по виду документа трудозатраты идут по убыванию
    by_client = build_report(raw, grouping=["Клиент"])
    assert list(by_client["Клиент"]) == ["ООО Альфа", "ООО Бета"]
    alpha_hours = list(
        by_client["Трудозатраты, нормочасы"].where(
            by_client["Клиент"] == "ООО Альфа"
        ).dropna()
    )
    assert alpha_hours == sorted(alpha_hours, reverse=True)

# ================== ПРЕДУПРЕЖДЕНИЕ О НЕЗАДАННЫХ НОРМАХ (ТЗ §4.1) ==========

def test_find_missing_norms_detects_unknown_title(with_norms_and_employees):
    raw = _df([
        _row(),
        _row(**{"Вид документа": "Счёт покупателю"}),
        _row(**{"Вид документа": "Счёт покупателю"}),
    ])
    # «Поступление на расчетный счет» нормировано, «Счёт покупателю» — нет
    assert find_missing_norms(raw) == ["Счёт покупателю"]


def test_find_missing_norms_detects_zero_norm(with_norms_and_employees):
    """Ноль в DEFAULT_NORMS_HOURS — это заглушка «норма не задана»."""
    db.upsert_norm(
        doc_type="return_customer", category="Первичные документы",
        title="Возврат товаров от покупателя",
        entity="Document_ВозвратТоваровОтПокупателя", norm_hours=0.0,
    )
    raw = _df([
        _row(),
        _row(**{"Вид документа": "Возврат товаров от покупателя"}),
    ])
    assert find_missing_norms(raw) == ["Возврат товаров от покупателя"]


def test_find_missing_norms_detects_inactive_norm(with_norms_and_employees):
    db.upsert_norm(
        doc_type="payment_order", category="Банк и касса",
        title="Платежное поручение", entity="Document_ПлатежноеПоручение",
        norm_hours=0.06, active=False,
    )
    raw = _df([_row(**{"Вид документа": "Платежное поручение"})])
    # Выключенная администратором норма не участвует в расчёте
    assert find_missing_norms(raw) == ["Платежное поручение"]


def test_find_missing_norms_empty_when_all_normed(with_norms_and_employees):
    raw = _df([
        _row(),
        _row(**{"Вид документа": "Авансовый отчет"}),
    ])
    assert find_missing_norms(raw) == []


def test_find_missing_norms_sorted_unique_and_tolerates_empty(with_norms_and_employees):
    raw = _df([
        _row(**{"Вид документа": "Ящик"}),
        _row(**{"Вид документа": "Акт"}),
        _row(**{"Вид документа": "Акт"}),
    ])
    assert find_missing_norms(raw) == ["Акт", "Ящик"]
    assert find_missing_norms(pd.DataFrame()) == []
    assert find_missing_norms(None) == []


def test_find_missing_norms_works_on_aggregated_report(with_norms_and_employees):
    """Агрегация не теряет вид документа — список остаётся тем же."""
    raw = _df([_row(), _row(**{"Вид документа": "Счёт покупателю"})])
    assert find_missing_norms(build_report(raw)) == ["Счёт покупателю"]
