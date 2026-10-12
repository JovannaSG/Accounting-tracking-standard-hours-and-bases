#!/usr/bin/env bash
# Пакетный глубокий аудит всех активных баз 1С:Фреш — строго последовательно.
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
# Запуск вручную (переживает отключение SSH):
#     nohup ./scripts/audit_batch.sh &
#
# Пример записи в crontab (ежедневно в 02:00):
#     0 2 * * * cd /opt/accounting && ./scripts/audit_batch.sh
#
# Переопределяемые переменные окружения:
#     AUDIT_BATCH_LOG    файл лога (по умолчанию /var/log/norm_hours_audit.log)
#     AUDIT_BATCH_START  начало периода (по умолчанию 2026-01-01)
#     AUDIT_BATCH_END    конец периода  (по умолчанию 2026-12-31)
#     AUDIT_BATCH_PAUSE  пауза между запросами, сек (по умолчанию 0.2)
#     AUDIT_BATCH_SUMMARY  путь сводки внутри контейнера (по умолчанию
#                          /data/exports/norm_hours_audit_summary.csv)
#
# Отчёты пишутся внутри контейнера в /data/exports (том norm-hours-data).

set -u

# cron запускает с урезанным PATH — задаём типовой, чтобы найти docker.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

# Работаем из корня проекта (рядом с docker-compose.yml) независимо от cwd.
cd "$(dirname "$0")/.." || exit 1

LOGFILE="${AUDIT_BATCH_LOG:-/var/log/norm_hours_audit.log}"
SUMMARY_FILE="${AUDIT_BATCH_SUMMARY:-/data/exports/norm_hours_audit_summary.csv}"
YEAR_START="${AUDIT_BATCH_START:-2026-01-01}"
YEAR_END="${AUDIT_BATCH_END:-2026-12-31}"
PAUSE="${AUDIT_BATCH_PAUSE:-0.2}"
COMPOSE=(docker compose)
STAMP="$(date '+%F %T')"

# Весь вывод — в лог (stdout и stderr), чтобы tailить файл на хосте.
exec >>"$LOGFILE" 2>&1

echo "=================================================================="
echo "[$STAMP] Старт пакетного аудита: $YEAR_START..$YEAR_END, pause=$PAUSE"

BASES="$("${COMPOSE[@]}" exec -T norm-hours python -m core.audit --list-bases)"
if [ -z "$BASES" ]; then
    echo "[$(date '+%F %T')] Список баз пуст или контейнер недоступен — выход."
    exit 1
fi

total=0
ok=0
failed=0

# Базы читаются парами «id<TAB>имя»; id — числовой ключ без проблем с
# кавычками/пробелами/кириллицей в аргументе --base.
while IFS=$'\t' read -r base_id base_name; do
    [ -z "${base_id:-}" ] && continue
    total=$((total + 1))
    echo "------------------------------------------------------------------"
    echo "[$(date '+%F %T')] ($total) Аудит базы id=$base_id «$base_name»"

    if "${COMPOSE[@]}" exec -T norm-hours python -m core.audit \
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

# Итоговая сводка (та же БД истории, без сети) — в лог и отдельным файлом
# внутри тома /data (переживает пересоздание контейнера).
echo "------------------------------------------------------------------"
echo "[$(date '+%F %T')] Сводка по базам с проблемами:"
"${COMPOSE[@]}" exec -T norm-hours python -m core.audit --summary --problems-only || true
"${COMPOSE[@]}" exec -T norm-hours python -m core.audit --summary --out "$SUMMARY_FILE" || true
