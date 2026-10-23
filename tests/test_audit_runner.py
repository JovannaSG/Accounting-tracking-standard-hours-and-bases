# tests/test_audit_runner.py
"""Фоновый запуск аудита из UI: блокировка, статус, остановка.

Реальные процессы не поднимаем, кроме одного заведомо безопасного теста
остановки: дочерний процесс только держит flock и спит, сеть/1С не трогает.
"""
import fcntl
import os
import sys
import time

import pytest

from core import audit, audit_runner, config, db

_HELPER = (
    "import fcntl,os,sys,time\n"
    "fd=os.open(sys.argv[1], os.O_CREAT|os.O_RDWR)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "sys.stdout.write('locked\\n'); sys.stdout.flush()\n"
    "time.sleep(30)\n"
)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Изолированные lock/status/log рядом с тестом."""
    lock = str(tmp_path / ".audit.lock")
    status = str(tmp_path / ".audit.status.json")
    log = str(tmp_path / "audit_ui.log")
    monkeypatch.setattr(config, "AUDIT_LOCK_FILE", lock)
    monkeypatch.setattr(config, "AUDIT_STATUS_FILE", status)
    monkeypatch.setattr(config, "AUDIT_UI_LOG", log)
    return {"lock": lock, "status": status, "log": log}


def _hold_lock(path: str):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_is_running_false_without_lock_file(paths):
    assert audit_runner.is_running() is False


def test_is_running_true_when_lock_held(paths):
    fd = _hold_lock(paths["lock"])
    try:
        assert audit_runner.is_running() is True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert audit_runner.is_running() is False


def test_build_command_single_and_all():
    single = audit_runner._build_command(
        "single", "База Х", "2026-01-01", "2026-12-31", 0.3, True, "py"
    )
    assert single[:2] == ["py", "-m"]
    assert "--base" in single and "База Х" in single
    assert "--include-unmapped" in single
    assert "--pause" in single and "0.3" in single

    allb = audit_runner._build_command(
        "all", None, "2026-01-01", "2026-12-31", 0.2, False, "py"
    )
    assert "--all-bases" in allb
    assert "--base" not in allb


def test_launch_single_records_status(paths, monkeypatch):
    seen = {}

    class _Proc:
        pid = 4242

    def _fake_spawn(cmd, log_path, cwd):
        seen["cmd"] = cmd
        seen["log"] = log_path
        seen["cwd"] = cwd
        return _Proc()

    monkeypatch.setattr(audit_runner, "_spawn", _fake_spawn)

    info = audit_runner.launch(
        "single", "2026-01-01", "2026-12-31",
        base_filter="База Х", include_unmapped=True, wait=0,
    )
    assert info["pid"] == 4242
    assert info["scope"] == "single"
    assert info["base"] == "База Х"
    assert info["include_unmapped"] is True
    # статус записан на диск
    saved = audit_runner.read_status()
    assert saved["pid"] == 4242
    # команда собрана с базой и флагом
    assert "--base" in seen["cmd"] and "База Х" in seen["cmd"]
    assert seen["log"] == paths["log"]


def test_launch_all_uses_all_bases(paths, monkeypatch):
    monkeypatch.setattr(
        audit_runner, "_spawn",
        lambda cmd, log_path, cwd: type("P", (), {"pid": 1})(),
    )
    info = audit_runner.launch("all", "2026-01-01", "2026-01-31", wait=0)
    assert info["scope"] == "all"
    assert info["base"] is None


def test_launch_single_requires_base(paths):
    with pytest.raises(ValueError):
        audit_runner.launch("single", "2026-01-01", "2026-01-31")


def test_launch_refuses_when_running(paths):
    fd = _hold_lock(paths["lock"])
    try:
        with pytest.raises(audit.AuditBusyError):
            audit_runner.launch("all", "2026-01-01", "2026-01-31")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_status_clears_stale_and_reports_running(paths):
    audit_runner._write_status({"pid": 999, "scope": "all"})
    assert audit_runner.read_status()["pid"] == 999
    # блокировки нет → статус устарел и удаляется
    info = audit_runner.status()
    assert info["running"] is False
    assert audit_runner.read_status() == {}

    fd = _hold_lock(paths["lock"])
    try:
        assert audit_runner.status()["running"] is True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_stop_terminates_locked_child(paths):
    cmd = [sys.executable, "-c", _HELPER, paths["lock"]]
    proc = audit_runner._spawn(cmd, paths["log"], os.getcwd())
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not audit_runner.is_running():
            time.sleep(0.05)
        assert audit_runner.is_running() is True

        audit_runner._write_status({"pid": proc.pid, "scope": "all"})
        assert audit_runner.stop() is True

        proc.wait(timeout=5)
        deadline = time.time() + 5
        while time.time() < deadline and audit_runner.is_running():
            time.sleep(0.05)
        assert audit_runner.is_running() is False
        # Устаревший статус убирается при следующем опросе.
        assert audit_runner.status()["running"] is False
        assert audit_runner.read_status() == {}
    finally:
        if proc.poll() is None:
            proc.kill()


def test_stop_without_running_returns_false(paths):
    assert audit_runner.stop() is False


def test_read_log_tail(paths):
    with open(paths["log"], "w", encoding="utf-8") as f:
        f.write("one\ntwo\nthree\n")
    assert audit_runner.read_log_tail(max_lines=2) == "two\nthree\n"


def test_read_log_tail_skips_previous_run(paths):
    with open(paths["log"], "w", encoding="utf-8") as f:
        f.write("old run\n[145/145] Старая база\n")
    offset = os.path.getsize(paths["log"])
    with open(paths["log"], "a", encoding="utf-8") as f:
        f.write("[1/2] Новая база\n")
    assert audit_runner.read_log_tail(offset=offset) == "[1/2] Новая база\n"


def test_parse_batch_progress_uses_last_match():
    text = "[1/2] Старая\n[1/3] Первая\n[2/3] Вторая"
    assert audit_runner.parse_batch_progress(text) == (2, 3, "Вторая")
    assert audit_runner.parse_batch_progress("no progress") is None
    assert audit_runner.parse_batch_progress("[1/0] broken") is None


def _insert_run(base_url: str, status: str) -> int:
    conn = db._audit_conn()
    try:
        conn.execute(
            "INSERT INTO audit_runs (base_url, period_start, period_end, "
            "include_unmapped, status, created_at) "
            "VALUES (?, '2026-01-01', '2026-01-31', 0, ?, '2026-01-01T00:00:00')",
            (base_url, status),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()


def test_status_marks_stale_runs_failed(paths):
    stale_id = _insert_run("https://example/1", db.AUDIT_RUN_PENDING)
    done_id = _insert_run("https://example/2", db.AUDIT_RUN_COMPLETED)

    assert audit_runner.status()["running"] is False

    stale = db.get_audit_run(stale_id)
    assert stale["status"] == db.AUDIT_RUN_FAILED
    assert stale["finished_at"]
    done = db.get_audit_run(done_id)
    assert done["status"] == db.AUDIT_RUN_COMPLETED
    assert done["finished_at"] is None


def test_status_keeps_running_rows_while_lock_held(paths):
    run_id = _insert_run("https://example/3", db.AUDIT_RUN_PENDING)
    fd = _hold_lock(paths["lock"])
    try:
        assert audit_runner.status()["running"] is True
        assert db.get_audit_run(run_id)["status"] == db.AUDIT_RUN_PENDING
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
