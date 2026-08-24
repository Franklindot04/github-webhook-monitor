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
- Expose an authenticated management endpoint for viewing recently received payload summaries.

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
   MANAGEMENT_API_ENABLED=false
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
| GET | `/api/v1/delivery-attempts` | Preferred management diagnostics endpoint for paginated delivery attempts |
| GET | `/api/v1/delivery-attempts/{attempt_id}` | Preferred management diagnostics endpoint for one delivery attempt |
| GET | `/api/v1/delivery-attempts/{attempt_id}/github-deliveries` | Management endpoint for read-only GitHub repository-webhook delivery reconciliation |
| POST | `/api/v1/delivery-attempts/{attempt_id}/github-deliveries/{github_delivery_id}/redelivery` | Management endpoint for verified GitHub repository-webhook redelivery requests |
| GET | `/events` | Deprecated compatibility endpoint for recent stored webhook delivery attempt summaries |
| POST | `/webhook/github` | Receives and validates GitHub webhook deliveries |

## Endpoint classes

Public runtime endpoints:

- `GET /health`
- `GET /ready`

Webhook ingress:

- `POST /webhook/github`

Management plane:

- `GET /api/v1/delivery-attempts`
- `GET /api/v1/delivery-attempts/{attempt_id}`
- `GET /api/v1/delivery-attempts/{attempt_id}/github-deliveries`
- `POST /api/v1/delivery-attempts/{attempt_id}/github-deliveries/{github_delivery_id}/redelivery`
- `GET /events`

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
- Keep `MANAGEMENT_API_TOKEN` private and separate from `WEBHOOK_SECRET`.
- Keep credential-bearing `DATABASE_URL` values private.
- Configure GitHub webhooks with `application/json` payloads.
- The receiver validates `X-Hub-Signature-256`; the legacy SHA-1 signature header is not accepted as a substitute.
- `MAX_WEBHOOK_BODY_BYTES` bounds accepted request bodies and defaults to `26214400` bytes.
- Validate the signature before processing any payload.
- Reject invalid deliveries immediately.
- Avoid placing secrets or credentials in the payload URL.
- Generate production management tokens as high-entropy operator credentials using an appropriate secret-management process.

## Delivery ledger model

GitHub's `X-GitHub-Delivery` value identifies the logical upstream delivery. This receiver assigns a separate application-owned attempt ID to each accepted receipt it observes.

Repeated GitHub delivery IDs are retained as separate observed attempts rather than rejected or classified immediately. The in-memory ledger keeps bounded recent attempt metadata, including a SHA-256 digest of the exact accepted payload bytes, but it does not retain full raw payload bodies long term.

`MAX_EVENTS` keeps its existing operator-facing name. With the memory backend, it bounds the retained in-process ledger. With the PostgreSQL backend, it limits the recent attempts returned by `/events`; it does not delete durable rows.

The `/events` endpoint preserves the existing event fields when management access is enabled and authenticated. It also includes additive attempt metadata such as `attempt_id` and `payload_sha256` for operational comparison.

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

## Management access

Management APIs are disabled by default:

```env
MANAGEMENT_API_ENABLED=false
```

When disabled, management endpoints such as `GET /events` return `404 Not Found`. Webhook ingress, liveness, and readiness remain available according to their own contracts.

To enable the management plane, configure both:

```env
MANAGEMENT_API_ENABLED=true
MANAGEMENT_API_TOKEN=replace-with-a-high-entropy-management-token-000001
```

`MANAGEMENT_API_TOKEN` must be at least 32 characters. When management access is enabled, callers must send:

```text
Authorization: Bearer <management-token>
```

Missing, malformed, or incorrect bearer credentials return `401 Unauthorized` with a generic response. The GitHub webhook secret does not authenticate management endpoints, and the management bearer token does not authenticate webhook ingress. The current bearer-token boundary is a management-plane authentication foundation; future production authorization concerns such as OIDC, SSO, multiple operators, roles, scopes, and audit identity are intentionally deferred.

## Preferred management diagnostics API

Use the versioned management diagnostics API for read-only delivery-attempt inspection:

- `GET /api/v1/delivery-attempts`
- `GET /api/v1/delivery-attempts/{attempt_id}`
- `GET /api/v1/delivery-attempts/{attempt_id}/github-deliveries`
- `POST /api/v1/delivery-attempts/{attempt_id}/github-deliveries/{github_delivery_id}/redelivery`

The list endpoint returns recent observed receipt attempts in deterministic recent-first order. Responses include the application-owned `attempt_id`, GitHub logical delivery identity fields `delivery_guid` and `hook_id`, `received_at`, `payload_sha256`, event metadata, repository, sender, and installation target metadata. Responses do not include raw webhook bodies, HMAC signatures, authorization headers, database surrogate IDs, or database connection details.

Pagination uses `limit` and an opaque `cursor`:

- `limit` defaults to `50`.
- `limit` must be between `1` and `100`.
- `cursor` is returned as `next_cursor` when another page is available.
- Clients must not parse or construct cursor values.
- Invalid cursor values return `400 Bad Request`.

Example response:

```json
{
  "items": [
    {
      "attempt_id": "00000000-0000-0000-0000-000000000002",
      "delivery_guid": "synthetic-delivery-guid-002",
      "hook_id": 12345,
      "received_at": "2026-08-24T12:00:00Z",
      "payload_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "event_type": "pull_request",
      "action": "opened",
      "repository": "octo/example",
      "sender": "octocat",
      "installation_target_id": "67890",
      "installation_target_type": "repository"
    }
  ],
  "next_cursor": "opaque-cursor-value"
}
```

The detail endpoint looks up one observed receiver attempt by `attempt_id`. A syntactically valid but absent attempt ID returns `404 Not Found`.

Backend behavior differs only by retention boundary:

- Memory mode is bounded by `MAX_EVENTS`; pagination traverses only currently retained attempts.
- PostgreSQL mode traverses durable delivery-attempt history; page limits do not delete rows and `MAX_EVENTS` is not a PostgreSQL retention policy.

`GET /events` remains supported as a compatibility endpoint under the same management authentication contract, but it is deprecated in favor of the v1 diagnostics API. No removal date is set.

## GitHub reconciliation

GitHub upstream reconciliation is disabled by default and is available only as a management-plane capability. It supports GitHub repository webhook delivery history only:

```env
MANAGEMENT_API_ENABLED=true
MANAGEMENT_API_TOKEN=replace-with-a-high-entropy-management-token-000001
GITHUB_RECONCILIATION_ENABLED=true
GITHUB_API_TIMEOUT_SECONDS=5
GITHUB_RECONCILIATION_MAX_PAGES=5
```

When reconciliation is enabled, configure a repository-scoped token separately:

```env
GITHUB_REPOSITORY_WEBHOOK_TOKEN=replace-with-a-read-only-repository-webhook-token
```

The token requires repository **Webhooks: read** permission. Stage 10 does not require **Webhooks: write** permission and does not execute redelivery.

Reconciliation uses GitHub REST API version `2026-03-10` and the repository webhook list-deliveries endpoint:

```text
GET /repos/{owner}/{repo}/hooks/{hook_id}/deliveries
```

The management endpoint is:

```text
GET /api/v1/delivery-attempts/{attempt_id}/github-deliveries
```

The endpoint starts from a local `attempt_id`, verifies that the stored attempt can be safely attributed to a repository webhook, and searches bounded GitHub delivery history for records whose GitHub `guid` exactly matches the local `delivery_guid`. Results keep local and upstream identities separate:

- `attempt_id` is this service's local receiver-observation UUID.
- `delivery_guid` is the GitHub `X-GitHub-Delivery` header value retained locally.
- `hook_id` is the GitHub `X-GitHub-Hook-ID` value retained locally.
- `github_delivery_id` is GitHub's upstream numeric delivery-history record ID.

GitHub may return multiple upstream records for one `delivery_guid`; the response preserves all matches. The upstream `redelivery` field is GitHub-reported delivery-history metadata for that upstream record. It is not copied into the local delivery attempt and does not classify the local receiver attempt as a replay, duplicate, or redelivery.

The search uses GitHub cursor pagination with `per_page=100` and is bounded by `GITHUB_RECONCILIATION_MAX_PAGES`. If more GitHub history remains after the configured bound, the response sets `search_complete` to `false` and returns an opaque `next_cursor` bound to the same `attempt_id`. Clients must not parse or construct reconciliation cursors. The cursor is pagination state, not an authorization credential, and it is not cryptographically signed.

GitHub reconciliation is read-only. It does not call GitHub's delivery-detail endpoint, does not retrieve raw upstream payloads or request headers, does not persist upstream delivery records, and does not affect webhook ingestion.

GitHub availability is not part of application readiness. The app does not call GitHub during startup, `GET /health` remains liveness-only, `GET /ready` remains scoped to runtime persistence readiness, and valid webhook ingestion continues even if GitHub is unavailable.

## Controlled redelivery

Controlled GitHub repository-webhook redelivery is disabled by default and is available only as an authenticated management-plane action. It requires Stage 10 reconciliation to remain enabled because the selected upstream delivery record is reverified before mutation.

```env
MANAGEMENT_API_ENABLED=true
MANAGEMENT_API_TOKEN=replace-with-a-high-entropy-management-token-000001
GITHUB_RECONCILIATION_ENABLED=true
GITHUB_REPOSITORY_WEBHOOK_TOKEN=replace-with-a-read-only-repository-webhook-token
GITHUB_REDELIVERY_ENABLED=true
GITHUB_REPOSITORY_WEBHOOK_WRITE_TOKEN=replace-with-a-write-capable-repository-webhook-token
```

Stage 10 reconciliation uses the read credential and requires repository **Webhooks: read** permission. Stage 11 redelivery uses a separate write credential and requires repository **Webhooks: write** permission. Read-only operators can keep reconciliation enabled without configuring the write credential.

The redelivery endpoint is:

```text
POST /api/v1/delivery-attempts/{attempt_id}/github-deliveries/{github_delivery_id}/redelivery
```

The endpoint starts from a local `attempt_id`, validates repository webhook coordinates, performs a bounded read-only reconciliation search, and mutates only when the selected `github_delivery_id` is found with the same local `delivery_guid`. It does not trust an arbitrary caller-supplied upstream ID and does not call GitHub's delivery-detail endpoint.

On success the endpoint returns `202 Accepted`, meaning GitHub accepted the redelivery request. It does not mean the webhook has already been delivered, processed, or recorded locally. The local delivery ledger is updated only when GitHub later sends a webhook to `POST /webhook/github` and normal ingress authenticates and stores that receiver observation.

The redelivery action does not create a local `DeliveryAttempt`, does not mark an existing attempt as retried or redelivered, does not persist upstream action state, and does not add replay or idempotency semantics. Repeating the management `POST` can request additional GitHub redeliveries. If the POST outcome cannot be confirmed because of a timeout or ambiguous transport failure, operators should reconcile GitHub delivery history before deciding whether to retry manually.

Stage 11 does not add automatic retries, backoff, queues, workers, GitHub App redelivery, organization webhook redelivery, Enterprise host support, or upstream payload retrieval.

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
