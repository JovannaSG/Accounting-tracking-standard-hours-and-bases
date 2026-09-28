import io

import pandas as pd
import pytest
from openpyxl import load_workbook

from core.reporters import export_excel

_COLS = [
    "Клиент", "ИНН", "Система налогообложения", "Период", "Вид документа",
    "Вид операции", "Количество операций", "Ответственный сотрудник",
    "Роль сотрудника", "Норма на операцию", "Коэффициент сложности",
    "Трудозатраты, нормочасы", "Комментарий",
]


def _row(client, inn, ops, hours, employee="Иванова А.А.", **kw):
    base = {
        "Клиент": client, "ИНН": inn, "Система налогообложения": "УСН «Доходы»",
        "Период": "01.2026", "Вид документа": "Поступление на расчетный счет",
        "Вид операции": "Оплата от покупателя", "Количество операций": ops,
        "Ответственный сотрудник": employee, "Роль сотрудника": "Бухгалтер",
        "Норма на операцию": 0.04, "Коэффициент сложности": 1.0,
        "Трудозатраты, нормочасы": hours, "Комментарий": "",
    }
    base.update(kw)
    return base


def _df(rows):
    return pd.DataFrame(rows, columns=_COLS)


def _col_idx(ws, name):
    for cell in ws[1]:
        if cell.value == name:
            return cell.column
    return None


@pytest.fixture
def report_fixture():
    detail = _df([
        _row("ООО Альфа", "7701", 30, 1.20),
        _row("ООО Альфа", "7701", 10, 0.40),
        _row("ООО Альфа", "7701", 12, 0.48),
        _row("ООО Бета", "7702", 8, 0.32),
        _row("ООО Бета", "7702", 2, 0.08),
    ])
    emp_load = pd.DataFrame([
        {"Сотрудник": "Иванова А.А.", "Клиент": "ООО Альфа",
         "Количество операций": 52, "Нормочасы": 2.08, "Доля загрузки, %": 1.6},
        {"Сотрудник": "Петров П.П.", "Клиент": "ООО Бета",
         "Количество операций": 10, "Нормочасы": 0.40, "Доля загрузки, %": 0.3},
    ])
    totals = {
        "total_operations": 62,
        "total_hours": 2.48,
        "by_client": {
            "ООО Альфа": {"operations": 52, "hours": 2.08},
            "ООО Бета": {"operations": 10, "hours": 0.40},
        },
        "by_employee": {
            "Иванова А.А.": {"operations": 52, "hours": 2.08},
            "Петров П.П.": {"operations": 10, "hours": 0.40},
        },
    }
    return detail, emp_load, totals


def _workbook(excel_bytes):
    return load_workbook(io.BytesIO(excel_bytes))


def test_sheets_present(report_fixture):
    detail, emp_load, totals = report_fixture
    wb = _workbook(export_excel(detail, detail, emp_load, totals))
    assert wb.sheetnames == ["Отчёт", "Загрузка сотрудников", "Сводка по клиентам"]


def test_report_sheet_client_subtotals(report_fixture):
    detail, emp_load, totals = report_fixture
    wb = _workbook(export_excel(detail, detail, emp_load, totals))
    ws = wb["Отчёт"]
    ops_col = _col_idx(ws, "Количество операций")
    hours_col = _col_idx(ws, "Трудозатраты, нормочасы")

    found = {}
    for row in ws.iter_rows():
        if row[0].value and str(row[0].value).startswith("Итого по "):
            found[row[0].value] = row
    alfa = found["Итого по ООО Альфа"]
    beta = found["Итого по ООО Бета"]

    assert alfa[ops_col - 1].value == 52
    assert alfa[hours_col - 1].value == pytest.approx(2.08)
    assert beta[ops_col - 1].value == 10
    assert beta[hours_col - 1].value == pytest.approx(0.40)


def test_report_sheet_global_total(report_fixture):
    detail, emp_load, totals = report_fixture
    wb = _workbook(export_excel(detail, detail, emp_load, totals))
    ws = wb["Отчёт"]
    ops_col = _col_idx(ws, "Количество операций")
    hours_col = _col_idx(ws, "Трудозатраты, нормочасы")

    global_row = None
    for row in ws.iter_rows():
        if row[0].value == "Итого по всем клиентам":
            global_row = row
            break
    assert global_row is not None
    assert global_row[ops_col - 1].value == totals["total_operations"]
    assert global_row[hours_col - 1].value == pytest.approx(totals["total_hours"])
    assert global_row[hours_col - 1].number_format == "0.000"


def test_no_client_column_keeps_only_global_total(report_fixture):
    detail, emp_load, totals = report_fixture
    # Группировка, в которой «Клиента» нет (например, по СНО)
    grouped = detail.drop(columns=["Клиент"])
    wb = _workbook(export_excel(grouped, detail, emp_load, totals))
    ws = wb["Отчёт"]
    labels = [
        str(row[0].value) for row in ws.iter_rows()
        if row[0].value is not None
    ]
    assert not any(l.startswith("Итого по ") for l in labels)
    assert "Итого по всем клиентам" not in labels
    assert "Итого" in labels


def test_client_summary_sheet(report_fixture):
    detail, emp_load, totals = report_fixture
    wb = _workbook(export_excel(detail, detail, emp_load, totals))
    ws = wb["Сводка по клиентам"]

    assert ws.max_row == 3
    clients = [ws.cell(row=r, column=1).value for r in range(2, ws.max_row + 1)]
    assert clients == ["ООО Альфа", "ООО Бета"]  # по убыванию нормочасов
    assert ws.cell(row=2, column=2).value == "7701"

    hours_col = _col_idx(ws, "Нормочасы")
    assert ws.cell(row=2, column=hours_col).value == pytest.approx(2.08)
    assert ws.cell(row=2, column=hours_col).number_format == "0.000"

    share_col = _col_idx(ws, "Доля от итога, %")
    shares = [ws.cell(row=r, column=share_col).value for r in range(2, ws.max_row + 1)]
    assert sum(shares) == pytest.approx(100.0, abs=0.1)
    assert shares[0] == pytest.approx(2.08 / 2.48 * 100.0, abs=0.01)


def test_employee_sheet_subtotals(report_fixture):
    detail, emp_load, totals = report_fixture
    wb = _workbook(export_excel(detail, detail, emp_load, totals))
    ws = wb["Загрузка сотрудников"]
    ops_col = _col_idx(ws, "Количество операций")
    hours_col = _col_idx(ws, "Нормочасы")

    found = {}
    for row in ws.iter_rows():
        label = str(row[0].value or "")
        if label.startswith("Итого ") or label == "Всего":
            found[label] = row

    ivanova = found["Итого Иванова А.А."]
    assert ivanova[ops_col - 1].value == 52
    assert ivanova[hours_col - 1].value == pytest.approx(2.08)

    total_row = found["Всего"]
    assert total_row[ops_col - 1].value == totals["total_operations"]
    assert total_row[hours_col - 1].value == pytest.approx(totals["total_hours"])


def test_header_style_and_freeze(report_fixture):
    detail, emp_load, totals = report_fixture
    wb = _workbook(export_excel(detail, detail, emp_load, totals))
    ws = wb["Отчёт"]
    assert ws["A1"].font.bold is True
    assert ws.freeze_panes == "A2"


def test_empty_report_still_builds_workbook():
    empty = pd.DataFrame(columns=_COLS)
    totals = {"total_operations": 0, "total_hours": 0.0}
    wb = _workbook(export_excel(empty, empty, empty, totals))
    assert wb.sheetnames == ["Отчёт", "Загрузка сотрудников", "Сводка по клиентам"]

    ws = wb["Отчёт"]
    assert ws["A1"].value == "Клиент"
    assert ws["A1"].font.bold is True
    summary = wb["Сводка по клиентам"]
    assert summary["A1"].value == "Клиент"


def test_detail_not_aggregated_in_report_sheet(report_fixture):
    """«Отчёт» пишется как на экране: колонки детального отчёта остаются
    колонками Excel, а не «склеиваются» подытогами в pandas."""
    detail, emp_load, totals = report_fixture
    wb = _workbook(export_excel(detail, detail, emp_load, totals))
    ws = wb["Отчёт"]
    # после 5 строк данных и 2 подытогов максимум строк = 1 + 5 + 2 (+итог)
    # подытоги по клиентам должны лежать между данными по порядку клиентов
    first_client = [ws.cell(row=r, column=1).value for r in range(2, 7)]
    assert first_client == ["ООО Альфа", "ООО Альфа", "ООО Альфа",
                            "Итого по ООО Альфа", "ООО Бета"]