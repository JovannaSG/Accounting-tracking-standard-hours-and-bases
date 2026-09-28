import json
import os

from core import db

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC_TYPES_PATH = os.path.join(_PROJECT_ROOT, "core", "doc_types.json")

# Базовые нормы (нормочасы) по видам документов MVP (ТЗ §4.2).
# Ключи совпадают с doc_types.json. Коэффициент сложности по умолчанию 1,00.
DEFAULT_NORMS_HOURS: dict[str, float] = {
    "bank_incoming": 0.040,
    "bank_outgoing": 0.040,
    "cash_income": 0.070,
    "cash_outgoing": 0.070,
    "payment_order": 0.060,
    "goods_incoming": 0.110,
    "goods_outgoing": 0.110,
    "invoice_customer": 0.070,
    "sf_issued": 0.070,
    "sf_received": 0.070,
    "return_supplier": 0.130,
    "correction_incoming": 0.190,
    "correction_outgoing": 0.190,
    "debt_correction": 0.130,
    # Варианты авансового отчёта (ТЗ §4.2)
    "advance_report": 0.250,
    "advance_report_travel": 0.375,
    # ТМЦ и ОС (ТЗ §4.2)
    "transfer_materials_to_operation": 0.080,
    "requirement_invoice": 0.080,
    "writeoff_goods": 0.110,
    # Норм в ТЗ нет — выставляются администратором
    "writeoff_materials_from_use": 0.0,
    "return_customer": 0.0,
}

# Комментарии для норм без значения в ТЗ (администратор задаёт вручную)
DEFAULT_NORMS_COMMENTS: dict[str, str] = {
    "writeoff_materials_from_use": "Норма не задана в ТЗ — укажите вручную",
    "return_customer": "Норма не задана в ТЗ — укажите вручную",
}


def load_doc_types() -> dict:
    """Читает core/doc_types.json в виде словаря {ключ: def}."""
    if not os.path.exists(DOC_TYPES_PATH):
        return {}
    try:
        with open(DOC_TYPES_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def seed_default_norms(sno: str | None = None) -> int:
    """Синхронизирует каталог норм с doc_types.json.

    - отсутствующие ключи добавляются с нормами из DEFAULT_NORMS_HOURS;
    - у существующих норм обновляются только метаданные (название, сущность,
      категория, единица и порядок) — значения часов и коэффициента,
      заданные администратором, не затираются.
    Возвращает количество добавленных норм.
    """

    doc_types = load_doc_types()
    inserted = 0
    for key, spec in doc_types.items():
        existing = db.get_norm(doc_type=key)
        if existing is None:
            db.upsert_norm(
                doc_type=key,
                category=spec.get("category", ""),
                title=spec.get("title", key),
                entity=spec.get("entity", ""),
                unit=spec.get("unit", "документ"),
                norm_hours=DEFAULT_NORMS_HOURS.get(key, 0.0),
                comment=DEFAULT_NORMS_COMMENTS.get(key),
                sno=sno,
            )
            inserted += 1
        else:
            db.upsert_norm(
                doc_type=key,
                category=spec.get("category", existing.get("category", "")),
                title=spec.get("title", key),
                entity=spec.get("entity", existing.get("entity", "")),
                unit=spec.get("unit", existing.get("unit", "документ")),
                norm_hours=existing.get("norm_hours", 0.0),
                coeff=existing.get("coeff", 1.0),
                comment=existing.get("comment"),
                sno=sno or existing.get("sno"),
                date_from=existing.get("date_from"),
                date_to=existing.get("date_to"),
                sort_order=existing.get("sort_order", 0),
                active=existing.get("active", True),
            )
    return inserted


def norm_for(doc_type: str) -> dict | None:
    """Активная норма для ключа вида документа (или None)."""
    return db.get_norm(doc_type=doc_type)