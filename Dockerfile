FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src /app/src
RUN pip install --upgrade pip && pip install .

RUN mkdir -p /data/output && chown -R 10001:10001 /data
USER 10001:10001

EXPOSE 8000
CMD ["sh", "-c", "uvicorn localaimcp.server:app --host ${MCP_HOST:-0.0.0.0} --port ${MCP_PORT:-8000} --workers ${MCP_WORKERS:-2}"]
