# GitHub Webhook Monitor

A lightweight FastAPI service for receiving, validating, and inspecting GitHub webhook deliveries.

This project is a small GitHub integration MVP built around secure webhook ingestion. It validates incoming deliveries using `X-Hub-Signature-256`, stores recent observed delivery attempts in the selected delivery-store backend, and exposes simple endpoints for liveness, readiness, and event inspection. The default backend remains an in-memory ledger for local development.

## Live project page

GitHub Pages:
https://franklindot04.github.io/github-webhook-monitor/

## Features

- Receive GitHub webhook deliveries via `POST /webhook/github`.
- Accept GitHub webhook payloads sent as `application/json`.
- Validate incoming webhook signatures using HMAC SHA256 and `X-Hub-Signature-256`.
- Require GitHub delivery metadata such as event name, delivery ID, and hook ID.
- Reject unsupported media types, oversized payloads, and invalid delivery metadata before ingestion.
- Reject invalid deliveries with `401 Unauthorized`.
- Store recent observed delivery attempts for quick inspection, using memory by default or PostgreSQL when explicitly selected.
- Expose a health endpoint for liveness smoke checks.
- Expose a readiness endpoint for runtime dependency checks.
- Expose an events endpoint for viewing recently received payload summaries.

## Project structure

```text
github-webhook-monitor/
├── app/
│   ├── __init__.py
│   ├── api/
│   ├── config.py
│   ├── domain/
│   ├── factory.py
│   ├── main.py
│   ├── security.py
│   ├── services/
│   └── storage/
├── .env.example
├── .gitignore
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md
├── LICENSE
├── NOTICE
├── SECURITY.md
├── pyproject.toml
└── uv.lock
```

## Requirements

- Python 3.12+
- uv
- GitHub repository access for webhook setup
- A webhook secret stored in environment variables

## Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/Franklindot04/github-webhook-monitor.git
   cd github-webhook-monitor
   ```

2. Install `uv` if it is not already available:
   ```bash
   python -m pip install uv
   ```

3. Synchronize the locked project environment:
   ```bash
   uv sync --locked --group dev
   ```

4. Create your environment file:
   ```bash
   cp .env.example .env
   ```

5. Update `.env` with your values:
   ```env
   WEBHOOK_SECRET=replace-with-a-long-random-development-secret
   MAX_EVENTS=50
   MAX_WEBHOOK_BODY_BYTES=26214400
   DELIVERY_STORE_BACKEND=memory
   ```

## Tech stack

- Python
- FastAPI
- Uvicorn
- pydantic-settings
- uv

## Running the app

Start the development server with Uvicorn:

```bash
uv run --locked uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Once the server is running, you can open:

- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/ready`
- `http://127.0.0.1:8000/events`
- `http://127.0.0.1:8000/docs`

## API endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Liveness check endpoint |
| GET | `/ready` | Runtime dependency readiness endpoint |
| GET | `/events` | Returns recent stored webhook delivery attempt summaries |
| POST | `/webhook/github` | Receives and validates GitHub webhook deliveries |

## Local webhook test

You can test the receiver locally by creating a sample payload, generating a matching signature, and sending the request to the webhook endpoint.

Example payload file:

```json
{
  "action": "opened",
  "repository": {
    "full_name": "Franklindot04/github-webhook-monitor"
  },
  "sender": {
    "login": "Franklindot04"
  }
}
```

Generate a valid signature and send the request:

```bash
SIG=$(uv run --locked python - <<'PY'
import hmac
import hashlib
from app.config import Settings

payload = open("payload.json", "rb").read()
secret = Settings().webhook_secret.get_secret_value()
print("sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest())
PY
)

curl -X POST http://127.0.0.1:8000/webhook/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-GitHub-Delivery: test-delivery-001" \
  -H "X-GitHub-Hook-ID: 12345" \
  -H "X-Hub-Signature-256: $SIG" \
  --data-binary @payload.json
```

## Running tests

Run the regression suite through the locked project environment:

```bash
uv run --locked pytest -q
```

PostgreSQL integration tests are skipped unless an explicit disposable test database is provided:

```bash
TEST_DATABASE_URL=postgresql+psycopg://example_user:example_password@example-host:5432/example_database \
uv run --locked pytest -q -m integration
```

## GitHub webhook setup

To connect this service to a repository webhook:

1. Open your repository on GitHub.
2. Go to **Settings** → **Webhooks** → **Add webhook**.
3. Set **Payload URL** to your public endpoint, for example:
   ```text
   https://your-domain.com/webhook/github
   ```
4. Set **Content type** to `application/json`.
5. Set **Secret** to the same value as `WEBHOOK_SECRET` in your `.env`.
6. Choose the events you want to receive, or select individual events such as pull requests.
7. Ensure the webhook is active, then save it.

If you are testing locally, you can expose your development server with a tunnel such as ngrok and use the public forwarding URL as the payload URL.

## Security notes

- Never commit your real `.env` file.
- Keep `WEBHOOK_SECRET` private.
- Keep credential-bearing `DATABASE_URL` values private.
- Configure GitHub webhooks with `application/json` payloads.
- The receiver validates `X-Hub-Signature-256`; the legacy SHA-1 signature header is not accepted as a substitute.
- `MAX_WEBHOOK_BODY_BYTES` bounds accepted request bodies and defaults to `26214400` bytes.
- Validate the signature before processing any payload.
- Reject invalid deliveries immediately.
- Avoid placing secrets or credentials in the payload URL.

## Delivery ledger model

GitHub's `X-GitHub-Delivery` value identifies the logical upstream delivery. This receiver assigns a separate application-owned attempt ID to each accepted receipt it observes.

Repeated GitHub delivery IDs are retained as separate observed attempts rather than rejected or classified immediately. The in-memory ledger keeps bounded recent attempt metadata, including a SHA-256 digest of the exact accepted payload bytes, but it does not retain full raw payload bodies long term.

`MAX_EVENTS` keeps its existing operator-facing name. With the memory backend, it bounds the retained in-process ledger. With the PostgreSQL backend, it limits the recent attempts returned by `/events`; it does not delete durable rows.

The `/events` endpoint remains available and preserves the existing event fields. It also includes additive attempt metadata such as `attempt_id` and `payload_sha256` for operational comparison.

Replay detection, redelivery classification, idempotency, and durable payload retention are still deferred.

## Runtime persistence

The application defaults to the in-memory delivery ledger:

```env
DELIVERY_STORE_BACKEND=memory
```

PostgreSQL runtime persistence is selected only when explicitly configured:

```env
DELIVERY_STORE_BACKEND=postgresql
DATABASE_URL=postgresql+psycopg://example_user:example_password@example-host:5432/example_database
DATABASE_CONNECT_TIMEOUT_SECONDS=5
```

`DATABASE_URL` is optional for memory mode and required for PostgreSQL mode. PostgreSQL runtime mode requires the synchronous Psycopg SQLAlchemy driver form, `postgresql+psycopg://`.

Migrations are operationally explicit. Apply the Alembic migration before starting the app in PostgreSQL mode:

```bash
DATABASE_URL=postgresql+psycopg://example_user:example_password@example-host:5432/example_database \
uv run --locked alembic upgrade head
```

The app does not run migrations automatically, does not call `metadata.create_all()`, and does not silently fall back to memory when PostgreSQL is selected. PostgreSQL startup fails if the database is unreachable or the delivery-ledger schema is unavailable.

The PostgreSQL schema stores logical GitHub deliveries separately from observed delivery attempts. It persists attempt metadata and `payload_sha256`, but it does not persist full raw webhook request bodies.

`GET /health` is a process liveness check and does not query PostgreSQL. `GET /ready` checks runtime dependency readiness: memory mode returns ready without database access, while PostgreSQL mode verifies database connectivity and required delivery-ledger schema usability. If PostgreSQL persistence is unavailable during request handling, `POST /webhook/github` and `GET /events` return `503 Service Unavailable` with a generic response rather than acknowledging or returning misleading data.

## Repository files

This repository includes additional project health and governance files:

- `CODE_OF_CONDUCT.md`
- `CONTRIBUTING.md`
- `SECURITY.md`
- `LICENSE`
- `NOTICE`

## Current status

This project is currently in its initial MVP stage and serves as the foundation for future development.

The current implementation focuses on secure webhook ingestion, event validation, and lightweight event visibility rather than persistence, retries, background processing, or deployment automation.

More stages, hardening steps, and deployment improvements will be added in later iterations.

## License

This project is licensed under the MIT License. See `LICENSE` for details.
