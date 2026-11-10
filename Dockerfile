# NanoMemory API image (spec §17, §22.22).
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Non-root user (fixed uid/gid so host volume permissions are predictable).
RUN groupadd --gid 10001 nanomemory \
    && useradd --uid 10001 --gid nanomemory --create-home --shell /usr/sbin/nologin nanomemory

COPY pyproject.toml README.md LICENSE ./
COPY nanomemory ./nanomemory
COPY scripts ./scripts

RUN pip install . \
    && mkdir -p /data \
    && chown -R nanomemory:nanomemory /app /data

USER nanomemory

ENV NANOMEMORY_STORAGE_ROOT=/data \
    NANOMEMORY_ENVIRONMENT=production \
    NANOMEMORY_PROVIDER=mock \
    NANOMEMORY_LOG_FORMAT=json \
    NANOMEMORY_WORKER_COUNT=1

VOLUME ["/data"]
EXPOSE 8000

# The slim image has no curl; the probe uses the standard library only.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "scripts/health_probe.py", "--url", "http://127.0.0.1:8000/health"]

CMD ["python", "-m", "uvicorn", "nanomemory.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
