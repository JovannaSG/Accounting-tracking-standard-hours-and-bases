import pytest

from core import auth, db


@pytest.fixture
def clean_db():
    """Пустая тестовая БД (conftest чистит файл между тестами)."""
    db.init_db()
    yield
    db.init_db()


def test_permission_matrix_shapes():
    flags = auth.PERMISSION_MATRIX
    assert set(flags) == {auth.ROLE_ADMIN, auth.ROLE_MANAGER, auth.ROLE_ACCOUNTANT}
    expected_keys = {
        "can_manage_users",
        "can_manage_norms",
        "can_manage_employees",
        "can_edit_bases",
        "can_see_all_bases",
    }
    for role, row in flags.items():
        assert set(row) == expected_keys, f"роль {role}: не все флаги"


def test_permission_flags_from_role():
    admin = auth.permission_flags_from_role(auth.ROLE_ADMIN)
    assert all(admin.values())

    manager = auth.permission_flags_from_role(auth.ROLE_MANAGER)
    assert manager["can_manage_norms"] is True
    assert manager["can_manage_employees"] is True
    assert manager["can_see_all_bases"] is True
    assert manager["can_manage_users"] is False
    assert manager["can_edit_bases"] is False

    accountant = auth.permission_flags_from_role(auth.ROLE_ACCOUNTANT)
    assert not any(accountant.values())


def test_permission_flags_by_login(clean_db):
    db.upsert_user("adm", auth.ROLE_ADMIN, "h", [])
    db.upsert_user("mgr", auth.ROLE_MANAGER, "h", [])
    db.upsert_user("acc", auth.ROLE_ACCOUNTANT, "h", [])

    assert auth.permission_flags("adm")["can_manage_users"] is True
    assert auth.permission_flags("mgr")["can_manage_norms"] is True
    assert auth.permission_flags("mgr")["can_edit_bases"] is False
    assert auth.permission_flags("acc")["can_see_all_bases"] is False
    # неизвестный пользователь — прав нет
    assert not any(auth.permission_flags("ghost").values())


def test_user_role_manager(clean_db):
    db.upsert_user("mgr", auth.ROLE_MANAGER, "h", [])
    assert auth.user_role("mgr") == auth.ROLE_MANAGER


def test_user_can_access_by_role(clean_db):
    url = "https://msk1.1cfresh.com/a/ea/1119958"
    db.upsert_user("adm", auth.ROLE_ADMIN, "h", [])
    db.upsert_user("mgr", auth.ROLE_MANAGER, "h", [])
    db.upsert_user("acc", auth.ROLE_ACCOUNTANT, "h", [url])

    assert auth.user_can_access("adm", url) is True
    assert auth.user_can_access("mgr", url) is True
    assert auth.user_can_access("mgr", "https://other.example/x") is True
    assert auth.user_can_access("acc", url) is True
    assert auth.user_can_access("acc", "https://msk1.1cfresh.com/a/ea/9999999") is False


def test_user_allowed_urls_admin_manager_empty(clean_db):
    db.upsert_user("adm", auth.ROLE_ADMIN, "h", [])
    db.upsert_user("mgr", auth.ROLE_MANAGER, "h", [])
    db.upsert_user("acc", auth.ROLE_ACCOUNTANT, "h", ["https://x.example/a"])
    assert auth.user_allowed_urls("adm") == []
    assert auth.user_allowed_urls("mgr") == []
    assert auth.user_allowed_urls("acc") == ["https://x.example/a"]


def test_lockout_after_max_attempts(clean_db):
    db.upsert_user(
        "adm", auth.ROLE_ADMIN, auth.hash_password("secret123"), []
    )
    for _ in range(auth.MAX_LOGIN_ATTEMPTS - 1):
        msg = auth.failed_login_error("adm")
        assert "Неверный логин" in msg
        assert auth.login_locked("adm") is False
    msg = auth.failed_login_error("adm")
    assert "заблокирован" in msg
    assert auth.login_locked("adm") is True
    assert "заблокирован" in auth.lockout_message("adm")
    assert auth.lockout_seconds_left("adm") > 0


def test_lockout_expires_and_success_resets(clean_db):
    db.upsert_user(
        "adm", auth.ROLE_ADMIN, auth.hash_password("secret123"), []
    )
    for _ in range(auth.MAX_LOGIN_ATTEMPTS):
        auth.register_failure("adm")
    assert auth.login_locked("adm") is True
    auth.clear_failures("adm")
    assert auth.login_locked("adm") is False
    assert auth.verify("adm", "secret123") is True
    assert auth.verify("adm", "nope") is False


def test_lockout_is_per_login(clean_db):
    for _ in range(auth.MAX_LOGIN_ATTEMPTS):
        auth.register_failure("bad")
    assert auth.login_locked("bad") is True
    assert auth.login_locked("other") is False


def test_validate_password():
    assert auth.validate_password("") == "Введите пароль."
    assert "не короче" in auth.validate_password("short")
    assert auth.validate_password("longenough1") == ""