import os
import socket
import sys


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _port_in_use(host: str, port: int) -> bool:
    """
    True, если TCP-порт уже занят
    """

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.bind((host, port))
            return False
    except OSError:
        return True


def main():
    os.chdir(_PROJECT_ROOT)

    host = os.environ.get("UI_HOST", "127.0.0.1")
    port = int(os.environ.get("UI_PORT", "8503"))

    if _port_in_use(host, port):
        print(
            f"Приложение, похоже, уже запущено: порт {host}:{port} занят. "
            "Сначала остановите существующий экземпляр (Ctrl+C в его консоли)."
        )
        sys.exit(1)

    print(f"Запуск Учёта нормочасов 1С: http://{host}:{port}")
    print("Для завершения нажмите Ctrl+C\n")

    import streamlit.web.cli
    sys.argv = [
        "streamlit", "run", os.path.join("app", "ui.py"),
        "--server.port", str(port),
        "--server.address", host,
    ]
    streamlit.web.cli.main()


if __name__ == "__main__":
    main()
