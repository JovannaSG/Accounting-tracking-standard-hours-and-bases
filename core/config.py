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
