# core/scanner.py
"""Registry scanner skeleton (Step 2: parser + minimal diff).

READ-ONLY: no auto-fetch, no auto-promotion. Gating via flags.
"""
from __future__ import annotations

import logging
from typing import Any

from core import config

logger = logging.getLogger(__name__)

# Флаги живут в core/config.py (единая точка правды). Раньше здесь были свои
# ENABLE_REGISTRY_SCANNER и REGISTRY_SOURCE, из-за чего значения расходились
# с config-модулем, а core/config.py ещё и не компилировался.

# Track 2 denylist (apply to Document_* only as per requirements)
DOCUMENT_DENYLIST_PREFIXES: tuple[str, ...] = ("Document_",)


def _is_document_entity(entity_name: str | None) -> bool:
    if not entity_name:
        return False
    return str(entity_name).startswith("Document_")


def _is_denied(entity_name: str | None) -> bool:
    # Apply Track 2 denylist (Document_* only context implied)
    name = str(entity_name) if entity_name else ""
    if _is_document_entity(name):
        # denylist rules to be refined later; skeleton keeps minimal
        return False  # no concrete deny entries yet; structure only
    return False


def compute_registry_diff(
    *,
    current_registry: dict[str, Any] | None = None,
    metadata_props: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Return diff structure (Document_* only, apply Track 2 denylist).
    No side effects. No auto-fetch, no auto-promotion.
    """
    diff: dict[str, Any] = {
        "to_add": [],
        "to_update": [],
        "to_remove": [],
        "skipped": [],
        "meta": {
            "document_only": True,
            "denylist_applied": True,
            "gated": not config.ENABLE_REGISTRY_SCANNER,
        },
    }
    # TODO: implement diff logic once parser proven
    return diff
