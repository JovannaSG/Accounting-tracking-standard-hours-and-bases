import pandas as pd

from core import db
from core.employees import match_employee

# Схема отчёта после агрегации (ТЗ §6.1)
REPORT_COLUMNS: list[str] = [
    "Клиент", "ИНН", "Система налогообложения", "Период", "Вид документа",
    "Вид операции", "Количество операций", "Ответственный сотрудник",
    "Роль сотрудника", "Норма на операцию", "Коэффициент сложности",
    "Трудозатраты, нормочасы", "Комментарий",
]

# Группировка по умолчанию:
# Клиент -> Ответственный сотрудник -> Вид документа -> Вид операции
DEFAULT_GROUPING: list[str] = [
    "Клиент", "Ответственный сотрудник", "Вид документа", "Вид операции",
]


def _norm_name(name: str | None) -> str:
    return str(name or "").strip().lower()


def _row_to_key(row: pd.Series) -> str:
    return _norm_name(row.get("Ответственный сотрудник"))


def apply_norms_and_employees(df: pd.DataFrame) -> pd.DataFrame:
    """
    Проставляет по каждой строке: норму (по виду документа), коэффициент,
    роль сотрудника; вычисляет трудозатраты и ставит «Не распределено»
    для авторов без записи в таблице сотрудников.
    """

    out = df.copy()
    norms = {n["doc_type"]: n for n in db.list_norms(active_only=True)}
    # Индекс норм по наименованию вида документа (варианты форм в БП)
    by_title: dict[str, dict] = {}
    for n in norms.values():
        by_title.setdefault(n["title"], n)

    rows: list[dict] = []
    for _, rec in out.iterrows():
        title = str(rec.get("Вид документа") or "")
        norm = by_title.get(title) or _match_by_entity(rec, norms)

        author = _row_to_key(rec)
        emp = match_employee(author)
        full_name = emp["full_name"] if emp else "Не распределено"
        role = emp["role"] or "" if emp else "Не распределено"

        norm_hours = float(norm["norm_hours"]) if norm else 0.0
        coeff = float(norm["coeff"]) if norm else 1.00
        hours = float(rec.get("Количество операций") or 0) * norm_hours * coeff
        comment = (norm["comment"] or "") if norm else "Норма не задана"

        r = dict(rec)
        r["Ответственный сотрудник"] = full_name
        r["Роль сотрудника"] = role
        r["Норма на операцию"] = norm_hours
        r["Коэффициент сложности"] = coeff
        r["Трудозатраты, нормочасы"] = round(hours, 6)
        r["Комментарий"] = comment
        rows.append(r)

    return pd.DataFrame(rows, columns=REPORT_COLUMNS)


def _match_by_entity(rec, norms: dict) -> dict | None:
    """
    Запасной матчинг нормы: по doc_type из doc_types.json невозможен здесь
    (в DataFrame только наименование), поэтому ищем по точному совпадению
    наименования категории + наименования вида документа
    """

    title = str(rec.get("Вид документа") or "")
    for n in norms.values():
        if n["title"] == title:
            return n
    return None


def find_missing_norms(df: pd.DataFrame) -> list[str]:
    """
    Виды документов, для которых норма не задана (ТЗ §4.1).

    «Не задана» — это норма, которая не найдена в реестре/выключена
    администратором, либо равна нулю. Нулевая норма в DEFAULT_NORMS_HOURS
    означает ровно то же самое: norms.py заводит её как заглушку с
    комментарием «Норма не задана в ТЗ» (см. writeoff_materials_from_use,
    return_customer).

    Принимает сырой DataFrame от fetch_documents, а не агрегированный отчёт:
    apply_norms_and_employees пересобирает фрейм через
    ``pd.DataFrame(rows, columns=...)``, что теряет df.attrs, поэтому
    прокидывать список через атрибут нельзя.

    Возвращает отсортированный список наименований без дублей.
    """

    if df is None or df.empty:
        return []

    norms = {n["doc_type"]: n for n in db.list_norms(active_only=True)}
    by_title: dict[str, dict] = {}
    for n in norms.values():
        by_title.setdefault(n["title"], n)

    missing: set[str] = set()
    for title in df.get("Вид документа", pd.Series(dtype=str)).dropna():
        name = str(title).strip()
        if not name:
            continue
        norm = by_title.get(name)
        if norm is None or float(norm.get("norm_hours") or 0.0) == 0.0:
            missing.add(name)
    return sorted(missing)


def build_report(
    df: pd.DataFrame,
    grouping: list[str] | None = None,
) -> pd.DataFrame:
    """
    Строит агрегированный отчёт: количество операций × норма × коэффициент.

    По умолчанию группировка Клиент -> Ответственный -> Вид документа ->
    Вид операции (ТЗ §6.3). ``grouping=[]`` возвращает детальную выгрузку без
    агрегации (одна строка на документ), колонки в порядке ТЗ §6.1. Пустой
    датафрейм возвращает схему без строк.
    """

    if df.empty:
        return pd.DataFrame(columns=REPORT_COLUMNS)

    prepared = apply_norms_and_employees(df)

    if grouping == []:
        return prepared[REPORT_COLUMNS].reset_index(drop=True)

    keys = grouping or DEFAULT_GROUPING

    agg_cols = keys
    first_non_empty = (
        lambda s: next((v for v in s if pd.notna(v) and v != ""), "")
        if not s.empty else ""
    )
    report: pd.DataFrame = prepared.groupby(
        agg_cols, as_index=False, dropna=False
    ).agg(
        **{
            "Количество операций": ("Количество операций", "sum"),
            "ИНН": ("ИНН", first_non_empty),
            "Система налогообложения": ("Система налогообложения", first_non_empty),
            "Период": ("Период", first_non_empty),
            "Норма на операцию": (
                "Норма на операцию",
                lambda s: float(s.iloc[0]) if not s.empty and s.iloc[0] is not None else 0.0,
            ),
            "Коэффициент сложности": (
                "Коэффициент сложности",
                lambda s: float(s.iloc[0]) if not s.empty and s.iloc[0] is not None else 1.00,
            ),
            "Трудозатраты, нормочасы": ("Трудозатраты, нормочасы", "sum"),
            "Комментарий": ("Комментарий", first_non_empty),
            "Роль сотрудника": ("Роль сотрудника", first_non_empty),
        }
    )

    report["Трудозатраты, нормочасы"] = round(report["Трудозатраты, нормочасы"], 6)

    # Собираем итоговые колонки в порядке ТЗ §6.1 (доп. ключи группировки —
    # после обязательных колонок)
    final_cols = [c for c in REPORT_COLUMNS if c in report.columns]
    for c in report.columns:
        if c not in final_cols:
            final_cols.append(c)

    selected_cols: list = [c for c in final_cols if c in report.columns]
    report = report[selected_cols]

    # Сортировка по умолчанию (ТЗ §9.2): Клиент ▲, Вид документа ▲,
    # операций ▼, трудозатраты ▼
    sort_order: list[tuple] = [
        ("Клиент", True),
        ("Вид документа", True),
        ("Количество операций", False),
        ("Трудозатраты, нормочасы", False),
    ]
    sort_cols: list[str] = [c for c, _ in sort_order if c in report.columns]
    if sort_cols:
        sort_asc = [asc for c, asc in sort_order if c in report.columns]
        report = report.sort_values(
            by=sort_cols, ascending=sort_asc, kind="stable"
        )
    return report.reset_index(drop=True)


def summarize_totals(report: pd.DataFrame) -> dict:
    """
    Итоги (ТЗ §6.2): всего операций, нормочасов, по клиентам и сотрудникам.
    """

    if not report.empty:
        total_ops = int(report["Количество операций"].sum())
    else:
        total_ops = 0

    if not report.empty:
        total_hours = float(report["Трудозатраты, нормочасы"].sum())
    else:
        total_hours = 0.0

    by_client: dict = {}
    if not report.empty and "Клиент" in report.columns:
        for client, grp in report.groupby("Клиент", dropna=False):
            by_client[str(client or "Без клиента")] = {
                "operations": int(grp["Количество операций"].sum()),
                "hours": round(float(grp["Трудозатраты, нормочасы"].sum()), 6),
            }

    by_employee: dict = {}
    if not report.empty and "Ответственный сотрудник" in report.columns:
        for emp, grp in report.groupby("Ответственный сотрудник", dropna=False):
            by_employee[str(emp or "Не распределено")] = {
                "operations": int(grp["Количество операций"].sum()),
                "hours": round(float(grp["Трудозатраты, нормочасы"].sum()), 6),
            }

    return {
        "total_operations": total_ops,
        "total_hours": total_hours,
        "by_client": by_client,
        "by_employee": by_employee,
    }


def build_employee_load(report: pd.DataFrame) -> pd.DataFrame:
    """
    Отчёт «Загрузка сотрудников» (ТЗ §8): сотрудник × клиент, доля от фонда
    130 ч (устанавливается в таблице employees).

    Для «Не распределено» фонд берётся как 0 — доля не считается,
    а нормочасы показываются отдельно.
    """

    if report is None or report.empty:
        return pd.DataFrame()

    mapping: dict[str, dict] = {}
    for emp in db.list_employees(active_only=True):
        if emp.get("full_name"):
            mapping[_norm_name(emp["full_name"])] = emp

    rows: list[dict] = []
    group_cols = ["Ответственный сотрудник"]
    if "Клиент" in report.columns:
        group_cols.append("Клиент")

    for (emp, *rest), grp in report.groupby(group_cols, dropna=False, sort=False):
        hours = float(grp["Трудозатраты, нормочасы"].sum())
        ops = int(grp["Количество операций"].sum())
        client = str(rest[0] or "") if rest else ""
        emp_name = str(emp or "Не распределено")
        rec = mapping.get(_norm_name(emp_name), {})
        fund = float(rec.get("hours_per_month") or 0.0) if rec else 0.0
        share = (hours / fund * 100.0) if fund > 0 else 0.0
        rows.append({
            "Сотрудник": emp_name,
            "Клиент": client,
            "Количество операций": ops,
            "Нормочасы": round(hours, 6),
            "Доля загрузки, %": round(share, 2),
        })

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(
            ["Доля загрузки, %", "Сотрудник", "Клиент"],
            ascending=[False, True, True],
        ).reset_index(drop=True)
    return out
