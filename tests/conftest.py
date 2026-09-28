import os
import tempfile

import pytest

_APP_TEST_DIR = tempfile.mkdtemp(prefix="app_test_db_")
_APP_TEST_DB = os.path.join(_APP_TEST_DIR, "app_test.db")
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _remove_db_with_sidecars(db_path):
    """Удаляет файл БД и его WAL-соседей (-wal/-shm), если они остались."""
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def pytest_configure(config):
    """Отключаем аутентификацию в тестах (иначе st.stop() блокирует sidebar).

    Устанавливаем AUDIT_USERS="" и указываем изолированные пути для временной
    БД и конфига пользователей.

    AppTest работает in-process, поэтому core.db._DB_PATH и USERS_CONFIG_PATH
    фиксируются при первом импорте. Чтобы тесты приложения не писали в реальную
    БД проекта, не читали реальный users.json и не «загрязняли» друг друга,
    направляем оба пути в общий временный каталог ДО импорта core.db.
    """
    os.environ["AUDIT_USERS"] = ""
    os.environ["AUDIT_DB_PATH"] = _APP_TEST_DB
    # Настоящий users.json (если создан) не должен сеять пользователей в тестах:
    # указываем на несуществующий путь → load_users_config() вернёт {}.
    os.environ["AUDIT_USERS_CONFIG"] = os.path.join(_APP_TEST_DIR, "users.json")
    # AppTest.from_function исполняет ТОЛЬКО исходник функции как отдельный
    # скрипт, поэтому модульные переменные тестов ему недоступны. Корень
    # проекта передаём через переменную окружения, иначе пришлось бы зашивать
    # абсолютный путь (тесты падали бы у другого разработчика/на другой машине).
    os.environ["AUDIT_TEST_ROOT"] = _PROJECT_ROOT


def pytest_sessionfinish(session, exitstatus):
    """Убираем общий временный файл БД после прогона."""
    db_path = os.environ.get("AUDIT_DB_PATH")
    if db_path:
        _remove_db_with_sidecars(db_path)


@pytest.fixture(autouse=True)
def _clean_app_db():
    """Очищает общую тестовую БД между тестами (чтобы пользователи/аудиты,
    засеянные одним тестом, не влияли на следующий)."""
    yield
    os.environ["AUDIT_USERS"] = ""
    _remove_db_with_sidecars(_APP_TEST_DB)
    from core import auth

    auth._LOGIN_FAILURES.clear()
