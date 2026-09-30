import pytest
from streamlit.testing.v1 import AppTest

from core import auth, db


def _run_app():
    return AppTest.from_function(_ui_main, default_timeout=30).run()


def _ui_main():
    # Тело этой функции копируется AppTest в отдельный скрипт, поэтому здесь
    # доступны только stdlib и переменные окружения (корень проекта кладёт
    # tests/conftest.py; os.getcwd() — запасной вариант).
    import os
    import sys
    root = os.environ.get("AUDIT_TEST_ROOT") or os.getcwd()
    if root not in sys.path:
        sys.path.insert(0, root)
    from app.ui import main
    main()


@pytest.fixture
def clean_db():
    """Пустая тестовая БД (conftest чистит файл между тестами)."""
    db.init_db()
    yield
    db.init_db()


def _login(at, login, password):
    at.text_input(key="login_user").set_value(login)
    at.text_input(key="login_pass").set_value(password)
    at.button(key="btn_login").click()
    at.run()


def _has_key(at, prop, key):
    for el in getattr(at, prop):
        if getattr(el, "key", None) == key:
            return True
    return False


def _emp_edit_key(field: str, emp_id: int) -> str:
    """Ключ виджета формы правки сотрудника (совпадает с app.ui)."""

    return f"emp_edit_{field}_{emp_id}"


def test_no_auth_no_login_form(clean_db):
    at = _run_app()
    assert not at.exception
    assert not _has_key(at, "text_input", "login_user")
    # Пользователей нет — вкладки доступны без входа
    labels = {t.label for t in at.tabs}
    assert labels == {
        "Отчёт", "Диагностика", "Нормы", "Сотрудники", "Базы и доступ", "Пользователи",
    }


def test_login_form_shown_when_users_exist(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    at = _run_app()
    assert not at.exception
    assert _has_key(at, "text_input", "login_user")
    assert _has_key(at, "text_input", "login_pass")
    assert _has_key(at, "button", "btn_login")


def test_wrong_password(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    at = _run_app()
    _login(at, "admin", "wrong")
    assert not at.exception
    assert any("Неверный логин" in e.value for e in at.error)
    assert "login" not in at.session_state


def test_successful_login_sets_session(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    at = _run_app()
    _login(at, "admin", "secret")
    assert not at.exception
    assert at.session_state["login"] == "admin"
    assert at.session_state["user_role"] == auth.ROLE_ADMIN
    # После входа показывается кнопка выхода
    assert _has_key(at, "button", "btn_logout")


def test_logout_clears_session(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    at = _run_app()
    _login(at, "admin", "secret")
    at.button(key="btn_logout").click()
    at.run()
    assert "login" not in at.session_state


def test_admin_sees_all_bases(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    db.insert_base("База А", "https://msk1.1cfresh.com/a/ea/1119958", "u", "p")
    db.insert_base("База Б", "https://msk1.1cfresh.com/a/ea/2222222", "u", "p")
    at = _run_app()
    _login(at, "admin", "secret")
    assert not at.exception
    box = at.sidebar.selectbox(key="base_select")
    assert {o for o in box.options} == {"База А", "База Б"}


def test_accountant_sees_only_allowed_base(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    db.upsert_user(
        "acct",
        auth.ROLE_ACCOUNTANT,
        auth.hash_password("secret"),
        ["https://msk1.1cfresh.com/a/ea/1119958"],
    )
    db.insert_base("База А", "https://msk1.1cfresh.com/a/ea/1119958", "u", "p")
    db.insert_base("База Б", "https://msk1.1cfresh.com/a/ea/2222222", "u", "p")
    at = _run_app()
    _login(at, "acct", "secret")
    assert not at.exception
    box = at.sidebar.selectbox(key="base_select")
    assert box.options == ["База А"]


def test_non_admin_blocked_from_users_tab(clean_db):
    db.upsert_user("acct", auth.ROLE_ACCOUNTANT, auth.hash_password("secret"), [])
    at = _run_app()
    _login(at, "acct", "secret")
    assert not at.exception
    labels = {t.label for t in at.tabs}
    assert "Пользователи" not in labels
    assert "Нормы" not in labels
    assert "Сотрудники" not in labels
    assert "Отчёт" in labels
    assert "Диагностика" in labels
    assert "Базы и доступ" in labels
    assert any("Режим просмотра" in c.value for c in at.caption)


def test_manager_tabs_and_readonly_bases(clean_db):
    db.upsert_user("mgr", auth.ROLE_MANAGER, auth.hash_password("secret"), [])
    db.insert_base("База А", "https://msk1.1cfresh.com/a/ea/1119958", "u", "p")
    at = _run_app()
    _login(at, "mgr", "secret")
    assert not at.exception
    labels = {t.label for t in at.tabs}
    assert "Нормы" in labels
    assert "Сотрудники" in labels
    assert "Базы и доступ" in labels
    assert "Пользователи" not in labels
    assert any("Режим просмотра" in c.value for c in at.caption)
    # Базы видны все (manager смотреть может), но edit-форм нет
    box = at.sidebar.selectbox(key="base_select")
    assert box.options == ["База А"]
    assert not _has_key(at, "button", "bases_add_submit")


def test_admin_can_render_users_tab(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    at = _run_app()
    _login(at, "admin", "secret")
    assert not at.exception
    assert _has_key(at, "text_input", "um_add_login")
    assert _has_key(at, "button", "um_add_submit")


def test_add_user_via_ui(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    at = _run_app()
    _login(at, "admin", "secret")
    at.text_input(key="um_add_login").set_value("newuser")
    at.selectbox(key="um_add_role").set_value(auth.ROLE_ACCOUNTANT)
    at.text_input(key="um_add_pass").set_value("secret123")
    at.text_input(key="um_add_pass2").set_value("secret123")
    at.button(key="um_add_submit").click()
    at.run()
    assert not at.exception
    assert db.get_user("newuser") is not None


def test_last_admin_protected(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    at = _run_app()
    _login(at, "admin", "secret")
    # Попытка разжаловать последнего администратора той же ролью
    at.selectbox(key="um_edit_role").set_value(auth.ROLE_ACCOUNTANT)
    at.button(key="um_edit_submit").click()
    at.run()
    assert not at.exception
    assert any("последнего" in e.value for e in at.error)
    assert db.get_user("admin")["role"] == auth.ROLE_ADMIN


def test_role_label_in_sidebar_is_russian(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    at = _run_app()
    _login(at, "admin", "secret")
    assert not at.exception
    caption = next(c.value for c in at.sidebar.caption if "admin" in c.value)
    assert "Администратор" in caption
    assert "· admin" not in caption


def test_login_password_is_always_masked(clean_db):
    """Переключателя «показать пароль» на форме входа нет (ui.py)."""
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    at = _run_app()
    assert not _has_key(at, "checkbox", "login_show_pwd")
    _login(at, "admin", "secret")
    assert not at.exception
    assert at.session_state["login"] == "admin"


def test_locked_login_shows_lockout_message(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    for _ in range(auth.MAX_LOGIN_ATTEMPTS):
        auth.register_failure("admin")
    at = _run_app()
    _login(at, "admin", "secret")
    assert not at.exception
    assert any("заблокирован" in e.value for e in at.error)
    assert "login" not in at.session_state


def test_idle_timeout_logs_out(clean_db, monkeypatch):
    import time as _time

    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    monkeypatch.setenv("UI_IDLE_TIMEOUT_MIN", "1")
    at = _run_app()
    _login(at, "admin", "secret")
    assert at.session_state["login"] == "admin"
    # Отматываем метку последней активности за 5 минут
    at.session_state["login_ts"] = _time.time() - 5 * 60
    at.run()
    assert "login" not in at.session_state
    assert any("бездейств" in i.value for i in at.info)


def test_idle_timeout_disabled_by_zero(clean_db, monkeypatch):
    import time as _time

    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    monkeypatch.setenv("UI_IDLE_TIMEOUT_MIN", "0")
    at = _run_app()
    _login(at, "admin", "secret")
    at.session_state["login_ts"] = _time.time() - 5 * 60
    at.run()
    assert at.session_state["login"] == "admin"


def test_report_variants_and_sort_widgets(clean_db):
    from app import ui as ui_mod

    assert "Пользовательская" in ui_mod.REPORT_VARIANTS
    for name, spec in ui_mod.REPORT_VARIANTS.items():
        grouping = spec.get("grouping")
        if grouping is not None:
            assert set(grouping) <= set(ui_mod.GROUPING_OPTIONS), name
    assert ui_mod.REPORT_VARIANTS["Операции без ответственного"]["unassigned"]
    assert ui_mod.SORTABLE_COLUMNS[0] == "Клиент"


def test_active_filters_summary():
    from app.ui import _active_filters_summary

    assert _active_filters_summary({}) == "нет"
    summary = _active_filters_summary(
        {"client": "ООО Альфа", "min_hours": 1.5, "unassigned_only": False}
    )
    assert "Клиент" in summary
    assert "1.5" in summary


def test_apply_filters_tolerates_missing_columns():
    """Отчёты без колонки «Клиент» (вариант по видам документов)."""
    import pandas as pd

    from app.ui import apply_filters

    report = pd.DataFrame([
        {"Вид документа": "Авансовый отчет", "Вид операции": "Прочие",
         "Количество операций": 3, "Трудозатраты, нормочасы": 0.75,
         "Ответственный сотрудник": "Иванова Анна"},
        {"Вид документа": "Поступление", "Вид операции": "Оплата",
         "Количество операций": 1, "Трудозатраты, нормочасы": 0.04,
         "Ответственный сотрудник": "Не распределено"},
    ])
    filters = {
        "client": [], "inn": "77", "sno": [], "employee": [],
        "unassigned_only": True, "role": [], "doc_type": [],
        "op_type": [], "min_qty": 0, "min_hours": 0.0,
    }
    result = apply_filters(report, filters)
    assert list(result["Вид документа"]) == ["Поступление"]

    # Неизвестные фильтры по отсутствующим колонкам не ломают расчёт
    result = apply_filters(report, {**filters, "client": ["ООО Альфа"],
                                    "unassigned_only": False})
    assert len(result) == 2


def test_norms_tab_uses_select_and_saves_active(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    db.upsert_norm(
        doc_type="bank_incoming", category="Банк и касса",
        title="Поступление на расчетный счет",
        entity="Document_Поступление", norm_hours=0.05, coeff=1.0,
        active=True,
    )
    at = _run_app()
    _login(at, "admin", "secret")
    assert not at.exception
    pick = at.selectbox(key="norm_pick")
    assert "Поступление на расчетный счет (bank_incoming)" in pick.options
    # Активность и комментарий редактируются
    labels = {t.label for t in at.text_input}
    assert "Комментарий" in labels
    assert "СНО (пусто = любые)" in labels
    # Выбираем нужную норму, гасим её и сохраняем
    pick.set_value("Поступление на расчетный счет (bank_incoming)")
    at.run()
    for cbox in at.checkbox:
        if cbox.label == "Норма активна":
            cbox.set_value(False)
    for t in at.text_input:
        if t.label == "Комментарий":
            t.set_value("Проверка комментария")
    at.run()
    at.button(key="norm_save").click()
    at.run()
    assert not at.exception
    saved = db.get_norm(doc_type="bank_incoming")
    assert saved["active"] is False
    assert saved["comment"] == "Проверка комментария"


def test_users_tab_lists_users(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    db.upsert_user("acc", auth.ROLE_ACCOUNTANT, auth.hash_password("secret"), [])
    at = _run_app()
    _login(at, "admin", "secret")
    assert not at.exception
    frames = [d.value for d in at.dataframe if "login" in d.value.columns]
    assert frames, "ожидалась таблица пользователей"
    users_view = frames[0]
    assert {"admin", "acc"} <= set(users_view["login"])
    assert "password_hash" not in users_view.columns
    assert "Администратор" in set(users_view["role"])


def test_weak_password_rejected_in_ui(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    at = _run_app()
    _login(at, "admin", "secret")
    at.text_input(key="um_add_login").set_value("weak")
    at.text_input(key="um_add_pass").set_value("short")
    at.text_input(key="um_add_pass2").set_value("short")
    at.button(key="um_add_submit").click()
    at.run()
    assert not at.exception
    assert any("не короче" in e.value for e in at.error)
    assert db.get_user("weak") is None


def test_show_password_checkbox_in_user_forms(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    at = _run_app()
    _login(at, "admin", "secret")
    assert _has_key(at, "checkbox", "um_add_show")
    assert _has_key(at, "checkbox", "um_edit_show")
    at.checkbox(key="um_add_show").set_value(True)
    at.run()
    assert not at.exception
    at.checkbox(key="um_edit_show").set_value(True)
    at.run()
    assert not at.exception


def _raw_with_mapped_and_unmapped():
    import pandas as pd

    def row(employee):
        return {
            "Клиент": "ООО Альфа", "ИНН": "7700000001",
            "Система налогообложения": "", "Период": "01.2026",
            "Вид документа": "Поступление на расчетный счет",
            "Вид операции": "Оплата от покупателя",
            "Количество операций": 2, "Ответственный сотрудник": employee,
            "Роль сотрудника": "", "Норма на операцию": None,
            "Коэффициент сложности": None, "Трудозатраты, нормочасы": None,
            "Комментарий": "",
        }

    return pd.DataFrame([
        row("Иванова Анна"),
        row("Неизвестный Н."),
    ])


def _seed_report_cache(at, raw):
    """Кладёт в кэш сессии готовый «сырой» отчёт за январь 2026."""
    from app import ui as ui_mod

    start, end, label = ui_mod.period_bounds_for_month("01", 2026)
    at.session_state["last_result"] = {
        "raw": raw,
        "skipped": [],
        "base_url": "https://x.example/a",
        "base_name": "База",
        "period_label": label,
        "period_start": start,
        "period_end": end,
    }
    at.selectbox(key="period_mode").set_value("Месяц")
    at.selectbox(key="period_month").set_value("01")
    at.number_input(key="period_year").set_value(2026)
    return label


def test_variant_unassigned_filters_report(clean_db):
    db.upsert_employee(full_name="Иванова Анна", user_1c=None,
                        role="Бухгалтер", hours_per_month=130.0)
    db.insert_base("База", "https://x.example/a", "u", "p")
    at = _run_app()
    _seed_report_cache(at, _raw_with_mapped_and_unmapped())
    at.run()
    assert not at.exception
    # Вариант по умолчанию — обе строки
    report_frames = [
        d.value for d in at.dataframe
        if "Ответственный сотрудник" in d.value.columns
    ]
    assert set(report_frames[0]["Ответственный сотрудник"]) == {
        "Иванова Анна", "Не распределено",
    }

    at.selectbox(key="report_variant").set_value(
        "Операции без ответственного"
    )
    at.run()
    assert not at.exception
    assert any(
        "только «Не распределено»" in c.value for c in at.sidebar.caption
    )
    report_frames = [
        d.value for d in at.dataframe
        if "Ответственный сотрудник" in d.value.columns
    ]
    assert set(report_frames[0]["Ответственный сотрудник"]) == {
        "Не распределено"
    }


def test_variant_grouping_uses_cache(clean_db):
    """Смена варианта не требует повторной выгрузки (ТЗ §9.3)."""
    db.insert_base("База", "https://x.example/a", "u", "p")
    at = _run_app()
    _seed_report_cache(at, _raw_with_mapped_and_unmapped())
    at.run()
    at.selectbox(key="report_variant").set_value(
        "Нормирование по видам документов"
    )
    at.run()
    assert not at.exception
    frames = [d.value for d in at.dataframe if "Вид документа" in d.value.columns]
    assert frames
    assert "Клиент" not in frames[0].columns
    # Сортировка появляется вместе с отчётом
    assert _has_key(at, "selectbox", "report_sort_col")
    assert _has_key(at, "radio", "report_sort_dir")


def test_variant_deviation_shows_info(clean_db):
    db.insert_base("База", "https://x.example/a", "u", "p")
    at = _run_app()
    _seed_report_cache(at, _raw_with_mapped_and_unmapped())
    at.run()
    at.selectbox(key="report_variant").set_value("Отклонение от нормы")
    at.run()
    assert not at.exception
    assert any("очередь II" in i.value for i in at.info)
# ============== СОТРУДНИКИ: ПРАВКА ПО ID, КАСКАД, ДУБЛИ ====================
# Вкладки в st.tabs отрисовываются все сразу, поэтому переключать их
# не нужно — виджеты адресуются по key.

_EDIT_SAVE = "FormSubmitter:edit_employee-Сохранить изменения"


def test_employee_add_and_edit_forms_are_separate(clean_db):
    at = _run_app()
    subheaders = {s.value for s in at.subheader}
    assert "Новый сотрудник" in subheaders
    assert "Редактирование" in subheaders
    assert _has_key(at, "text_input", "emp_new_name")
    assert _has_key(at, "button", "emp_add_btn")


def test_employee_edit_renames_and_keeps_id(clean_db):
    """ФИО меняется — запись адресуется по id, бухгалтер не теряет связь."""
    emp = db.upsert_employee(full_name="Иванова Анна", user_1c="Иванова А.А.")
    db.upsert_user(
        "buh1", auth.ROLE_ACCOUNTANT, auth.hash_password("secret"), [],
        employee_full_name="Иванова Анна",
    )
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    at = _run_app()
    _login(at, "admin", "secret")
    assert not at.exception
    assert _has_key(at, "selectbox", "emp_edit_pick")

    at.selectbox(key="emp_edit_pick").set_value(emp["id"])
    at.run()
    _ID = emp["id"]
    assert at.text_input(key=_emp_edit_key("name", _ID)).value == "Иванова Анна"
    at.text_input(key=_emp_edit_key("name", _ID)).set_value("Иванова Анна Петровна")
    at.run()
    at.button(key=_EDIT_SAVE).click()
    at.run()
    assert not at.exception

    got = db.get_employee(emp_id=emp["id"])
    assert got["full_name"] == "Иванова Анна Петровна"
    # id прежний, алиас не затронут
    assert got["id"] == emp["id"]
    assert got["user_1c"] == "Иванова А.А."
    # Каскад: привязка учётной записи переехала вместе с сотрудником
    assert db.get_user("buh1")["employee_full_name"] == "Иванова Анна Петровна"
    assert db.get_employee(full_name="Иванова Анна") is None


def test_employee_edit_switch_employee_refreshes_form(clean_db):
    """Переключение сотрудника в селекторе должно перезаполнить форму.

    Виджеты с явным key хранят состояние в session_state, поэтому value из
    кода на следующих прогонах игнорируется: без привязки key к id
    выбранного сотрудника форма продолжала бы показывать данные
    предыдущего — и правка сохранила бы чужие значения.
    """
    first = db.upsert_employee(
        full_name="Иванова Анна", user_1c="Е_Иванова", role="Бухгалтер",
        hours_per_month=130.0,
    )
    second = db.upsert_employee(
        full_name="Петрова Елена", user_1c="Е_Петрова", role="Главный бухгалтер",
        hours_per_month=100.0,
    )
    at = _run_app()

    at.selectbox(key="emp_edit_pick").set_value(first["id"])
    at.run()
    assert at.text_input(key=_emp_edit_key("name", first["id"])).value == "Иванова Анна"
    assert at.text_input(key=_emp_edit_key("user1c", first["id"])).value == "Е_Иванова"
    assert at.selectbox(key=_emp_edit_key("role", first["id"])).value == "Бухгалтер"
    assert at.number_input(key=_emp_edit_key("hours", first["id"])).value == pytest.approx(130.0)

    at.selectbox(key="emp_edit_pick").set_value(second["id"])
    at.run()
    assert at.text_input(key=_emp_edit_key("name", second["id"])).value == "Петрова Елена"
    assert at.text_input(key=_emp_edit_key("user1c", second["id"])).value == "Е_Петрова"
    assert at.selectbox(key=_emp_edit_key("role", second["id"])).value == "Главный бухгалтер"
    assert at.number_input(key=_emp_edit_key("hours", second["id"])).value == pytest.approx(100.0)

    # и обратно — прежние значения не «залипают»
    at.selectbox(key="emp_edit_pick").set_value(first["id"])
    at.run()
    assert at.text_input(key=_emp_edit_key("name", first["id"])).value == "Иванова Анна"
    assert at.text_input(key=_emp_edit_key("user1c", first["id"])).value == "Е_Иванова"
    assert at.number_input(key=_emp_edit_key("hours", first["id"])).value == pytest.approx(130.0)


def test_employee_edit_duplicate_name_shows_error(clean_db):
    db.upsert_employee(full_name="Иванова Анна")
    second = db.upsert_employee(full_name="Петрова Елена")
    at = _run_app()
    at.selectbox(key="emp_edit_pick").set_value(second["id"])
    at.run()
    at.text_input(key=_emp_edit_key("name", second["id"])).set_value("Иванова Анна")
    at.run()
    at.button(key=_EDIT_SAVE).click()
    at.run()
    assert not at.exception
    assert any("уже занято" in e.value for e in at.error)
    assert db.get_employee(emp_id=second["id"])["full_name"] == "Петрова Елена"


def test_employee_edit_can_deactivate_and_change_role(clean_db):
    emp = db.upsert_employee(full_name="Петрова Елена", role="Бухгалтер")
    at = _run_app()
    at.selectbox(key="emp_edit_pick").set_value(emp["id"])
    at.run()
    at.checkbox(key=_emp_edit_key("active", emp["id"])).set_value(False)
    at.selectbox(key=_emp_edit_key("role", emp["id"])).set_value("Главный бухгалтер")
    at.number_input(key=_emp_edit_key("hours", emp["id"])).set_value(100.0)
    at.run()
    at.button(key=_EDIT_SAVE).click()
    at.run()
    assert not at.exception
    got = db.get_employee(emp_id=emp["id"])
    assert got["active"] is False
    assert got["role"] == "Главный бухгалтер"
    assert got["hours_per_month"] == pytest.approx(100.0)
    assert db.list_employees(active_only=True) == []


def test_employee_delete_selector_uses_id_not_name(clean_db):
    """Два ФИО, отличающиеся регистром, не должны схлопнуться в селекторе."""
    db.upsert_employee(full_name="Иванова Анна")
    db.upsert_employee(full_name="ИВАНОВА АННА")
    at = _run_app()
    picker = at.selectbox(key="emp_del_pick")
    assert len(picker.options) == 2
    picker.set_value(picker.options[1])
    at.run()
    at.button(key="emp_del_btn").click()
    at.run()
    assert not at.exception
    assert len(db.list_employees(active_only=False)) == 1


# =============== ВИДЫ ДОКУМЕНТОВ НА УРОВНЕ БАЗЫ (multiselect) ================

def test_bases_tab_has_doc_types_multiselect(clean_db):
    from app import ui as ui_mod

    db.insert_base("База", "https://x.example/a", "u", "p")
    at = _run_app()
    keys = [m.key for m in at.multiselect]
    assert "add_base_types" in keys
    assert "edit_base_types" in keys
    registry = ui_mod.load_doc_types()
    # format_func показывает «Наименование · Категория»
    expected = {
        f"{spec.get('title', k)} · {spec.get('category', '')}"
        for k, spec in registry.items()
    }
    for m in at.multiselect:
        if m.key in ("add_base_types", "edit_base_types"):
            assert set(m.options) == expected
            assert m.value == []  # по умолчанию «выгружать все»


def test_base_doc_types_roundtrip_through_ui(clean_db):
    base = db.insert_base("База", "https://x.example/a", "u", "p")
    at = _run_app()
    picker = at.selectbox(key="edit_base_pick")
    assert len(picker.options) == 1
    at.run()
    at.multiselect(key="edit_base_types").set_value(
        ["bank_incoming", "payment_order"]
    )
    at.run()
    at.button(key="FormSubmitter:edit_base-Сохранить").click()
    at.run()
    assert not at.exception
    assert db.get_base_by_id(base["id"])["active_doc_types"] == [
        "bank_incoming", "payment_order",
    ]


def test_bases_tab_viewonly_has_no_multiselect_for_manager(clean_db):
    db.upsert_user("mgr", auth.ROLE_MANAGER, auth.hash_password("secret"), [])
    db.insert_base("База", "https://x.example/a", "u", "p")
    at = _run_app()
    _login(at, "mgr", "secret")
    assert not at.exception
    keys = [m.key for m in at.multiselect]
    assert "add_base_types" not in keys
    assert "edit_base_types" not in keys


def test_run_report_narrows_by_base_allowlist(monkeypatch):
    """Настройка базы — белый список, блок операций выбирает внутри него."""
    from app import ui as ui_mod
    import pandas as pd

    seen: dict = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

    def fake_fetch(client, start, end, doc_types=None, sno=""):
        seen["doc_types"] = list(doc_types or [])
        return pd.DataFrame()

    monkeypatch.setattr(ui_mod, "OneCClient", FakeClient)
    monkeypatch.setattr(ui_mod, "fetch_documents", fake_fetch)

    base = {"url": "u", "login": "l", "password": "p", "sno": "",
            "active_doc_types": ["bank_incoming", "cash_income"]}
    ui_mod.run_report(
        base, "2026-01-01", "2026-01-31",
        ["bank_incoming", "payment_order", "cash_income"],
    )
    assert seen["doc_types"] == ["bank_incoming", "cash_income"]

    # Пустой белый список = выгружать все, что выбрал пользователь
    base["active_doc_types"] = []
    ui_mod.run_report(
        base, "2026-01-01", "2026-01-31", ["bank_incoming", "payment_order"],
    )
    assert seen["doc_types"] == ["bank_incoming", "payment_order"]

    # Отсутствующий ключ (старый dict из кэша) = все
    ui_mod.run_report(
        {"url": "u", "login": "l", "password": "p", "sno": ""},
        "2026-01-01", "2026-01-31", ["bank_incoming"],
    )
    assert seen["doc_types"] == ["bank_incoming"]


# ============ ПАНЕЛЬ ДЕЙСТВИЙ: ВЫГРУЗКА И ОЧИСТКА ОТЧЁТА (в теле) ==========

def _raw_with_unknown_doc_type():
    """Документ, для которого в реестре нет нормы (ТЗ §4.1)."""
    import pandas as pd

    return pd.DataFrame([{
        "Клиент": "ООО Альфа", "ИНН": "7700000001",
        "Система налогообложения": "УСН Доходы", "Период": "01.2026",
        "Вид документа": "Акт сверки расчетов с контрагентом",
        "Вид операции": "Прочие", "Количество операций": 1,
        "Ответственный сотрудник": "Иванова Анна", "Роль сотрудника": "",
        "Норма на операцию": None, "Коэффициент сложности": None,
        "Трудозатраты, нормочасы": None, "Комментарий": "",
    }])


def _normed_raw():
    """Сырые данные, у которых все виды документов нормированы."""
    import pandas as pd

    return pd.DataFrame([{
        "Клиент": "ООО Альфа", "ИНН": "7700000001",
        "Система налогообложения": "УСН Доходы", "Период": "01.2026",
        "Вид документа": "Поступление на расчетный счет",
        "Вид операции": "Оплата от покупателя", "Количество операций": 2,
        "Ответственный сотрудник": "Иванова Анна", "Роль сотрудника": "",
        "Норма на операцию": None, "Коэффициент сложности": None,
        "Трудозатраты, нормочасы": None, "Комментарий": "",
    }])


def test_excel_button_moved_to_main_area_not_sidebar(clean_db):
    db.insert_base("База", "https://x.example/a", "u", "p")
    at = _run_app()
    _seed_report_cache(at, _normed_raw())
    at.run()
    assert not at.exception
    dl_labels = {d.label for d in at.get("download_button")}
    assert "Скачать Excel (.xlsx)" in dl_labels
    # В сайдбаре выгрузки больше нет
    assert not any(
        "Excel" in b.label for b in at.sidebar.button
    ), [b.label for b in at.sidebar.button]


def test_clear_report_button_resets_session(clean_db):
    db.insert_base("База", "https://x.example/a", "u", "p")
    at = _run_app()
    _seed_report_cache(at, _normed_raw())
    at.run()
    assert "last_result" in at.session_state
    assert _has_key(at, "button", "clear_report_btn")

    at.button(key="clear_report_btn").click()
    at.run()
    assert not at.exception
    # Кэш сброшен — интерфейс вернулся в исходное состояние
    assert "last_result" not in at.session_state
    assert not any(
        d.label == "Скачать Excel (.xlsx)" for d in at.get("download_button")
    )


def test_clear_and_export_are_side_by_side_columns(clean_db):
    """Панель действий — один ряд из двух колонок под данными."""
    db.insert_base("База", "https://x.example/a", "u", "p")
    at = _run_app()
    _seed_report_cache(at, _normed_raw())
    at.run()
    cols = [c for c in at.columns]
    assert len(cols) == 2
    assert cols[0].download_button
    assert any(b.key == "clear_report_btn" for b in cols[1].button)


# ================= ПРЕДУПРЕЖДЕНИЕ О НЕЗАДАННЫХ НОРМАХ =====================

def test_report_warns_about_missing_norms(clean_db):
    """Приложение само сеет нормы из doc_types.json, поэтому «пропуск» —
    это вид документа, которого в реестре нет."""
    db.insert_base("База", "https://x.example/a", "u", "p")
    at = _run_app()
    _seed_report_cache(at, _raw_with_unknown_doc_type())
    at.run()
    assert not at.exception
    warn = [w.value for w in at.warning if "нормы" in w.value.lower()]
    assert warn, [w.value for w in at.warning]
    assert "нормой 0" in warn[0]


def test_report_warning_lists_titles_as_bullets(clean_db):
    import pandas as pd

    def row(title):
        return {
            "Клиент": "ООО Альфа", "ИНН": "1",
            "Система налогообложения": "", "Период": "01.2026",
            "Вид документа": title, "Вид операции": "Прочие",
            "Количество операций": 1, "Ответственный сотрудник": "Неизвестный Н.",
            "Роль сотрудника": "", "Норма на операцию": None,
            "Коэффициент сложности": None, "Трудозатраты, нормочасы": None,
            "Комментарий": "",
        }

    db.insert_base("База", "https://x.example/a", "u", "p")
    at = _run_app()
    _seed_report_cache(at, pd.DataFrame([row("Акт сверки"), row("Доверенность")]))
    at.run()
    assert not at.exception
    bullets = "\n".join(m.value for m in at.markdown)
    assert "- Акт сверки" in bullets
    assert "- Доверенность" in bullets


def test_no_warning_when_all_norms_defined(clean_db):
    db.insert_base("База", "https://x.example/a", "u", "p")
    db.upsert_norm(
        doc_type="bank_incoming", category="Банк и касса",
        title="Поступление на расчетный счет",
        entity="Document_ПоступлениеНаРасчетныйСчет", norm_hours=0.04,
    )
    at = _run_app()
    _seed_report_cache(at, _normed_raw())
    at.run()
    assert not at.exception
    assert not any("нормой 0" in w.value for w in at.warning)


def test_missing_norms_warning_hidden_from_accountant(clean_db):
    db.upsert_employee(full_name="Иванова Анна", user_1c="Иванова А.А.")
    db.upsert_user(
        "buh1", auth.ROLE_ACCOUNTANT, auth.hash_password("secret"),
        ["https://x.example/a"], employee_full_name="Иванова Анна",
    )
    db.insert_base("База", "https://x.example/a", "u", "p")
    at = _run_app()
    _login(at, "buh1", "secret")
    _seed_report_cache(at, _raw_with_mapped_and_unmapped())
    at.run()
    assert not at.exception
    # Бухгалтер не правит нормы — предупреждение ему не показываем
    assert not any("нормой 0" in w.value for w in at.warning)


# ============ НОРМЫ: РУЧНОЕ СОЗДАНИЕ НОВОГО ВИДА ДОКУМЕНТА =================

def test_norms_tab_can_create_new_doc_type(clean_db):
    at = _run_app()
    assert _has_key(at, "text_input", "new_norm_type")
    assert _has_key(at, "text_input", "new_norm_title")
    at.text_input(key="new_norm_type").set_value("act_sverki")
    at.text_input(key="new_norm_title").set_value(
        "Акт сверки расчетов с контрагентом"
    )
    at.text_input(key="new_norm_category").set_value("Расчёты и сверка")
    at.text_input(key="new_norm_entity").set_value("Document_АктСверки")
    at.number_input(key="new_norm_min").set_value(15.0)
    at.run()
    at.button(key="FormSubmitter:new_norm_form-Создать норму").click()
    at.run()
    assert not at.exception
    got = db.get_norm(doc_type="act_sverki")
    assert got is not None
    assert got["title"] == "Акт сверки расчетов с контрагентом"
    assert got["category"] == "Расчёты и сверка"
    assert got["entity"] == "Document_АктСверки"
    # 15 минут -> 0,25 нормочаса (upsert_norm пересчитывает)
    assert got["norm_min"] == pytest.approx(15.0)
    assert got["norm_hours"] == pytest.approx(0.25)


def test_new_norm_rejects_duplicate_doc_type(clean_db):
    db.upsert_norm(
        doc_type="bank_incoming", category="Банк и касса",
        title="Поступление на расчетный счет", entity="Document_X",
    )
    at = _run_app()
    at.text_input(key="new_norm_type").set_value("bank_incoming")
    at.text_input(key="new_norm_title").set_value("Дубль")
    at.run()
    at.button(key="FormSubmitter:new_norm_form-Создать норму").click()
    at.run()
    assert not at.exception
    assert any("уже есть" in e.value for e in at.error)
    assert db.get_norm(doc_type="bank_incoming")["title"] == (
        "Поступление на расчетный счет"
    )


def test_new_norm_requires_key_and_title(clean_db):
    at = _run_app()
    at.button(key="FormSubmitter:new_norm_form-Создать норму").click()
    at.run()
    assert not at.exception
    assert any("Заполните" in e.value for e in at.error)
    # Норма с пустым ключом не создана (стартовый сид реестра не считаем)
    assert all(n["doc_type"] for n in db.list_norms())


def test_newly_created_norm_appears_in_editor(clean_db):
    """Созданный вручную вид доступен для правки в общем списке."""
    db.upsert_norm(
        doc_type="act_sverki", category="Расчёты и сверка",
        title="Акт сверки расчетов с контрагентом",
        entity="Document_АктСверки", norm_hours=0.25,
    )
    at = _run_app()
    pick = at.selectbox(key="norm_pick")
    assert any("act_sverki" in str(o) for o in pick.options)
