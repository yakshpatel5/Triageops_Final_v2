# TriageOps — production Dockerfile
# Three-stage build: Python deps -> React build -> Slim Runtime

# ---------------------------------------------------------------------------
# Stage 1: Python dependency builder
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS py-builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ---------------------------------------------------------------------------
# Stage 2: Frontend builder
# ---------------------------------------------------------------------------
FROM node:20-slim AS fe-builder

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install --legacy-peer-deps
COPY frontend/ ./
RUN npm run build


# ---------------------------------------------------------------------------
# Stage 3: Runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies
COPY --from=py-builder /install /usr/local

# Create non-root user
RUN groupadd -g 1001 appgroup && useradd -u 1001 -g appgroup -s /bin/sh appuser
WORKDIR /app

# Copy application code
COPY --chown=appuser:appgroup . .

# Copy built frontend assets
COPY --from=fe-builder --chown=appuser:appgroup /app/frontend/dist ./frontend/dist

# Ensure package structure
RUN mkdir -p db middleware routers llm slack escalation suppression alembic scripts tests teams \
    && touch db/__init__.py middleware/__init__.py routers/__init__.py \
             llm/__init__.py slack/__init__.py escalation/__init__.py \
             suppression/__init__.py alembic/__init__.py scripts/__init__.py \
             tests/__init__.py teams/__init__.py

# Give appuser write access to beat schedule directory
RUN mkdir -p /var/celery && chown -R appuser:appgroup /var/celery

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1}"]
