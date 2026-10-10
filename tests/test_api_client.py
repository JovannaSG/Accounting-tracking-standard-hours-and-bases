# tests/test_api_client.py
"""Phase 1 Deep Data Audit: транспорт OData (ReTry, nextLink/$skip, генератор).

Проверяем только транспорт: страницы, паузы, фолбэк на минимальный $select,
деградацию и повторы. Реальную 1С не трогаем — фейковый session.get + локальный
http-стаб для проверки urllib3.Retry.
"""
import http.server
import threading

import pytest
import requests

from core.api_client import OneCClient


class _FakeResponse:
    """Имитация requests.Response: только raise_for_status() и json()."""

    def __init__(self, payload=None, status=200, url=""):
        self._payload = payload
        self._status = status
        self._url = url

    def raise_for_status(self):
        if self._status >= 400:
            response = requests.Response()
            response.status_code = self._status
            response.url = self._url
            raise requests.exceptions.HTTPError(response=response)

    def json(self):
        return self._payload


def _client() -> OneCClient:
    return OneCClient("https://1cfresh.example", "login", "password")


# ============================= RETRY CONFIG ==============================

def test_retry_policy_mounted():
    """Кастомный urllib3.Retry смонтирован на адаптеры обоих схем."""
    client = _client()
    for scheme in ("http://", "https://"):
        retry = client.session.adapters[scheme].max_retries
        assert retry is not None
        assert retry.total == 5
        assert retry.connect == 5
        assert retry.read == 5
        assert retry.status == 5
        assert retry.backoff_factor == 1.0
        assert set(retry.status_forcelist) == {429, 500, 502, 503, 504}
        assert set(retry.allowed_methods) == {"GET"}
    assert client._degraded_entities == set()


# ========================= ПАГИНАЦИЯ (nextLink / $skip) =================

def test_paginate_follows_nextlink():
    """Страницы продолжаются по @odata.nextLink, params больше не передаются."""
    client = _client()
    seen = []

    def fake_get(url, params=None, timeout=None):
        seen.append((url, dict(params or {})))
        if url == "E":
            return _FakeResponse(
                {"value": [{"Ref_Key": "a"}], "@odata.nextLink": "NL:skip1"},
                200,
                url,
            )
        if url == "NL:skip1":
            return _FakeResponse({"value": [{"Ref_Key": "b"}]}, 200, url)
        raise AssertionError(f"неожиданный endpoint: {url}")

    client.session.get = fake_get
    rows = client._paginate("E", {"$top": 2})

    assert rows == [{"Ref_Key": "a"}, {"Ref_Key": "b"}]
    assert seen == [
        ("E", {"$top": 2}),
        ("NL:skip1", {}),
    ]


def test_paginate_uses_skip_until_short_page():
    """Без nextLink работает инкрементальный $skip; короткая страница — стоп."""
    client = _client()
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        skip = (params or {}).get("$skip", 0)
        if skip == 0:
            return _FakeResponse(
                {"value": [{"Ref_Key": "a"}, {"Ref_Key": "b"}]}, 200, url
            )
        if skip == 2:
            return _FakeResponse({"value": [{"Ref_Key": "c"}]}, 200, url)
        raise AssertionError(f"неожиданный $skip: {skip}")

    client.session.get = fake_get
    rows = client._paginate("E", {"$top": 2})

    assert rows == [{"Ref_Key": "a"}, {"Ref_Key": "b"}, {"Ref_Key": "c"}]
    assert calls["n"] == 2


def test_paginate_stops_on_empty_page():
    client = _client()
    client.session.get = lambda *a, **k: _FakeResponse({"value": []}, 200, "")
    assert client._paginate("E", {"$top": 1000}) == []


def test_paginate_paces_between_requests(monkeypatch):
    """Пауза применяется МЕЖДУ запросами, а не перед первым."""
    client = _client()
    slept = []
    monkeypatch.setattr("core.api_client.time.sleep", lambda s: slept.append(s))

    def fake_get(url, params=None, timeout=None):
        skip = (params or {}).get("$skip", 0)
        if skip == 0:
            return _FakeResponse({"value": [{"Ref_Key": "a"}]}, 200, url)
        return _FakeResponse({"value": []}, 200, url)

    client.session.get = fake_get
    list(client._paginate_pages("E", {"$top": 1}, pause=0.2))

    assert slept == [0.2]


# ============================ ЗАПРОС ДОКУМЕНТОВ ==========================

def test_documents_request_url_and_filter():
    """URL и фильтр собираются как раньше (quote против 500 из-за '+' в фильтре)."""
    from urllib.parse import parse_qs, unquote, urlparse

    client = _client()
    url, params = client._documents_request(
        "Document_ОперацияБух", "2026-01-01", "2026-01-31",
        ["Ref_Key", "Date"], 1000,
    )
    parsed = parse_qs(urlparse(url).query)
    assert parsed["$select"][0] == "Ref_Key,Date"
    assert unquote(parsed["$filter"][0]) == (
        "Date ge datetime'2026-01-01T00:00:00' "
        "and Date le datetime'2026-01-31T23:59:59'"
    )
    assert parsed["$format"] == ["json"]
    assert parsed["$top"] == ["1000"]
    assert params == {"$skip": 0}


def test_fetch_documents_iter_degrades_400_to_minimal_select(monkeypatch):
    """400 на первом запросе -> повтор минимальным $select + флаг деградации."""
    monkeypatch.setattr(
        "core.api_client._load_schema",
        lambda: {"entities": {}},
    )
    client = _client()
    seen_selects = []

    def fake_get(url, params=None, timeout=None):
        from urllib.parse import parse_qs, urlparse

        fields = parse_qs(urlparse(url).query)["$select"][0].split(",")
        seen_selects.append(fields)
        if len(fields) > 3:
            return _FakeResponse(None, 400, url)
        return _FakeResponse(
            {"value": [{"Ref_Key": "mb1"}, {"Ref_Key": "mb2"}]}, 200, url
        )

    client.session.get = fake_get
    rows = list(client.fetch_documents_iter(
        "Document_НеизвестнаяСущность", "2026-01-01", "2026-01-31"
    ))

    assert len(seen_selects) == 2
    assert len(seen_selects[0]) > 3
    assert seen_selects[1] == client._COMMON_FIELDS[:3]
    assert rows == [{"Ref_Key": "mb1"}, {"Ref_Key": "mb2"}]
    assert client._degraded_entities == {"Document_НеизвестнаяСущность"}


def test_fetch_documents_iter_mid_stream_error_propagates_without_fallback():
    """Ошибка ПОСЛЕ первого успешного запроса не вызывает фолбэк (нет дублей)."""
    client = _client()
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse({"value": [{"Ref_Key": "a"}]}, 200, url)
        return _FakeResponse(None, 400, url)

    client.session.get = fake_get
    with pytest.raises(ValueError) as exc:
        list(client.fetch_documents_iter(
            "Document_ОперацияБух", "2026-01-01", "2026-01-31",
            select=["Ref_Key"], page_size=1,
        ))
    assert "400" in str(exc.value)
    assert client._degraded_entities == set()


def test_fetch_documents_legacy_wrapper_matches_iter():
    """fetch_documents (список) = list(fetch_documents_iter), деградации нет."""
    client = _client()

    def fake_get(url, params=None, timeout=None):
        return _FakeResponse(
            {"value": [{"Ref_Key": "a"}, {"Ref_Key": "b"}]}, 200, url
        )

    client.session.get = fake_get
    as_list = client.fetch_documents("Document_X", "2026-01-01", "2026-01-31")
    as_iter = list(client.fetch_documents_iter(
        "Document_X", "2026-01-01", "2026-01-31"
    ))
    assert as_list == as_iter == [{"Ref_Key": "a"}, {"Ref_Key": "b"}]
    assert client._degraded_entities == set()


# ============================ RETRY НА РЕАЛЬНОМ СТАБЕ ===================

class _StubHandler(http.server.BaseHTTPRequestHandler):
    server_version = "audit-stub"
    statuses: list[int] = []
    counter = 0
    lock = threading.Lock()
    body = b'{"value": []}'

    def do_GET(self):  # noqa: N802 - имя протокола http.server
        # self.counter += 1 создал бы атрибут экземпляра; обработчик создаётся
        # на каждый запрос (ThreadingHTTPServer), поэтому счётчик держим на классе.
        with self.lock:
            status = _StubHandler.statuses[
                min(_StubHandler.counter, len(_StubHandler.statuses) - 1)
            ]
            _StubHandler.counter += 1
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def stub_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_retry_transient_500_then_success(stub_server, monkeypatch):
    """500 и 502 до первого успеха: повторы отрабатывают, данные приходят."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    _StubHandler.statuses = [500, 502, 200]
    _StubHandler.counter = 0
    _StubHandler.body = b'{"value": [{"Ref_Key": "ok"}]}'

    client = OneCClient(
        f"http://127.0.0.1:{stub_server.server_address[1]}", "u", "p"
    )
    endpoint = f"{client.base_url}/odata/standard.odata/E"
    rows = client._paginate(endpoint, {"$top": 1000})

    assert rows == [{"Ref_Key": "ok"}]
    assert _StubHandler.counter == 3


def test_retry_exhaustion_raises_friendly_error(stub_server, monkeypatch):
    """5 повторов 503 исчерпали Retry -> дружелюбный ValueError, без размотки стека."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    _StubHandler.statuses = [503] * 7
    _StubHandler.counter = 0
    _StubHandler.body = b'{"value": []}'

    from core.api_client import _DEFAULT_RETRY_TOTAL

    client = OneCClient(
        f"http://127.0.0.1:{stub_server.server_address[1]}", "u", "p"
    )
    endpoint = f"{client.base_url}/odata/standard.odata/E"
    with pytest.raises(ValueError) as exc:
        client._paginate(endpoint, {"$top": 1000})
    assert f"не ответила после {_DEFAULT_RETRY_TOTAL} повторов" in str(exc.value)
    assert _StubHandler.counter == _DEFAULT_RETRY_TOTAL + 1


def test_retry_exhaustion_no_side_effect_on_degraded_entities(stub_server, monkeypatch):
    """Исчерпание Retry не трогает флаг деградации (на 400-фолбэк не влияет)."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    _StubHandler.statuses = [500] * 7
    _StubHandler.counter = 0
    _StubHandler.body = b'{"value": []}'

    client = OneCClient(
        f"http://127.0.0.1:{stub_server.server_address[1]}", "u", "p"
    )
    endpoint = f"{client.base_url}/odata/standard.odata/E"
    with pytest.raises(ValueError):
        client._paginate(endpoint, {"$top": 1000})
    assert client._degraded_entities == set()


def test_retry_respects_metadata_fetch(stub_server, monkeypatch):
    """$metadata-запросы тоже ходят через сессию с Retry (429 повторён)."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    _StubHandler.statuses = [429, 200]
    _StubHandler.counter = 0
    _StubHandler.body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="1.0">'
        '<edmx:DataServices>'
        '<Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm" Namespace="StandardODATA">'
        '<EntityContainer Name="StandardOData">'
        '<EntitySet Name="Document_ОперацияБух" EntityType="StandardODATA.Document_ОперацияБух"/>'
        '</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>'
    ).encode("utf-8")

    client = OneCClient(
        f"http://127.0.0.1:{stub_server.server_address[1]}", "u", "p"
    )
    sets = client.fetch_metadata_entity_sets()

    assert sets == {
        "Document_ОперацияБух": "StandardODATA.Document_ОперацияБух"
    }
    assert _StubHandler.counter == 2