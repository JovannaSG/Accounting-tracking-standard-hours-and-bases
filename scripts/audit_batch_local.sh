#!/usr/bin/env bash
# Пакетный глубокий аудит всех активных баз 1С:Фреш БЕЗ Docker — строго
# последовательно, прямо на хосте (приложение это Python + SQLite, Docker лишь
# способ доставки).
#
# Список баз берётся из БД приложения (таблица bases) через
# `python -m core.audit --list-bases`: это единственный источник истины,
# отдельного data/client_databases.json в проекте нет.
#
# Режим по умолчанию: полный год, без --include-unmapped (реестровые виды
# документов; тяжёлую сверку нереестровых Document_* запускают отдельным
# проходом). Базы выполняются по одной с паузой 0.2 c между запросами, чтобы
# не упереться в общий WAF/рейт-лимит 1С:Фреш по нашему IP.
#
# Запуск (переживает отключение SSH):
#     nohup ./scripts/audit_batch_local.sh &
#
# Переопределяемые переменные окружения (можно задать в .env):
#     AUDIT_BATCH_LOG    файл лога (по умолчанию norm_hours_audit.log)
#     AUDIT_BATCH_START  начало периода (по умолчанию 2026-01-01)
#     AUDIT_BATCH_END    конец периода  (по умолчанию 2026-12-31)
#     AUDIT_BATCH_PAUSE  пауза между запросами, сек (по умолчанию 0.2)
#     AUDIT_BATCH_SUMMARY  путь сводки по итогам (по умолчанию
#                          norm_hours_audit_summary.csv)
#
# Важно: переменные окружения (AUDIT_DB_PATH, AUDIT_HISTORY_DB) читаются
# приложением при импорте. Если .env существует — он подгружается ниже до
# первого запуска python.

set -u

cd "$(dirname "$0")/.." || exit 1

# .env не читается автоматически (python-dotenv не используется) — загружаем
# сами, чтобы AUDIT_DB_PATH/AUDIT_HISTORY_DB взялись из него.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# Виртуальное окружение: активируем, если есть, иначе берём python3.
if [ -f .venv/bin/activate ]; then
    # shellcheck disable=SC1091
    . .venv/bin/activate
fi
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python

LOGFILE="${AUDIT_BATCH_LOG:-norm_hours_audit.log}"
SUMMARY_FILE="${AUDIT_BATCH_SUMMARY:-norm_hours_audit_summary.csv}"
YEAR_START="${AUDIT_BATCH_START:-2026-01-01}"
YEAR_END="${AUDIT_BATCH_END:-2026-12-31}"
PAUSE="${AUDIT_BATCH_PAUSE:-0.2}"

# Весь вывод — в лог, чтобы tailить файл на хосте.
exec >>"$LOGFILE" 2>&1

echo "=================================================================="
echo "[$(date '+%F %T')] Старт пакетного аудита (host): $YEAR_START..$YEAR_END, pause=$PAUSE"

BASES="$("$PYTHON" -m core.audit --list-bases)"
if [ -z "$BASES" ]; then
    echo "[$(date '+%F %T')] Список баз пуст — выход."
    exit 1
fi

total=0
ok=0
failed=0

# Базы читаются парами «id<TAB>имя»; id — числовой ключ без проблем с
# кавычками/пробелами/кириллицей в аргументе --base (имена баз содержат
# пробелы, поэтому цикл `for base in $BASES` недопустим).
while IFS=$'\t' read -r base_id base_name; do
    [ -z "${base_id:-}" ] && continue
    total=$((total + 1))
    echo "------------------------------------------------------------------"
    echo "[$(date '+%F %T')] ($total) Аудит базы id=$base_id «$base_name»"

    if "$PYTHON" -m core.audit \
            --base "$base_id" \
            --start "$YEAR_START" \
            --end "$YEAR_END" \
            --pause "$PAUSE"; then
        ok=$((ok + 1))
    else
        code=$?
        failed=$((failed + 1))
        echo "[!] База id=$base_id «$base_name» завершилась с кодом $code — пропускаю"
    fi
done <<< "$BASES"

echo "=================================================================="
echo "[$(date '+%F %T')] Готово: всего $total, успешно $ok, с ошибкой $failed"

# Итоговая сводка (та же БД истории, без сети) — в лог и отдельным файлом.
echo "------------------------------------------------------------------"
echo "[$(date '+%F %T')] Сводка по базам с проблемами:"
"$PYTHON" -m core.audit --summary --problems-only || true
"$PYTHON" -m core.audit --summary --out "$SUMMARY_FILE" || true
