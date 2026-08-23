# PayRecover

**AI-powered payment recovery agent for Indian merchants.**
Razorpay AI Buildathon — Track 03: AI Revenue Recovery.

PayRecover is an autonomous multi-agent system that detects failed payments via
Razorpay webhooks, diagnoses the root cause, picks a bounded recovery strategy
with a confidence score, enforces compliance through **deterministic** (non-AI)
rules, and executes recovery actions — with a full audit trail and a live
dashboard.

> This repository is being built in phases. **Phase 1 (backend foundation) is
> complete and runnable today.** The agent pipeline, full simulator, and the
> Next.js dashboard land in later phases — see [Roadmap](#roadmap).

---

## Runs with zero credentials

Every external key is **optional**. With no keys set, the backend runs in
**simulation mode**: Razorpay calls return realistic mock responses and the
(future) LLM layer falls back to a deterministic mock. This means you can
develop and demo the whole pipeline before wiring any real API keys. Drop keys
into `.env` whenever you want to switch to live calls.

---

## Tech stack

| Layer      | Choice                                             |
| ---------- | -------------------------------------------------- |
| Backend    | FastAPI (Python 3.11+), async                      |
| Database   | PostgreSQL 16 via SQLAlchemy 2 (async) + asyncpg   |
| Real-time  | Server-Sent Events (SSE)                           |
| Payments   | Official `razorpay` SDK (test mode), key-optional  |
| AI (later) | Groq API (Llama 3.3 70B) — direct SDK, no LangChain|
| Frontend   | Next.js 15 + React 19 + Tailwind (later phase)     |
| Deploy     | Docker Compose                                     |

---

## Quick start — Docker (recommended)

Boots PostgreSQL + the backend in simulation mode. One command:

```bash
docker compose up --build
```

Then:

- API root & health: <http://localhost:8000/> and <http://localhost:8000/health>
- Interactive API docs (Swagger): <http://localhost:8000/docs>

Try it:

```bash
# Inject 5 synthetic failed payments
curl -X POST http://localhost:8000/api/simulator/inject \
  -H "Content-Type: application/json" -d '{"count": 5}'

# See them
curl http://localhost:8000/api/dashboard/events

# Aggregate metrics (₹ recovered, recovery rate, breakdowns)
curl http://localhost:8000/api/dashboard/metrics

# Watch the live event stream (leave running, then inject in another terminal)
curl -N http://localhost:8000/api/stream
```

---

## Quick start — local (without Docker)

Requires Python 3.11+. A local PostgreSQL is optional — for a quick spin you can
point at SQLite.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional; defaults work

# Option A: use your local Postgres (matches .env default)
#   ensure a DB reachable at the DATABASE_URL in .env, then:
uvicorn app.main:app --reload --port 8000

# Option B: no Postgres handy? use SQLite for a quick run:
DATABASE_URL="sqlite+aiosqlite:///./payrecover.db" uvicorn app.main:app --reload --port 8000
```

---

## Configuration

Copy `backend/.env.example` to `backend/.env`. All keys are optional.

| Variable                  | Purpose                                             | If unset                          |
| ------------------------- | --------------------------------------------------- | --------------------------------- |
| `RAZORPAY_KEY_ID`         | Razorpay API key id (`rzp_test_…`)                  | Razorpay calls are **simulated**  |
| `RAZORPAY_KEY_SECRET`     | Razorpay API key secret                             | Razorpay calls are **simulated**  |
| `RAZORPAY_WEBHOOK_SECRET` | Verifies inbound webhook HMAC signatures            | Signature check **skipped** (dev) |
| `GROQ_API_KEY`            | Groq LLM key (`gsk_…`) — used by agents later       | Agent layer uses a **mock**       |
| `DATABASE_URL`            | Async DB URL                                        | Local Postgres default            |
| `CORS_ORIGINS`            | Allowed origins (JSON array or comma-separated)     | `http://localhost:3000`           |
| `FORCE_SIMULATION`        | Force simulation even if keys are present           | `false`                           |

---

## How to get the API keys

### Razorpay test keys (`RAZORPAY_KEY_ID` + `RAZORPAY_KEY_SECRET`)

1. Create/log in to an account at <https://dashboard.razorpay.com>.
2. Toggle to **Test Mode** (switch at the top of the dashboard).
3. Go to **Account & Settings → API Keys** (or **Settings → API Keys**).
4. Click **Generate Test Key**. Copy the **Key Id** (`rzp_test_…`) and the
   **Key Secret** — the secret is shown **only once**, so save it now.
5. Put both into `backend/.env`.

Test cards / UPI for triggering payments live in the PRD (`success@razorpay`,
`failure@razorpay`, test card `4384 7968 2770 3274`, etc.).

### Razorpay webhook secret (`RAZORPAY_WEBHOOK_SECRET`)

1. In the dashboard (Test Mode): **Settings → Webhooks → Create New Webhook**.
2. **Webhook URL:** your backend's public URL ending in `/webhooks/razorpay`.
   For local dev, expose `localhost:8000` with a tunnel:
   ```bash
   # e.g. cloudflared or ngrok
   cloudflared tunnel --url http://localhost:8000
   # then use https://<tunnel-domain>/webhooks/razorpay as the URL
   ```
3. **Secret:** enter any strong string you choose — this exact value goes into
   `RAZORPAY_WEBHOOK_SECRET`.
4. **Active events:** at minimum `payment.failed` and `payment.captured` (also
   useful: `order.paid`, `payment.dispute.created`, `subscription.cancelled`,
   `refund.created`).
5. Save. Razorpay signs each delivery; the backend verifies it with your secret.

### Groq API key (`GROQ_API_KEY`) — needed once agents are added

1. Sign in at <https://console.groq.com>.
2. Open **API Keys → Create API Key**, copy the value (`gsk_…`).
3. Put it into `backend/.env`. Free tier is ~30 requests/min, which the pipeline
   is designed around (2 LLM calls per event).

---

## API endpoints (Phase 1)

| Method | Path                              | Description                                  |
| ------ | --------------------------------- | -------------------------------------------- |
| GET    | `/` , `/health`                   | Service status, current mode                 |
| GET    | `/api/stream`                     | SSE stream of live recovery activity         |
| POST   | `/webhooks/razorpay`              | Webhook receiver (HMAC verify, dedup, SSE)   |
| POST   | `/api/simulator/inject`           | Inject 1–200 synthetic failures              |
| GET    | `/api/simulator/profiles`         | List available failure profiles              |
| GET    | `/api/dashboard/metrics`          | ₹ recovered, recovery rate, breakdowns       |
| GET    | `/api/dashboard/events`           | Paginated recovery events (filter by status) |
| GET    | `/api/dashboard/events/{id}`      | Single event + its actions + circuit breakers|

---

## Tests

```bash
cd backend
pip install -r requirements.txt   # includes pytest + pytest-asyncio
pytest                            # runs against a throwaway SQLite DB
```

Covers: HMAC signature (valid/invalid), webhook ingestion + idempotent dedup,
`payment.captured` marking an event recovered, simulator injection, event
detail shape, and metrics after recovery.

---

## Project structure

```
razorpay/
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI app: CORS, lifespan, SSE, routers
│   │   ├── config.py          # settings (all keys optional)
│   │   ├── database.py        # async engine + session (Postgres / SQLite)
│   │   ├── models.py          # 3 ORM models (events, actions, breakers)
│   │   ├── ingest.py          # shared ingestion + serialization service
│   │   ├── sse.py             # SSE manager
│   │   ├── webhooks/          # signature.py (HMAC) + router.py
│   │   ├── razorpay/client.py # key-optional Razorpay wrapper
│   │   └── api/               # dashboard.py + simulator_routes.py
│   ├── tests/                 # pytest suite (SQLite)
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env.example
├── docker-compose.yml
└── README.md
```

---

## Roadmap

**Done — Phase 1: backend foundation**
- FastAPI app, config (key-optional), async DB + 3-table schema
- Razorpay client wrapper with simulated fallback
- Webhook receiver: HMAC verification, dedup, circuit-event handling
- SSE real-time stream
- Basic failure simulator (`/inject`) + dashboard metrics/events endpoints
- Docker Compose + pytest suite

**Remaining**
- **Diagnostic Agent** (LLM #1): classify failure, recoverability score
- **Strategy Agent** (LLM #2): pick one of 6 bounded tools + confidence score
- **Compliance Engine** (deterministic Python): NPCI/TRAI/RBI rules, cost ceiling
- **Confidence gate + HITL** (human-in-the-loop) approval flow
- **Execution engine**: real Payment Links, scheduler (APScheduler), audit logging
- **8 circuit breakers** as a first-class engine
- **Full simulator**: weighted batches, 6 chaos presets, cascade mode
- **Next.js dashboard**: dual-pane command center, agent trace, economics, timeline, before/after
- **ARCHITECTURE.md** + 5-minute demo script

Each remaining piece is fully designed and lands in subsequent phases.
