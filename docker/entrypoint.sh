#!/bin/sh
# Fail-fast для контейнера «Учёт нормочасов».
#
# Приложение не стартует, пока в томе нет корректного файла пользователей:
# иначе аутентификация была бы отключена, а порт доступен по сети.
# JSON проверяем штатным Python из образа (jq в python:3.12-slim нет).
set -e

CONFIG_PATH="${AUDIT_USERS_CONFIG:-/data/users.json}"

if [ ! -f "$CONFIG_PATH" ]; then
    echo "ОШИБКА: не найден файл пользователей: $CONFIG_PATH" >&2
    echo "" >&2
    echo "Создайте его на хосте и положите в том:" >&2
    echo "  python -m core.auth hash \"<пароль>\"   # в корне проекта" >&2
    echo "  docker cp users.json norm-hours:/data/users.json" >&2
    echo "  docker compose start norm-hours" >&2
    echo "" >&2
    echo "Подробности: руководство, раздел «Первый запуск»." >&2
    exit 1
fi

if ! python - "$CONFIG_PATH" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    # utf-8-sig, а не utf-8: PowerShell 5.1 и «Блокнот» на Windows пишут
    # BOM, и обычный utf-8 такой файл отвергает.
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
except ValueError as e:
    print(f"ОШИБКА: {path} не является корректным JSON: {e}", file=sys.stderr)
    raise SystemExit(1)
except OSError as e:
    print(f"ОШИБКА: не удалось прочитать {path}: {e}", file=sys.stderr)
    raise SystemExit(1)

if not isinstance(data, dict) or not data:
    print(
        f"ОШИБКА: {path} пуст или не содержит пользователей — "
        "аутентификация была бы отключена",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
then
    exit 1
fi

exec "$@"
