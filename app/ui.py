import os
import sys
import time

import pandas as pd
import streamlit as st

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core import auth, db
from core.api_client import OneCClient
from core.norms import load_doc_types, seed_default_norms, DEFAULT_NORMS_HOURS
from core.fetch import fetch_documents, unmapped_user_names
from core.calculator import (
    build_report,
    build_employee_load,
    find_missing_norms,
    summarize_totals,
    REPORT_COLUMNS,
    DEFAULT_GROUPING,
)
from core.reporters import export_excel
from core.employees import unmapped_users, linked_employee, filter_by_employee

PERIOD_MODES = {
    "Месяц": "month",
    "Квартал": "quarter",
    "Год": "year",
    "Произвольный": "custom",
}

# Доступные группировки отчёта (ТЗ §6.3)
GROUPING_OPTIONS: list[str] = [
    "Клиент", "Ответственный сотрудник", "Вид документа", "Вид операции",
    "Система налогообложения", "Роль сотрудника", "Период",
]

# Предустановленные варианты отчёта (ТЗ §9.3)
REPORT_VARIANTS: dict[str, dict] = {
    "Пользовательская": {"grouping": None},
    "Нормирование по клиенту": {"grouping": list(DEFAULT_GROUPING)},
    "Нормирование по сотруднику": {
        "grouping": [
            "Ответственный сотрудник", "Клиент", "Вид документа",
            "Вид операции",
        ]
    },
    "Нормирование по видам документов": {
        "grouping": ["Вид документа", "Вид операции"]
    },
    "Нормирование по клиентам за месяц": {
        "grouping": ["Клиент", "Период"]
    },
    "Нормирование по видам операций": {
        "grouping": ["Вид операции", "Вид документа"]
    },
    "Операции без ответственного": {
        "grouping": list(DEFAULT_GROUPING),
        "unassigned": True,
    },
    "Отклонение от нормы": {
        "grouping": list(DEFAULT_GROUPING),
        "info": "Учёт фактического времени не подключён (очередь II) — "
                "сравнение нормочасов с фактом пока недоступно.",
    },
}

# Колонки, по которым можно сортировать отчёт (ТЗ §9.2)
SORTABLE_COLUMNS: list[str] = [
    "Клиент", "Вид документа", "Вид операции", "Период",
    "Ответственный сотрудник", "Роль сотрудника",
    "Система налогообложения", "ИНН", "Количество операций",
    "Трудозатраты, нормочасы",
]


def apply_filters(report: pd.DataFrame, filters: dict) -> pd.DataFrame:
    """
    Применяет отборы из сайдбара к сформированному отчёту.
    Пустое значение фильтра = отбор не применяется (показываются все строки).
    """

    if report.empty:
        return report

    filtered = report.copy()

    def _isin(col: str, values) -> None:
        # Колонка может отсутствовать, если её нет в выбранной группировке
        if values and col in filtered.columns:
            filtered.drop(
                filtered.index[~filtered[col].isin(values)], inplace=True
            )

    _isin("Клиент", filters.get("client"))
    if filters.get("inn") and "ИНН" in filtered.columns:
        needle = str(filters["inn"]).strip()
        if needle:
            filtered.drop(
                filtered.index[~filtered["ИНН"].astype(str).str.contains(
                    needle, na=False, regex=False
                )],
                inplace=True,
            )
    _isin("Система налогообложения", filters.get("sno"))
    _isin("Ответственный сотрудник", filters.get("employee"))
    _isin("Роль сотрудника", filters.get("role"))
    _isin("Вид документа", filters.get("doc_type"))
    _isin("Вид операции", filters.get("op_type"))
    if (
        filters.get("unassigned_only")
        and "Ответственный сотрудник" in filtered.columns
    ):
        filtered.drop(
            filtered.index[
                filtered["Ответственный сотрудник"] != "Не распределено"
            ],
            inplace=True,
        )
    if (
        filters.get("min_qty")
        and "Количество операций" in filtered.columns
    ):
        filtered.drop(
            filtered.index[
                filtered["Количество операций"] < filters["min_qty"]
            ],
            inplace=True,
        )
    if (
        filters.get("min_hours")
        and "Трудозатраты, нормочасы" in filtered.columns
    ):
        filtered.drop(
            filtered.index[
                filtered["Трудозатраты, нормочасы"] < filters["min_hours"]
            ],
            inplace=True,
        )

    return filtered


def render_sidebar_filters(report: pd.DataFrame) -> dict:
    """
    Отрисовывает отборы в сайдбаре на основе данных отчёта.
    Возвращает словарь фильтров для apply_filters.
    """

    st.sidebar.header("Отборы")
    filters: dict = {}

    if report.empty:
        return filters

    def _options(col: str) -> list:
        if col not in report.columns:
            return []
        return sorted(
            {str(v) for v in report[col].dropna().unique() if str(v) != ""}
        )

    def _multiselect(label: str, col: str, key: str) -> list:
        # Фильтр показываем, только если колонка есть в текущей группировке
        if col not in report.columns:
            filters[col] = []
            return []
        filters[col] = st.sidebar.multiselect(label, _options(col), key=key)
        return filters[col]

    _multiselect("Клиент", "Клиент", "filter_client")
    if "ИНН" in report.columns:
        filters["inn"] = st.sidebar.text_input(
            "ИНН (поиск)", key="filter_inn"
        )
    else:
        filters["inn"] = ""
    _multiselect(
        "Система налогообложения", "Система налогообложения", "filter_sno"
    )
    _multiselect(
        "Ответственный сотрудник", "Ответственный сотрудник", "filter_employee"
    )
    filters["unassigned_only"] = st.sidebar.checkbox(
        "Только нераспределенные", key="filter_unassigned"
    )
    _multiselect("Роль сотрудника", "Роль сотрудника", "filter_role")
    _multiselect("Вид документа", "Вид документа", "filter_doc_type")
    _multiselect("Вид операции", "Вид операции", "filter_op_type")
    if "Количество операций" in report.columns:
        filters["min_qty"] = st.sidebar.number_input(
            "Мин. количество операций", min_value=0, value=0,
            key="filter_min_qty",
        )
    else:
        filters["min_qty"] = 0
    if "Трудозатраты, нормочасы" in report.columns:
        filters["min_hours"] = st.sidebar.number_input(
            "Мин. нормочасов", min_value=0.0, value=0.0, step=0.1,
            key="filter_min_hours",
        )
    else:
        filters["min_hours"] = 0.0

    active = _active_filters_summary(filters)
    if active:
        st.sidebar.caption(f"Применяются отборы: {active}")

    return filters


def _active_filters_summary(filters: dict) -> str:
    """Короткая сводка применяемых отборов для сайдбара."""

    labels: dict[str, tuple] = {
        "client": ("Клиент", None),
        "inn": ("ИНН", None),
        "sno": ("СНО", None),
        "employee": ("Сотрудник", None),
        "unassigned_only": ("Только «Не распределено»", None),
        "role": ("Роль", None),
        "doc_type": ("Вид документа", None),
        "op_type": ("Вид операции", None),
    }
    parts: list[str] = []
    for key, (label, _) in labels.items():
        val = filters.get(key)
        if key == "unassigned_only":
            if val:
                parts.append(label)
        elif val:
            count = len(val) if isinstance(val, (list, tuple, set)) else 1
            parts.append(label if count <= 1 else f"{label} ({count})")
    if filters.get("min_qty", 0):
        parts.append(f"операций ≥ {filters['min_qty']}")
    if filters.get("min_hours", 0.0):
        parts.append(f"нормочасов ≥ {filters['min_hours']}")
    return ", ".join(parts) or "нет"


def _current_user() -> dict | None:
    if not auth.auth_enabled():
        return None
    login = st.session_state.get("login")
    if not login:
        return None
    return auth.get_user(login)


def _is_admin() -> bool:
    user = _current_user()
    if user is None:
        # без аутентификации всё разрешено
        return True
    return user_role() == auth.ROLE_ADMIN


def _permission_flag(flag: str) -> bool:
    """
    Флаг прав текущей роли (ТЗ §11). Без аутентификации — всё разрешено.
    """

    user = _current_user()
    if user is None:
        return True
    return auth.permission_flags(user.get("login") or "").get(flag, False)


def _permission_flags_for_session() -> dict[str, bool]:
    """Полный набор флагов текущей сессии (для роутинга вкладок)."""

    user = _current_user()
    if user is None:
        return dict(auth.PERMISSION_MATRIX[auth.ROLE_ADMIN])
    return auth.permission_flags(user.get("login") or "")


def user_role() -> str:
    role = st.session_state.get("user_role")
    if role:
        return role
    login = st.session_state.get("login")
    if not login:
        return auth.ROLE_ADMIN
    return auth.user_role(login)


def _store_access_ctx() -> None:
    """
    Сохраняет роль и список доступных баз
    текущего пользователя в session_state
    Вызывается после успешного входа.
    """

    login = st.session_state.get("login") or ""
    if not login:
        st.session_state.pop("user_role", None)
        st.session_state.pop("user_allowed_urls", None)
        st.session_state.pop("login_ts", None)
        return
    st.session_state["user_role"] = auth.user_role(login)
    st.session_state["user_allowed_urls"] = auth.user_allowed_urls(login)
    # Метка входа для таймаута бездействия: ставится один раз при входе
    if not st.session_state.get("login_ts"):
        st.session_state["login_ts"] = time.time()


def _render_login_form() -> None:
    """
    Форма входа (ТЗ §11)

    При успехе логин пишется в session_state["login"]
    """

    st.title("🔐 Вход в приложение")
    st.caption(
        "Доступ ограничен. Учётные записи задаёт администратор "
        "во вкладке «Пользователи»."
    )
    with st.form("login_form"):
        login_input = st.text_input("Логин", key="login_user")
        password_input = st.text_input(
            "Пароль",
            type="password",
            key="login_pass",
        )
        submitted = st.form_submit_button(
            "Войти",
            type="primary",
            key="btn_login"
        )

    if submitted:
        if not login_input.strip() or not password_input:
            st.error("Введите логин и пароль.")
        elif auth.login_locked(login_input):
            st.error(auth.lockout_message(login_input))
        elif auth.verify(login_input, password_input):
            auth.clear_failures(login_input)
            st.session_state["login"] = login_input.strip().lower()
            _store_access_ctx()
            st.rerun()
        else:
            st.error(auth.failed_login_error(login_input))


def _render_logout_button() -> None:
    """
    Кнопка «Выйти» в сайдбаре. Очищает сессию и возвращает на экран входа
    """

    login = st.session_state.get("login") or ""
    if not login:
        return
    role = st.session_state.get("user_role") or ""
    st.sidebar.caption(f"👤 {login} · {_role_label(role)}")
    if st.sidebar.button("🔓 Выйти", key="btn_logout", type="secondary"):
        for key in ("login", "user_role", "user_allowed_urls", "login_ts"):
            st.session_state.pop(key, None)
        st.rerun()


def _active_admins(users: list) -> list:
    """
    Действующие администраторы из списка пользователей
    """

    return [
        u for u in users
        if u["role"] == auth.ROLE_ADMIN and u.get("active", True)
    ]


def _visible_databases(entries: list) -> list:
    """
    Фильтрует список баз для текущего пользователя.

    - без логина (аутентификация выключена) — всё;
    - admin — всё;
    - accountant — только базы, URL которых есть в его списке доступа.
    """

    login = st.session_state.get("login") or ""
    if not login:
        return entries
    return [
        e for e in entries
        if auth.user_can_access(login, (e.get("url") or ""))
    ]


def _list_bases_or_error(active_only: bool, where=None) -> list[dict]:
    """
    db.list_bases с понятным сообщением вместо трассировки, если ключ
    шифрования задан, но неверен: приложение не может показать пароли,
    которые лежат в БД зашифрованными.
    """

    try:
        return db.list_bases(active_only=active_only)
    except db.SecretKeyError as e:
        st.error(
            f"Не удалось прочитать пароли клиентских баз: {e}\n\n"
            "Пока ключ шифрования неверен, приложение не покажет базы и не "
            "подключится к 1С — намеренно, чтобы не подставить пустой пароль.",
            icon="🔐",
        )
        if where is not None:
            where.caption("Исправьте ключ и обновите страницу.")
        return []


def select_base() -> dict:
    st.sidebar.header("База 1С:Фреш")
    all_bases = _list_bases_or_error(active_only=True, where=st.sidebar)
    bases = _visible_databases(all_bases)
    if all_bases and not bases:
        st.sidebar.info("Нет баз, доступных вашей учётной записи.")
        return {"id": None, "name": "", "url": "", "login": "",
                "password": "", "sno": ""}
    options = {b["name"]: b for b in bases}
    names = list(options.keys())
    if names:
        chosen = st.sidebar.selectbox(
            "Клиентская база",
            names,
            key="base_select"
        )
        return options[chosen]

    st.sidebar.info("Базы не настроены. Заполните их во вкладке «Базы и доступ».")
    st.sidebar.subheader("Без сохранения (.json)")
    url = st.sidebar.text_input(
        "URL OData",
        value="https://msk1.1cfresh.com/a/ea/000000"
    )
    user = st.sidebar.text_input("Логин OData", value="odata.user")
    pwd = st.sidebar.text_input("Пароль OData", type="password")
    return {"id": None, "name": url, "url": url, "login": user,
            "password": pwd, "sno": ""}


def period_bounds_for_month(month: str, year: int) -> tuple[str, str, str]:
    """
    Границы месяца в ISO-формате и подпись периода: (start, end, label)
    """

    start = pd.Timestamp(f"{int(year)}-{str(month).zfill(2)}-01")
    end = start + pd.offsets.MonthEnd(1)
    label = f"{str(month).zfill(2)}.{int(year)}"
    return start.date().isoformat(), end.date().isoformat(), label


def select_period() -> tuple[str, str, str]:
    st.sidebar.header("Период")
    mode = st.sidebar.selectbox(
        "Период", list(PERIOD_MODES.keys()), index=0, key="period_mode"
    )
    today = pd.Timestamp.today()
    if mode == "Месяц":
        month = st.sidebar.selectbox(
            "Месяц", [f"{m:02d}" for m in range(1, 13)],
            index=today.month - 1, key="period_month",
        )
        year = st.sidebar.number_input(
            "Год", min_value=2000, max_value=2100,
            value=int(today.year), key="period_year",
        )
        start, end, label = period_bounds_for_month(month, int(year))
        start, end = pd.Timestamp(start), pd.Timestamp(end)
    elif mode == "Квартал":
        quarter = st.sidebar.selectbox(
            "Квартал", ["1", "2", "3", "4"],
            index=(today.quarter - 1), key="period_quarter",
        )
        year = st.sidebar.number_input(
            "Год", min_value=2000, max_value=2100,
            value=int(today.year), key="period_year_q",
        )
        month_start = 3 * int(quarter) - 2
        start = pd.Timestamp(f"{year}-{month_start:02d}-01")
        end = start + pd.offsets.MonthEnd(3)
        label = f"кв.{quarter} {year}"
    elif mode == "Год":
        year = st.sidebar.number_input(
            "Год", min_value=2000, max_value=2100,
            value=int(today.year), key="period_year_y",
        )
        start = pd.Timestamp(f"{year}-01-01")
        end = pd.Timestamp(f"{year}-12-31")
        label = str(year)
    else:
        d1 = st.sidebar.date_input(
            "Начало",
            value=today - pd.Timedelta(days=30)
        )
        d2 = st.sidebar.date_input("Конец", value=today)
        start = pd.Timestamp(d1)
        end = pd.Timestamp(d2)
        label = f"{d1.strftime('%d.%m.%Y')}—{d2.strftime('%d.%m.%Y')}"

    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), label


def run_report(
    base: dict,
    period_start: str,
    period_end: str,
    selected_types: list[str],
    grouping: list[str] | None = None,
) -> tuple:
    """
    Выгружает документы и строит отчёт.

    ``selected_types`` приходит из блока «Блок операций» в сайдбаре.
    Настройка базы (active_doc_types) сужает его: список заданной
    администратором базы — белый список, а блок операций выбирает внутри
    него. Пустой список у базы означает «выгружать все виды».
    """

    allow = base.get("active_doc_types") or []
    types = [k for k in selected_types if k in allow] if allow else list(selected_types)

    client = OneCClient(base["url"], base["login"], base["password"])
    raw = fetch_documents(
        client, period_start, period_end, types,
        sno=(base.get("sno") or ""),
    )
    report = build_report(raw, grouping=grouping)
    skipped = raw.attrs.get("skipped", [])
    return report, raw, skipped


def render_report_tab(base: dict):
    st.title("Нормирование труда бухгалтера")
    period_start, period_end, period_label = select_period()

    doc_types = load_doc_types()
    categories = sorted({
        spec.get("category", "")
        for spec in doc_types.values()
    }    )
    selected_cats = st.sidebar.multiselect(
        "Блок операций", categories, default=categories)
    keys: list = [
        k for k, spec in doc_types.items()
        if spec.get("category") in selected_cats
    ]

    variant = st.sidebar.selectbox(
        "Вариант отчёта (§9.3)", list(REPORT_VARIANTS.keys()),
        index=0, key="report_variant"
    )
    v_spec = REPORT_VARIANTS.get(variant, {})
    if v_spec.get("grouping") is not None:
        grouping = list(v_spec["grouping"])
    else:
        grouping = st.sidebar.multiselect(
            "Группировка", GROUPING_OPTIONS,
            default=list(DEFAULT_GROUPING), key="grouping_custom"
        )
    if not grouping:
        grouping = list(DEFAULT_GROUPING)
    if v_spec.get("info"):
        st.sidebar.info(v_spec["info"])

    if st.sidebar.button("Сформировать отчёт", type="primary"):
        with st.spinner("Сбор данных из 1С:Фреш..."):
            try:
                report, raw, skipped = run_report(
                    base,
                    period_start, period_end,
                    keys,
                    grouping,
                )
            except ValueError as e:
                st.error(str(e))
                return

        st.session_state["last_result"] = {
            "raw": raw,
            "skipped": skipped,
            "base_url": base.get("url") or "",
            "base_name": base.get("name") or "",
            "period_label": period_label,
            "period_start": period_start,
            "period_end": period_end,
        }

    cached = st.session_state.get("last_result")
    if not cached:
        return

    # Если сменились база или период — сбросить устаревший кэш
    if (
        cached.get("base_url") != (base.get("url") or "")
        or cached.get("period_start") != period_start
        or cached.get("period_end") != period_end
    ):
        st.session_state.pop("last_result", None)
        return

    report = build_report(cached["raw"], grouping=grouping)
    detail_report = build_report(cached["raw"], grouping=[])
    filters = render_sidebar_filters(report)
    if v_spec.get("unassigned"):
        # Вариант «Операции без ответственного» — принудительный отбор
        filters["unassigned_only"] = True
        st.sidebar.caption("Вариант отчёта: только «Не распределено»")
    filtered = apply_filters(report, filters)
    filtered_detail = apply_filters(detail_report, filters)

    if filtered is not None and not filtered.empty:
        available = [c for c in SORTABLE_COLUMNS if c in filtered.columns]
        default_sort = "Клиент" if "Клиент" in available else available[0]
        sort_col = st.sidebar.selectbox(
            "Сортировка", available,
            index=available.index(default_sort),
            key="report_sort_col",
        )
        sort_dir = st.sidebar.radio(
            "Направление", ("▲ (по возрастанию)", "▼ (по убыванию)"),
            horizontal=True, key="report_sort_dir",
        )
        asc = sort_dir.startswith("▲")
        # Клиент и вид документа — основные ключи: сортируем по ним обоим,
        # иначе блоки одного клиента рассыпаются (подытоги клиента).
        wanted = {
            "Клиент": [("Клиент", asc), ("Вид документа", True)],
            "Вид документа": [("Вид документа", asc), ("Клиент", True)],
        }.get(sort_col, [(sort_col, asc)])
        pairs = [(c, a) for c, a in wanted if c in filtered.columns]
        if pairs:
            filtered = filtered.sort_values(
                by=[c for c, _ in pairs],
                ascending=[a for _, a in pairs],
                kind="stable",
            ).reset_index(drop=True)

    # Row-level ограничение (ТЗ §11): бухгалтер видит только свои операции
    if user_role() == auth.ROLE_ACCOUNTANT:
        emp = linked_employee(st.session_state.get("login") or "")
        scope_name = (emp or {}).get("full_name")
        filtered = filter_by_employee(filtered, scope_name)
        filtered_detail = filter_by_employee(filtered_detail, scope_name)

    totals = summarize_totals(filtered)
    emp_load = build_employee_load(filtered)

    st.subheader(f"Отчёт за {cached['period_label']} · {cached['base_name']}")
    st.markdown(
        f"Всего операций: **{totals['total_operations']}**, "
        f"нормочасов: **{totals['total_hours']:.2f}**"
    )

    if cached.get("skipped"):
        with st.expander("Виды документов, которые не удалось загрузить"):
            for s in cached["skipped"]:
                st.warning(s)

    # Предупреждение о незаполненных нормах (ТЗ §4.1). Считаем по сырым
    # данным: apply_norms_and_employees пересобирает фрейм и теряет attrs,
    # а отчёт заново строится из кэша при каждом rerun.
    missing_norms = find_missing_norms(cached["raw"])
    if missing_norms and user_role() != auth.ROLE_ACCOUNTANT:
        st.warning(
            "Внимание: нормы трудозатрат не заданы для следующих видов "
            "документов — они посчитаны с нормой 0:"
        )
        st.markdown(
            "\n".join(f"- {name}" for name in missing_norms)
        )

    if v_spec.get("info"):
        st.info(v_spec["info"])
    if filtered.empty:
        if report.empty:
            st.info("За период документы не найдены.")
        else:
            st.info("Ничего не соответствует выбранным отборам.")
        return

    st.dataframe(filtered)

    # Документы без нормы / с нулевыми нормочасами (ТЗ §4.1). Считаем по
    # уже отобранному `filtered`, ничего не пересчитывая: totals/emp_load
    # (строки 649-650) и export_excel (строка 704) используют этот же фрейм,
    # поэтому ниже — только копия среза, без мутаций.
    if user_role() != auth.ROLE_ACCOUNTANT:
        _hours_col = "Трудозатраты, нормочасы"
        if _hours_col in filtered.columns:
            # NaN/строки -> 0: «норма не задана» == «нормочасов нет»
            _hours = pd.to_numeric(filtered[_hours_col], errors="coerce")
            _zero_docs = filtered.loc[_hours.fillna(0).eq(0)]
            if not _zero_docs.empty:
                # Колонки, фактически присутствующие в текущей группировке
                _show_cols = [
                    c for c in (
                        "Клиент", "Вид документа", "Вид операции",
                        "Ответственный сотрудник", "Количество операций",
                        "Норма на операцию", "Коэффициент сложности",
                        _hours_col, "Комментарий",
                    )
                    if c in _zero_docs.columns
                ]
                with st.expander(
                    f"Документы без нормы / нулевые нормочасы ({len(_zero_docs)})",
                    key="zero_hours_docs",
                ):
                    st.dataframe(_zero_docs[_show_cols].copy())

    st.subheader("Загрузка сотрудников")
    if emp_load.empty:
        st.info("Нет распределённых операций.")
    else:
        st.dataframe(emp_load)

    names = unmapped_user_names(cached["raw"])
    unmapped = unmapped_users(names)
    if unmapped and user_role() != auth.ROLE_ACCOUNTANT:
        with st.expander("Не распределены (нет записи в сотрудниках)"):
            st.write(", ".join(unmapped))

    # Панель действий под данными: сайдбар остаётся только вводом и
    # отборами, а выгрузка и сброс — рядом с самим отчётом.
    st.markdown("---")
    col_export, col_clear = st.columns(2)
    with col_export:
        excel = export_excel(filtered, filtered_detail, emp_load, totals)
        st.download_button(
            "Скачать Excel (.xlsx)", excel,
            file_name=f"normirovanie_{cached['period_label'].replace('.', '_')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    with col_clear:
        if st.button("Очистить отчёт", key="clear_report_btn", use_container_width=True):
            st.session_state.pop("last_result", None)
            st.rerun()


def render_diagnostics_tab(base: dict):
    st.title("Диагностика OData")
    client = OneCClient(base["url"], base["login"], base["password"])
    if st.button("Проверить состав публикации"):
        with st.spinner("Чтение $metadata..."):
            try:
                sets = client.fetch_metadata_entity_sets()
            except ValueError as e:
                st.error(str(e))
                return
        doc_types = load_doc_types()
        rows: list = []
        for key, spec in doc_types.items():
            entity = spec["entity"]
            rows.append({
                "Ключ": key,
                "Опубликован": "Да" if entity in sets else "НЕТ",
                "Сущность": entity,
            })
        st.dataframe(pd.DataFrame(rows))
        st.markdown(f"Всего сущностей в составе: **{len(sets)}**")


def render_norms_tab():
    st.title("Нормы трудозатрат")
    if not _permission_flag("can_manage_norms"):
        st.warning("Недостаточно прав.")
        return
    seed_default_norms()
    st.caption("Норма на операцию, нормочасы · коэффициент 1,00 по умолчанию")
    norms = db.list_norms(active_only=False)
    if not norms:
        st.info("Нет норм. Нажмите «Засеять по умолчанию».")
    else:
        df = pd.DataFrame(norms)
        for col, fmt in [("norm_hours", "{:.3f}"), ("norm_min", "{:.1f}"),
                         ("coeff", "{:.2f}")]:
            df[col] = df[col].map(
                lambda v: fmt.format(float(v))
                if v is not None else ""
            )
        st.dataframe(df[[
            "doc_type", "category", "title", "unit",
            "norm_hours", "coeff", "active"
        ]])

    st.markdown("---")
    st.subheader("Редактирование")
    if not norms:
        st.info("Норм нет. Нажмите «Засеять нормы по умолчанию».")
    else:
        labels = [
            f"{n['title']} ({n['doc_type']})"
            for n in sorted(norms, key=lambda n: (n["category"], n["title"]))
        ]
        pick = st.selectbox("Вид документа", labels, key="norm_pick")
        doc_type_sel = pick.rsplit(" (", 1)[-1][:-1]
        norm = db.get_norm(doc_type=doc_type_sel)
        if norm:
            st.write(
                f"**{norm['title']}** · {norm['category']} · {norm['entity']}"
            )
            norm_min = st.number_input(
                "Норма, минут на единицу", min_value=0.0, step=0.5,
                value=float(norm["norm_min"]),
            )
            coeff = st.number_input(
                "Коэффициент сложности", min_value=0.0, step=0.05,
                value=float(norm["coeff"]),
            )
            sno = st.text_input("СНО (пусто = любые)", value=norm["sno"] or "")
            active = st.checkbox("Норма активна", value=bool(norm["active"]))
            comment = st.text_input(
                "Комментарий", value=norm["comment"] or ""
            )
            if st.button("Сохранить норму", key="norm_save"):
                db.upsert_norm(
                    doc_type=norm["doc_type"],
                    category=norm["category"],
                    title=norm["title"],
                    entity=norm["entity"],
                    unit=norm["unit"],
                    norm_min=norm_min,
                    coeff=coeff,
                    sno=sno or None,
                    comment=comment or None,
                    active=active,
                    date_from=norm.get("date_from"),
                    date_to=norm.get("date_to"),
                    sort_order=norm.get("sort_order", 0),
                )
                st.success("Сохранено")
                st.rerun()

    st.markdown("---")
    st.subheader("Новый вид документа")
    st.caption(
        "Для вида, которого нет в реестре: заведите норму вручную, чтобы "
        "документы перестали попадать в предупреждение об отсутствии норм. "
        "После этого вид появится в редакторе выше."
    )
    with st.form("new_norm_form"):
        n_doc_type = st.text_input(
            "Ключ вида документа (doc_type)",
            key="new_norm_type",
            help="Латиницей, уникально. Например: act_sverki.",
        ).strip()
        n_title = st.text_input(
            "Наименование для отчёта", key="new_norm_title"
        ).strip()
        n_category = st.text_input(
            "Категория (блок операций)", key="new_norm_category"
        ).strip()
        n_entity = st.text_input(
            "OData-сущность", key="new_norm_entity",
            placeholder="Document_АктСверкиВзаиморасчетов",
        ).strip()
        n_unit = st.selectbox(
            "Единица измерения",
            ["документ", "операция", "запись", "отчет", "акт", "объект",
             "сотрудник", "пакет"],
            key="new_norm_unit",
        )
        n_min = st.number_input(
            "Норма, минут на единицу", min_value=0.0, step=0.5,
            key="new_norm_min",
        )
        n_coeff = st.number_input(
            "Коэффициент сложности", min_value=0.0, step=0.05, value=1.0,
            key="new_norm_coeff",
        )
        n_comment = st.text_input(
            "Комментарий", key="new_norm_comment"
        ).strip()
        n_save = st.form_submit_button("Создать норму")
        if n_save:
            if not n_doc_type or not n_title:
                st.error("Заполните «Ключ вида документа» и «Наименование».")
            elif db.get_norm(doc_type=n_doc_type) is not None:
                st.error(
                    f"Вид с ключом «{n_doc_type}» уже есть — "
                    "отредактируйте его в блоке выше."
                )
            else:
                db.upsert_norm(
                    doc_type=n_doc_type,
                    category=n_category,
                    title=n_title,
                    entity=n_entity,
                    unit=n_unit,
                    norm_min=n_min,
                    coeff=n_coeff,
                    comment=n_comment or None,
                )
                st.success(f"Вид «{n_title}» добавлен")
                st.rerun()

    st.markdown("---")
    st.subheader("Действия")
    if st.button("Засеять нормы по умолчанию"):
        inserted = seed_default_norms()
        st.success(f"Добавлено норм: {inserted}")


# bases: list[dict]
EMPLOYEE_ROLES: list[str] = [
    "Бухгалтер по первичке", "Бухгалтер", "Главный бухгалтер",
    "Руководитель проекта", "",
]


def _employee_label(emp: dict) -> str:
    alias = f" · {emp['user_1c']}" if emp.get("user_1c") else ""
    mark = "" if emp.get("active") else " (выключен)"
    return f"{emp['full_name']}{alias}{mark}"


def _emp_edit_key(field: str, emp_id: int) -> str:
    """
    Ключ виджета формы правки сотрудника — с id выбранного сотрудника.

    Виджет с явным key хранит значение в session_state, и на следующих
    прогонах переданный в код value игнорируется. С ключом без id форма
    продолжила бы показывать (и сохранять) данные предыдущего сотрудника
    после переключения селектора.
    """

    return f"emp_edit_{field}_{emp_id}"


def render_employees_tab():
    st.title("Сотрудники аутсорсера")
    if not _permission_flag("can_manage_employees"):
        st.warning("Недостаточно прав.")
        return
    employees = db.list_employees(active_only=False)
    if employees:
        st.dataframe(pd.DataFrame(employees))

    st.markdown("---")
    st.subheader("Новый сотрудник")
    full_name = st.text_input("ФИО (ключ сотрудника)", key="emp_new_name").strip()
    user_1c = st.text_input(
        "Пользователь 1С (алиас, опц.)", key="emp_new_user1c"
    ).strip()
    role = st.selectbox(
        "Роль сотрудника", EMPLOYEE_ROLES, key="emp_new_role"
    )
    hours = st.number_input(
        "Фонд часов в месяц", min_value=1.0, value=130.0, key="emp_new_hours"
    )
    if st.button("Добавить сотрудника", key="emp_add_btn"):
        if not full_name:
            st.error("Заполните «ФИО».")
        else:
            db.upsert_employee(
                full_name=full_name, user_1c=user_1c or None,
                role=role or None, hours_per_month=hours,
            )
            st.success("Сохранено")
            st.rerun()

    st.markdown("---")
    st.subheader("Редактирование")
    # Выбор по id, а не по ФИО: ФИО изменяемо, и два сотрудника могут
    # отличаться только регистром — словарь с ключом full_name схлопнул бы их.
    if employees:
        pick_id = st.selectbox(
            "Сотрудник",
            [e["id"] for e in employees],
            format_func=lambda i: _employee_label(
                next(x for x in employees if x["id"] == i)
            ),
            key="emp_edit_pick",
        )
        emp = db.get_employee(emp_id=pick_id) or {}
        with st.form("edit_employee"):
            name2 = st.text_input(
                "ФИО", value=emp.get("full_name") or "",
                key=_emp_edit_key("name", pick_id),
            )
            user1c2 = st.text_input(
                "Пользователь 1С (алиас)", value=emp.get("user_1c") or "",
                key=_emp_edit_key("user1c", pick_id),
            )
            role2 = st.selectbox(
                "Роль сотрудника", EMPLOYEE_ROLES,
                index=EMPLOYEE_ROLES.index(emp.get("role") or "")
                if (emp.get("role") or "") in EMPLOYEE_ROLES else 0,
                key=_emp_edit_key("role", pick_id),
            )
            hours2 = st.number_input(
                "Фонд часов в месяц", min_value=1.0,
                value=float(emp.get("hours_per_month") or 130.0),
                key=_emp_edit_key("hours", pick_id),
            )
            active2 = st.checkbox(
                "Сотрудник активен", value=bool(emp.get("active")),
                key=_emp_edit_key("active", pick_id),
            )
            save2 = st.form_submit_button("Сохранить изменения")
            if save2:
                if not name2.strip():
                    st.error("Заполните «ФИО».")
                else:
                    ok = db.update_employee(
                        pick_id,
                        full_name=name2.strip(),
                        user_1c=user1c2.strip() or None,
                        role=role2 or None,
                        hours_per_month=hours2,
                        active=active2,
                    )
                    if ok:
                        st.success("Сохранено")
                        st.rerun()
                    else:
                        st.error(
                            "Не удалось сохранить: ФИО уже занято другим "
                            "сотрудником."
                        )
    else:
        st.info("Сотрудников пока нет — добавьте первого выше.")

    st.markdown("---")
    st.subheader("Массовый импорт по ФИО")
    bulk_names = st.text_area("ФИО, по одному на строку", key="bulk_emp_names")
    if st.button("Импортировать", key="bulk_emp_btn"):
        names = [n.strip() for n in bulk_names.splitlines() if n.strip()]
        if not names:
            st.warning("Нет имён для импорта.")
        else:
            for n in names:
                db.upsert_employee(full_name=n)
            st.success(f"Импортировано: {len(names)}")
            st.rerun()

    st.markdown("---")
    st.subheader("Удаление")
    existing = [e for e in employees]
    if existing:
        del_id = st.selectbox(
            "Сотрудник", [e["id"] for e in existing],
            format_func=lambda i: _employee_label(
                next(x for x in existing if x["id"] == i)
            ),
            key="emp_del_pick",
        )
        if st.button("Удалить", key="emp_del_btn"):
            db.delete_employee(del_id)
            st.success("Удалено")
            st.rerun()


def render_bases_tab():
    st.title("Базы клиентов")
    can_edit = _permission_flag("can_edit_bases")
    all_bases = _list_bases_or_error(active_only=False)
    bases = _visible_databases(all_bases)
    if not bases:
        st.info(
            "Нет баз, доступных вашей учётной записи."
            if all_bases else "Базы ещё не добавлены."
        )
    else:
        st.dataframe(pd.DataFrame(bases)[
            ["id", "name", "url", "sno", "active_doc_types", "active"]])

    if not can_edit:
        st.caption(
            "Режим просмотра: список и реквизиты доступны только для чтения. "
            "Добавление и реквизиты доступны администратору."
        )
        return

    doc_types = load_doc_types()
    dt_keys = list(doc_types.keys())

    def dt_label(k: str) -> str:
        spec = doc_types[k]
        return f"{spec.get('title', k)} · {spec.get('category', '')}"

    st.markdown("---")
    st.subheader("Массовый импорт из файла")
    st.caption(
        "Загрузите выгрузку 1С: JSON или CSV со столбцами "
        "«название, ссылка, логин, пароль» (или name, url, login, password). "
        "Существующие базы не перезаписываются, записи без ссылки "
        "пропускаются. Пароли сохраняются зашифрованными, если задан "
        "ключ AUDIT_DB_SECRET_KEY."
    )
    uploaded = st.file_uploader(
        "Файл с клиентскими базами", type=["json", "csv"],
        key="import_bases_file",
        help="Файл не сохраняется на сервере приложения и читается только "
             "в момент импорта.",
    )
    if uploaded is not None:
        try:
            parsed = db.load_client_databases(uploaded.getvalue())
        except ValueError as e:
            st.error(f"Не удалось разобрать файл: {e}")
        else:
            ready = [r for r in parsed if r["valid"]]
            broken = [r for r in parsed if not r["valid"]]
            already = [r for r in ready if db.get_base(r["url"]) is not None]
            fresh = [r for r in ready if db.get_base(r["url"]) is None]
            st.write(
                f"**Готово к импорту: {len(fresh)}**. "
                f"Уже в базе: {len(already)}. Без ссылки (пропуск): {len(broken)}."
            )
            if broken:
                st.warning(
                    "Без ссылки и будут пропущены: "
                    + ", ".join(r["name"] or "?" for r in broken[:10])
                )
            preview = fresh[:50]
            if preview:
                with st.expander(
                    f"Предпросмотр первых {len(preview)} из {len(fresh)}"
                ):
                    st.dataframe(
                        pd.DataFrame(preview)[["name", "url", "login"]],
                        use_container_width=True,
                    )
            if db._secret_key_is_broken():
                st.error(
                    "Ключ AUDIT_DB_SECRET_KEY задан, но неверен. Импорт "
                    "невозможен: приложение не станет сохранять пароли "
                    "в открытом виде. Исправьте ключ и обновите страницу."
                )
            elif fresh and st.button(
                f"Импортировать {len(fresh)} баз", type="primary",
                key="import_bases_run",
            ):
                report = db.import_bases(parsed)
                if report["errors"]:
                    for err in report["errors"][:5]:
                        st.error(err)
                st.success(
                    f"Импортировано: {report['added']}, "
                    f"пропущено (уже есть/дубль): {report['skipped']}, "
                    f"без ссылки: {report['invalid']}."
                )
                st.rerun()

    st.markdown("---")
    st.subheader("Новая база")
    with st.form("add_base"):
        name = st.text_input("Название (клиент)")
        url = st.text_input("URL OData", value="https://msk1.1cfresh.com/a/ea/")
        login = st.text_input("Логин OData")
        password = st.text_input("Пароль OData", type="password")
        sno = st.text_input("Система налогообложения (вручную)")
        new_types = st.multiselect(
            "Виды документов этой базы (пусто = все)", dt_keys,
            format_func=dt_label, key="add_base_types",
            help="Пустой список — выгружать все виды из doc_types.json.",
        )
        submitted = st.form_submit_button("Добавить")
        if submitted:
            if not name or not url:
                st.error("Укажите название и URL.")
            else:
                base = db.insert_base(
                    name, url, login, password, sno=sno or None,
                    active_doc_types=new_types,
                )
                if base is None:
                    st.warning("Такая база уже есть.")
                else:
                    st.success("База добавлена")
                    st.rerun()

    st.markdown("---")
    st.subheader("Обновление базы")
    bases = _list_bases_or_error(active_only=False)
    if bases:
        pick = st.selectbox(
            "База", [(b["name"], b["id"]) for b in bases],
            format_func=lambda x: x[0], key="edit_base_pick"
        )
        b = next((x for x in bases if x["id"] == pick[1]), None)
        with st.form("edit_base"):
            name2 = st.text_input("Название", value=b["name"])
            url2 = st.text_input("URL", value=b["url"])
            sno2 = st.text_input("СНО", value=b["sno"] or "")
            types2 = st.multiselect(
                "Виды документов этой базы (пусто = все)", dt_keys,
                default=[k for k in b["active_doc_types"] if k in dt_keys],
                format_func=dt_label, key="edit_base_types",
                help="Пустой список — выгружать все виды из doc_types.json.",
            )
            password2 = st.text_input(
                "Новый пароль (пусто = без изменений)",
                type="password"
            )
            submitted2 = st.form_submit_button("Сохранить")
            if submitted2:
                db.update_base(
                    b["id"], name=name2, url=url2, sno=sno2,
                    active_doc_types=types2, password=password2 or None,
                )
                st.success("Обновлено")
                st.rerun()
        if st.button("Удалить базу", key=f"del_{b['id']}"):
            db.delete_base(b["id"])
            st.success("Удалено")
            st.rerun()


def _role_label(role: str) -> str:
    return {
        auth.ROLE_ADMIN: "Администратор",
        auth.ROLE_MANAGER: "Руководитель",
        auth.ROLE_ACCOUNTANT: "Бухгалтер",
    }.get(str(role or "").strip(), str(role or ""))


def _employee_full_names() -> list[str]:
    return [e["full_name"] for e in db.list_employees(active_only=True)]


def render_users_tab():
    st.title("Пользователи")
    if not _is_admin():
        st.warning("Нужна роль администратора.")
        return
    users = db.list_users()
    current = st.session_state.get("login") or ""

    if users:
        view = pd.DataFrame(users)
        view["role"] = view["role"].map(_role_label)
        view["allowed_urls"] = view["allowed_urls"].apply(
            lambda x: "; ".join(x) if x else ""
        )
        view["employee_full_name"] = view["employee_full_name"].fillna("")
        st.dataframe(view[sorted(c for c in view.columns if c != "password_hash")])

    st.markdown("**➕ Добавить пользователя**")
    with st.form("users_add_form"):
        add_login = st.text_input("Логин", key="um_add_login")
        add_role = st.selectbox(
            "Роль",
            (auth.ROLE_ACCOUNTANT, auth.ROLE_MANAGER, auth.ROLE_ADMIN),
            key="um_add_role",
            format_func=_role_label,
        )
        add_show = st.checkbox("Показать пароль", key="um_add_show")
        add_pwd_type = "default" if add_show else "password"
        add_pass = st.text_input(
            f"Пароль (не короче {auth.MIN_PASSWORD_LENGTH} символов)",
            type=add_pwd_type, key="um_add_pass",
        )
        add_pass2 = st.text_input(
            "Повтор пароля", type=add_pwd_type, key="um_add_pass2"
        )
        add_urls = st.text_area(
            "Доступные базы (URL, по одному на строку)",
            key="um_add_urls",
            placeholder="https://msk1.1cfresh.com/a/ea/000000",
        )
        add_employee = ""
        if add_role == auth.ROLE_ACCOUNTANT:
            add_employee = st.selectbox(
                "Сотрудник (бухгалтер видит только его операции)",
                [""] + _employee_full_names(),
                format_func=lambda x: x or "— без привязки —",
                key="um_add_employee",
            )
        add_submitted = st.form_submit_button(
            "Добавить",
            type="primary",
            key="um_add_submit"
        )

    if add_submitted:
        login_norm = add_login.strip().lower()
        if not add_login.strip():
            st.error("Введите логин.")
        elif db.get_user(login_norm):
            st.error(f"Пользователь «{add_login.strip()}» уже существует.")
        elif not add_pass or add_pass != add_pass2:
            st.error("Пароль не заполнен или пароли не совпадают.")
        elif (weak := auth.validate_password(add_pass)):
            st.error(weak)
        else:
            urls = [u.strip() for u in add_urls.splitlines() if u.strip()]
            db.upsert_user(
                login_norm,
                add_role,
                auth.hash_password(add_pass),
                urls,
                active=True,
                employee_full_name=add_employee or None,
            )
            st.success(f"Пользователь «{add_login.strip()}» добавлен.")
            st.rerun()

    st.markdown("---")
    st.markdown("**✏️ Изменить пользователя**")
    if users:
        sel_login = st.selectbox(
            "Пользователь",
            [u["login"] for u in users],
            key="um_edit_sel",
        )
        sel = next((u for u in users if u["login"] == sel_login), None)
        if sel is not None:
            with st.form("users_edit_form"):
                edit_role = st.selectbox(
                    "Роль",
                    (auth.ROLE_ACCOUNTANT, auth.ROLE_MANAGER, auth.ROLE_ADMIN),
                    index={
                        auth.ROLE_ACCOUNTANT: 0,
                        auth.ROLE_MANAGER: 1,
                        auth.ROLE_ADMIN: 2,
                    }.get(sel["role"], 0),
                    key="um_edit_role",
                    format_func=_role_label,
                )
                edit_active = st.checkbox(
                    "Учётная запись активна",
                    value=sel.get("active", True),
                    key="um_edit_active",
                )
                edit_show = st.checkbox(
                    "Показать пароль",
                    key="um_edit_show",
                )
                edit_pass = st.text_input(
                    "Новый пароль (пусто = оставить прежний)",
                    type="default" if edit_show else "password",
                    key="um_edit_pass",
                )
                edit_pass2 = st.text_input(
                    "Повтор нового пароля",
                    type="default" if edit_show else "password",
                    key="um_edit_pass2",
                )
                edit_urls = st.text_area(
                    "Доступные базы (URL, по одному на строку)",
                    value="\n".join(sel.get("allowed_urls") or []),
                    key="um_edit_urls",
                )
                edit_employee = sel.get("employee_full_name") or ""
                if edit_role == auth.ROLE_ACCOUNTANT:
                    emp_options = [""] + _employee_full_names()
                    st.selectbox(
                        "Сотрудник (бухгалтер видит только его операции)",
                        emp_options,
                        format_func=lambda x: x or "— без привязки —",
                        key="um_edit_employee",
                        index=(
                            emp_options.index(edit_employee)
                            if edit_employee in emp_options else 0
                        ),
                    )
                edit_submitted = st.form_submit_button(
                    "Сохранить", type="primary", key="um_edit_submit"
                )

            if edit_submitted:
                err = None
                if edit_pass or edit_pass2:
                    if edit_pass != edit_pass2:
                        err = "Пароли не совпадают."
                    elif (weak := auth.validate_password(edit_pass)):
                        err = weak
                if err is None and sel_login == current and not edit_active:
                    err = "Нельзя отключить собственную учётную запись."
                if err is None:
                    admins = _active_admins(users)
                    if (
                        sel_login in {a["login"] for a in admins}
                        and len(admins) == 1
                        and (edit_role != auth.ROLE_ADMIN or not edit_active)
                    ):
                        err = "Нельзя изменить или отключить последнего действующего администратора."
                if err:
                    st.error(err)
                else:
                    current_row = db.get_user(sel_login)
                    new_hash = (current_row or {}).get("password_hash") or ""
                    if edit_pass:
                        new_hash = auth.hash_password(edit_pass)
                    urls = [u.strip() for u in edit_urls.splitlines() if u.strip()]
                    employee_link = None
                    if edit_role == auth.ROLE_ACCOUNTANT:
                        employee_link = st.session_state.get(
                            "um_edit_employee") or None
                    db.upsert_user(
                        sel_login, edit_role, new_hash, urls, active=edit_active,
                        employee_full_name=employee_link,
                    )
                    st.success(f"Пользователь «{sel_login}» обновлён.")
                    if sel_login == current:
                        _store_access_ctx()
                    st.rerun()
    else:
        st.caption("Пользователей пока нет.")

    st.markdown("---")
    st.markdown("**🗑️ Удалить пользователя**")
    if users:
        del_login = st.selectbox(
            "Пользователь",
            [u["login"] for u in users],
            key="um_del_sel",
        )
        del_confirm = st.checkbox(
            "Подтверждаю безвозвратное удаление", key="um_del_confirm"
        )
        del_clicked = st.button("Удалить", key="um_del_btn")
        if del_clicked:
            err = None
            if del_login == current:
                err = "Нельзя удалить собственную учётную запись."
            elif not del_confirm:
                err = "Подтвердите удаление флажком."
            else:
                admins = _active_admins(users)
                if del_login in {a["login"] for a in admins} and len(admins) == 1:
                    err = "Нельзя удалить последнего действующего администратора."
            if err:
                st.error(err)
            else:
                db.delete_user(del_login)
                st.success(f"Пользователь «{del_login}» удалён.")
                st.rerun()
    else:
        st.caption("Удалять пока некого.")


def idle_timeout_minutes() -> int:
    """
    Таймаут бездействия в минутах; 0 — не завершать сессию.
    Настраивается переменной окружения UI_IDLE_TIMEOUT_MIN.
    """

    raw = os.environ.get("UI_IDLE_TIMEOUT_MIN", "30")
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return 30


def _check_idle_timeout() -> bool:
    """
    Проверяет время последней активности. True — сессию завершаем.
    """

    minutes = idle_timeout_minutes()
    if minutes <= 0:
        return False
    last = st.session_state.get("login_ts")
    if not last:
        return False
    try:
        elapsed = time.time() - float(last)
    except (TypeError, ValueError):
        return False
    return elapsed > minutes * 60


def main():
    st.set_page_config(page_title="Учёт нормочасов", layout="wide")
    if auth.auth_enabled() and not st.session_state.get("login"):
        _render_login_form()
        st.stop()
    elif st.session_state.get("login"):
        if _check_idle_timeout():
            for key in (
                "login", "user_role", "user_allowed_urls", "login_ts"
            ):
                st.session_state.pop(key, None)
            st.info(
                f"Сессия завершена: бездействие более "
                f"{idle_timeout_minutes()} мин. Войдите снова."
            )
            st.stop()
        _store_access_ctx()

    _render_logout_button()

    base = select_base()

    # Вкладки по матрице прав (ТЗ §11): Пользователи — только администратор,
    # Нормы/Сотрудники — там, где есть право управления; Базы — у всех
    # (для manager/accountant — просмотр).
    flags = _permission_flags_for_session()
    tabs_def: list[tuple[str, object]] = [
        ("Отчёт", lambda: render_report_tab(base)),
        ("Диагностика", lambda: render_diagnostics_tab(base)),
    ]
    if flags["can_manage_norms"]:
        tabs_def.append(("Нормы", render_norms_tab))
    if flags["can_manage_employees"]:
        tabs_def.append(("Сотрудники", render_employees_tab))
    tabs_def.append(("Базы и доступ", render_bases_tab))
    if flags["can_manage_users"]:
        tabs_def.append(("Пользователи", render_users_tab))

    tabs = st.tabs([name for name, _ in tabs_def])
    for tab, fn in zip(tabs, tabs_def):
        with tab:
            fn[1]()


if __name__ == "__main__":
    main()
