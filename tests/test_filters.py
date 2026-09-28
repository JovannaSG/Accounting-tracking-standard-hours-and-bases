import pandas as pd

from app.ui import apply_filters


def _df():
    rows = [
        {
            "Клиент": "ООО Альфа", "ИНН": "7700000001",
            "Система налогообложения": "УСН «Доходы»", "Период": "01.2026",
            "Вид документа": "Поступление на расчетный счет",
            "Вид операции": "Оплата от покупателя", "Количество операций": 2,
            "Ответственный сотрудник": "Иванова Анна",
            "Роль сотрудника": "Бухгалтер", "Норма на операцию": 0.04,
            "Коэффициент сложности": 1.0, "Трудозатраты, нормочасы": 0.08,
            "Комментарий": "",
        },
        {
            "Клиент": "ООО Бета", "ИНН": "7700000002",
            "Система налогообложения": "ОСН", "Период": "01.2026",
            "Вид документа": "Авансовый отчет", "Вид операции": "До 10 чеков",
            "Количество операций": 1,
            "Ответственный сотрудник": "Не распределено",
            "Роль сотрудника": "Не распределено", "Норма на операцию": 0.25,
            "Коэффициент сложности": 1.0, "Трудозатраты, нормочасы": 0.25,
            "Комментарий": "",
        },
    ]
    return pd.DataFrame(rows)


def test_no_filters_returns_copy():
    df = _df()
    out = apply_filters(df, {})
    assert len(out) == 2
    assert out is not df


def test_client_filter():
    out = apply_filters(_df(), {"client": ["ООО Альфа"]})
    assert len(out) == 1
    assert out.iloc[0]["Клиент"] == "ООО Альфа"


def test_inn_substring():
    assert len(apply_filters(_df(), {"inn": "770000000"})) == 2
    assert len(apply_filters(_df(), {"inn": "7700000002"})) == 1


def test_sno_filter():
    out = apply_filters(_df(), {"sno": ["ОСН"]})
    assert len(out) == 1
    assert out.iloc[0]["Клиент"] == "ООО Бета"


def test_employee_filter():
    assert len(apply_filters(_df(), {"employee": ["Иванова Анна"]})) == 1


def test_doc_and_op_type_filters():
    assert len(apply_filters(_df(), {"doc_type": ["Авансовый отчет"]})) == 1
    assert len(apply_filters(_df(), {"op_type": ["Оплата от покупателя"]})) == 1


def test_unassigned_only():
    out = apply_filters(_df(), {"unassigned_only": True})
    assert len(out) == 1
    assert out.iloc[0]["Ответственный сотрудник"] == "Не распределено"


def test_min_qty_and_hours():
    assert len(apply_filters(_df(), {"min_qty": 2})) == 1
    assert len(apply_filters(_df(), {"min_hours": 0.05})) == 2
    assert len(apply_filters(_df(), {"min_hours": 0.09})) == 1


def test_empty_report():
    out = apply_filters(pd.DataFrame(), {"client": ["x"]})
    assert out.empty