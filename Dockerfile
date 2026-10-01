# Учёт нормочасов (1С:Фреш) — контейнер Streamlit
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    AUDIT_DB_PATH=/data/norm_hours.db \
    AUDIT_USERS_CONFIG=/data/users.json \
    UI_HOST=0.0.0.0 \
    UI_PORT=8503

WORKDIR /app

# Сначала зависимости — слой кэшируется, пока requirements.txt не менялся
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY core ./core
COPY docker/entrypoint.sh /usr/local/bin/audit-entrypoint.sh
RUN chmod +x /usr/local/bin/audit-entrypoint.sh

# Рабочие данные (БД + конфиг пользователей) лежат в томе на /data.
# Конфиг пользователей НЕ создаётся автоматически: entrypoint завершает
# контейнер, если /data/users.json отсутствует, нечитаем или пуст — иначе
# приложение стартовало бы без аутентификации в доступной по сети порту.
# См. «Первый запуск» в руководстве: docker cp users.json norm-hours:/data/users.json
RUN mkdir -p /data

EXPOSE 8503

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8503/_stcore/health', timeout=3)" || exit 1

CMD ["/usr/local/bin/audit-entrypoint.sh", "python", "app/run.py"]