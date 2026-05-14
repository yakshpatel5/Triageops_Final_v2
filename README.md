# TriageOps

AI-powered alert triage platform for MSPs and NOC teams.

## Quick Start

### 1. Clone and configure
```bash
git clone https://github.com/yakshpatel5/Triageops.git
cd Triageops
cp .env.example .env   # fill in secrets
```

### 2. Start all services
```bash
docker compose up --build
```

### 3. Run migrations
```bash
docker compose exec app alembic upgrade head
```

### 4. Create first API key
```bash
docker compose exec app python scripts/seed_api_key.py --tenant my-noc
```

### 5. Open dashboard
- **React UI**: http://localhost:8000/app  
- **Old dashboard**: http://localhost:8000/dashboard  
- **API docs**: http://localhost:8000/docs  

---

## Local Development (2 terminals)

**Terminal 1 — Backend:**
```bash
uvicorn main:app --reload --port 8000
```

**Terminal 2 — Frontend:**
```bash
cd frontend
npm install
npm run dev   # http://localhost:5173
```

---

## Deploy to Railway

```bash
npm install -g @railway/cli
railway login
railway link

# Set env vars in Railway dashboard, then:
railway up
railway run alembic upgrade head
railway run python scripts/seed_api_key.py --tenant my-noc
```

---

## Project Structure

```
├── main.py              FastAPI app entry point
├── models.py            SQLAlchemy ORM models
├── schemas.py           Pydantic schemas
├── tasks.py             Celery tasks
├── cron.py              Beat scheduler logic
├── frontend/            React dashboard (Anthropic UI)
├── dashboard/           Legacy static HTML dashboard
├── routers/             API route handlers
├── llm/                 OpenAI GPT-4o pipeline
├── slack/               Slack Block Kit notifications
├── teams/               Microsoft Teams Adaptive Cards
├── escalation/          PagerDuty / OpsGenie / Webhook
├── suppression/         Alert suppression rules engine
├── db/                  Database session & queries
├── middleware/          Auth, rate limiting, request ID
├── alembic/             Database migrations (3 revisions)
└── tests/               Test suite (120+ tests)
```

---

## Environment Variables

See `.env.example` for all required variables.

Minimum to get started:
```
OPENAI_API_KEY=sk-...
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
BOOTSTRAP_API_KEY=your-random-string
POSTGRES_PASSWORD=your-db-password
```
