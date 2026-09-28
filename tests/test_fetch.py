import pandas as pd

from core.fetch import fetch_documents


class FakeClient:
    """Формирует записи как 1С OData и отдаёт их через клиентский интерфейс."""

    def __init__(self, records_by_entity: dict):
        self._records = records_by_entity
        self.catalogs_loaded = False

    def _prefetch_catalogs(self, include_users=True):
        self.catalogs_loaded = True

    def fetch_documents(self, entity, period_start, period_end):
        return self._records.get(entity, [])

    def organization_info(self, guid):
        orgs = {
            "org1": {"name": "ООО Альфа", "inn": "7700000001"},
        }
        return orgs.get(guid, {"name": "", "inn": ""})

    def _name_by_guid(self, guid):
        users = {
            "u1": "Иванова А.А.",
            "u2": "<Служебный пользователь 1>",
        }
        return users.get(guid, str(guid))


def _rec(org, author, op_type="Оплата от покупателя"):
    return {
        "Ref_Key": "x",
        "Date": "2026-01-15T12:00:00",
        "Number": "0000-000001",
        "Posted": True,
        "Организация_Key": org,
        "Ответственный_Key": author,
        "ВидОперации": op_type,
        "СуммаДокумента": 1000.0,
    }


def test_fetch_documents_builds_rows():
    client = FakeClient({
        "Document_ПоступлениеНаРасчетныйСчет": [
            _rec("org1", "u1"),
            _rec("org1", "u2"),
        ],
        "Document_АвансовыйОтчет": [],
    })
    df = fetch_documents(client, "2026-01-01", "2026-01-31", [
        "bank_incoming", "advance_report",
    ], sno="УСН «Доходы»")

    rows = df.to_dict(orient="records")
    assert len(rows) == 2
    assert rows[0]["Клиент"] == "ООО Альфа"
    assert rows[0]["ИНН"] == "7700000001"
    assert rows[0]["Система налогообложения"] == "УСН «Доходы»"
    assert rows[0]["Вид документа"] == "Поступление на расчетный счет"
    assert rows[0]["Вид операции"] == "Оплата от покупателя"
    assert rows[0]["Ответственный сотрудник"] == "Иванова А.А."
    assert rows[0]["Период"] == "01.2026"
    assert df.attrs["skipped"] == []


def test_fetch_catalog_preload_toggle():
    client = FakeClient({})
    fetch_documents(client, "2026-01-01", "2026-01-31", [])
    assert client.catalogs_loaded is True


def test_period_label_for_quarter():
    client = FakeClient({
        "Document_ПоступлениеНаРасчетныйСчет": [_rec("org1", "u1")],
    })
    df = fetch_documents(client, "2026-01-01", "2026-03-31", ["bank_incoming"])
    assert df.iloc[0]["Период"] == "01.01.2026 — 31.03.2026"


def test_empty_result_keeps_columns():
    client = FakeClient({})
    df = fetch_documents(client, "2026-01-01", "2026-01-31", ["bank_incoming"])
    assert df.empty
    assert list(df.columns) == [
        "Клиент", "ИНН", "Система налогообложения", "Период", "Вид документа",
        "Вид операции", "Количество операций", "Ответственный сотрудник",
        "Роль сотрудника", "Норма на операцию", "Коэффициент сложности",
        "Трудозатраты, нормочасы", "Комментарий",
    ]


def test_unmapped_user_names():
    client = FakeClient({
        "Document_ПоступлениеНаРасчетныйСчет": [
            _rec("org1", "u1"),
            _rec("org1", "u2"),
        ],
    })
    from core.fetch import unmapped_user_names
    df = fetch_documents(client, "2026-01-01", "2026-01-31", ["bank_incoming"])
    assert set(unmapped_user_names(df)) == {"Иванова А.А.",
                                            "<Служебный пользователь 1>"}


def test_pandas_dependency_heuristic():
    # весь module использует pandas только в точках, где работаем с df
    import core.fetch as f
    assert hasattr(f, "REPORT_COLUMNS")


def test_advance_report_variant_resolves_by_operation_type():
    calls = {"n": 0}

    class CountingClient(FakeClient):
        def fetch_documents(self, entity, period_start, period_end):
            calls["n"] += 1
            return super().fetch_documents(entity, period_start, period_end)

    client = CountingClient({
        "Document_АвансовыйОтчет": [
            _rec("org1", "u1", op_type="Командировочные расходы"),
            _rec("org1", "u2", op_type="Прочие расходы"),
            _rec("org1", "u2", op_type="ГСМ и топливо"),
        ],
    })

    df = fetch_documents(client, "2026-01-01", "2026-01-31", [
        "advance_report", "advance_report_travel",
    ], sno="")

    rows = df.to_dict(orient="records")

    assert calls["n"] == 1, "сущность должна опрашиваться один раз для обоих вариантов"
    assert [r["Вид документа"] for r in rows] == [
        "Авансовый отчет до 10 чеков с ГСМ или командировкой",
        "Авансовый отчет до 10 чеков",
        "Авансовый отчет до 10 чеков с ГСМ или командировкой",
    ]


def test_tmc_documents_have_own_entities():
    from core.norms import load_doc_types
    spec = load_doc_types()
    assert spec["requirement_invoice"]["entity"] == "Document_ТребованиеНакладная"
    assert spec["transfer_materials_to_operation"]["entity"] == (
        "Document_ПередачаМатериаловВЭксплуатацию"
    )
    assert spec["writeoff_goods"]["entity"] == "Document_СписаниеТоваров"
    assert spec["writeoff_materials_from_use"]["entity"] == (
        "Document_СписаниеМатериаловИзЭксплуатации"
    )
    assert spec["return_customer"]["entity"] == "Document_ВозвратТоваровОтПокупателя"
    assert spec["return_customer"]["title"] == "Возврат товаров от покупателя"