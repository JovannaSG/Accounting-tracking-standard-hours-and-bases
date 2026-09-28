import io
import pandas as pd
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

HEADER_FILL = PatternFill("solid", fgColor="D9E1F2")
HEADER_FONT = Font(bold=True)
SUBTOTAL_FILL = PatternFill("solid", fgColor="F2F2F2")
SUBTOTAL_FONT = Font(bold=True)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center")

# Форматы чисел по колонкам (ТЗ §6.1: норма — 3 знака, доля — один знак)
_NUMBER_FORMATS: dict[str, str] = {
    "Количество операций": "0",
    "Норма на операцию": "0.000",
    "Коэффициент сложности": "0.00",
    "Трудозатраты, нормочасы": "0.000",
    "Нормочасы": "0.000",
    "Доля загрузки, %": '0.0"%"',
    "Доля от итога, %": '0.0"%"',
}

_REPORT_PLACEHOLDER = [
    "Клиент", "ИНН", "Система налогообложения", "Период", "Вид документа",
    "Вид операции", "Количество операций", "Ответственный сотрудник",
    "Роль сотрудника", "Норма на операцию", "Коэффициент сложности",
    "Трудозатраты, нормочасы", "Комментарий",
]

_EMPLOYEE_PLACEHOLDER = [
    "Сотрудник", "Клиент", "Количество операций", "Нормочасы",
    "Доля загрузки, %",
]

_CLIENT_SUMMARY_PLACEHOLDER = [
    "Клиент", "ИНН", "Количество операций", "Нормочасы", "Доля от итога, %",
]


def export_excel(
    report: pd.DataFrame,
    detail: pd.DataFrame,
    employee_load: pd.DataFrame,
    totals: dict,
) -> bytes:
    """
    Формирует файл Excel (ТЗ §6.2, §7, §8, §9.3, §12):

    - «Отчёт» — данные как на экране (с выбранной группировкой) +
      подытог «Итого по <клиенту>» после каждого клиента и
      «Итого по всем клиентам» внизу;
    - «Загрузка сотрудников» — сотрудник × клиент с итогом по сотруднику
      и строкой «Всего»;
    - «Сводка по клиентам» — предустановленный вариант «Нормирование
      по клиентам за месяц».

    Подытоги вставляются на уровне openpyxl (не в pandas), чтобы не
    превращать числовые колонки в object. Возвращает байты .xlsx.
    """

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        _write_report_sheet(writer, report, totals)
        _write_employee_sheet(writer, employee_load)
        _write_client_summary_sheet(writer, detail, totals)
    return buf.getvalue()


def _write_report_sheet(
    writer: pd.ExcelWriter,
    report: pd.DataFrame,
    totals: dict,
) -> None:
    sheet_name = "Отчёт"
    if report is None or report.empty:
        pd.DataFrame(columns=_REPORT_PLACEHOLDER).to_excel(
            writer, sheet_name=sheet_name, index=False)
        ws = writer.sheets[sheet_name]
        _style_header(ws, len(_REPORT_PLACEHOLDER))
        return

    report.to_excel(writer, sheet_name=sheet_name, index=False)
    ws = writer.sheets[sheet_name]
    ncols = len(report.columns)

    _style_header(ws, ncols)
    _apply_formats(ws, report, 2, len(report) + 1)

    if "Клиент" not in report.columns:
        _append_global_total(ws, report, ncols, totals)
        return

    ops_col = None
    hours_col = None
    if "Количество операций" in report.columns:
        ops_col = list(report.columns).index("Количество операций") + 1
    if "Трудозатраты, нормочасы" in report.columns:
        hours_col = list(report.columns).index("Трудозатраты, нормочасы") + 1

    next_row = _inject_client_subtotals(ws, report, ncols, ops_col, hours_col)
    next_row += 1  # отступ перед общим итогом
    _write_subtotal_row(
        ws, next_row, ncols,
        label="Итого по всем клиентам",
        ops=float(totals.get("total_operations", 0.0)),
        hours=float(totals.get("total_hours", 0.0)),
        ops_col=ops_col,
        hours_col=hours_col,
    )
    ws.freeze_panes = "A2"


def _append_global_total(
    ws: openpyxl.worksheet.worksheet.Worksheet,
    report: pd.DataFrame,
    ncols: int,
    totals: dict,
) -> None:
    """Общий итог, когда колонки «Клиент» нет (например, группировка по СНО)."""

    row = len(report) + 3
    _write_subtotal_row(
        ws, row, ncols,
        label="Итого",
        ops=float(totals.get("total_operations", 0.0)),
        hours=float(totals.get("total_hours", 0.0)),
        ops_col=(list(report.columns).index("Количество операций") + 1
                 if "Количество операций" in report.columns else None),
        hours_col=(list(report.columns).index("Трудозатраты, нормочасы") + 1
                   if "Трудозатраты, нормочасы" in report.columns else None),
    )


def _inject_client_subtotals(
    ws: openpyxl.worksheet.worksheet.Worksheet,
    report: pd.DataFrame,
    ncols: int,
    ops_col: int | None = None,
    hours_col: int | None = None,
) -> int:
    """Вставляет «Итого по <клиенту>» после каждого блока клиента.

    Строки вставляются снизу вверх, чтобы сдвиг не сломал индексы.
    Возвращает ряд свободной строки после последнего вставленного подытога.
    """

    sums = report.groupby("Клиент", dropna=False, sort=False).agg(
        ops=("Количество операций", "sum"),
        hours=("Трудозатраты, нормочасы", "sum"),
    )

    blocks: list[list] = []  # [name, start_row, end_row]
    prev = None
    for i, client in enumerate(report["Клиент"], start=2):
        name = str(client) if pd.notna(client) else ""
        if name != prev:
            if blocks:
                blocks[-1].append(i - 1)
            blocks.append([name, i])
        prev = name
    if blocks:
        blocks[-1].append(len(report) + 1)

    for name, start, end in reversed(blocks):
        insert_at = end + 1
        ws.insert_rows(insert_at)
        client_sums = sums.loc[name] if name in sums.index else {"ops": 0.0, "hours": 0.0}
        _write_subtotal_row(
            ws, insert_at, ncols,
            label=f"Итого по {name}" if name else "Итого по клиенту",
            ops=float(client_sums["ops"]),
            hours=float(client_sums["hours"]),
            ops_col=ops_col,
            hours_col=hours_col,
        )

    # Максимальная занятая строка после всех вставок (нижние вставки не
    # сдвигаются верхними, поэтому итог считается простой формулой)
    return len(report) + len(sums) + 1


def _write_employee_sheet(
    writer: pd.ExcelWriter,
    employee_load: pd.DataFrame,
) -> None:
    sheet_name = "Загрузка сотрудников"
    if employee_load is None or employee_load.empty:
        pd.DataFrame(columns=_EMPLOYEE_PLACEHOLDER).to_excel(
            writer, sheet_name=sheet_name, index=False)
        ws = writer.sheets[sheet_name]
        _style_header(ws, len(_EMPLOYEE_PLACEHOLDER))
        return

    # Для читаемых блоков с подытогом по сотруднику сортируем по сотруднику
    data = employee_load.sort_values(
        ["Сотрудник", "Клиент"], kind="stable").reset_index(drop=True)
    data.to_excel(writer, sheet_name=sheet_name, index=False)
    ws = writer.sheets[sheet_name]
    ncols = len(data.columns)
    _style_header(ws, ncols)
    _apply_formats(ws, data, 2, len(data) + 1)

    emp_col = list(data.columns).index("Сотрудник") + 1
    ops_col = list(data.columns).index("Количество операций") + 1
    hours_col = list(data.columns).index("Нормочасы") + 1
    share_col = list(data.columns).index("Доля загрузки, %") + 1

    blocks: list[tuple[str, int, int]] = []  # (имя, первая, последняя)
    prev = None
    for i, emp in enumerate(data["Сотрудник"], start=2):
        name = str(emp or "")
        if name != prev:
            if prev is not None:
                blocks[-1][2] = i - 1
            blocks.append([name, i, i])
        prev = name
    if blocks:
        blocks[-1][2] = len(data) + 1

    for name, start, end in reversed(blocks):
        ws.insert_rows(end + 1)
        block = data.iloc[start - 2:end - 1]
        _write_subtotal_row(
            ws, end + 1, ncols,
            label=f"Итого {name}" if name else "Итого",
            ops=float(block["Количество операций"].sum()),
            hours=float(block["Нормочасы"].sum()),
            share=float(block["Доля загрузки, %"].sum()),
            ops_col=ops_col, hours_col=hours_col, share_col=share_col,
        )

    total_row = len(data) + len(blocks) + 2
    _write_subtotal_row(
        ws, total_row, ncols,
        label="Всего",
        ops=float(data["Количество операций"].sum()),
        hours=float(data["Нормочасы"].sum()),
        ops_col=ops_col, hours_col=hours_col,
    )
    ws.freeze_panes = "A2"


def _write_client_summary_sheet(
    writer: pd.ExcelWriter,
    detail: pd.DataFrame,
    totals: dict,
) -> None:
    """Сводка по клиентам (ТЗ §9.3 «Нормирование по клиентам за месяц»)."""

    sheet_name = "Сводка по клиентам"
    if detail is None or detail.empty:
        pd.DataFrame(columns=_CLIENT_SUMMARY_PLACEHOLDER).to_excel(
            writer, sheet_name=sheet_name, index=False)
        ws = writer.sheets[sheet_name]
        _style_header(ws, len(_CLIENT_SUMMARY_PLACEHOLDER))
        return

    keys = ["Клиент", "ИНН"] if "ИНН" in detail.columns else ["Клиент"]
    agg = detail.groupby(keys, dropna=False, sort=False).agg(
        ops=("Количество операций", "sum"),
        hours=("Трудозатраты, нормочасы", "sum"),
    ).reset_index()
    agg = agg.sort_values(["hours", "Клиент"], ascending=[False, True])

    total_hours = float(totals.get("total_hours", 0.0))
    if total_hours <= 0:
        total_hours = float(agg["hours"].sum())
    agg["Доля от итога, %"] = (
        agg["hours"] / total_hours * 100.0 if total_hours > 0 else 0.0
    )

    agg = agg.rename(columns={"ops": "Количество операций", "hours": "Нормочасы"})
    agg = agg[_CLIENT_SUMMARY_PLACEHOLDER]
    agg.to_excel(writer, sheet_name=sheet_name, index=False)
    ws = writer.sheets[sheet_name]
    _style_header(ws, len(_CLIENT_SUMMARY_PLACEHOLDER))
    _apply_formats(ws, agg, 2, len(agg) + 1)
    ws.freeze_panes = "A2"


# ========================= ОФОРМЛЕНИЕ ======================================

def _style_header(ws: openpyxl.worksheet.worksheet.Worksheet, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = CENTER
        _border_cell(cell)
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 20


def _write_subtotal_row(
    ws: openpyxl.worksheet.worksheet.Worksheet,
    row: int,
    ncols: int,
    label: str,
    ops: float | None = None,
    hours: float | None = None,
    share: float | None = None,
    ops_col: int | None = None,
    hours_col: int | None = None,
    share_col: int | None = None,
) -> None:
    label_cell = ws.cell(row=row, column=1, value=label)
    label_cell.font = SUBTOTAL_FONT
    label_cell.fill = SUBTOTAL_FILL
    for c in range(1, ncols + 1):
        _border_cell(ws.cell(row=row, column=c))
        ws.cell(row=row, column=c).fill = SUBTOTAL_FILL

    if ops is not None and ops_col is not None:
        cell = ws.cell(row=row, column=ops_col, value=float(ops))
        cell.font = SUBTOTAL_FONT
        cell.number_format = "0"
    if hours is not None and hours_col is not None:
        cell = ws.cell(row=row, column=hours_col, value=round(float(hours), 6))
        cell.font = SUBTOTAL_FONT
        cell.number_format = "0.000"
    if share is not None and share_col is not None:
        cell = ws.cell(row=row, column=share_col, value=round(float(share), 2))
        cell.font = SUBTOTAL_FONT
        cell.number_format = '0.0"%"'


def _apply_formats(
    ws: openpyxl.worksheet.worksheet.Worksheet,
    df: pd.DataFrame,
    start_row: int,
    end_row: int,
) -> None:
    for col in df.columns:
        fmt = _NUMBER_FORMATS.get(col)
        if not fmt:
            continue
        ci = list(df.columns).index(col) + 1
        for r in range(start_row, end_row + 1):
            ws.cell(row=r, column=ci).number_format = fmt


def _border_cell(cell) -> None:
    cell.border = BORDER