from core import db


def _norm(name: str | None) -> str:
    return str(name or "").strip().lower()


def match_employee(user_name: str) -> dict | None:
    """
    Находит сотрудника аутсорсера по имени из 1С.

    Сначала сопоставление по полному ФИО (ключ таблицы `employees`), затем —
    по алиасу `user_1c` (технические имена вида «Е_Кирищёнок», которые админ
    привязал вручную). Сравнение без учёта регистра и концевых пробелов
    """

    if not user_name:
        return None
    needle = _norm(user_name)
    for emp in db.list_employees(active_only=True):
        if _norm(emp.get("full_name")) == needle:
            return emp
    for emp in db.list_employees(active_only=True):
        if _norm(emp.get("user_1c")) == needle:
            return emp
    return None


def unmapped_users(user_names: list[str]) -> list[str]:
    """
    Имена из 1С, для которых нет записи в таблице employees
    (ни по ФИО, ни по алиасу user_1c)
    """

    mapped: set = set()
    for e in db.list_employees(active_only=True):
        mapped.add(_norm(e.get("full_name")))
        if e.get("user_1c"):
            mapped.add(_norm(e.get("user_1c")))

    result: list = []
    for name in user_names or []:
        s = str(name).strip()
        if s and _norm(s) not in mapped:
            result.append(s)
    return sorted(set(result))


def linked_employee(login: str) -> dict | None:
    """
    Сотрудник аутсорсера, привязанный к учётной записи пользователя
    (users.employee_full_name -> full_name в таблице employees).

    Используется для row-level ограничения отчёта бухгалтера (ТЗ §11):
    accountant видит только свои операции и клиентов.
    """

    user = db.get_user(login)
    if not user:
        return None
    full_name = user.get("employee_full_name")
    if not full_name:
        return None
    for emp in db.list_employees(active_only=True):
        if _norm(emp.get("full_name")) == _norm(full_name):
            return emp
    return None


def employee_scope_names(full_name: str | None) -> set[str]:
    """
    Множество имён, по которым в отчёте распознаётся сотрудник при
    row-level фильтрации: полное ФИО + алиас user_1c (техническое имя 1С).
    """

    if not full_name:
        return set()
    names: set[str] = {full_name}
    emp = None
    for e in db.list_employees(active_only=True):
        if _norm(e.get("full_name")) == _norm(full_name):
            emp = e
            break
    if emp and emp.get("user_1c"):
        names.add(emp["user_1c"])
    return names


def filter_by_employee(df, full_name: str | None):
    """
    Row-level фильтр отчёта: оставляет только строки «Ответственный сотрудник»,
    которых идентифицирует привязанный сотрудник (по ФИО и алиасу user_1c).

    Без привязки (full_name пуст) возвращает пустую выборку той же схемы —
    бухгалтер без сотрудника не видит ничего.
    """

    allowed = employee_scope_names(full_name)
    if not allowed:
        return df.iloc[0:0]
    if df is None or df.empty or "Ответственный сотрудник" not in df.columns:
        return df
    mask = df["Ответственный сотрудник"].astype(str).isin(allowed)
    return df.loc[mask].reset_index(drop=True)
