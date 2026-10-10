import logging
import json
import os
import sys
import threading
import time
import concurrent.futures
from typing import Any, Callable

import requests
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
from requests.exceptions import (HTTPError, RequestException, RetryError)
from urllib3.util.retry import Retry

if __package__ in (None, ""):
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

logger = logging.getLogger(__name__)

_SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "core",
    "odata_schema.json",
)

_schema_lock = threading.Lock()
_schema_cache: dict[str, Any] | None = None

# Транспорт: повторы 1С:Фреш (см. OneCClient.__init__). 1,2,4,8,... секунд.
_DEFAULT_RETRY_TOTAL = 5


def _load_schema() -> dict[str, Any]:
    """Лениво читает снимок odata_schema.json (офлайн-выбор полей выгрузки)."""
    global _schema_cache
    if _schema_cache is None:
        with _schema_lock:
            if _schema_cache is None:
                try:
                    with open(_SCHEMA_PATH, encoding="utf-8-sig") as f:
                        _schema_cache = json.load(f)
                except (OSError, ValueError):
                    _schema_cache = {}
    return _schema_cache


class OneCClient:
    """
    Клиент OData 1С:Фреш.

    Один экземпляр = одна клиентская база (1С:Фреш). Предоставляет:

    - пагинацию OData через ``_paginate_pages`` (генератор, nextLink/$skip);
    - экспоненциальный backoff для 429/5xx через urllib3.Retry;
    - кэш «GUID -> Наименование» для справочников (клиентский JOIN);
    - выгрузку документов за период лениво (``fetch_documents_iter``) и
      списком (``fetch_documents``, обратная совместимость);
    - диагностику состава публикации (``fetch_metadata_entity_sets``).
    """

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, password)
        self.session.headers.update({"Accept": "application/json"})

        retries = Retry(
            total=_DEFAULT_RETRY_TOTAL,
            connect=_DEFAULT_RETRY_TOTAL,
            read=_DEFAULT_RETRY_TOTAL,
            status=_DEFAULT_RETRY_TOTAL,
            backoff_factor=1.0,      # экспонента 1, 2, 4, 8, 16 c
            backoff_max=32.0,
            backoff_jitter=1.0,      # рассинхронизация повторов (no thundering herd)
            retry_after_max=30.0,    # кап для Retry-After от 1С:Фреш
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        adapter = HTTPAdapter(
            pool_connections=20, pool_maxsize=20, max_retries=retries
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Кэш «GUID -> Название» (Контрагенты, Организации, Пользователи, ...)
        self._guid_to_name: dict[str, str] = {}
        # Справочник организаций: GUID -> {name, inn} — для колонок ИНН/Клиент
        self._organizations: dict[str, dict[str, str]] = {}
        self._catalogs_loaded = False
        self._cache_lock = threading.Lock()
        # Деградировавшие на минимальный $select сущности (silent-400 фолбэк).
        # Только запись: аудит читает её и помечает выгрузку как data-poor.
        self._degraded_entities: set[str] = set()

    @staticmethod
    def _is_select_error(message: str) -> bool:
        """Признак 400/500 — фолбэк на минимальный $select (как было раньше).

        400 возникает из-за несовместимого $select; исключённый 500 обычно
        был следствием кривого кодирования $filter (исправлен через quote) и
        теперь в основном уходит в Retry, но значение из бэк-офиса 1С тоже
        остаётся поводом для деградации (обратная совместимость).
        """
        return "400" in message or "500" in message

    def _request_page(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """Один GET страницы OData с маппингом ошибок в ValueError.

        Повторы 429/5xx выполняет urllib3.Retry на уровне сессии; сюда
        приходит либо успешный ответ, либо ошибка ПОСЛЕ исчерпания повторов.
        """
        try:
            response = self.session.get(endpoint, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except RetryError as e:
            raise ValueError(
                f"1С не ответила после {_DEFAULT_RETRY_TOTAL} повторов: {e}"
            ) from e
        except HTTPError as e:
            raise ValueError(self._friendly_http_error(e)) from e
        except RequestException as e:
            raise ValueError(f"Не удалось соединиться с 1C: {e}") from e

    @staticmethod
    def _page_top(endpoint: str, params: dict[str, Any]) -> int:
        """Размер страницы из URL ($top) либо из params, иначе 1000.

        1С:Фреш кладёт $top в URL (в т.ч. в @odata.nextLink); справочники
        передают его в params. Используется для критерия «страница короче
        $top = последняя».
        """
        from urllib.parse import parse_qs, urlparse

        raw = None
        query = urlparse(endpoint).query
        if query:
            raw = parse_qs(query).get(r"$top")
        if not raw:
            value = (params or {}).get(r"$top")
            raw = (
                value if isinstance(value, list)
                else ([str(value)] if value else None)
            )
        if not raw:
            return 1000
        try:
            return int(raw[0])
        except (TypeError, ValueError):
            return 1000

    def _paginate_pages(
        self,
        endpoint: str,
        params: dict[str, Any],
        pause: float = 0.0,
        on_page: Callable[[], None] | None = None,
    ):
        """
        Генератор страниц OData.

        Следует ``@odata.nextLink``, если 1С его вернула; иначе продолжает
        инкрементальный ``$skip`` (поведение, проверенное на доноре). Постоянная
        память независимо от объёма: страница обрабатывается и отпускается.

        ``pause > 0`` — жёсткая пауза между запросами (токен-рейт для 1С:Фреш;
        отчёт использует 0.0, аудит — 0.2). ``on_page`` — хук после каждой
        успешной страницы (аудит считает страницы для checkpoint'а).
        """
        params = dict(params or {})
        first = True
        while True:
            if not first and pause > 0:
                time.sleep(pause)
            data = self._request_page(endpoint, params)
            first = False
            yield data
            if on_page is not None:
                on_page()

            chunk = data.get("value", [])
            if not chunk:
                return
            next_link = data.get("@odata.nextLink")
            if next_link:
                endpoint = next_link
                params = {}
                continue
            params["$skip"] = params.get("$skip", 0) + len(chunk)
            if len(chunk) < self._page_top(endpoint, params):
                return

    def _paginate(
        self,
        endpoint: str,
        params: dict[str, Any],
        pause: float = 0.0,
    ) -> list[dict]:
        """
        Загружает все страницы OData списком записей (обёртка над
        ``_paginate_pages`` для обратной совместимости: справочники,
        старые вызовы).
        """

        return [
            rec
            for page in self._paginate_pages(endpoint, params, pause=pause)
            for rec in page.get("value", [])
        ]

    @staticmethod
    def _friendly_http_error(error: requests.exceptions.HTTPError) -> str:
        status: int | None = None
        url: str = ""
        if error.response is not None:
            status = error.response.status_code
            url = error.response.url

        if status == 401:
            return "OData вернул 401 Unauthorized. Проверьте логин/пароль."
        if status == 403:
            return "OData вернул 403 Forbidden. Проверьте роль УдаленныйДоступOData."
        return f"OData-запрос завершился ошибкой {status}: {url}"

    # ================= ЗАГРУЗКА СПРАВОЧНИКОВ (CLIENT-SIDE JOIN) =================
    def _prefetch_catalogs(self, include_users: bool = True) -> None:
        if self._catalogs_loaded:
            return

        catalogs: list[tuple[str, list[str]]] = [
            ("Catalog_Контрагенты", ["Ref_Key", "Description"]),
            ("Catalog_Организации", ["Ref_Key", "Description", "ИНН"]),
        ]
        if include_users:
            catalogs.append(("Catalog_Пользователи", ["Ref_Key", "Description"]))

        if include_users:
            logger.info("Предзагрузка справочников (Контрагенты, Пользователи, Организации)...")
        else:
            logger.info("Предзагрузка справочников (Контрагенты, Организации)...")

        def load_cat(cat_name: str, select: list[str]):
            endpoint = f"{self.base_url}/odata/standard.odata/{cat_name}"
            params = {"$format": "json", "$select": ",".join(select), "$top": 2000}
            try:
                recs = self._paginate(endpoint, params)
                with self._cache_lock:
                    for r in recs:
                        k = r.get("Ref_Key")
                        d = r.get("Description")
                        if k and d:
                            self._guid_to_name[str(k)] = str(d)
                        if cat_name == "Catalog_Организации" and k:
                            self._organizations[str(k)] = {
                                "name": str(d or ""),
                                "inn": str(r.get("ИНН") or ""),
                            }
            except Exception as e:
                logger.debug(f"Пропуск справочника {cat_name}: {e}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(load_cat, cat_name, fields)
                for cat_name, fields in catalogs
            ]
            for future in futures:
                future.result()

        self._catalogs_loaded = True
        logger.info(f"Справочники загружены. В кэше {len(self._guid_to_name)} записей.")

    def organization_info(self, guid: str | None) -> dict[str, str]:
        """
        Возвращает {name, inn} организации по GUID (или пустой словарь)
        """

        if not guid:
            return {"name": "", "inn": ""}
        return self._organizations.get(str(guid), {"name": "", "inn": ""})

    def _name_by_guid(self, guid: str | None) -> str:
        """
        Расшифровка GUID справочника по локальному кэшу
        """

        if not guid:
            return ""
        if isinstance(guid, dict):
            return guid.get("Description") or guid.get("Наименование") or str(guid)
        return self._guid_to_name.get(str(guid), str(guid))

    # ========================= ВЫГРУЗКА ДОКУМЕНТОВ ===========================
    _COMMON_FIELDS: list[str] = [
        "Ref_Key", "Date", "Number", "Posted",
        "Организация_Key", "Ответственный_Key",
        "ВидОперации", "СуммаДокумента",
        # Поля-источники variant_rules (field/variant_field): без них правило
        # всегда видит пустое поле. «Содержание» — текст ручных операций
        # (ОперацияБух); «Комментарий» — универсальный; «КодВидаОперации» —
        # коррек. счёт-фактуры (sf_correct). Отбор по доступности у сущности
        # делает entity_properties() из снимка odata_schema.json, поэтому
        # отсутствующие у сущности поля не попадают в $select и не дают 400.
        "Комментарий", "КодВидаОперации", "Содержание",
    ]

    def entity_properties(self, entity: str) -> set[str]:
        """
        Имена реквизитов сущности из локального снимка odata_schema.json.

        Снимок позволяет выбирать только доступные поля и не проваливаться
        в 400-fallback, из-за которого терялись реквизиты организации и автора.
        """
        ent = _load_schema().get("entities", {}).get(entity)
        if not ent:
            return set()
        return set(ent.get("properties") or {})

    def _documents_request(
        self,
        entity: str,
        period_start: str,
        period_end: str,
        fields: list[str],
        page_size: int,
    ) -> tuple[str, dict[str, Any]]:
        """Собирает endpoint + params запроса документов сущности за период.

        Фильтр по дате: ``Date ge datetime'..' and Date le datetime'..'``
        (граница конца дня приклеивается как ``T23:59:59``). Кодирование
        через urllib.parse.quote: requests по умолчанию кодирует пробелы
        как '+', а 1С такой фильтр не парсит (500).
        """
        from urllib.parse import quote

        period_start_safe = self._start_of_day(period_start)
        period_end_safe = self._end_of_day(period_end)
        ffilter = (
            f"Date ge datetime'{period_start_safe}' "
            f"and Date le datetime'{period_end_safe}'"
        )
        select_query = quote(",".join(fields), safe="")
        filter_query = quote(ffilter, safe="")
        endpoint = (
            f"{self.base_url}/odata/standard.odata/{entity}"
            f"?{r'$format'}=json&{r'$select'}={select_query}"
            f"&{r'$filter'}={filter_query}&{r'$top'}={page_size}"
        )
        return endpoint, {r"$skip": 0}

    def fetch_documents_iter(
        self,
        entity: str,
        period_start: str,
        period_end: str,
        select: list[str] | None = None,
        pause: float = 0.0,
        page_size: int = 1000,
        on_page: Callable[[], None] | None = None,
    ):
        """Генератор документов сущности за период (ленивая выгрузка).

        Потребитель (аудит) обрабатывает каждую строку и отпускает — память
        не растёт с объёмом данных. Первый запрос пробует полный ``$select``;
        если 1С отвечает 400/500 (несовместимость полей), выполняется повтор
        с минимальным набором из трёх полей, а сущность фиксируется в
        ``self._degraded_entities`` — аудит помечает такую выгрузку как
        data-poor. Если ошибка приходит ПОСЛЕ первого успешного запроса —
        она пробрасывается напрямую (иначе строки задвоились бы).
        """
        if select is None:
            available = self.entity_properties(entity)
            fields = (
                [f for f in self._COMMON_FIELDS if f in available]
                if available
                else list(self._COMMON_FIELDS)
            )
        else:
            fields = list(select)

        endpoint, params = self._documents_request(
            entity, period_start, period_end, fields, page_size
        )
        yielded = False
        try:
            for page in self._paginate_pages(
                endpoint, params, pause=pause, on_page=on_page
            ):
                for rec in page.get("value", []):
                    yielded = True
                    yield rec
        except ValueError as e:
            if yielded or not self._is_select_error(str(e)):
                raise
            self._degraded_entities.add(entity)
            fields = list(self._COMMON_FIELDS[:3])
            endpoint, params = self._documents_request(
                entity, period_start, period_end, fields, page_size
            )
            for page in self._paginate_pages(
                endpoint, params, pause=pause, on_page=on_page
            ):
                yield from page.get("value", [])

    def fetch_documents(
        self,
        entity: str,
        period_start: str,
        period_end: str,
        select: list[str] | None = None,
    ) -> list[dict]:
        """
        Выгружает документы сущности за период списком.

        Обёртка над ``fetch_documents_iter``: интерфейс и поведение для
        отчёта (``core.fetch.fetch_documents``) и старых вызовов не меняются.
        Если часть полей недоступна для сущности (400 Bad Request) — повторный
        запрос с минимальным набором полей (фолбэк ниже в ``_iter``).
        """
        return list(
            self.fetch_documents_iter(entity, period_start, period_end, select=select)
        )

    def fetch_metadata_entity_sets(self) -> dict[str, str]:
        """Fetch and parse OData $metadata to extract entity sets.

        Returns:
            dict: mapping of entity set name (e.g., 'Document_РеализацияТоваровУслуг')
                  to its EntityType (e.g., 'StandardODATA.Document_РеализацияТоваровУслуг')
        """
        from xml.etree import ElementTree

        url = f"{self.base_url}/odata/standard.odata/$metadata"
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            raise ValueError(self._friendly_http_error(e)) from e
        except requests.exceptions.RequestException as e:
            raise ValueError(f"Ошибка при обращении к 1C: {e}") from e

        root = ElementTree.fromstring(response.content)
        sets: dict[str, str] = {}
        for elem in root.iter():
            tag = elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag
            if tag == "EntitySet":
                name = elem.attrib.get("Name") or elem.attrib.get("name")
                etype = elem.attrib.get("EntityType") or elem.attrib.get("entityType")
                if name and etype:
                    sets[name] = etype
        return sets

    def fetch_metadata_entity_properties(self) -> dict[str, dict[str, set[str]]]:
        """Fetch and parse OData $metadata to extract EntityType properties.

        Returns:
            dict: mapping of EntityType name to dict with 'properties' set
        """
        from xml.etree import ElementTree

        url = f"{self.base_url}/odata/standard.odata/$metadata"
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            raise ValueError(self._friendly_http_error(e)) from e
        except requests.exceptions.RequestException as e:
            raise ValueError(f"Ошибка при обращении к 1C: {e}") from e

        root = ElementTree.fromstring(response.content)
        result: dict[str, dict[str, set[str]]] = {}
        current_entity_type: str | None = None
        for elem in root.iter():
            tag = elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag
            if tag == "EntityType":
                current_entity_type = elem.attrib.get("Name") or elem.attrib.get("name")
                if current_entity_type:
                    result.setdefault(current_entity_type, {"properties": set()})
                continue
            if tag == "Property" and current_entity_type:
                prop_name = elem.attrib.get("Name") or elem.attrib.get("name")
                if prop_name:
                    result[current_entity_type]["properties"].add(prop_name)
        return result

    # ============================ ДАТЫ/ПЕРИОДЫ ===============================
    @staticmethod
    def _end_of_day(period_end: str) -> str:
        """
        Возвращает границу периода с последней секундой дня (23:59:59).

        1С OData интерпретирует голую дату (2026-01-31) как начало дня 00:00:00,
        что обрезает документы, проведённые позже. Если время уже указано —
        возвращаем значение без изменений.
        """

        if "T" in period_end:
            return period_end
        return f"{period_end}T23:59:59"

    @staticmethod
    def _start_of_day(period_start: str) -> str:
        """
        Возвращает границу периода с началом дня (00:00:00), если время не указано
        """

        if "T" in period_start:
            return period_start
        return f"{period_start}T00:00:00"
