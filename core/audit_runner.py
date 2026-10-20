# core/audit_runner.py
"""Запуск и контроль фонового аудита из Streamlit без блокировки цикла UI.

Тяжёлый сетевой обход 1С не выполняется в процессе Streamlit: UI лишь
порождает отсоединённый дочерний процесс ``python -m core.audit`` и читает
его состояние. Живость определяется эксклюзивной блокировкой
(``fcntl.flock`` на ``config.AUDIT_LOCK_FILE``), а не только PID-файлом:
устаревший файл с мёртвым PID не считается «выполняется», а саму блокировку
атомарно удерживает ровно один процесс (UI или cron).
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

from core import audit, config

SCOPES = ("all", "single")


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _lock_file(path: str | None = None) -> str:
    return path or config.AUDIT_LOCK_FILE


def _status_file(path: str | None = None) -> str:
    return path or config.AUDIT_STATUS_FILE


def is_running(lock_file: str | None = None) -> bool:
    """True, если блокировку аудита удерживает живой процесс.

    Проверяется именно блокировка, а не существование файла: flock снимается
    ОС при смерти процесса, поэтому зависший PID-файл не мешает запуску.
    """

    path = _lock_file(lock_file)
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def read_status(status_file: str | None = None) -> dict:
    """Сырой статус последнего запуска из UI ({} если файла нет/битый)."""

    try:
        with open(_status_file(status_file), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_status(info: dict, status_file: str | None = None) -> None:
    path = _status_file(status_file)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False)
    os.replace(tmp, path)


def clear_status(status_file: str | None = None) -> None:
    try:
        os.remove(_status_file(status_file))
    except OSError:
        pass


def status(status_file: str | None = None,
           lock_file: str | None = None) -> dict:
    """Сводный статус для UI: ``running`` + детали запуска (если известны)."""

    running = is_running(lock_file)
    info = read_status(status_file)
    if not running and info:
        # Запуск завершился, но статус остался — не показываем как актуальный.
        clear_status(status_file)
        info = {}
    info = dict(info)
    info["running"] = running
    return info


def _build_command(scope: str, base_filter: str | None, start: str, end: str,
                   pause: float, include_unmapped: bool,
                   python: str | None = None) -> list[str]:
    cmd = [
        python or sys.executable, "-m", "core.audit",
        "--start", start, "--end", end, "--pause", str(pause),
    ]
    if scope == "all":
        cmd.append("--all-bases")
    else:
        cmd += ["--base", str(base_filter)]
    if include_unmapped:
        cmd.append("--include-unmapped")
    return cmd


def _spawn(cmd: list[str], log_path: str, cwd: str):
    """Отсоединённый процесс (своя сессия) с логом в файл.

    ``start_new_session`` создаёт новую группу процессов — по ней кнопка
    «Остановить» шлёт SIGTERM, не задевая Streamlit.
    """

    log = open(log_path, "a", encoding="utf-8")
    try:
        return subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log.close()


def launch(scope: str, start: str, end: str, base_filter: str | None = None,
           include_unmapped: bool = False, pause: float = 0.2,
           python: str | None = None, wait: float = 1.0) -> dict:
    """Запускает фоновый аудит; возвращает записанный статус.

    ``scope='all'`` — все активные базы, ``scope='single'`` — одна база
    (``base_filter``: имя/URL/id). Если аудит уже выполняется — ValueError.
    ``wait`` — недолгое ожидание, пока процесс возьмёт блокировку (чтобы UI
    сразу показал «выполняется», а не сработал на гонке старта).
    """

    if scope not in SCOPES:
        raise ValueError(f"Неизвестная область аудита: {scope!r}")
    if scope == "single" and not base_filter:
        raise ValueError("Не указана база для одиночного аудита")
    if is_running():
        raise audit.AuditBusyError("Аудит уже выполняется")

    cmd = _build_command(scope, base_filter, start, end, pause, include_unmapped,
                         python)
    log_path = config.AUDIT_UI_LOG
    os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)

    proc = _spawn(cmd, log_path, _project_root())
    deadline = time.monotonic() + max(wait, 0.0)
    while time.monotonic() < deadline:
        if is_running():
            break
        poll = getattr(proc, "poll", None)
        if poll is not None and poll() is not None:
            break
        time.sleep(0.05)
    info = {
        "pid": proc.pid,
        "scope": scope,
        "base": base_filter,
        "start": start,
        "end": end,
        "include_unmapped": bool(include_unmapped),
        "pause": pause,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "log": log_path,
    }
    _write_status(info)
    return info


def stop(status_file: str | None = None, lock_file: str | None = None) -> bool:
    """SIGTERM группе процесса аудита. True — сигнал отправлен.

    Защита от переиспользования PID: сигнал шлётся только если блокировка
    ещё удерживается (аудит действительно идёт).
    """

    if not is_running(lock_file):
        clear_status(status_file)
        return False
    pid = read_status(status_file).get("pid")
    if not pid:
        return False
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, ValueError, OSError):
        return False
    return True


def read_log_tail(max_lines: int = 40) -> str:
    """Последние строки лога фонового аудита (для панели UI)."""

    try:
        with open(config.AUDIT_UI_LOG, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return ""
    return "".join(lines[-max_lines:])
