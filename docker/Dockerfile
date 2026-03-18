FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/agent-code

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN mkdir -p /app/agent-code

# Copy only minimal runtime package + entrypoints (no CLI/build code)
COPY src/dank_runtime /app/dank_runtime
COPY docker/entrypoint.py /app/entrypoint.py
COPY docker/default_index.py /app/default_index.py
COPY docker/requirements-runtime.txt /tmp/requirements-runtime.txt

# Runtime dependencies for FastAPI + typed validation (pinned)
RUN pip install --no-cache-dir -r /tmp/requirements-runtime.txt && \
    rm -f /tmp/requirements-runtime.txt

# Non-root runtime user
RUN groupadd -g 1001 dankuser && \
    useradd -u 1001 -g 1001 -m -s /usr/sbin/nologin dankuser && \
    chown -R dankuser:dankuser /app

USER dankuser

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:3000/health || exit 1

ENTRYPOINT ["python", "/app/entrypoint.py"]
