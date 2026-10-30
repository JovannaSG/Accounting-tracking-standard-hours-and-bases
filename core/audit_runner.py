"""
Запуск и контроль фонового аудита из Streamlit без блокировки цикла UI.

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
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

from core import audit, config, db

SCOPES: tuple = ("all", "single")


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _lock_file(path: str | None = None) -> str:
    return path or config.AUDIT_LOCK_FILE


def _status_file(path: str | None = None) -> str:
    return path or config.AUDIT_STATUS_FILE


def is_running(lock_file: str | None = None) -> bool:
    """
    True, если блокировку аудита удерживает живой процесс.

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
    """
    Сырой статус последнего запуска из UI ({} если файла нет/битый)
    """

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


def status(
    status_file: str | None = None,
    lock_file: str | None = None
) -> dict:
    """
    Сводный статус для UI: ``running`` + детали запуска (если известны)
    """

    running = is_running(lock_file)
    info = read_status(status_file)
    if not running and info:
        # Запуск завершился, но статус остался — не показываем как актуальный.
        clear_status(status_file)
        info = {}
    if not running:
        # Блокировка свободна → процессы аудита не идут, а оставшиеся
        # RUNNING в БД появились из-за падения/перезапуска контейнера.
        try:
            db.cleanup_stale_runs()
        except (OSError, sqlite3.Error):
            pass
    info = dict(info)
    info["running"] = running
    return info


def _build_command(
    scope: str,
    base_filter: str | None,
    start: str,
    end: str,
    pause: float,
    include_unmapped: bool,
    python: str | None = None,
    chunk_mode: str = "month",
) -> list[str]:
    cmd: list[str] = [
        python or sys.executable, "-m", "core.audit",
        "--start", start, "--end", end, "--pause", str(pause),
        "--chunk-mode", str(chunk_mode or "month"),
    ]
    if scope == "all":
        cmd.append("--all-bases")
    else:
        cmd += ["--base", str(base_filter)]
    if include_unmapped:
        cmd.append("--include-unmapped")
    return cmd


def _spawn(cmd: list[str], log_path: str, cwd: str):
    """
    Отсоединённый процесс (своя сессия) с логом в файл.

    ``start_new_session`` создаёт новую группу процессов — по ней кнопка
    «Остановить» шлёт SIGTERM, не задевая Streamlit.
    """

    env = os.environ.copy()
    # stdout перенаправлен в файл и иначе буферизуется блоками: строки
    # прогресса «[i/N]» не доходили бы до UI до заполнения буфера.
    env["PYTHONUNBUFFERED"] = "1"
    log = open(log_path, "a", encoding="utf-8")
    try:
        return subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log.close()


def launch(
    scope: str,
    start: str,
    end: str,
    base_filter: str | None = None,
    include_unmapped: bool = False,
    pause: float = 0.2,
    python: str | None = None,
    wait: float = 1.0,
    chunk_mode: str = "month"
) -> dict:
    """
    Запускает фоновый аудит; возвращает записанный статус.

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

    # Сбрасываем старый флаг остановки перед новым запуском
    flag_path = _lock_file() + ".stop"
    if os.path.exists(flag_path):
        try:
            os.remove(flag_path)
        except OSError:
            pass

    cmd = _build_command(
        scope,
        base_filter, start, end,
        pause, include_unmapped,
        python,
        chunk_mode
    )
    log_path = config.AUDIT_UI_LOG
    os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)
    # Лог общий и дописывается между запусками: запоминаем смещение, чтобы
    # прогресс не показывал последнюю строку предыдущего прогона.
    try:
        log_offset = os.path.getsize(log_path)
    except OSError:
        log_offset = 0

    proc = _spawn(cmd, log_path, _project_root())
    deadline = time.monotonic() + max(wait, 0.0)
    while time.monotonic() < deadline:
        if is_running():
            break
        poll = getattr(proc, "poll", None)
        if poll is not None and poll() is not None:
            break
        time.sleep(0.05)

    info: dict = {
        "pid": proc.pid,
        "scope": scope,
        "base": base_filter,
        "start": start,
        "end": end,
        "include_unmapped": bool(include_unmapped),
        "pause": pause,
        "chunk_mode": chunk_mode,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "log": log_path,
        "log_offset": log_offset,
    }
    _write_status(info)
    return info


def stop(
    status_file: str | None = None,
    lock_file: str | None = None
) -> bool:
    """
    SIGTERM группе процесса аудита. True — сигнал отправлен.
    """
    if not is_running(lock_file):
        return False

    flag_path = _lock_file(lock_file) + ".stop"
    try:
        with open(flag_path, "w", encoding="utf-8") as f:
            f.write("stop")
    except OSError:
        pass

    pid = read_status(status_file).get("pid")
    if pid:
        try:
            import signal
            os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            pass
    return True


def read_log_tail(max_lines: int = 40, offset: int = 0) -> str:
    """
    Последние строки лога фонового аудита (для панели UI).

    ``offset`` — байтовое смещение начала текущего прогона: так прогресс не
    смешивается с выводом предыдущих запусков. Если файл короче смещения
    (лог пересоздали), читаем с начала.
    """

    try:
        size = os.path.getsize(config.AUDIT_UI_LOG)
        with open(config.AUDIT_UI_LOG, "rb") as f:
            if 0 < offset <= size:
                f.seek(offset)
            data = f.read()
    except OSError:
        return ""
    lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
    return "".join(lines[-max_lines:])


def parse_batch_progress(text: str) -> tuple[int, int, str] | None:
    """
    Последняя строка прогресса пакетного аудита ``[i/N] имя базы``.

    Берём именно последнее совпадение: лог дописывается, и в хвосте могут
    остаться строки предыдущих баз/прогонов.
    """

    matches = re.findall(r"\[(\d+)/(\d+)\]\s*(.*)", text or "")
    if not matches:
        return None
    current, total, name = matches[-1]
    try:
        current_i = int(current)
        total_i = int(total)
    except ValueError:
        return None
    if total_i <= 0:
        return None
    return current_i, total_i, name.strip()
