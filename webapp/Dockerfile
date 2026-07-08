# Wasabi 管理 WebApp（リポジトリルートから: docker build -f webapp/Dockerfile .）
FROM python:3.12-slim AS base
WORKDIR /app

COPY webapp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# src/（smartsync_client, team_config, backlog_client）と webapp 本体
COPY src/ /app/src/
COPY webapp/app/ /app/app/

ENV PYTHONPATH=/app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
