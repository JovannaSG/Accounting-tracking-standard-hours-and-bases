# core/config.py
"""Единая точка правды для флагов сканирования реестра.

Раньше ENABLE_REGISTRY_SCANNER был объявлен здесь и в core/scanner.py,
а сам файл содержал незакрытую строку-литерал::

    REGISTRY_SOURCE = " json

и не компилировался. Ничего не импортировало этот модуль, поэтому тесты
оставались зелёными, а первый же ``import core.config`` падал бы.

Теперь флаг живёт только здесь, а читается через атрибут модуля
(``config.ENABLE_REGISTRY_SCANNER``), чтобы тесты могли подменять его
через ``monkeypatch.setattr(config, ...)`` без повторного импорта.
"""
from __future__ import annotations

import os

# Допустимые истинные значения переменной окружения.
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, default: str = "0") -> bool:
    """Читает булев флаг из переменной окружения."""
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    return str(raw).strip().lower() in _TRUTHY


# Панель сканирования реестра. По умолчанию выключена: панель не рендерится
# вовсе, а не показывается в отключённом состоянии.
# Включение при деплое: ENABLE_REGISTRY_SCANNER=1 (см. .env.example).
# Поведение сканера — только чтение: без авто-выгрузки и без авто-промоушенов.
ENABLE_REGISTRY_SCANNER = _flag("ENABLE_REGISTRY_SCANNER")

# Источник данных сканирования: 'json' (по умолчанию) или 'metadata'.
REGISTRY_SOURCE = (
    str(os.environ.get("REGISTRY_SOURCE") or "json").strip().lower() or "json"
)

# Дашборд глубокого аудита (вкладка «Аудит»). По умолчанию включён; панель
# видит только администратор. Отключение: ENABLE_AUDIT_DASHBOARD=0
# (см. .env.example).
ENABLE_AUDIT_DASHBOARD = _flag("ENABLE_AUDIT_DASHBOARD", "1")

# Каталог рабочих файлов аудита (блокировка/статус/лог). По умолчанию — рядом
# с AUDIT_HISTORY_DB: в docker-томе /data это переживает перезапуск
# контейнера. Пути переопределяются переменными окружения.
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AUDIT_DIR = os.path.dirname(
    os.path.abspath(
        os.environ.get(
            "AUDIT_HISTORY_DB", os.path.join(_PKG_ROOT, "audit_history.db")
        )
    )
)

# Файл эксклюзивной блокировки (flock): удерживается процессом аудита на всё
# время прогона, поэтому отличает «выполняется» от устаревшего PID-файла.
AUDIT_LOCK_FILE = os.environ.get(
    "AUDIT_LOCK_FILE", os.path.join(_AUDIT_DIR, ".audit.lock")
)

# Статус запущенного из UI аудита: PID, область, период (JSON).
AUDIT_STATUS_FILE = os.environ.get(
    "AUDIT_STATUS_FILE", os.path.join(_AUDIT_DIR, ".audit.status.json")
)

# Лог фонового аудита, запущенного из UI.
AUDIT_UI_LOG = os.environ.get(
    "AUDIT_UI_LOG", os.path.join(_AUDIT_DIR, "audit_ui.log")
)
