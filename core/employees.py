from core import db


def _norm(name: str | None) -> str:
    return str(name or "").strip().lower()


def resolve_employee_smart(name_1c: str, db_employees: list[dict]) -> dict | None:
    """
    Каскадный поиск сотрудника по имени из 1С с защитой от дублей
    (Collision Guard). Заменяет прямое сравнение строк: снимает с
    администратора ручную заводку алиасов для простых расхождений вида
    «Yablontseva» <-> «Elena Yablontseva».

    db_employees: список сотрудников из таблицы employees (словари с
    ключами full_name, user_1c, id).

    Порядок:
      1. точное совпадение по ФИО (ключ таблицы), затем по алиасу user_1c;
      2. каскад частичных совпадений:
         A) взаимное вхождение подстроки — покрывает «Yablontseva» <->
            «Elena Yablontseva»;
         B) ВСЕ слова-токены длиннее 3 символов обязаны присутствовать в
            строке 1С — покрывает перестановку слов («Yablontseva Elena»
            <-> «Elena Yablontseva») и не даёт общему имени («Elena»)
            тянуть чужих кандидатов. Условие «все», а не «любое»: у
            «Ivanova Elena» не хватает «ivanova» — она не кандидат.
            Порог >3 отсекает инициалы и предлоги;
      3. Collision Guard: если подошёл ровно один сотрудник — он и
         возвращается; при двух и более совпадениях возвращается None —
         часы уходят в «Не распределено» и требуется явный алиас.
    """

    target = _norm(name_1c)
    if not target:
        return None

    employees = db_employees or []

    # 1. Точное совпадение. ФИО проверяется по всем сотрудникам раньше,
    #    чем алиасы, — приоритет ключа таблицы сохранён.
    for emp in employees:
        if _norm(emp.get("full_name")) == target:
            return emp
    for emp in employees:
        if _norm(emp.get("user_1c")) == target:
            return emp

    # 2. Каскад частичных совпадений.
    candidates: list[dict] = []
    seen: set = set()

    def _add(emp: dict) -> None:
        # dict == dict сравнивает содержимое, поэтому дедуп по id (или
        # identity, если id почему-то нет).
        key = emp.get("id")
        if key is None:
            key = id(emp)
        if key not in seen:
            seen.add(key)
            candidates.append(emp)

    for emp in employees:
        db_name = _norm(emp.get("full_name"))
        if not db_name:
            continue

        # Правило A: взаимное вхождение подстроки.
        if target in db_name or db_name in target:
            _add(emp)
            continue

        # Правило B: ВСЕ значимые токены из БД обязаны присутствовать в
        # строке 1С. Общее имя («Elena») больше не тянет чужих кандидатов:
        # у «Ivanova Elena» не хватает «ivanova» — она не кандидат.
        db_tokens = [t for t in db_name.split() if len(t) > 3]
        if db_tokens and all(t in target for t in db_tokens):
            _add(emp)

    # 3. Collision Guard.
    if len(candidates) == 1:
        return candidates[0]
    return None


def match_employee(user_name: str) -> dict | None:
    """
    Находит сотрудника аутсорсера по имени из 1С (каскадный поиск,
    см. resolve_employee_smart). Работает по активным записям таблицы
    employees.
    """

    return resolve_employee_smart(user_name, db.list_employees(active_only=True))


def unmapped_users(user_names: list[str]) -> list[str]:
    """
    Имена из 1С, которые не удалось сопоставить ни с одной записью
    сотрудников. Использует тот же каскадный резолвер, что и отчёт, чтобы
    список «Не распределены» не расходился с фактическим распределением.
    """

    employees = db.list_employees(active_only=True)
    result: list = []
    for name in user_names or []:
        s = str(name).strip()
        if s and resolve_employee_smart(s, employees) is None:
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
