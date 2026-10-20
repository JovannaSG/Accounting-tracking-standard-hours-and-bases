# core/audit.py
"""Глубокий аудит данных 1С:Фреш (Phase 2-3).

Checkpoint-состояние живёт в отдельной БД ``audit_history.db``
(``core.db.init_audit_db``), движок строго последователен по базе и
покрыт ``tests/test_audit.py``.

Запуск из CLI::

    python -m core.audit --base <имя> --start 2026-01-01 --end 2026-01-31 \\
        [--include-unmapped] [--gap-source auto|journal|metadata] \\
        [--pause 0.2] [--restart]

Отчёт пишется в ``exports/audit_*.xlsx`` (листы «Сводка», «Проблемы»,
«Gaps»).
"""
from __future__ import annotations

import argparse
import fcntl
import os
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import openpyxl

from core import config, db
from core.api_client import EntityNotFoundError, OneCClient
from core.calculator import build_norm_index, resolve_business_row
from core.fetch import resolve_doc_spec
from core.norms import load_doc_types
from core.reporters import BORDER, CENTER, HEADER_FILL, HEADER_FONT


class AuditBusyError(RuntimeError):
    """Аудит уже выполняется: блокировка удерживается другим процессом."""

_TIME = time  # патчится в тестах (паузы между запросами)

# Возврат _run_chunk для сущности, отсутствующей в базе (404, не применимо).
_CHUNK_NOT_APPLICABLE = "NOT_APPLICABLE"

# ====================== ЧАНКИНГ ПО КАЛЕНДАРНЫМ МЕСЯЦАМ ======================


def _month_chunks(period_start: str, period_end: str) -> list[tuple[str, str]]:
    """Дробит период на календарные месяцы (включительно).

    ``("2026-01-15", "2026-03-10")`` ->
    ``[("2026-01-15","2026-01-31"), ("2026-02-01","2026-02-28"),
    ("2026-03-01","2026-03-10")]``. Точные границы reduce запросов
    1С:Фреш и дают естественные «фрагменты» для checkpoint'ов.
    """
    from calendar import monthrange

    s = date.fromisoformat(period_start)
    e = date.fromisoformat(period_end)
    if e < s:
        raise ValueError(
            f"Период некорректен: {period_start} позже {period_end}"
        )

    chunks: list[tuple[str, str]] = []
    cur = s
    while cur <= e:
        month_last = cur.replace(day=monthrange(cur.year, cur.month)[1])
        end = month_last if month_last < e else e
        chunks.append((cur.isoformat(), end.isoformat()))
        cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(
            cur.year, cur.month + 1, 1
        )
    return chunks


# =========================== УТИЛИТЫ ТРАНСПОРТА ==============================


def _amount(rec: dict) -> float:
    """Числовой итог документа; мусор/None → 0.0 (не роняем чанк)."""

    raw = rec.get("СуммаДокумента")
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _count_records(
    client: OneCClient,
    entity: str,
    chunk_start: str,
    chunk_end: str,
) -> tuple[int | None, bool]:
    """Лёгкий ``$top=1&$inlinecount=allpages`` с тем же фильтром по Date.

    Возвращает ``(total, available)``. 1С:Фреш отвергает OData v4-параметр
    ``$count=true`` (HTTP 501), но поддерживает v2/v3-синтаксис
    ``$inlinecount=allpages`` и возвращает количество в ``odata.count``.
    Если счётчик всё равно недоступен — ``(None, False)``: аудит деградирует
    в export-only, без hard-fail.
    """
    start_safe = client._start_of_day(chunk_start)
    end_safe = client._end_of_day(chunk_end)
    ffilter = (
        f"Date ge datetime'{start_safe}' "
        f"and Date le datetime'{end_safe}'"
    )
    endpoint = (
        f"{client.base_url}/odata/standard.odata/{entity}"
        f"?{r'$format'}=json&{r'$filter'}={quote(ffilter, safe='')}"
        f"&{r'$top'}=1&{r'$inlinecount'}=allpages"
    )
    try:
        data = client._request_page(endpoint, {})
    except ValueError:
        return None, False
    count = data.get("@odata.count", data.get("odata.count"))
    if count is None:
        return None, False
    try:
        return int(count), True
    except (TypeError, ValueError):
        return None, False


# ====================== БЛОКИРОВКА И СИГНАЛЫ ================================


@contextmanager
def _audit_lock(path: str):
    """Эксклюзивная блокировка аудита (flock, неблокирующая).

    Держится процессом на всё время прогона; автоматически снимается ОС при
    завершении (в т.ч. по SIGTERM/Ctrl+C), поэтому устаревший файл не мешает.
    Если блокировка занята другим процессом (UI или cron) — ``AuditBusyError``.
    """

    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        raise AuditBusyError(
            f"Аудит уже выполняется (блокировка {path})"
        ) from e
    try:
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _install_sigterm_handler() -> None:
    """SIGTERM (кнопка «Остановить» в UI) → KeyboardInterrupt.

    Так остановка из UI использует тот же бережный путь, что и Ctrl+C:
    сообщение «состояние сохранено», код 130, незавершённый чанк остаётся
    RUNNING и продолжается при следующем запуске.
    """

    def _raise(signum, frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _raise)
    except (ValueError, AttributeError, OSError):
        # Не главный поток или платформа без SIGTERM — не критично.
        pass


# ============================= ДВИЖОК АУДИТА =================================


def _audit_entity_specs(base: dict) -> dict[str, list[tuple[str, dict]]]:
    """Сущность -> список (ключ, спека) для маршрутизации вариантов норм.

    Учитывает ``bases.active_doc_types``: пустой список = все виды реестра
    (прецедент — core.fetch.fetch_documents).
    """
    registry = load_doc_types()
    keys = base.get("active_doc_types") or list(registry.keys())
    by_entity: dict[str, list[tuple[str, dict]]] = {}
    for key in keys:
        spec = registry.get(key)
        if spec and spec.get("entity"):
            by_entity.setdefault(spec["entity"], []).append((key, spec))
    return by_entity


def _audit_entities(base: dict) -> list[str]:
    """Уникальные OData-сущности реестра в порядке doc_types.json."""

    return list(_audit_entity_specs(base))


def run_audit(
    base: dict,
    period_start: str,
    period_end: str,
    pause: float = 0.2,
    include_unmapped: bool = False,
    client: OneCClient | None = None,
    restart: bool = False,
    entities: list[str] | None = None,
    gap_source: str = "auto",
) -> dict:
    """Прогоняет аудит базы за период; резюмирует через checkpoint'ы.

    - (entity × месяц) как чанк: COMPLETED пропускаются, RUNNING/FAILED
      перезапускаются ровно с этой точки (без дублей сети);
    - каждый чанк: стрим ``fetch_documents_iter`` (память O(1)),
      агрегаты rows/сумма/страницы, затем ``$inlinecount``-сверка;
    - деградация на минимальный ``$select`` (``client._degraded_entities``)
      фиксируется как data-poor;
    - ``include_unmapped`` — подсчёт нереестровых ``Document_*``: сначала
      журнал операций (``Ref_Type``), иначе ``$metadata``; ``gap_source``
      позволяет форсировать ``journal``/``metadata``.

    ``client``/``entities`` — тестовые интерфейсы: движок не строит сетевого
    клиента, если клиент уже передан; сущности — из реестра, а не сети.
    """
    base_url = str(base["url"]).rstrip("/")
    client = client or OneCClient(
        base_url, base.get("login") or "", base.get("password") or ""
    )

    init_audit_db = db.init_audit_db
    init_audit_db()
    conn = db._audit_conn()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO audit_runs (base_url, period_start, period_end, "
            "include_unmapped, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (base_url, period_start, period_end, int(bool(include_unmapped)),
             db.AUDIT_RUN_PENDING, now),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    summary: dict[str, Any] = {
        "run_id": run_id,
        "base_url": base_url,
        "base_name": base.get("name", ""),
        "period_start": period_start,
        "period_end": period_end,
        "chunks_total": 0,
        "chunks_completed": 0,
        "chunks_skipped": 0,
        "chunks_failed": 0,
        "count_mismatches": 0,
        "degraded_entities": [],
    }

    entity_specs = _audit_entity_specs(base)
    selected_entities = entities or list(entity_specs)
    chunk_items = [
        (entity, entity_specs.get(entity) or [(entity, {"title": entity})],
         chunk)
        for entity in selected_entities
        for chunk in _month_chunks(period_start, period_end)
    ]
    summary["chunks_total"] = len(chunk_items)

    by_title = build_norm_index()
    employees = db.list_employees(active_only=True)

    try:
        for entity, entries, (chunk_start, chunk_end) in chunk_items:
            status = _run_chunk(
                client, run_id, base_url, entity,
                chunk_start, chunk_end, pause, restart,
                entries=entries, by_title=by_title, employees=employees,
            )
            if status in ("SKIPPED", _CHUNK_NOT_APPLICABLE):
                summary["chunks_skipped"] += 1
            elif status == db.AUDIT_CP_COMPLETED:
                summary["chunks_completed"] += 1
            elif status == db.AUDIT_CP_FAILED:
                summary["chunks_failed"] += 1

        summary["degraded_entities"] = sorted(client._degraded_entities)

        if include_unmapped:
            gaps, source = _run_gap_counts(
                client, base_url, run_id,
                selected_entities, pause,
                period_start, period_end, gap_source,
            )
            summary["gaps_count"] = len(gaps)
            summary["gap_source"] = source

        summary["count_mismatches"] = _count_mismatches(run_id)

        run_status = (
            db.AUDIT_RUN_COMPLETED
            if summary["chunks_failed"] == 0
            else db.AUDIT_RUN_FAILED
        )
        _finalize_run(run_id, run_status)
    except Exception as e:
        _finalize_run(run_id, db.AUDIT_RUN_FAILED, error=str(e))
        raise

    summary["run_status"] = run_status
    return summary


def _business_bucket(
    rec: dict,
    entries: list[tuple[str, dict]],
    client: OneCClient,
    by_title: dict[str, dict],
    employees: list[dict],
    cache: dict[tuple[str, str], dict],
    buckets: dict[tuple[str, str], list],
) -> None:
    """Добавляет документ в агрегат (компания × сотрудник) с кэшем резолва."""

    key, spec = resolve_doc_spec(entries, rec)
    title = str(spec.get("title") or key)

    org_guid = rec.get("Организация_Key")
    company = (
        client.organization_info(org_guid).get("name", "") if org_guid else ""
    )
    author_guid = rec.get("Ответственный_Key")
    author = client._name_by_guid(author_guid) if author_guid else ""

    cache_key = (author, title)
    business = cache.get(cache_key)
    if business is None:
        business = resolve_business_row(
            author, title, 1, by_title, employees
        )
        cache[cache_key] = business

    bucket = buckets.setdefault((company, business["full_name"]), [0, 0.0])
    bucket[0] += 1
    bucket[1] += business["hours"]


def _run_chunk(
    client: OneCClient,
    run_id: int,
    base_url: str,
    entity: str,
    chunk_start: str,
    chunk_end: str,
    pause: float,
    restart: bool,
    entries: list[tuple[str, dict]] | None = None,
    by_title: dict[str, dict] | None = None,
    employees: list[dict] | None = None,
) -> str:
    """Аудит одного (entity × месяц); возвращает итоговый статус чанка."""

    conn = db._audit_conn()
    try:
        row = conn.execute(
            "SELECT " + ", ".join(db._AUDIT_CHECKPOINTS_COLUMNS)
            + " FROM audit_checkpoints WHERE base_url=? AND entity_name=?"
            + " AND chunk_start=? AND chunk_end=?",
            (base_url, entity, chunk_start, chunk_end),
        ).fetchone()
    finally:
        conn.close()

    completed = (
        row is not None and not restart
        and db._audit_row(row, db._AUDIT_CHECKPOINTS_COLUMNS)["status"]
        == db.AUDIT_CP_COMPLETED
    )
    # Чанк, завершённый до появления audit_hours (старый прогон), нельзя
    # просто пропустить: дашборд остался бы без нормочасов. Перечитываем его
    # один раз, чтобы забэкфиллить агрегат; маркер пустого чанка не даёт
    # повторять это при каждом резюме.
    if completed and db.has_audit_hours(
        base_url, entity, chunk_start, chunk_end
    ):
        # Его checkpoint должен «принадлежать» текущему run'у: иначе отчёт
        # (list_audit_checkpoints по run_id) после резюма потерял бы все
        # ранее завершённые чанки — вместе с их расхождениями с ожидаемым
        # количеством и деградацией. Тот же приём — у audit_hours.
        conn = db._audit_conn()
        try:
            conn.execute(
                "UPDATE audit_checkpoints SET run_id=? "
                "WHERE base_url=? AND entity_name=? AND chunk_start=? "
                "AND chunk_end=?",
                (run_id, base_url, entity, chunk_start, chunk_end),
            )
            conn.commit()
        finally:
            conn.close()
        db.reassign_audit_hours(
            base_url, entity, chunk_start, chunk_end, run_id
        )
        return "SKIPPED"

    now = datetime.now().isoformat(timespec="seconds")
    conn = db._audit_conn()
    try:
        conn.execute(
            "INSERT INTO audit_checkpoints (run_id, base_url, entity_name, "
            "chunk_start, chunk_end, status, started_at, rows_processed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(base_url, entity_name, chunk_start, chunk_end) "
            "DO UPDATE SET run_id=excluded.run_id, status='RUNNING', "
            "started_at=excluded.started_at, rows_processed=0, sum_amount=NULL, "
            "page_count=0, degraded=0, expected_count=NULL, "
            "count_available=0, count_mismatch=0, error=NULL, finished_at=NULL",
            (run_id, base_url, entity, chunk_start, chunk_end,
             db.AUDIT_CP_RUNNING, now),
        )
        conn.commit()
    finally:
        conn.close()

    # Старые строки чанка могли остаться от предыдущего прогона (в т.ч.
    # успешного, если теперь перечитываем ради бэкфилла). Сбрасываем их:
    # при ошибке дашборд не должен показать устаревшие часы.
    db.replace_audit_hours(
        base_url, entity, chunk_start, chunk_end, run_id, []
    )

    rows = 0
    total = 0.0
    pages = 0
    entries = entries or [(entity, {"title": entity})]
    by_title = by_title or build_norm_index()
    employees = employees or db.list_employees(active_only=True)
    business_cache: dict[tuple[str, str], dict] = {}
    business_buckets: dict[tuple[str, str], list] = {}

    def _on_page() -> None:
        nonlocal pages
        pages += 1

    try:
        client._prefetch_catalogs()
        for rec in client.fetch_documents_iter(
            entity, chunk_start, chunk_end, pause=pause, on_page=_on_page
        ):
            rows += 1
            total += _amount(rec)
            _business_bucket(
                rec, entries, client, by_title, employees,
                business_cache, business_buckets,
            )

        db.replace_audit_hours(
            base_url, entity, chunk_start, chunk_end, run_id,
            [
                {
                    "company": company,
                    "employee": employee,
                    "operations": operations,
                    "hours": round(hours, 6),
                }
                for (company, employee), (operations, hours)
                in business_buckets.items()
            ],
        )

        count, count_available = _count_records(
            client, entity, chunk_start, chunk_end
        )
        mismatch = (abs(count - rows) if count_available and count is not None
                    else 0)
        degraded = 1 if entity in client._degraded_entities else 0
        finished = datetime.now().isoformat(timespec="seconds")

        conn = db._audit_conn()
        try:
            conn.execute(
                "UPDATE audit_checkpoints SET status=?, rows_processed=?, "
                "sum_amount=?, page_count=?, degraded=?, expected_count=?, "
                "count_available=?, count_mismatch=?, finished_at=? "
                "WHERE base_url=? AND entity_name=? AND chunk_start=? "
                "AND chunk_end=?",
                (db.AUDIT_CP_COMPLETED, rows, round(total, 2), pages,
                 degraded, count, int(count_available), mismatch, finished,
                 base_url, entity, chunk_start, chunk_end),
            )
            conn.commit()
        finally:
            conn.close()
        return db.AUDIT_CP_COMPLETED
    except EntityNotFoundError:
        # 404: сущности нет в этой базе (не активирован контур — ГТД, зарплата
        # и т.п.). Это не сбой: помечаем COMPLETED с 0 строк, чтобы (а) резюм
        # не дёргал её снова и (б) она не попала в «Проблемы». Пометка в
        # error видна в «Сводке», но «Проблемы» фильтруют по COMPLETED.
        finished = datetime.now().isoformat(timespec="seconds")
        conn = db._audit_conn()
        try:
            conn.execute(
                "UPDATE audit_checkpoints SET status=?, rows_processed=0, "
                "sum_amount=0, page_count=0, degraded=0, expected_count=NULL, "
                "count_available=0, count_mismatch=0, error=?, finished_at=? "
                "WHERE base_url=? AND entity_name=? AND chunk_start=? "
                "AND chunk_end=?",
                (db.AUDIT_CP_COMPLETED, "сущность отсутствует в базе (404)",
                 finished, base_url, entity, chunk_start, chunk_end),
            )
            conn.commit()
        finally:
            conn.close()
        return _CHUNK_NOT_APPLICABLE
    except Exception as e:
        failed = datetime.now().isoformat(timespec="seconds")
        conn = db._audit_conn()
        try:
            conn.execute(
                "UPDATE audit_checkpoints SET status=?, error=?, finished_at=? "
                "WHERE base_url=? AND entity_name=? AND chunk_start=? "
                "AND chunk_end=?",
                (db.AUDIT_CP_FAILED, str(e)[:500], failed,
                 base_url, entity, chunk_start, chunk_end),
            )
            conn.commit()
        finally:
            conn.close()
        return db.AUDIT_CP_FAILED


def _persist_gaps(base_url: str, gaps: list[dict]) -> None:
    """Пишет снимок счётчиков нереестровых Document_* для базы.

    Строки прошлого прогона удаляются: источник (журнал/`$metadata`) мог
    перестать возвращать сущность, и её старый счётчик не должен «залипать»
    в отчёте и дашборде.
    """

    now = datetime.now().isoformat(timespec="seconds")
    conn = db._audit_conn()
    try:
        conn.execute(
            "DELETE FROM audit_gap_counts WHERE base_url = ?", (base_url,)
        )
        for gap in gaps:
            conn.execute(
                "INSERT INTO audit_gap_counts (base_url, entity_name, run_id, "
                "doc_count, count_available, count_source, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(base_url, entity_name) DO UPDATE SET "
                "run_id=excluded.run_id, doc_count=excluded.doc_count, "
                "count_available=excluded.count_available, "
                "count_source=excluded.count_source, "
                "updated_at=excluded.updated_at",
                (base_url, gap["entity_name"], gap["run_id"],
                 gap["doc_count"], int(gap["count_available"]),
                 gap["count_source"], now),
            )
        conn.commit()
    finally:
        conn.close()


def _run_gap_counts(
    client: OneCClient,
    base_url: str,
    run_id: int,
    registry_entities: list[str],
    pause: float,
    period_start: str,
    period_end: str,
    gap_source: str = "auto",
) -> tuple[list[dict], str]:
    """Счётчики нереестровых ``Document_*`` (только --include-unmapped).

    Источник списка сущностей:
    - ``journal`` — журнал операций (один запрос): ``Ref_Type`` даёт типы,
      а счётчики берутся из самого журнала (совпадают с числом документов,
      включая непроведённые);
    - ``metadata`` — перебор всех ``Document_*`` из ``$metadata`` с
      ``$inlinecount``-счётчиком (дорого, но всеобъемлюще);
    - ``auto`` — журнал, если доступен, иначе ``metadata``.

    Возвращает ``(gaps, source)``; ``source`` — "journal"/"metadata"/"none".
    """
    registry_set = set(registry_entities)

    if gap_source in ("auto", "journal"):
        counts = client.fetch_journal_document_types(period_start, period_end)
        if counts is not None:
            gaps = [
                {
                    "base_url": base_url,
                    "entity_name": name,
                    "run_id": run_id,
                    "doc_count": int(count),
                    "count_available": True,
                    "count_source": "journal",
                }
                for name, count in sorted(counts.items())
                if name.startswith("Document_") and name not in registry_set
            ]
            _persist_gaps(base_url, gaps)
            return gaps, "journal"
        if gap_source == "journal":
            print(
                "Журнал операций недоступен — счётчики нереестровых "
                "Document_* пропущены.",
                file=sys.stderr,
            )
            return [], "none"

    try:
        sets = client.fetch_metadata_entity_sets()
    except ValueError as e:
        raise ValueError(f"Не удалось получить $metadata для gaps: {e}") from e

    gaps = []
    for name in sorted(sets):
        if not name.startswith("Document_") or name in registry_set:
            continue
        if pause > 0:
            _TIME.sleep(pause)
        count, available = _count_records(
            client, name, period_start, period_end
        )
        gaps.append({
            "base_url": base_url,
            "entity_name": name,
            "run_id": run_id,
            "doc_count": count if count is not None else 0,
            "count_available": bool(available),
            "count_source": "inlinecount" if available else "none",
        })
    _persist_gaps(base_url, gaps)
    return gaps, "metadata"


def _count_mismatches(run_id: int) -> int:
    conn = db._audit_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM audit_checkpoints WHERE run_id=? "
            "AND count_mismatch != 0",
            (run_id,),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _finalize_run(run_id: int, status: str, error: str | None = None) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = db._audit_conn()
    try:
        if error is None:
            conn.execute(
                "UPDATE audit_runs SET status=?, finished_at=? WHERE id=?",
                (status, now, run_id),
            )
        else:
            conn.execute(
                "UPDATE audit_runs SET status=?, finished_at=? WHERE id=?",
                (db.AUDIT_RUN_FAILED, now, run_id),
            )
            conn.execute(
                "UPDATE audit_checkpoints SET status=?, error=? "
                "WHERE run_id=? AND status IN ('RUNNING','PENDING')",
                (db.AUDIT_CP_FAILED, error[:500], run_id),
            )
        conn.commit()
    finally:
        conn.close()


def _resolve_base(name_or_url: str) -> dict | None:
    """База из таблицы bases по имени (без учёта регистра), URL или id."""

    needle = str(name_or_url or "").strip()
    if not needle:
        return None
    for b in db.list_bases():
        if b["name"].strip().lower() == needle.lower():
            return b
    if needle.lower().startswith(("http://", "https://")):
        return db.get_base(needle)
    for b in db.list_bases():
        if str(b["id"]) == needle:
            return b
    return None


# ================================ ОТЧЁТ ======================================


def write_audit_report(
    summary: dict,
    out_dir: str = "exports",
) -> str:
    """Формирует ``exports/audit_*.xlsx``: «Сводка», «Проблемы», «Gaps».

    Каждая строка Сводки — checkpoint (entity × месяц). «Проблемы» — вход
    для ручной сверки: расхождения с ожидаемым количеством (``$inlinecount``,
    миграция данных), деградация (data-poor), ошибки выгрузки. Возвращает
    путь к файлу.
    """
    import os

    os.makedirs(out_dir, exist_ok=True)
    safe_name = "".join(
        c if (c.isalnum() or c in "-_.") else "_" for c in summary["base_name"]
    ).strip("_")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(
        out_dir,
        f"audit_{safe_name or 'base'}_{summary['period_start']}_"
        f"{summary['period_end']}_{ts}.xlsx",
    )
    checkpoints = db.list_audit_checkpoints(summary["run_id"])
    gaps = db.list_audit_gap_counts(summary["base_url"])

    workbook = openpyxl.Workbook()

    _write_summary_sheet(workbook, checkpoints, summary)
    _write_problems_sheet(workbook, checkpoints)
    _write_gaps_sheet(workbook, gaps)

    workbook.save(path)
    return path


def _style_headers(ws, headers: list[str]) -> None:
    for col, title in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = CENTER
        cell.border = BORDER
    ws.freeze_panes = "A2"


def _cell(ws, row: int, col: int, value) -> None:
    cell = ws.cell(row=row, column=col, value=value)
    cell.border = BORDER
    return cell


def _write_summary_sheet(
    workbook: openpyxl.Workbook,
    checkpoints: list[dict],
    summary: dict,
) -> None:
    ws = workbook.active
    ws.title = "Сводка"
    headers = [
        "Сущность", "Чанк с", "Чанк по", "Строк", "Сумма документов",
        "Страниц", "Данных-бедно", "$inlinecount", "Ожидалось",
        "Расхождение", "Статус", "Ошибка",
    ]
    _style_headers(ws, headers)
    for row_i, cp in enumerate(checkpoints, start=2):
        values = [
            cp["entity_name"], cp["chunk_start"], cp["chunk_end"],
            cp["rows_processed"], cp["sum_amount"], cp["page_count"],
            "да" if cp["degraded"] else "",
            "да" if cp["count_available"] else "нет",
            cp["expected_count"], cp["count_mismatch"],
            cp["status"], cp["error"],
        ]
        for col_i, value in enumerate(values, start=1):
            _cell(ws, row_i, col_i, value)
    for col in (4, 5, 6, 9, 10):
        _fmt_column(ws, col, "0" if col in (4, 6, 9, 10) else "0.00")
    ws.column_dimensions["A"].width = 46
    ws.column_dimensions["L"].width = 60


def _fmt_column(ws, col: int, fmt: str) -> None:
    for row in range(2, ws.max_row + 1):
        ws.cell(row=row, column=col).number_format = fmt


def _write_problems_sheet(
    workbook: openpyxl.Workbook,
    checkpoints: list[dict],
) -> None:
    ws = workbook.create_sheet("Проблемы")
    headers = ["Сущность", "Чанк с", "Чанк по", "Проблема", "Детали"]
    _style_headers(ws, headers)
    row_i = 2
    for cp in checkpoints:
        if cp["status"] == db.AUDIT_CP_FAILED:
            problem, detail = ("Ошибка выгрузки", cp["error"] or "")
        elif cp["count_mismatch"]:
            problem = "Расхождение с ожидаемым количеством"
            detail = (
                f"ожидалось {cp['expected_count']}, загружено "
                f"{cp['rows_processed']}"
            )
        elif cp["degraded"]:
            problem = "Минимальный набор полей (data-poor)"
            detail = "400/500 на полном $select — деградация на 3 поля"
        else:
            continue
        for col_i, value in enumerate(
            [cp["entity_name"], cp["chunk_start"], cp["chunk_end"],
             problem, detail],
            start=1,
        ):
            _cell(ws, row_i, col_i, value)
        row_i += 1
    ws.column_dimensions["A"].width = 46
    ws.column_dimensions["E"].width = 70


def _write_gaps_sheet(
    workbook: openpyxl.Workbook,
    gaps: list[dict],
) -> None:
    ws = workbook.create_sheet("Gaps")
    headers = ["Сущность", "Количество документов", "Источник"]
    _style_headers(ws, headers)
    source_labels = {
        "journal": "журнал операций",
        "inlinecount": "$inlinecount",
    }
    for row_i, gap in enumerate(gaps, start=2):
        source = source_labels.get(str(gap.get("count_source") or ""), "—")
        for col_i, value in enumerate(
            [gap["entity_name"], gap["doc_count"], source],
            start=1,
        ):
            _cell(ws, row_i, col_i, value)
    _fmt_column(ws, 2, "0")
    ws.column_dimensions["A"].width = 46


# ================================== CLI ======================================


def _problems_in_checkpoints(checkpoints: list[dict]) -> list[dict]:
    """Проблемные чанки по тем же правилам, что лист «Проблемы» отчёта."""

    return [
        cp for cp in checkpoints
        if cp["status"] == db.AUDIT_CP_FAILED
        or cp["count_mismatch"]
        or cp["degraded"]
    ]


def _summarize_base(base: dict | None, base_url: str) -> dict:
    """Сводка текущего состояния аудита одной базы (из БД, без сети).

    Агрегирует checkpoint'ы по ``base_url`` (см.
    ``db.list_audit_checkpoints_by_base``); статусы чанков: ``done`` —
    завершён со строками, ``empty`` — завершён с 0 строк (отсутствующая
    сущность 404 или пропуск при резюме), ``failed`` — ошибка. Явный
    «пропущено» в БД не хранится, поэтому не подделывается.
    """

    checkpoints = db.list_audit_checkpoints_by_base(base_url)
    done = empty = failed = 0
    rows_total = 0
    sum_total = 0.0
    for cp in checkpoints:
        if cp["status"] == db.AUDIT_CP_FAILED:
            failed += 1
            continue
        if cp["status"] == db.AUDIT_CP_COMPLETED:
            rows_total += cp["rows_processed"] or 0
            sum_total += cp["sum_amount"] or 0.0
            if cp["rows_processed"]:
                done += 1
            else:
                empty += 1

    runs = db.list_audit_runs(base_url=base_url)
    latest = runs[0] if runs else {}
    gaps = db.list_audit_gap_counts(base_url)
    problems = _problems_in_checkpoints(checkpoints)

    return {
        "base_name": (base or {}).get("name") or base_url,
        "base_url": base_url,
        "base_id": (base or {}).get("id"),
        "run_id": latest.get("id"),
        "run_status": latest.get("status"),
        "period_start": latest.get("period_start"),
        "period_end": latest.get("period_end"),
        "include_unmapped": bool(latest.get("include_unmapped")),
        "created_at": latest.get("created_at"),
        "finished_at": latest.get("finished_at"),
        "chunks_total": len(checkpoints),
        "chunks_done": done,
        "chunks_empty": empty,
        "chunks_failed": failed,
        "rows_total": rows_total,
        "sum_total": round(sum_total, 2),
        "mismatches": sum(1 for cp in checkpoints if cp["count_mismatch"]),
        "degraded": sum(1 for cp in checkpoints if cp["degraded"]),
        "gaps_count": len(gaps),
        "problems": len(problems),
        "has_problems": bool(problems),
    }


def audit_summary(base_filter: str | None = None) -> list[dict]:
    """Сводка по базам с историей аудита (свежие базы — по имени).

    ``base_filter`` (имя/URL/id) сужает до одной базы; неизвестная — ValueError.
    """

    names = {b["url"].rstrip("/"): b for b in db.list_bases()}

    if base_filter:
        base = _resolve_base(base_filter)
        if base is None:
            raise ValueError(f"База '{base_filter}' не найдена")
        return [_summarize_base(base, str(base["url"]).rstrip("/"))]

    urls: list[str] = []
    for run in db.list_audit_runs():
        url = str(run["base_url"]).rstrip("/")
        if url not in urls:
            urls.append(url)

    rows = [_summarize_base(names.get(url), url) for url in urls]
    rows.sort(key=lambda r: (not r["has_problems"], str(r["base_name"]).lower()))
    return rows


def audit_hours_summary(base_filter: str | None = None) -> list[dict]:
    """Нормочасы последнего прогона по каждой базе, разбитые по компании/сотруднику.

    Объединяет ``audit_summary`` (какой run считать актуальным для базы) с
    агрегатом ``audit_hours``. Пустые чанки-маркеры уже отфильтрованы в
    ``db.aggregate_audit_hours``.
    """

    out: list[dict] = []
    for row in audit_summary(base_filter):
        run_id = row.get("run_id")
        if not run_id:
            continue
        for item in db.aggregate_audit_hours(run_id):
            out.append({
                "base_name": row["base_name"],
                "base_url": row["base_url"],
                "base_id": row.get("base_id"),
                "period_start": row.get("period_start"),
                "period_end": row.get("period_end"),
                "run_status": row.get("run_status"),
                "company": item["company"],
                "employee": item["employee"],
                "operations": item["operations"],
                "hours": item["hours"],
            })
    out.sort(key=lambda r: (
        str(r["base_name"]).lower(),
        str(r["company"]).lower(),
        str(r["employee"]).lower(),
    ))
    return out


def audit_base_detail(base_url: str) -> list[dict]:
    """Разбивка чанков базы по сущностям (для ``--summary --base``)."""

    by_entity: dict[str, dict] = {}
    for cp in db.list_audit_checkpoints_by_base(base_url):
        row = by_entity.setdefault(cp["entity_name"], {
            "entity_name": cp["entity_name"], "chunks": 0, "rows": 0,
            "sum": 0.0, "mismatches": 0, "degraded": 0, "failed": 0,
        })
        row["chunks"] += 1
        row["rows"] += cp["rows_processed"] or 0
        row["sum"] += cp["sum_amount"] or 0.0
        if cp["status"] == db.AUDIT_CP_FAILED:
            row["failed"] += 1
        if cp["count_mismatch"]:
            row["mismatches"] += 1
        if cp["degraded"]:
            row["degraded"] += 1
    detail = list(by_entity.values())
    for row in detail:
        row["sum"] = round(row["sum"], 2)
    return detail


def verify_journal_coverage(
    base: dict,
    period_start: str,
    period_end: str,
    client: OneCClient | None = None,
) -> dict:
    """Проверка: все ли записи журнала операций покрыты аудитом.

    Аудит покрывает запись журнала одним из двух способов:

    * ``registry`` — тип есть в реестре ``doc_types.json``: документы
      читаются поштучно (строки/суммы/нормочасы);
    * ``gap`` — тип не в реестре, но начинается с ``Document_``: количество
      учитывается в счётчиках gaps (при ``--include-unmapped``).

    Любой другой ``Ref_Type`` (не ``Document_*``) — **дыра покрытия**:
    он не попадёт ни в аудит, ни в gaps. Возвращает отчёт с разбивкой по
    типам, флагом ``available`` (журнал недоступен) и списком
    ``uncovered``. Ручные (``Document_ОперацияБух``) и регламентные
    (``Document_РегламентнаяОперация``) операции выделяются отдельно.
    """

    client = client or OneCClient(
        base["url"], base["login"], base["password"]
    )
    refs = client.fetch_journal_ref_types(period_start, period_end)
    report: dict[str, Any] = {
        "available": refs is not None,
        "base_name": base.get("name", ""),
        "base_url": base.get("url", ""),
        "period_start": period_start,
        "period_end": period_end,
        "total": 0,
        "covered": 0,
        "coverage": None,
        "manual": 0,
        "routine": 0,
        "types": [],
        "uncovered": [],
    }
    if refs is None:
        return report

    registry = {s["entity"] for s in load_doc_types().values() if s.get("entity")}
    entries: list[dict] = []
    uncovered: list[str] = []
    for name, count in sorted(refs.items()):
        if name in registry:
            covered_by = "registry"
        elif name.startswith("Document_"):
            covered_by = "gap"
        else:
            covered_by = "uncovered"
            uncovered.append(name)
        entries.append({"type": name, "count": count, "covered_by": covered_by})
    total = sum(e["count"] for e in entries)
    covered = sum(e["count"] for e in entries if e["covered_by"] != "uncovered")
    report.update(
        total=total,
        covered=covered,
        coverage=(covered / total) if total else 1.0,
        manual=refs.get("Document_ОперацияБух", 0),
        routine=refs.get("Document_РегламентнаяОперация", 0),
        types=entries,
        uncovered=uncovered,
    )
    return report


def format_journal_coverage(report: dict) -> str:
    """Человекочитаемая печать отчёта ``verify_journal_coverage``."""

    lines = [
        f"База: {report['base_name']}",
        f"Период: {report['period_start']}..{report['period_end']}",
    ]
    if not report["available"]:
        lines.append("Журнал операций недоступен — покрытие не проверено.")
        return "\n".join(lines)

    source_label = {
        "registry": "реестр (поштучно)",
        "gap": "нереестровый (счётчик gaps)",
        "uncovered": "НЕ ПОКРЫТО",
    }
    rows = [
        [e["type"], e["count"], source_label[e["covered_by"]]]
        for e in report["types"]
    ]
    lines.append(_render_table(["Тип журнала", "Записей", "Покрытие"], rows))
    pct = (report["coverage"] or 0) * 100
    lines.append(
        f"Итого записей: {report['total']}, покрыто: {report['covered']} "
        f"({pct:.2f}%)."
    )
    lines.append(
        f"Ручные (Document_ОперацияБух): {report['manual']}, "
        f"регламентные (Document_РегламентнаяОперация): {report['routine']}."
    )
    if report["uncovered"]:
        lines.append(
            "ВНИМАНИЕ, дыры покрытия (не Journal Document_*): "
            + ", ".join(report["uncovered"])
        )
    return "\n".join(lines)


def _render_table(headers: list[str], rows: list[list]) -> str:
    """Простая выровненная таблица (ширины по содержимому)."""

    widths = [len(h) for h in headers]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(str(value)))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(str(v).ljust(widths[i])
                               for i, v in enumerate(row)))
    return "\n".join(lines)


_SUMMARY_HEADERS = [
    "База", "id", "Прогон", "Статус", "Период", "Чанков",
    "Готово", "Пусто", "Ошибок", "Расхожд.", "Данных-бедно", "Gaps", "Проблемы",
]


def _summary_row_values(row: dict) -> list:
    period = (
        f"{row['period_start']}..{row['period_end']}"
        if row["period_start"] else "-"
    )
    return [
        row["base_name"], row["base_id"] if row["base_id"] is not None else "-",
        row["run_id"] if row["run_id"] is not None else "-",
        row["run_status"] or "-", period, row["chunks_total"],
        row["chunks_done"], row["chunks_empty"], row["chunks_failed"],
        row["mismatches"], row["degraded"], row["gaps_count"],
        row["problems"] if row["problems"] else "",
    ]


def _format_summary_table(rows: list[dict]) -> str:
    return _render_table(_SUMMARY_HEADERS, [_summary_row_values(r) for r in rows])


def _format_base_detail(row: dict) -> str:
    """Сводка одной базы + таблица по сущностям."""

    block = [_render_table(_SUMMARY_HEADERS, [_summary_row_values(row)]), ""]
    detail = audit_base_detail(row["base_url"])
    headers = ["Сущность", "Чанков", "Строк", "Сумма документов",
               "Расхожд.", "Данных-бедно", "Ошибок"]
    values = [
        [d["entity_name"], d["chunks"], d["rows"], d["sum"],
         d["mismatches"], d["degraded"], d["failed"]]
        for d in detail
    ]
    block.append(f"Детализация по сущностям ({len(detail)}):")
    block.append(_render_table(headers, values) if values
                 else "(нет данных)")
    return "\n".join(block)


def _summary_json(rows: list[dict]) -> str:
    import json

    return json.dumps(rows, ensure_ascii=False, indent=2)


def _write_summary_file(path: str, rows: list[dict], as_json: bool) -> None:
    """Сохраняет плоскую сводку в файл: JSON либо CSV (разделитель «;»)."""

    if as_json:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_summary_json(rows))
        return

    import csv

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(_SUMMARY_HEADERS)
        for row in rows:
            writer.writerow(_summary_row_values(row))


def _default_out_dir() -> str:
    """Каталог отчёта по умолчанию.

    В контейнере рабочие данные живут в томе ``/data``; писать отчёт рядом с
    ними, чтобы он переживал пересоздание контейнера. Вне Docker — локальный
    ``exports/``.
    """
    return "/data/exports" if Path("/data").is_dir() else "exports"


def _print_run_summary(summary: dict, out_dir: str) -> None:
    """Печатает отчёт и итоговую строку одного прогона."""

    path = write_audit_report(summary, out_dir=out_dir)
    print(f"Отчёт: {path}")
    print(
        f"Чанков: {summary['chunks_total']} (готово "
        f"{summary['chunks_completed']}, пропущено "
        f"{summary['chunks_skipped']}, ошибок {summary['chunks_failed']}), "
        f"расхождений с ожидаемым количеством: "
        f"{summary['count_mismatches']}"
    )
    if summary["degraded_entities"]:
        print("Data-poor (минимальный $select): "
              + ", ".join(summary["degraded_entities"]))
    if summary.get("gaps_count") is not None:
        source = {
            "journal": "журнал операций",
            "metadata": "$metadata",
            "none": "источник недоступен",
        }.get(str(summary.get("gap_source") or ""), "—")
        print(f"Нереестровых Document_* подсчитано: {summary['gaps_count']} "
              f"(источник: {source})")


def _run_all_bases(
    start: str,
    end: str,
    out_dir: str,
    pause: float,
    include_unmapped: bool,
    restart: bool,
    gap_source: str = "auto",
) -> int:
    """Пакетный обход активных баз: ошибка одной базы не прерывает остальные."""

    bases = db.list_bases(active_only=True)
    if not bases:
        print("Активных баз нет.", file=sys.stderr)
        return 0
    total = len(bases)
    failures = 0
    for i, base in enumerate(bases, 1):
        print(f"[{i}/{total}] {base['name']}")
        try:
            summary = run_audit(
                base, start, end, pause=pause,
                include_unmapped=include_unmapped, restart=restart,
                gap_source=gap_source,
            )
            _print_run_summary(summary, out_dir)
        except Exception as e:
            print(f"  Пропущено: {e}", file=sys.stderr)
            failures += 1
    print(f"Пакетный аудит завершён: баз {total}, с ошибками {failures}.")
    return 1 if failures else 0


def _coverage_bases(args) -> list[dict]:
    """Базы для ``--verify-coverage``: одна (--base) или все активные."""

    if args.base:
        base = _resolve_base(args.base)
        return [base] if base else []
    return db.list_bases(active_only=True)


def _verify_coverage(args) -> int:
    """Проверка покрытия журнала операций аудитом (без записи в БД)."""

    bases = _coverage_bases(args)
    if not bases:
        print("Базы не найдены.", file=sys.stderr)
        return 2
    failures = 0
    skipped = 0
    for i, base in enumerate(bases, 1):
        if len(bases) > 1:
            print(f"[{i}/{len(bases)}] {base['name']}")
        report = verify_journal_coverage(base, args.start, args.end)
        print(format_journal_coverage(report))
        if not report["available"]:
            skipped += 1
        elif report["uncovered"]:
            failures += 1
    if failures:
        print(
            f"Покрытие проверено: баз с дырами {failures} из {len(bases)}"
            + (f", журнал недоступен у {skipped}" if skipped else "")
            + ".",
            file=sys.stderr,
        )
        return 1
    if skipped:
        print(
            f"Покрытие проверено: дыр нет у {len(bases) - skipped} баз"
            f" с журналом операций; журнал недоступен у {skipped} "
            "(покрытие не проверено)."
        )
        return 0
    print(f"Покрытие проверено: все записи журнала учтены ({len(bases)} баз).")
    return 0


def _audit_one(args) -> int:
    """Аудит одной базы по аргументам CLI (внутри уже взятой блокировки)."""

    base = _resolve_base(args.base)
    if base is None:
        names = [b["name"] for b in db.list_bases()]
        print(
            f"База '{args.base}' не найдена. "
            f"Доступные: {', '.join(names) or '-'}",
            file=sys.stderr,
        )
        return 2
    summary = run_audit(
        base, args.start, args.end, pause=args.pause,
        include_unmapped=args.include_unmapped, restart=args.restart,
        gap_source=args.gap_source,
    )
    _print_run_summary(summary, args.out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.audit",
        description="Глубокий аудит данных баз 1С:Фреш (checkpoint-резюм).",
    )
    parser.add_argument("--base",
                        help="Имя (или URL/id) базы из таблицы bases")
    parser.add_argument("--all-bases", action="store_true",
                        help="Пакетный обход всех активных баз (по одной, "
                             "продолжает при ошибке отдельной базы)")
    parser.add_argument("--start", help="Начало периода, YYYY-MM-DD")
    parser.add_argument("--end", help="Конец периода, YYYY-MM-DD")
    parser.add_argument("--list-bases", action="store_true",
                        help="Напечатать активные базы как «id<TAB>имя» и выйти "
                             "(источник списка для пакетного обхода)")
    parser.add_argument("--summary", action="store_true",
                        help="Сводка по истории аудита (без сети): по базам "
                             "или по одной с --base")
    parser.add_argument("--problems-only", action="store_true",
                        help="С --summary: только базы с проблемами")
    parser.add_argument("--json", action="store_true",
                        help="С --summary: вывести JSON вместо таблицы")
    parser.add_argument("--verify-coverage", action="store_true",
                        help="Проверить, что все записи журнала операций "
                             "покрыты аудитом (реестр поштучно или счётчик "
                             "gaps); требует --start/--end и --base/--all-bases")
    parser.add_argument("--include-unmapped", action="store_true",
                        help="Подсчитать нереестровые Document_* (журнал "
                             "операций, иначе $metadata)")
    parser.add_argument("--gap-source", choices=("auto", "journal", "metadata"),
                        default="auto",
                        help="Источник списка нереестровых Document_* для "
                             "--include-unmapped: auto — журнал операций, "
                             "иначе $metadata (по умолчанию); journal — только "
                             "журнал; metadata — полный перебор $metadata")
    parser.add_argument("--pause", type=float, default=0.2,
                        help="Пауза между запросами, сек (по умолчанию 0.2)")
    parser.add_argument("--restart", action="store_true",
                        help="Перечитать COMPLETED-чанки заново")
    parser.add_argument("--out", default=None,
                        help="Для аудита — каталог отчёта (по умолчанию "
                             "/data/exports в контейнере, иначе exports/); "
                             "для --summary — файл (JSON при --json, иначе CSV)")
    args = parser.parse_args(argv)

    if args.list_bases:
        for b in db.list_bases(active_only=True):
            print(f"{b['id']}\t{b['name']}")
        return 0

    if args.summary:
        try:
            rows = audit_summary(args.base)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        if args.problems_only:
            rows = [r for r in rows if r["has_problems"]]
        if args.base and rows:
            payload = _summary_json(rows) if args.json else _format_base_detail(rows[0])
        else:
            payload = _summary_json(rows) if args.json else _format_summary_table(rows)
        if args.out:
            _write_summary_file(args.out, rows, as_json=args.json)
            print(f"Сводка: {args.out}")
        else:
            print(payload)
        return 0

    if args.base and args.all_bases:
        parser.error("--base и --all-bases взаимоисключающи")

    if args.verify_coverage:
        if not ((args.base or args.all_bases) and args.start and args.end):
            parser.error("--verify-coverage требует --start/--end и "
                         "--base или --all-bases")
        return _verify_coverage(args)

    if not ((args.base or args.all_bases) and args.start and args.end):
        parser.error("--start и --end обязательны вместе с --base или "
                     "--all-bases (кроме --list-bases и --summary)")

    if args.out is None:
        args.out = _default_out_dir()

    # SIGTERM (кнопка «Остановить» в UI) ведёт себя как Ctrl+C — состояние
    # сохраняется, повторный запуск продолжит с прерванного чанка.
    _install_sigterm_handler()
    try:
        with _audit_lock(config.AUDIT_LOCK_FILE):
            if args.all_bases:
                return _run_all_bases(
                    args.start, args.end, args.out, args.pause,
                    args.include_unmapped, args.restart, args.gap_source,
                )
            return _audit_one(args)
    except AuditBusyError as e:
        print(str(e), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        # Осознанный Ctrl+C/остановка из UI, а не сбой: прогон и незавершённый
        # чанк остаются в состоянии RUNNING, поэтому повторный запуск
        # продолжит с места прерывания (COMPLETED-чанки пропускаются).
        print(
            "\nАудит приостановлен — состояние сохранено. "
            "Повторный запуск той же команды продолжит с прерванного чанка.",
            file=sys.stderr,
        )
        return 130
    except ValueError as e:
        print(f"Аудит не выполнен: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
