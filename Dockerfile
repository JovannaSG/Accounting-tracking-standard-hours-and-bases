# Учёт нормочасов (1С:Фреш) — контейнер Streamlit
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    AUDIT_DB_PATH=/data/norm_hours.db \
    AUDIT_USERS_CONFIG=/data/users.json \
    UI_HOST=0.0.0.0 \
    UI_PORT=8502

WORKDIR /app

# Сначала зависимости — слой кэшируется, пока requirements.txt не менялся
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY core ./core

# Рабочие данные (БД + конфиг пользователей) хранятся в /data.
# Это каталог тома docker-compose: при первом запуске в него копируется
# содержимое из образа, поэтому кладём сюда стартовый конфиг.
RUN mkdir -p /data && cp users.example.json /data/users.json

EXPOSE 8502

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8502/_stcore/health', timeout=3)" || exit 1

CMD ["python", "app/run.py"]