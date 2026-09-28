import pytest

from core import db
from core.norms import DEFAULT_NORMS_HOURS, seed_default_norms


@pytest.fixture
def clean_db():
    db.init_db()
    yield
    db.init_db()


def test_seed_adds_all_norm_keys(clean_db):
    inserted = seed_default_norms(sno="УСН «Доходы»")
    assert inserted == 21
    assert db.count_norms() == 21

    travel = db.get_norm(doc_type="advance_report_travel")
    assert travel["norm_hours"] == 0.375
    assert travel["title"] == "Авансовый отчет до 10 чеков с ГСМ или командировкой"

    no_norm = db.get_norm(doc_type="writeoff_materials_from_use")
    assert no_norm["norm_hours"] == 0.0
    assert "Норма не задана" in (no_norm.get("comment") or "")


def test_seed_is_idempotent(clean_db):
    assert seed_default_norms() == 21
    assert seed_default_norms() == 0
    assert db.count_norms() == 21


def test_seed_preserves_admin_hours_and_updates_title(clean_db):
    seed_default_norms()

    key = "bank_incoming"
    db.upsert_norm(
        doc_type=key,
        category="Банк и касса",
        title="Поступление на расчетный счет",
        entity="Document_ПоступлениеНаРасчетныйСчет",
        unit="документ",
        norm_hours=0.99,
        coeff=1.50,
        comment="Своя норма бухгалтерии",
    )

    assert seed_default_norms() == 0

    norm = db.get_norm(doc_type=key)
    assert norm["norm_hours"] == 0.99
    assert norm["coeff"] == 1.50
    assert norm["comment"] == "Своя норма бухгалтерии"
    assert norm["norm_min"] == pytest.approx(0.99 * 60, rel=1e-3)

    renamed = db.get_norm(doc_type="advance_report")
    assert renamed["title"] == "Авансовый отчет до 10 чеков"


def test_default_hours_cover_all_doc_types(clean_db):
    from core.norms import load_doc_types
    doc_types = load_doc_types()
    missing = [k for k in doc_types if k not in DEFAULT_NORMS_HOURS]
    assert missing == []