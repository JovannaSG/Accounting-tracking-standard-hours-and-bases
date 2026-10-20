# tests/test_audit.py
"""Phase 2 Deep Data Audit: движок run_audit (checkpoints, $count-gate, gaps).

Сеть не трогаем: подменяем session.get фейковым диспетчером, который отвечает
на страницы документов, "$count=true" и "$metadata" (см. доку, синхронно).
Проверяются: чанкинг, idempotent-резюм, расхождение с $count, деградация,
gaps, паузы, FAILED-обработка.
"""
import os
import types

import openpyxl
import pytest
import requests

from core import audit, db
from core.api_client import OneCClient

_BASE = {
    "name": "Тестовая база",
    "url": "https://audit.example/a/1",
    "login": "odata.user",
    "password": "secret",
}

_JOURNAL_ENTITY_SET = (
    '<EntitySet Name="DocumentJournal_ЖурналОпераций" '
    'EntityType="StandardODATA.DocumentJournal_ЖурналОпераций"/>'
)


_METADATA_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="1.0">'
    "<edmx:DataServices>"
    '<Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm" '
    'Namespace="StandardODATA">'
    '<EntityContainer Name="StandardOData">'
    '<EntitySet Name="Document_ПлатежноеПоручение" '
    'EntityType="StandardODATA.Document_ПлатежноеПоручение"/>'
    '<EntitySet Name="Document_НеРеестровое" '
    'EntityType="StandardODATA.Document_НеРеестровое"/>'
    "</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"
).encode("utf-8")


class _FakeResponse:
    """requests.Response-совместимость для session.get: json() и .content."""

    def __init__(self, payload=None, status=200, content=b""):
        self._payload = payload
        self._status = status
        self.content = content

    def raise_for_status(self):
        if self._status >= 400:
            response = requests.Response()
            response.status_code = self._status
            raise requests.exceptions.HTTPError(response=response)

    def json(self):
        return self._payload


def _row(sum_amount: float, ref: str = "r") -> dict:
    return {"Ref_Key": ref, "СуммаДокумента": sum_amount}


class _FakeData:
    """Сценарий выгрузки: сущность -> страницы/счётчик/ошибки.

    Параметры определяют поведение «1С»: какие сущности существуют,
    сколько строк в каждой, чему равен $count, какие сущности падают.
    """

    def __init__(self):
        self.requests_seen: list[str] = []
        self.entities: dict[str, dict] = {}  # entity -> {"rows": int,
        # "sum": float, "count": int|None, "fail": str|None, "degrade": bool}
        self.calls = 0
        self.journal: list[dict] | None = None

    def set_journal(self, records: list[dict]) -> None:
        """Публикует журнал операций с указанными строками."""

        self.journal = records

    def set_entity(self, name, *, rows=0, sum=0.0, count=None, fail=None,
                   degrade=False, not_found=False, records=None):
        self.entities[name] = {
            "rows": rows, "sum": sum, "count": count, "fail": fail,
            "degrade": degrade, "not_found": not_found, "records": records,
        }

    def _metadata(self) -> bytes:
        xml = _METADATA_XML.decode("utf-8")
        if self.journal is not None:
            xml = xml.replace(
                "</EntityContainer>",
                _JOURNAL_ENTITY_SET + "</EntityContainer>",
            )
        return xml.encode("utf-8")

    def fake_get(self, url, params=None, timeout=None):
        self.calls += 1
        self.requests_seen.append(url)
        if "/$metadata" in url:
            return _FakeResponse(content=self._metadata())
        entity = "?" if "/odata/standard.odata/" not in url else url.split(
            "/odata/standard.odata/", 1)[1].split("?", 1)[0]
        if entity.startswith("DocumentJournal_"):
            if self.journal is None:
                return _FakeResponse(None, 404)
            return _FakeResponse({"value": self.journal})
        spec = self.entities.get(entity)
        if spec is None:
            return _FakeResponse({"value": []}, 200)
        if spec["not_found"]:
            return _FakeResponse(None, 404)
        if "inlinecount=allpages" in url:
            if spec["count"] is None:
                return _FakeResponse({"value": []}, 400)
            return _FakeResponse(
                {"@odata.count": spec["count"], "value": [{"Ref_Key": "c"}]}
            )
        if spec["fail"]:
            return _FakeResponse(None, 400)
        if spec["degrade"]:
            from urllib.parse import parse_qs
            fields = parse_qs(url.split("?", 1)[1]).get("$select", [""])[0]
            if len(fields.split(",")) > 3:
                return _FakeResponse(None, 400)
        if spec.get("records") is not None:
            rows = spec["records"]
        else:
            rows = [_row(spec["sum"] / spec["rows"] if spec["rows"] else 0.0,
                         f"{entity}#{i}") for i in range(spec["rows"])]
        return _FakeResponse({"value": rows})


class _FakeClient:
    """OneCClient с подменённым session.get (без старта реальной сети)."""

    def __init__(self, fake: _FakeData):
        self.client = OneCClient(_BASE["url"], _BASE["login"], _BASE["password"])
        self.client.session.get = fake.fake_get


def _checkpoint(base_url, entity, start, end) -> dict | None:
    conn = db._audit_conn()
    try:
        row = conn.execute(
            "SELECT " + ", ".join(db._AUDIT_CHECKPOINTS_COLUMNS)
            + " FROM audit_checkpoints WHERE base_url=? AND entity_name=?"
            + " AND chunk_start=? AND chunk_end=?",
            (base_url, entity, start, end),
        ).fetchone()
        return db._audit_row(row, db._AUDIT_CHECKPOINTS_COLUMNS) if row else None
    finally:
        conn.close()


# ============================= ЧАНКИНГ ======================================

def test_month_chunks_basic():
    assert audit._month_chunks("2026-01-15", "2026-03-10") == [
        ("2026-01-15", "2026-01-31"),
        ("2026-02-01", "2026-02-28"),
        ("2026-03-01", "2026-03-10"),
    ]


def test_month_chunks_single_month_and_day():
    assert audit._month_chunks("2026-01-01", "2026-01-31") == [
        ("2026-01-01", "2026-01-31")
    ]
    assert audit._month_chunks("2026-01-31", "2026-01-31") == [
        ("2026-01-31", "2026-01-31")
    ]


def test_month_chunks_across_year():
    assert audit._month_chunks("2025-12-15", "2026-01-10") == [
        ("2025-12-15", "2025-12-31"),
        ("2026-01-01", "2026-01-10"),
    ]


def test_month_chunks_rejects_inverted_period():
    with pytest.raises(ValueError):
        audit._month_chunks("2026-03-01", "2026-01-01")


# ============================= ДВИЖОК =======================================

def test_run_audit_completes_and_count_matches():
    fake = _FakeData()
    fake.set_entity("Document_A", rows=3, sum=300.0, count=3)
    fc = _FakeClient(fake)

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
        entities=["Document_A"],
    )

    assert summary["run_status"] == db.AUDIT_RUN_COMPLETED
    assert summary["chunks_total"] == 1
    assert summary["chunks_completed"] == 1
    assert summary["chunks_failed"] == 0
    assert summary["count_mismatches"] == 0

    cp = _checkpoint(_BASE["url"], "Document_A", "2026-01-01", "2026-01-31")
    assert cp["status"] == db.AUDIT_CP_COMPLETED
    assert cp["rows_processed"] == 3
    assert cp["sum_amount"] == 300.0
    assert cp["expected_count"] == 3
    assert cp["count_available"] == 1
    assert cp["count_mismatch"] == 0
    assert cp["degraded"] == 0
    assert cp["page_count"] == 1


def test_run_audit_aggregates_business_hours(monkeypatch):
    """Аудит сам считает нормочасы по компании/сотруднику, как отчёт."""
    monkeypatch.setattr(
        audit, "load_doc_types",
        lambda: {"doc_a": {"entity": "Document_A", "title": "Реализация"}},
    )
    db.upsert_norm(
        "doc_a", "Тест", "Реализация", "Document_A",
        norm_hours=0.5, coeff=2.0,
    )
    db.upsert_employee("Иванова Елена", role="бухгалтер")
    db.insert_base(_BASE["name"], _BASE["url"], _BASE["login"], _BASE["password"])

    fake = _FakeData()
    fake.set_entity(
        "Document_A", rows=3, sum=30.0, count=3,
        records=[
            {"Ref_Key": "1", "СуммаДокумента": 10.0,
             "Организация_Key": "org-1", "Ответственный_Key": "user-1"},
            {"Ref_Key": "2", "СуммаДокумента": 10.0,
             "Организация_Key": "org-1", "Ответственный_Key": "user-1"},
            {"Ref_Key": "3", "СуммаДокумента": 10.0,
             "Организация_Key": "org-2", "Ответственный_Key": "user-2"},
        ],
    )
    fc = _FakeClient(fake)
    fc.client._catalogs_loaded = True
    fc.client._organizations = {
        "org-1": {"name": "ООО Ромашка", "inn": ""},
        "org-2": {"name": "ООО Лютик", "inn": ""},
    }
    fc.client._guid_to_name = {"user-1": "Иванова Елена", "user-2": "Неизвестный"}

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
    )
    assert summary["run_status"] == db.AUDIT_RUN_COMPLETED

    rows = audit.audit_hours_summary(_BASE["name"])
    by_key = {(r["company"], r["employee"]): r for r in rows}
    assert by_key[("ООО Ромашка", "Иванова Елена")]["operations"] == 2
    assert by_key[("ООО Ромашка", "Иванова Елена")]["hours"] == 2.0
    assert by_key[("ООО Лютик", "Не распределено")]["operations"] == 1
    assert by_key[("ООО Лютик", "Не распределено")]["hours"] == 1.0


def test_audit_hours_backfilled_for_old_completed_chunk(monkeypatch):
    """Старый COMPLETED-чанк без audit_hours перечитывается один раз."""
    registry = {
        "doc_a": {"entity": "Document_A", "title": "Реализация"},
    }
    monkeypatch.setattr(audit, "load_doc_types", lambda: registry)
    db.upsert_norm(
        "doc_a", "Тест", "Реализация", "Document_A", norm_hours=1.0,
    )
    db.upsert_employee("Иванова Елена")

    fake = _FakeData()
    fake.set_entity("Document_A", rows=1, sum=10.0, count=1)
    fc = _FakeClient(fake)
    fc.client._catalogs_loaded = True
    fc.client._organizations = {"org-1": {"name": "ООО Ромашка", "inn": ""}}
    fc.client._guid_to_name = {"user-1": "Иванова Елена"}
    fake.entities["Document_A"]["records"] = [{
        "Ref_Key": "1", "СуммаДокумента": 10.0,
        "Организация_Key": "org-1", "Ответственный_Key": "user-1",
    }]

    first = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
        entities=["Document_A"],
    )
    assert db.aggregate_audit_hours(first["run_id"])

    conn = db._audit_conn()
    try:
        conn.execute("DELETE FROM audit_hours")
        conn.commit()
    finally:
        conn.close()

    n_before = fake.calls
    second = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
        entities=["Document_A"],
    )
    assert second["chunks_completed"] == 1
    assert second["chunks_skipped"] == 0
    assert fake.calls > n_before
    assert db.aggregate_audit_hours(second["run_id"])[0]["operations"] == 1


def test_run_audit_routes_variant_to_its_norm(monkeypatch):
    registry = {
        "doc_a": {
            "entity": "Document_A", "title": "Авансовый отчёт",
            "variant_rules": [{"terms": ["гсм"], "to": "doc_a_fuel"}],
        },
        "doc_a_fuel": {
            "entity": "Document_A", "title": "Авансовый отчёт (ГСМ)",
        },
    }
    monkeypatch.setattr(audit, "load_doc_types", lambda: registry)
    db.upsert_norm(
        "doc_a", "Тест", "Авансовый отчёт", "Document_A", norm_hours=1.0,
    )
    db.upsert_norm(
        "doc_a_fuel", "Тест", "Авансовый отчёт (ГСМ)", "Document_A",
        norm_hours=3.0,
    )
    db.upsert_employee("Иванова Елена")
    db.insert_base(_BASE["name"], _BASE["url"], _BASE["login"], _BASE["password"])

    fake = _FakeData()
    fake.set_entity(
        "Document_A", rows=2, sum=20.0, count=2,
        records=[
            {"Ref_Key": "1", "СуммаДокумента": 10.0,
             "Организация_Key": "org-1", "Ответственный_Key": "user-1",
             "ВидОперации": "ГСМ"},
            {"Ref_Key": "2", "СуммаДокумента": 10.0,
             "Организация_Key": "org-1", "Ответственный_Key": "user-1",
             "ВидОперации": "Обычный"},
        ],
    )
    fc = _FakeClient(fake)
    fc.client._catalogs_loaded = True
    fc.client._organizations = {"org-1": {"name": "ООО Ромашка", "inn": ""}}
    fc.client._guid_to_name = {"user-1": "Иванова Елена"}

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
    )
    rows = audit.audit_hours_summary(_BASE["name"])
    assert len(rows) == 1
    assert rows[0]["operations"] == 2
    assert rows[0]["hours"] == 4.0


def test_run_audit_multiple_entities_two_months():
    fake = _FakeData()
    fake.set_entity("Document_A", rows=2, sum=10.0, count=2)
    fake.set_entity("Document_B", rows=1, sum=5.0, count=1)
    fc = _FakeClient(fake)

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-02-28",
        client=fc.client, pause=0.0,
        entities=["Document_A", "Document_B"],
    )

    assert summary["chunks_total"] == 4
    assert summary["chunks_completed"] == 4
    assert summary["run_status"] == db.AUDIT_RUN_COMPLETED
    # каждый чанк пишется своим checkpoint'ом
    cps = db.list_audit_checkpoints(summary["run_id"])
    assert len(cps) == 4
    assert {c["entity_name"] for c in cps} == {"Document_A", "Document_B"}


def test_count_mismatch_recorded():
    fake = _FakeData()
    fake.set_entity("Document_A", rows=1, sum=10.0, count=5)
    fc = _FakeClient(fake)

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
        entities=["Document_A"],
    )

    assert summary["count_mismatches"] == 1
    cp = _checkpoint(_BASE["url"], "Document_A", "2026-01-01", "2026-01-31")
    assert cp["count_mismatch"] == 4
    assert cp["expected_count"] == 5
    assert cp["rows_processed"] == 1


def test_count_unavailable_falls_back_to_export_only():
    fake = _FakeData()
    fake.set_entity("Document_A", rows=2, sum=20.0, count=None)  # $count 400
    fc = _FakeClient(fake)

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
        entities=["Document_A"],
    )

    cp = _checkpoint(_BASE["url"], "Document_A", "2026-01-01", "2026-01-31")
    assert cp["count_available"] == 0
    assert cp["count_mismatch"] == 0
    assert cp["rows_processed"] == 2
    # Запрос $count был сделан (не hard-fail)
    assert summary["run_status"] == db.AUDIT_RUN_COMPLETED


def test_resume_skips_completed_chunks():
    fake = _FakeData()
    fake.set_entity("Document_A", rows=3, sum=30.0, count=3)
    fc = _FakeClient(fake)

    first = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
        entities=["Document_A"],
    )
    n_before = fake.calls

    second = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
        entities=["Document_A"],
    )

    assert second["chunks_skipped"] == 1
    assert second["chunks_completed"] == 0
    # резюм не делает ни одного сетевого запроса
    assert fake.calls == n_before
    # checkpoint переходит под текущий прогон: отчёт второго запуска не
    # теряет ранее завершённый чанк (см. test_resume_report_keeps_skipped_chunks)
    assert db.list_audit_checkpoints(first["run_id"]) == []
    assert len(db.list_audit_checkpoints(second["run_id"])) == 1


def test_resume_report_keeps_skipped_chunks(tmp_path):
    """Резюм: отчёт второго прогона содержит и пропущенные (COMPLETED) чанки.

    Регрессия: раньше SKIPPED не обновлял run_id checkpoint'а, поэтому
    write_audit_report(second) и _count_mismatches видели только чанки,
    выполненные во втором прогоне, — ранее завершённые (вместе с их
    $count-расхождениями) пропадали из «Сводки»/«Проблем».
    """
    fake = _FakeData()
    fake.set_entity("Document_A", rows=1, sum=10.0, count=5)  # mismatch
    fake.set_entity("Document_B", rows=2, sum=20.0, count=2)  # ok
    fc = _FakeClient(fake)

    first = audit.run_audit(
        _BASE, "2026-01-01", "2026-02-28",
        client=fc.client, pause=0.0,
        entities=["Document_A", "Document_B"],
    )
    assert first["chunks_total"] == 4
    assert first["chunks_completed"] == 4
    n_before = fake.calls

    second = audit.run_audit(
        _BASE, "2026-01-01", "2026-02-28",
        client=fc.client, pause=0.0,
        entities=["Document_A", "Document_B"],
    )

    # резюм: ни одного сетевого запроса, все чанки пропущены
    assert second["chunks_skipped"] == 4
    assert second["chunks_completed"] == 0
    assert fake.calls == n_before

    # отчёт второго прогона полон (4 чанка) и включает расхождения
    checkpoints = db.list_audit_checkpoints(second["run_id"])
    assert len(checkpoints) == 4
    assert second["count_mismatches"] == 2  # Document_A за оба месяца

    path = audit.write_audit_report(second, out_dir=str(tmp_path))
    wb = openpyxl.load_workbook(path)
    assert wb["Сводка"].max_row == 4 + 1
    problems = wb["Проблемы"]
    problem_entities = {
        problems.cell(row=r, column=1).value
        for r in range(2, problems.max_row + 1)
    }
    assert "Document_A" in problem_entities


def test_restart_overrides_completed():
    fake = _FakeData()
    fake.set_entity("Document_A", rows=2, sum=20.0, count=2)
    fc = _FakeClient(fake)

    audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
        entities=["Document_A"],
    )
    n_before = fake.calls
    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0, restart=True,
        entities=["Document_A"],
    )

    assert summary["chunks_skipped"] == 0
    assert summary["chunks_completed"] == 1
    assert fake.calls > n_before  # перечитано заново


def test_degradation_marks_data_poor():
    fake = _FakeData()
    fake.set_entity("Document_C", rows=2, sum=10.0, count=2, degrade=True)
    fc = _FakeClient(fake)

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
        entities=["Document_C"],
    )

    assert "Document_C" in summary["degraded_entities"]
    cp = _checkpoint(_BASE["url"], "Document_C", "2026-01-01", "2026-01-31")
    assert cp["degraded"] == 1
    assert cp["count_mismatch"] == 0  # при деградации строки есть, gate честный


def test_entity_error_marks_checkpoint_and_run_failed():
    fake = _FakeData()
    fake.set_entity("Document_Err", rows=0, count=0, fail="always")
    fc = _FakeClient(fake)

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
        entities=["Document_Err"],
    )

    assert summary["run_status"] == db.AUDIT_RUN_FAILED
    assert summary["chunks_failed"] == 1
    cp = _checkpoint(_BASE["url"], "Document_Err", "2026-01-01", "2026-01-31")
    assert cp["status"] == db.AUDIT_CP_FAILED
    assert cp["error"]


def test_entity_404_is_not_applicable_not_failed(tmp_path):
    """404 (сущность не активирована в базе) — не сбой, а «не применимо».

    Регрессия: раньше отсутствующая сущность помечала чанк и весь прогон
    FAILED, из-за чего каждая база без подсистемного контура (ГТД, зарплата)
    выглядела сломанной в батч-мониторинге и «Проблемах».
    """
    fake = _FakeData()
    fake.set_entity("Document_ГТД", not_found=True)
    fc = _FakeClient(fake)

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
        entities=["Document_ГТД"],
    )

    # прогон здоров: сущности нет — это не ошибка
    assert summary["run_status"] == db.AUDIT_RUN_COMPLETED
    assert summary["chunks_failed"] == 0
    assert summary["chunks_completed"] == 0
    assert summary["chunks_skipped"] == 1
    assert summary["count_mismatches"] == 0

    cp = _checkpoint(_BASE["url"], "Document_ГТД", "2026-01-01", "2026-01-31")
    assert cp["status"] == db.AUDIT_CP_COMPLETED
    assert cp["rows_processed"] == 0
    assert "404" in (cp["error"] or "")

    # не попадает в «Проблемы»
    path = audit.write_audit_report(summary, out_dir=str(tmp_path))
    wb = openpyxl.load_workbook(path)
    assert wb["Проблемы"].max_row == 1

    # резюм не дёргает отсутствующую сущность повторно
    n_before = fake.calls
    second = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
        entities=["Document_ГТД"],
    )
    assert second["chunks_skipped"] == 1
    assert second["chunks_failed"] == 0
    assert fake.calls == n_before


# ============================= GAPS =========================================

def test_include_unmapped_counts_non_registry_documents():
    fake = _FakeData()
    fake.set_entity("Document_ПлатежноеПоручение", rows=1, sum=1.0, count=1)
    fake.set_entity("Document_НеРеестровое", rows=0, count=7)  # gap
    fc = _FakeClient(fake)

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0, include_unmapped=True,
        entities=["Document_ПлатежноеПоручение"],
    )

    assert summary["gaps_count"] == 1
    gaps = db.list_audit_gap_counts(_BASE["url"])
    assert [g["entity_name"] for g in gaps] == ["Document_НеРеестровое"]
    assert gaps[0]["doc_count"] == 7
    assert gaps[0]["count_available"] == 1

    # реестровая сущность в gaps не попадает
    assert all(g["entity_name"] != "Document_ПлатежноеПоручение" for g in gaps)


def test_gaps_without_flag_skips_metadata():
    fake = _FakeData()
    fake.set_entity("Document_ПлатежноеПоручение", rows=1, sum=1.0, count=1)
    fc = _FakeClient(fake)

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0, include_unmapped=False,
        entities=["Document_ПлатежноеПоручение"],
    )

    assert "gaps_count" not in summary
    assert db.list_audit_gap_counts(_BASE["url"]) == []
    assert not any("/$metadata" in u for u in fake.requests_seen)


def _journal_row(entity: str, ref: str = "r") -> dict:
    return {
        "Ref": ref,
        "Ref_Type": f"StandardODATA.{entity}",
        "Date": "2026-01-10T00:00:00",
        "Posted": True,
    }


def test_count_records_uses_inlinecount_not_v4_count():
    """1С:Фреш отвечает 501 на $count=true — сверка идёт через $inlinecount."""
    fake = _FakeData()
    fake.set_entity("Document_A", rows=0, count=4)
    fc = _FakeClient(fake)

    total, available = audit._count_records(
        fc.client, "Document_A", "2026-01-01", "2026-01-31"
    )

    assert (total, available) == (4, True)
    assert any("inlinecount=allpages" in u for u in fake.requests_seen)
    assert not any("count=true" in u for u in fake.requests_seen)


def test_journal_gap_source_counts_types_without_metadata_sweep():
    fake = _FakeData()
    fake.set_entity("Document_ПлатежноеПоручение", rows=0, count=2)
    fake.set_journal([
        _journal_row("Document_ПлатежноеПоручение"),
        _journal_row("Document_НеРеестровое"),
        _journal_row("Document_НеРеестровое"),
        _journal_row("Catalog_Контрагенты"),
    ])
    fc = _FakeClient(fake)

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0, include_unmapped=True,
        entities=["Document_ПлатежноеПоручение"],
    )

    assert summary["gap_source"] == "journal"
    assert summary["gaps_count"] == 1
    gaps = db.list_audit_gap_counts(_BASE["url"])
    assert [
        (g["entity_name"], g["doc_count"], g["count_source"]) for g in gaps
    ] == [("Document_НеРеестровое", 2, "journal")]

    # Журнал заменил перебор $metadata-счётчиков по нереестровым сущностям.
    assert not any(
        "Document_НеРеестровое" in u for u in fake.requests_seen
    )


def test_journal_available_but_empty_yields_zero_gaps_without_sweep():
    fake = _FakeData()
    fake.set_entity("Document_ПлатежноеПоручение", rows=0, count=1)
    fake.set_journal([])
    fc = _FakeClient(fake)
    conn = db._audit_conn()
    try:
        conn.execute(
            "INSERT INTO audit_gap_counts (base_url, entity_name, doc_count) "
            "VALUES (?, 'Document_Старое', 9)",
            (_BASE["url"],),
        )
        conn.commit()
    finally:
        conn.close()

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0, include_unmapped=True,
        entities=["Document_ПлатежноеПоручение"],
    )

    assert summary["gap_source"] == "journal"
    assert summary["gaps_count"] == 0
    # Снимок текущего прогона: старые строки не залипают.
    assert db.list_audit_gap_counts(_BASE["url"]) == []
    assert not any(
        "Document_НеРеестровое" in u for u in fake.requests_seen
    )


def test_gap_source_journal_unavailable_warns_and_skips(capsys):
    fake = _FakeData()
    fake.set_entity("Document_ПлатежноеПоручение", rows=0, count=1)
    fake.set_entity("Document_НеРеестровое", rows=0, count=7)
    fc = _FakeClient(fake)

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0, include_unmapped=True,
        entities=["Document_ПлатежноеПоручение"], gap_source="journal",
    )

    assert summary["gap_source"] == "none"
    assert summary["gaps_count"] == 0
    assert "Журнал операций недоступен" in capsys.readouterr().err
    assert db.list_audit_gap_counts(_BASE["url"]) == []


def test_gap_source_metadata_forces_sweep_even_with_journal():
    fake = _FakeData()
    fake.set_entity("Document_ПлатежноеПоручение", rows=0, count=1)
    fake.set_entity("Document_НеРеестровое", rows=0, count=7)
    fake.set_journal([_journal_row("Document_НеРеестровое")])
    fc = _FakeClient(fake)

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0, include_unmapped=True,
        entities=["Document_ПлатежноеПоручение"], gap_source="metadata",
    )

    assert summary["gap_source"] == "metadata"
    gaps = db.list_audit_gap_counts(_BASE["url"])
    assert gaps[0]["doc_count"] == 7
    assert gaps[0]["count_source"] == "inlinecount"
    assert not any("DocumentJournal_" in u for u in fake.requests_seen)


def test_verify_journal_coverage_classifies_sources(monkeypatch):
    """Реестр — поштучно, нереестровые Document_* — gaps, прочее — дыра."""
    monkeypatch.setattr(
        audit, "load_doc_types",
        lambda: {"doc_a": {"entity": "Document_A", "title": "A"}},
    )
    fake = _FakeData()
    fake.set_journal([
        _journal_row("Document_A"),
        _journal_row("Document_НеРеестровое"),
        _journal_row("Document_НеРеестровое"),
        _journal_row("Catalog_Контрагенты"),
    ])
    fc = _FakeClient(fake)

    report = audit.verify_journal_coverage(
        _BASE, "2026-01-01", "2026-01-31", client=fc.client
    )

    assert report["available"] is True
    assert report["total"] == 4
    assert report["covered"] == 3
    assert report["coverage"] == 0.75
    assert report["uncovered"] == ["Catalog_Контрагенты"]
    by_type = {e["type"]: e["covered_by"] for e in report["types"]}
    assert by_type == {
        "Document_A": "registry",
        "Document_НеРеестровое": "gap",
        "Catalog_Контрагенты": "uncovered",
    }


def test_verify_journal_coverage_counts_manual_and_routine(monkeypatch):
    """Ручные и регламентные операции выделяются отдельными счётчиками."""
    monkeypatch.setattr(
        audit, "load_doc_types",
        lambda: {
            "manual": {"entity": "Document_ОперацияБух", "title": "Операция"},
        },
    )
    fake = _FakeData()
    fake.set_journal([
        _journal_row("Document_ОперацияБух"),
        _journal_row("Document_РегламентнаяОперация"),
        _journal_row("Document_РегламентнаяОперация"),
    ])
    fc = _FakeClient(fake)

    report = audit.verify_journal_coverage(
        _BASE, "2026-01-01", "2026-01-31", client=fc.client
    )

    assert report["manual"] == 1
    assert report["routine"] == 2
    assert report["coverage"] == 1.0
    by_type = {e["type"]: e["covered_by"] for e in report["types"]}
    assert by_type["Document_ОперацияБух"] == "registry"
    assert by_type["Document_РегламентнаяОперация"] == "gap"


def test_verify_journal_coverage_unavailable():
    """Без журнала операций покрытие не проверяется (available=False)."""
    fake = _FakeData()  # журнал не опубликован
    fc = _FakeClient(fake)

    report = audit.verify_journal_coverage(
        _BASE, "2026-01-01", "2026-01-31", client=fc.client
    )

    assert report["available"] is False
    assert report["coverage"] is None
    assert "недоступен" in audit.format_journal_coverage(report)


# ============================= ПАУЗЫ ========================================

def test_pause_applied_between_count_and_gap_requests(monkeypatch):
    slept = []
    monkeypatch.setattr(
        audit, "_TIME", types.SimpleNamespace(sleep=lambda s: slept.append(s))
    )

    fake = _FakeData()
    fake.set_entity("Document_ПлатежноеПоручение", rows=0, count=1)
    fake.set_entity("Document_НеРеестровое", rows=0, count=3)
    fc = _FakeClient(fake)

    audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.5, include_unmapped=True,
        entities=["Document_ПлатежноеПоручение"],
    )

    assert slept, "с pause>0 ожидались паузы между запросами"
    assert all(round(s, 3) == 0.5 for s in slept)


# ============================= ОТЧЁТ ========================================

def test_write_audit_report_three_sheets(tmp_path):
    fake = _FakeData()
    fake.set_entity("Document_A", rows=1, sum=10.0, count=5)  # mismatch
    fake.set_entity("Document_B", rows=2, sum=20.0, count=2)  # ok
    fc = _FakeClient(fake)

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0,
        entities=["Document_A", "Document_B"],
    )
    path = audit.write_audit_report(summary, out_dir=str(tmp_path))

    assert os.path.exists(path)
    assert path.endswith(".xlsx")
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["Сводка", "Проблемы", "Gaps"]

    # Сводка: шапка + по строке на каждый чанк
    assert wb["Сводка"].max_row == len(
        db.list_audit_checkpoints(summary["run_id"])
    ) + 1

    # Проблемы: расхождение с $count по Document_A
    problems = wb["Проблемы"]
    assert problems.max_row >= 2
    problem_entities = [
        problems.cell(row=r, column=1).value
        for r in range(2, problems.max_row + 1)
    ]
    assert "Document_A" in problem_entities
    assert "Document_B" not in problem_entities


def test_gaps_sheet_reports_count_source(tmp_path):
    fake = _FakeData()
    fake.set_entity("Document_ПлатежноеПоручение", rows=0, count=1)
    fake.set_entity("Document_НеРеестровое", rows=0, count=5)
    fc = _FakeClient(fake)

    summary = audit.run_audit(
        _BASE, "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0, include_unmapped=True,
        entities=["Document_ПлатежноеПоручение"],
    )
    path = audit.write_audit_report(summary, out_dir=str(tmp_path))
    wb = openpyxl.load_workbook(path)

    assert [c.value for c in wb["Gaps"][1]] == [
        "Сущность", "Количество документов", "Источник",
    ]
    assert [c.value for c in wb["Gaps"][2]] == [
        "Document_НеРеестровое", 5, "$inlinecount",
    ]
    assert "$inlinecount" in [c.value for c in wb["Сводка"][1]]


def test_main_unknown_base_returns_2(capsys):
    rc = audit.main([
        "--base", "ТакойБазыНет-12345",
        "--start", "2026-01-01", "--end", "2026-01-31",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "не найдена" in err


def test_main_summary_reports_skipped_count(monkeypatch, capsys):
    """Итоговая строка CLI показывает пропущенные чанки (а не только готово).

    Регрессия: после резюма печаталось «готово 0, ошибок 2», что читалось как
    «ничего не сделано».
    """
    base = db.insert_base(
        "Аудит Сводка", "https://summary.example/a/1", "u", "p"
    )
    assert base is not None

    summary = {
        "base_name": "Аудит Сводка",
        "base_url": base["url"],
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "run_id": 1,
        "chunks_total": 5,
        "chunks_completed": 3,
        "chunks_skipped": 1,
        "chunks_failed": 1,
        "count_mismatches": 0,
        "degraded_entities": [],
        "gaps_count": None,
    }
    monkeypatch.setattr(audit, "run_audit", lambda *a, **k: summary)
    monkeypatch.setattr(
        audit, "write_audit_report", lambda s, out_dir=None: "/tmp/fake.xlsx"
    )
    rc = audit.main([
        "--base", "Аудит Сводка",
        "--start", "2026-01-01", "--end", "2026-01-31",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "готово 3" in out
    assert "пропущено 1" in out
    assert "ошибок 1" in out


def test_list_bases_prints_active_id_and_name(capsys):
    """--list-bases отдаёт «id<TAB>имя» — источник для пакетного обхода."""
    b1 = db.insert_base("Аудит Список A", "https://list.example/a/1", "u", "p")
    b2 = db.insert_base("Аудит Список B", "https://list.example/a/2", "u", "p")
    db.insert_base("Аудит Список C", "https://list.example/a/3", "u", "p",
                   active=False)

    rc = audit.main(["--list-bases"])
    assert rc == 0
    out = capsys.readouterr().out
    rows = dict(
        line.split("\t", 1) for line in out.splitlines() if line
    )
    assert rows[str(b1["id"])] == "Аудит Список A"
    assert rows[str(b2["id"])] == "Аудит Список B"
    # неактивная база в список не попадает
    assert "Аудит Список C" not in rows.values()


def test_main_requires_period_unless_listing():
    """Без --list-bases период обязателен (argparse завершает с кодом 2)."""
    with pytest.raises(SystemExit) as exc:
        audit.main(["--base", "что угодно"])
    assert exc.value.code == 2


def _coverage_report(uncovered):
    return {
        "available": True, "base_name": "Покрытие Тест",
        "base_url": "https://c.example/a/1",
        "period_start": "2026-01-01", "period_end": "2026-01-31",
        "total": 3, "covered": 3 - len(uncovered),
        "coverage": (3 - len(uncovered)) / 3, "manual": 0, "routine": 0,
        "types": [], "uncovered": uncovered,
    }


def test_main_verify_coverage_returns_0_when_covered(monkeypatch, capsys):
    db.insert_base("Покрытие Тест", "https://c.example/a/1", "u", "p")
    monkeypatch.setattr(
        audit, "verify_journal_coverage", lambda *a, **k: _coverage_report([])
    )

    rc = audit.main([
        "--verify-coverage", "--base", "Покрытие Тест",
        "--start", "2026-01-01", "--end", "2026-01-31",
    ])

    assert rc == 0
    assert "все записи журнала учтены" in capsys.readouterr().out


def test_main_verify_coverage_returns_1_on_hole(monkeypatch, capsys):
    db.insert_base("Покрытие Дыра", "https://c.example/a/2", "u", "p")
    monkeypatch.setattr(
        audit, "verify_journal_coverage",
        lambda *a, **k: _coverage_report(["Catalog_X"]),
    )

    rc = audit.main([
        "--verify-coverage", "--base", "Покрытие Дыра",
        "--start", "2026-01-01", "--end", "2026-01-31",
    ])

    captured = capsys.readouterr()
    assert rc == 1
    assert "дыры покрытия" in captured.out
    assert "Catalog_X" in captured.out
    assert "баз с дырами" in captured.err


def test_main_verify_coverage_requires_period():
    with pytest.raises(SystemExit) as exc:
        audit.main(["--verify-coverage", "--base", "что угодно"])
    assert exc.value.code == 2


def _seed_base_audit(name, url, *, rows, amount, count):
    """Прогон аудита одной сущности за месяц — чтобы появилась история."""

    base = db.insert_base(name, url, "u", "p")
    fake = _FakeData()
    fake.set_entity("Document_A", rows=rows, sum=amount, count=count)
    fc = _FakeClient(fake)
    audit.run_audit(
        {**_BASE, "id": base["id"], "name": name, "url": url},
        "2026-01-01", "2026-01-31",
        client=fc.client, pause=0.0, entities=["Document_A"],
    )
    return base


def test_summary_overview_lists_bases_with_status(capsys):
    _seed_base_audit("Сводка Чистая", "https://s.example/a/1",
                     rows=2, amount=20.0, count=2)
    _seed_base_audit("Сводка Проблемная", "https://s.example/a/2",
                     rows=1, amount=10.0, count=5)  # расхождение $count

    rc = audit.main(["--summary"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Сводка Чистая" in out
    assert "Сводка Проблемная" in out

    rows = audit.audit_summary()
    by_name = {r["base_name"]: r for r in rows}
    assert by_name["Сводка Чистая"]["has_problems"] is False
    assert by_name["Сводка Чистая"]["chunks_done"] == 1
    assert by_name["Сводка Проблемная"]["has_problems"] is True
    assert by_name["Сводка Проблемная"]["mismatches"] == 1
    # проблемные базы идут первыми
    assert rows[0]["base_name"] == "Сводка Проблемная"


def test_summary_problems_only_filters_clean_bases(capsys):
    _seed_base_audit("Сводка Чистая2", "https://s.example/b/1",
                     rows=2, amount=20.0, count=2)
    _seed_base_audit("Сводка Проблемная2", "https://s.example/b/2",
                     rows=1, amount=10.0, count=9)

    rc = audit.main(["--summary", "--problems-only"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Сводка Проблемная2" in out
    assert "Сводка Чистая2" not in out


def test_summary_base_detail_shows_entities(capsys):
    _seed_base_audit("Сводка Деталь", "https://s.example/c/1",
                     rows=1, amount=10.0, count=3)

    rc = audit.main(["--summary", "--base", "Сводка Деталь"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Детализация по сущностям" in out
    assert "Document_A" in out


def test_summary_json_output(capsys):
    _seed_base_audit("Сводка JSON", "https://s.example/d/1",
                     rows=2, amount=20.0, count=2)

    rc = audit.main(["--summary", "--json"])
    assert rc == 0
    import json

    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and data
    assert data[0]["base_name"] == "Сводка JSON"
    assert {"has_problems", "chunks_done", "chunks_failed"} <= set(data[0])


def test_summary_out_writes_file(tmp_path, capsys):
    _seed_base_audit("Сводка Файл", "https://s.example/e/1",
                     rows=2, amount=20.0, count=2)
    out_file = tmp_path / "summary.json"

    rc = audit.main(["--summary", "--json", "--out", str(out_file)])
    assert rc == 0
    assert f"Сводка: {out_file}" in capsys.readouterr().out

    import json

    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data[0]["base_name"] == "Сводка Файл"


def test_summary_out_writes_csv(tmp_path):
    _seed_base_audit("Сводка CSV", "https://s.example/f/1",
                     rows=2, amount=20.0, count=2)
    out_file = tmp_path / "summary.csv"

    rc = audit.main(["--summary", "--out", str(out_file)])
    assert rc == 0
    text = out_file.read_text(encoding="utf-8-sig")
    assert "База;id;Прогон" in text
    assert "Сводка CSV" in text


def test_summary_empty_history_is_ok(capsys):
    rc = audit.main(["--summary"])
    assert rc == 0
    assert "База" in capsys.readouterr().out


def test_summary_unknown_base_returns_2(capsys):
    rc = audit.main(["--summary", "--base", "ТакойБазыНет-999"])
    assert rc == 2
    assert "не найдена" in capsys.readouterr().err


def test_resolve_base_by_name_and_url():
    base = db.insert_base(
        "Аудит Резолв", "https://resolve.example/a/1", "u", "p"
    )
    assert base is not None
    base_id = base["id"]
    by_name = audit._resolve_base("аудит резолв")
    by_url = audit._resolve_base("https://resolve.example/a/1")
    by_id = audit._resolve_base(str(base_id))
    assert by_name and by_name["id"] == base_id
    assert by_url and by_url["id"] == base_id
    assert by_id and by_id["id"] == base_id
    assert audit._resolve_base("") is None


def test_main_keyboard_interrupt_is_clean(monkeypatch, capsys):
    base = db.insert_base(
        "Аудит Прерван", "https://pause.example/a/1", "u", "p"
    )
    assert base is not None

    def _interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(audit, "run_audit", _interrupt)
    rc = audit.main([
        "--base", "Аудит Прерван",
        "--start", "2026-01-01", "--end", "2026-01-31",
    ])
    assert rc == 130
    err = capsys.readouterr().err
    assert "приостановлен" in err
    assert "продолжит" in err


def test_default_out_dir_prefers_data_volume(monkeypatch):
    class _Path:
        def __init__(self, path):
            self.path = path

        def is_dir(self):
            return self.path == "/data"

    monkeypatch.setattr(audit, "Path", _Path)
    assert audit._default_out_dir() == "/data/exports"

    class _NoData(_Path):
        def is_dir(self):
            return False

    monkeypatch.setattr(audit, "Path", _NoData)
    assert audit._default_out_dir() == "exports"


# ============================ ПАКЕТНЫЙ ОБХОД ================================

def _minimal_summary(base_name: str, base_url: str) -> dict:
    return {
        "base_name": base_name, "base_url": base_url,
        "period_start": "2026-01-01", "period_end": "2026-01-31",
        "run_id": 1, "chunks_total": 1, "chunks_completed": 1,
        "chunks_skipped": 0, "chunks_failed": 0, "count_mismatches": 0,
        "degraded_entities": [], "gaps_count": None,
    }


def test_main_all_bases_loops_active(monkeypatch, capsys):
    db.insert_base("Пакет А", "https://batch.example/a/1", "u", "p")
    db.insert_base("Пакет Б", "https://batch.example/a/2", "u", "p")
    db.insert_base("Пакет Неактивная", "https://batch.example/a/3", "u", "p",
                   active=False)
    calls = []

    def _fake(base, start, end, **kw):
        calls.append((base["name"], start, end))
        return _minimal_summary(base["name"], base["url"])

    monkeypatch.setattr(audit, "run_audit", _fake)
    monkeypatch.setattr(
        audit, "write_audit_report", lambda s, out_dir=None: "/tmp/r.xlsx"
    )
    rc = audit.main(
        ["--all-bases", "--start", "2026-01-01", "--end", "2026-01-31"]
    )
    assert rc == 0
    assert {c[0] for c in calls} == {"Пакет А", "Пакет Б"}
    assert all(c[1] == "2026-01-01" and c[2] == "2026-01-31" for c in calls)
    out = capsys.readouterr().out
    assert "[1/2]" in out and "[2/2]" in out
    assert "Пакетный аудит завершён" in out


def test_main_all_bases_continues_after_failure(monkeypatch, capsys):
    db.insert_base("Пакет Сбой", "https://batch2.example/a/1", "u", "p")
    db.insert_base("Пакет Успех", "https://batch2.example/a/2", "u", "p")

    def _fake(base, start, end, **kw):
        if "Сбой" in base["name"]:
            raise ValueError("сломанная база")
        return _minimal_summary(base["name"], base["url"])

    monkeypatch.setattr(audit, "run_audit", _fake)
    monkeypatch.setattr(
        audit, "write_audit_report", lambda s, out_dir=None: "/tmp/r.xlsx"
    )
    rc = audit.main(
        ["--all-bases", "--start", "2026-01-01", "--end", "2026-01-31"]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "Пропущено" in captured.err
    assert "с ошибками 1" in captured.out


def test_main_base_and_all_bases_mutually_exclusive():
    with pytest.raises(SystemExit) as exc:
        audit.main([
            "--base", "что угодно", "--all-bases",
            "--start", "2026-01-01", "--end", "2026-01-31",
        ])
    assert exc.value.code == 2


def test_main_busy_lock_returns_3(capsys):
    import fcntl

    from core import config

    fd = os.open(config.AUDIT_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = audit.main([
            "--base", "что угодно",
            "--start", "2026-01-01", "--end", "2026-01-31",
        ])
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert rc == 3
    assert "уже выполняется" in capsys.readouterr().err
