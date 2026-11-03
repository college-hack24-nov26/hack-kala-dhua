# Indradhanu API.
#
# Python 3.12 because the codebase uses StrEnum and datetime.UTC, both 3.11+.
# Slim rather than alpine: ortools and asyncpg ship manylinux wheels, so glibc
# means no compiler in the image and a build that takes seconds.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so a code change does not reinstall ortools.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# Live road-risk scoring: the feature code and the serving bundle (trained model,
# per-segment features, river rating inputs). Refresh with
# `python -m ml.export_serving` after a new champion is registered.
COPY ml ./ml
COPY serving ./serving
ENV DISRUPTIONOPS_DATA=/app/serving/data \
    DISRUPTIONOPS_MODELS=/app/serving/models \
    LIVE_RISK_TTL_S=300

# Never run as root.
RUN useradd --create-home --uid 10001 indradhanu \
    && chown -R indradhanu:indradhanu /app
USER indradhanu

# Render detects whichever port the service listens on, so a fixed port is
# correct there and needs no $PORT indirection.
EXPOSE 8000

# Liveness only: readiness pulls the database and would fail the container on
# a transient database blip.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status==200 else 1)"

# One worker per container by default; scale with replicas, not with in-process
# workers, so each has its own connection pool against the Supabase pooler's
# limit. WEB_CONCURRENCY raises it on a bigger box (one per core).
#
# uvloop + httptools explicitly: they are what uvicorn[standard] installs, and
# naming them means a missing wheel fails the build instead of silently halving
# throughput. Backlog 4096 so a burst queues in the kernel instead of refusing.
#
# --proxy-headers because Render terminates TLS at its edge: without it every
# request looks like plain http from a private address, and anything deriving a
# URL or a client IP gets it wrong.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-1} --loop uvloop --http httptools --backlog 4096 --timeout-keep-alive 30 --proxy-headers --forwarded-allow-ips '*' --no-access-log"]
