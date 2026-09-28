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


def test_login_password_can_be_shown(clean_db):
    db.upsert_user("admin", auth.ROLE_ADMIN, auth.hash_password("secret"), [])
    at = _run_app()
    assert at.checkbox(key="login_show_pwd").value is False
    at.checkbox(key="login_show_pwd").set_value(True)
    at.run()
    assert not at.exception
    assert at.checkbox(key="login_show_pwd").value is True
    assert at.text_input(key="login_pass") is not None


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