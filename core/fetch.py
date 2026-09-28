import pandas as pd

from core.api_client import OneCClient
from core.norms import load_doc_types

REPORT_COLUMNS = [
    "Клиент", "ИНН", "Система налогообложения", "Период", "Вид документа",
    "Вид операции", "Количество операций", "Ответственный сотрудник", "Роль сотрудника",
    "Норма на операцию", "Коэффициент сложности", "Трудозатраты, нормочасы",
    "Комментарий",
]


def _period_label(period_start: str, period_end: str) -> str:
    """Показывает период отчёта (месяц/квартал/произвольный)."""
    try:
        s = pd.to_datetime(period_start)
        e = pd.to_datetime(period_end)
    except Exception:
        return f"{period_start} — {period_end}"
    if s.year == e.year and s.month == e.month:
        return s.strftime("%m.%Y")
    return f"{s.strftime('%d.%m.%Y')} — {e.strftime('%d.%m.%Y')}"


def _variant_value(rec: dict, spec: dict) -> str:
    field = spec.get("variant_field") or "ВидОперации"
    return str(rec.get(field) or "")


def _resolve_key(
    entries: list[tuple[str, dict]],
    rec: dict,
) -> tuple[str, dict]:
    """
    Определяет ключ нормы для документа.

    Для сущностей с вариантами (авансовый отчёт «до 10 чеков» vs
    «с ГСМ или командировкой») выбирается вариант по вхождению термов
    в ``variant_field`` записи (по умолчанию ВидОперации), case-insensitive.
    Базовая запись с ``variants`` — селектор: совпавший терм уводит на
    ``variant_to``, иначе остаёмся на базовом ключе (default).
    """

    if len(entries) == 1:
        return entries[0]

    by_key = dict(entries)
    for key, spec in entries:
        if not spec.get("variants"):
            continue
        value = _variant_value(rec, spec).lower()
        for term in spec["variants"]:
            if term.lower() in value:
                target = spec.get("variant_to")
                if target and target in by_key:
                    return target, by_key[target]
        return key, spec
    return entries[0]


def fetch_documents(
    client: OneCClient,
    period_start: str,
    period_end: str,
    doc_types: list[str] | None = None,
    sno: str = "",
) -> pd.DataFrame:
    """Выгружает документы указанных видов за период через OData.

    Для каждого вида документа опрашивается соответствующая OData-сущность,
    каждая строка — один документ. GUID организаций и пользователей
    расшифровываются в наименования через кэш справочников клиента.

    Если на одну сущность заведено несколько ключей (варианты норм,
    например авансовый отчёт «до 10 чеков» / «с ГСМ или командировкой») —
    сущность опрашивается один раз, а документ относится к подходящему ключу
    по ``variants`` (вхождение термов в ВидОперации).

    ``sno`` — система налогообложения клиентской базы (bases.sno): заполняет
    колонку «Система налогообложения» (одно значение на базу; ТЗ §6.1).

    Возвращает DataFrame по схеме REPORT_COLUMNS (без агрегации).
    """

    client._prefetch_catalogs()

    registry = load_doc_types()
    keys = doc_types or list(registry.keys())

    rows: list[dict] = []
    skipped: list[str] = []
    period = _period_label(period_start, period_end)

    by_entity: dict[str, list[tuple[str, dict]]] = {}
    for key in keys:
        spec = registry.get(key)
        if not spec or not spec.get("entity"):
            continue
        by_entity.setdefault(spec["entity"], []).append((key, spec))

    for entity, entries in by_entity.items():
        titles = ", ".join(spec.get("title", key) for key, spec in entries)
        try:
            records = client.fetch_documents(entity, period_start, period_end)
        except ValueError as e:
            skipped.append(f"{titles}: {e}")
            continue

        for rec in records:
            key, spec = _resolve_key(entries, rec)
            org_guid = rec.get("Организация_Key")
            org = client.organization_info(org_guid) if org_guid else {}
            author_guid = rec.get("Ответственный_Key")
            author = client._name_by_guid(author_guid) if author_guid else ""

            rows.append({
                "Клиент": org.get("name", ""),
                "ИНН": org.get("inn", ""),
                "Система налогообложения": sno,
                "Период": period,
                "Вид документа": spec.get("title", key),
                "Вид операции": rec.get("ВидОперации") or "",
                "Количество операций": 1,
                "Ответственный сотрудник": author,
                "Роль сотрудника": "",
                "Норма на операцию": None,
                "Коэффициент сложности": None,
                "Трудозатраты, нормочасы": None,
                "Комментарий": "",
            })

    df = pd.DataFrame(rows, columns=REPORT_COLUMNS)
    df.attrs["skipped"] = skipped
    return df


def unmapped_user_names(df: pd.DataFrame) -> list[str]:
    """Уникальные авторы документов для сверки с таблицей сотрудников."""
    if df.empty or "Ответственный сотрудник" not in df.columns:
        return []
    names = df["Ответственный сотрудник"].dropna().astype(str)
    return sorted({n for n in names if n.strip()})