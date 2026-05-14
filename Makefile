.PHONY: up down build test smoke logs migrate migrate-down migration seed-key lint clean help

up:
	docker compose up --build

up-dev:
	docker compose --profile dev up --build

down:
	docker compose down

down-volumes:
	docker compose down -v

build:
	docker compose build

restart:
	docker compose restart app worker

logs:
	docker compose logs -f app worker

ps:
	docker compose ps

migrate:
	docker compose exec app alembic upgrade head

migrate-down:
	docker compose exec app alembic downgrade -1

migrate-history:
	docker compose exec app alembic history --verbose

migrate-current:
	docker compose exec app alembic current

migration:
	@test -n "$(MSG)" || (echo "Usage: make migration MSG='describe change'" && exit 1)
	docker compose exec app alembic revision --autogenerate -m "$(MSG)"

shell:
	docker compose exec app python

psql:
	docker compose exec postgres psql -U triageops -d triageops

redis-cli:
	docker compose exec redis redis-cli

test:
	docker compose exec app pytest tests/ -v --tb=short

test-local:
	pytest tests/ -v --tb=short

smoke:
	@test -n "$(KEY)" || (echo "Usage: make smoke KEY=your-api-key" && exit 1)
	@echo "Health check..."
	@curl -sf http://localhost:8000/health | python3 -m json.tool
	@echo "\nIngest test alert..."
	@curl -sf -X POST http://localhost:8000/webhook/prtg \
	  -H "X-API-Key: $(KEY)" -H "Content-Type: application/json" \
	  -d '{"sensorid":"smoke-001","device":"smoke-host","status":"Down","message":"Smoke test"}' \
	  | python3 -m json.tool
	@echo "\nStats..."
	@sleep 3
	@curl -sf http://localhost:8000/ops/stats -H "X-API-Key: $(KEY)" | python3 -m json.tool
	@echo "\nSmoke test complete"

seed-key:
	@test -n "$(TENANT)" || (echo "Usage: make seed-key TENANT=acme" && exit 1)
	docker compose exec app python scripts/seed_api_key.py \
	  --tenant "$(TENANT)" --description "$(or $(DESC),Created via Makefile)"

lint:
	ruff check . --fix

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true

help:
	@echo "TriageOps Makefile targets:"
	@echo "  up              Start all services"
	@echo "  up-dev          Start all services + Flower"
	@echo "  down            Stop services"
	@echo "  down-volumes    Stop services and delete volumes"
	@echo "  build           Rebuild Docker images"
	@echo "  test            Run test suite in container"
	@echo "  smoke KEY=x     End-to-end smoke test"
	@echo "  migrate         Apply Alembic migrations"
	@echo "  migrate-down    Rollback last migration"
	@echo "  migration MSG=x Generate new migration"
	@echo "  seed-key TENANT=x  Create tenant API key"
	@echo "  logs            Tail app + worker logs"
	@echo "  psql            Open psql"
	@echo "  redis-cli       Open redis-cli"
	@echo "  clean           Remove pycache"

.DEFAULT_GOAL := help
