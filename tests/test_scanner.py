"""Сканирование реестра: флаги, диф сущностей, ключи doc_type.

Здесь же — регрессии на два инцидента:

1. core/config.py был закоммичен с незакрытой строкой-литералом
   (``REGISTRY_SOURCE = " json``) и не компилировался. Файл никто не
   импортировал, поэтому 166 тестов оставались зелёными, а первый же
   ``import core.config`` падал бы.
2. Связь entity -> doc_type одно-ко-многим, поэтому doc_type нельзя
   вывести обратным преобразованием из имени сущности.
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import pathlib
from collections import defaultdict

import pytest

from core import config, db
from core.norms import load_doc_types
from app import ui

VALID_SOURCES = {"json", "metadata"}


def _reload_config_with(env_value: str | None) -> None:
    """Перечитывает config с заданным значением переменной окружения."""
    if env_value is None:
        os.environ.pop("ENABLE_REGISTRY_SCANNER", None)
    else:
        os.environ["ENABLE_REGISTRY_SCANNER"] = env_value
    importlib.reload(config)


@pytest.fixture
def clean_db():
    """Пустая тестовая БД (conftest чистит файл между тестами)."""
    db.init_db()
    yield
    db.init_db()


@pytest.fixture(autouse=True)
def _restore_config_flag():
    """Возвращает флаг в дорестовое состояние после каждого теста.

    Значение запоминается ДО теста, а не в teardown: иначе тест, выставивший
    ENABLE_REGISTRY_SCANNER=1, оставил бы флаг включённым для соседних тестов.
    """
    original = os.environ.get("ENABLE_REGISTRY_SCANNER")
    yield
    _reload_config_with(original)


# =========================== ФЛАГИ И КОНФИГУРАЦИЯ ==========================

def test_core_config_parses_and_exposes_flags():
    """Регрессия: core/config.py должна компилироваться.

    До правки файл заканчивался на ``REGISTRY_SOURCE = " json`` и падал
    с SyntaxError при импорте — незаметно, пока его никто не импортирует.
    """
    source = pathlib.Path(config.__file__).read_text(encoding="utf-8")
    ast.parse(source)  # SyntaxError -> тест красный
    assert hasattr(config, "ENABLE_REGISTRY_SCANNER")
    assert hasattr(config, "REGISTRY_SOURCE")
    assert config.REGISTRY_SOURCE in VALID_SOURCES


def test_scanner_flag_defaults_to_off():
    _reload_config_with(None)
    assert config.ENABLE_REGISTRY_SCANNER is False


def test_scanner_flag_env_override():
    for raw, expected in (("1", True), ("true", True), ("YES", True),
                          ("0", False), ("no", False), ("", False),
                          ("garbage", False)):
        _reload_config_with(raw)
        assert config.ENABLE_REGISTRY_SCANNER is expected, (
            f"ENABLE_REGISTRY_SCANNER={raw!r} -> "
            f"{config.ENABLE_REGISTRY_SCANNER}, ожидалось {expected}"
        )


def test_scanner_config_is_single_source_of_truth():
    """scanner.get_diff() обязан читать флаг из core.config, а не сам из себя."""
    import core.scanner as scanner

    _reload_config_with(None)
    assert scanner.compute_registry_diff()["meta"]["gated"] is True

    _reload_config_with("1")
    assert scanner.compute_registry_diff()["meta"]["gated"] is False


def test_compute_registry_diff_has_no_side_effects():
    """Сканер читает и сравнивает: без авто-выгрузки и авто-промоушенов."""
    import core.scanner as scanner

    norms_before = db.list_norms(active_only=False)
    result = scanner.compute_registry_diff(current_registry={"a": {}}, metadata_props={})
    assert set(result) == {"to_add", "to_update", "to_remove", "skipped", "meta"}
    assert db.list_norms(active_only=False) == norms_before


# ==================== entity -> doc_type НЕ ОБРАТИМА =======================

def test_entity_to_doc_type_is_one_to_many():
    """Документирует, почему doc_type нельзя получить из имени сущности.

    Один entity обслуживает несколько ключей (варианты форм). Если бы
    сканер выводил doc_type из entity, он гарантированно выбрать бы только
    один из них — молча потеряв второй.
    """
    by_entity: dict[str, list[str]] = defaultdict(list)
    for key, spec in load_doc_types().items():
        if spec.get("entity"):
            by_entity[spec["entity"]].append(key)

    shared = {e: keys for e, keys in by_entity.items() if len(keys) > 1}
    assert shared, (
        "В манифесте не осталось сущностей с несколькими ключами — "
        "правило «обратный вывод невозможен» нужно перепроверить."
    )
    assert "Document_ПоступлениеНаРасчетныйСчет" in shared


def test_build_scan_diff_lists_only_unmapped_entities(clean_db):
    mapped_entities = {
        spec["entity"] for spec in load_doc_types().values()
        if spec.get("entity")
    }
    mapped_entity = sorted(mapped_entities)[0]
    novel = "Document_СовсемНовыеДанные"

    diff = ui._build_scan_diff(
        {"url": "https://donor/1"},
        {
            mapped_entity: {"properties": {"Ref_Key"}},
            novel: {"properties": {"Ref_Key", "Ответственный"}},
        },
    )

    assert [c["entity"] for c in diff] == [novel], (
        "в diff попала уже описанная в манифесте сущность"
    )


def test_prefill_key_fails_latin_check_so_admin_must_choose():
    """Предзаполненный doc_type (= имя сущности) НЕ должен проходить проверку.

    Это и есть защита от «слепого» ключа: русское имя сущности не пройдёт
    _DOC_TYPE_RE, поэтому сохранение без участия администратора невозможно.
    """
    diff = ui._build_scan_diff(
        {"url": "https://donor/1"},
        {"Document_Увольнение": {"properties": {"Ref_Key", "Ответственный"}}},
    )
    assert diff, "сущность должна попасть в diff"
    candidate = diff[0]
    assert candidate["doc_type"] == "Document_Увольнение"
    assert candidate["title"] == "Увольнение"
    assert not ui._DOC_TYPE_RE.fullmatch(candidate["doc_type"])
    # Заблокированные колонки — entity и источник, а не ключ.
    assert candidate["entity"] == "Document_Увольнение"
    assert candidate["ref_base"] == "https://donor/1"


def test_entity_label_strips_1c_prefixes():
    assert ui._entity_label("Document_Увольнение") == "Увольнение"
    assert ui._entity_label("InformationRegister_Курсы") == "Курсы"
    assert ui._entity_label("AccumulationRegister_Обороты") == "Обороты"
    assert ui._entity_label("Document_A") == "A"
    assert ui._entity_label("Document_") == "Document_"
    assert ui._entity_label("БезПрефикса") == "БезПрефикса"


def test_build_scan_diff_only_proposes_document_entities(clean_db):
    """Diff предлагает только Document_* без row-типов (*_RecordType).

    Регрессия на выгрузку 1393 сущностей: каталоги, регистры, бизнес-процессы
    и ряды регистров не являются документами с нормами и в кандидаты не
    попадают (Track 2, denylist по Document_*). Табличные части (например,
    Document_ГТДИмпорт_Товары) с тем же именем нельзя отличить по одному
    имени — их отсекает пересечение с EntitySet (см. следующий тест).
    """
    diff = ui._build_scan_diff(
        {"url": "https://donor/1"},
        {
            "Document_Новый": {"properties": {"Ref_Key"}},
            "Catalog_Товары": {"properties": {"Ref_Key"}},
            "InformationRegister_Остатки": {"properties": {"Ref_Key"}},
            "AccumulationRegister_Обороты": {"properties": {"Ref_Key"}},
            "AccumulationRegister_Обороты_RecordType": {"properties": {"Ref_Key"}},
            "Document_Ложный_RecordType": {"properties": {"Ref_Key"}},
        },
    )
    entities = {c["entity"] for c in diff}
    assert entities == {"Document_Новый"}, f"в diff осталось лишнее: {entities}"


def test_build_scan_diff_intersects_with_entity_sets(clean_db):
    """Пересечение с сущностями публикации отсекает row-типы табличных частей.

    Без entity_sets (fallback) остаётся любой Document_*, не оканчивающийся
    на _RecordType; с entity_sets — только реальные наборы OData.
    """
    props = {
        "Document_Акт": {"properties": {"Ref_Key"}},
        "Document_ГТДИмпорт_Разделы": {"properties": {"Ref_Key"}},
    }
    entity_sets = {"Document_Акт": "StandardODATA.Document_Акт"}

    with_sets = ui._build_scan_diff({"url": "https://donor/1"}, props, entity_sets)
    assert {c["entity"] for c in with_sets} == {"Document_Акт"}

    fallback = ui._build_scan_diff({"url": "https://donor/1"}, props)
    assert {c["entity"] for c in fallback} == {
        "Document_Акт", "Document_ГТДИмпорт_Разделы",
    }


def test_scan_flags_read_russian_properties(clean_db):
    """Флаги has_* определяются по русским именам реквизитов 1С:Фреш.

    «Ответственный_Key» и «ВидОперации» — как в реальном $metadata;
    has_author_key удалён («Автор» в 1С:Фреш не публикуется).
    """
    diff = ui._build_scan_diff(
        {"url": "https://donor/1"},
        {
            "Document_СОтветственным": {
                "properties": {"Ответственный_Key", "ВидОперации"},
            },
            "Document_БезРеквизитов": {"properties": {"Ref_Key", "Комментарий"}},
        },
    )
    by_entity = {c["entity"]: c for c in diff}
    assert by_entity["Document_СОтветственным"]["has_responsible_key"] is True
    assert by_entity["Document_СОтветственным"]["has_operation_type"] is True
    assert by_entity["Document_БезРеквизитов"]["has_responsible_key"] is False
    assert by_entity["Document_БезРеквизитов"]["has_operation_type"] is False
    assert "has_author_key" not in by_entity["Document_СОтветственным"]


# ============================ КЛЮЧ doc_type =================================

def test_doc_type_regex_accepts_every_registry_key():
    """Все существующие ключи манифеста должны проходить валидацию.

    Если правило валидации ужесточится настолько, что легальные ключи
    перестанут проходить, сохранение сломается для боевых данных.
    """
    for key in load_doc_types():
        assert ui._DOC_TYPE_RE.fullmatch(key), (
            f"легальный ключ {key!r} не проходит валидацию doc_type"
        )


@pytest.mark.parametrize("key,valid", [
    ("dismissal", True),
    ("bank_incoming", True),
    ("act_sverki", True),
    ("goods_outgoing_bulk", True),
    ("Payment1", True),
    ("_", False),            # не начинается с буквы
    ("1abc", False),         # не начинается с буквы
    ("doc type", False),     # пробел
    ("doc-type", False),     # дефис
    ("doc.type", False),
    ("", False),
    ("Document_Увольнение", False),   # кириллица
    ("норма", False),
    ("дoc", False),           # кириллическая 'д' выглядит как латинская
])
def test_doc_type_regex_rejects_bad_keys(key, valid):
    result = bool(ui._DOC_TYPE_RE.fullmatch(key))
    assert result is valid, f"{key!r}: ожидалось {valid}, получено {result}"


def test_manifest_snippet_is_valid_json_with_escaping():
    """Блок для core/doc_types.json собирается json.dumps, а не f-строкой.

    Наименования документов содержат кавычки; собранный вручную фрагмент
    был бы невалидным JSON и сломал бы отслеживаемый git-файл при вставке.
    """
    snippets = {
        "dismissal": {
            "entity": "Document_Увольнение",
            "title": 'Увольнение по "статье" №1',
            "category": "Кадры",
            "unit": "документ",
        },
        "doc_type": {
            "entity": "Document_X",
            "title": "Обратный \\ слэш",
            "category": "К",
            "unit": "операция",
        },
    }
    rendered = json.dumps(snippets, ensure_ascii=False, indent=2)
    parsed = json.loads(rendered)

    assert parsed == snippets
    assert set(parsed) == set(snippets)
    for entry in parsed.values():
        assert {"entity", "title", "category", "unit"} == set(entry)
    # Кириллица не экранируется \uXXXX — файл должен оставаться читаемым.
    assert "\\u0423" not in rendered
