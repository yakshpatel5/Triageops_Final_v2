# TriageOps — production Dockerfile
# Multi-stage: builder installs Python deps; runtime adds Node build + app

# ---------------------------------------------------------------------------
# Stage 1: Python dependency builder
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ---------------------------------------------------------------------------
# Stage 2: Runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

RUN groupadd -g 1001 appgroup && useradd -u 1001 -g appgroup -s /bin/sh appuser
WORKDIR /app
COPY --chown=appuser:appgroup . .

# FIX: schemas is schemas.py (a file, not a package directory)
RUN mkdir -p db middleware routers llm slack escalation suppression alembic scripts tests teams \
    && touch db/__init__.py middleware/__init__.py routers/__init__.py \
             llm/__init__.py slack/__init__.py escalation/__init__.py \
             suppression/__init__.py alembic/__init__.py scripts/__init__.py \
             tests/__init__.py teams/__init__.py

# Build React frontend
RUN if [ -d "frontend" ]; then \
    cd frontend && npm install --legacy-peer-deps && npm run build && cd ..; \
    fi

# Give appuser write access to beat schedule directory
RUN mkdir -p /var/celery && chown -R appuser:appgroup /var/celery

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1}"]
