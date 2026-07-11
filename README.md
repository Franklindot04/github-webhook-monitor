# GitHub Webhook Monitor

A lightweight FastAPI service for receiving, validating, and inspecting GitHub webhook deliveries.

This project is a small GitHub integration MVP built around secure webhook ingestion. It validates incoming deliveries using `X-Hub-Signature-256`, stores a capped in-memory list of recent events, and exposes simple endpoints for health checks and event inspection. The core focus is secure webhook validation and lightweight event visibility.

## Features

- Receive GitHub webhook deliveries via `POST /webhook/github`.
- Validate incoming webhook signatures using HMAC SHA256 and `X-Hub-Signature-256`.
- Reject invalid deliveries with `401 Unauthorized`.
- Store a capped in-memory list of recent events for quick inspection.
- Expose a health endpoint for smoke checks.
- Expose an events endpoint for viewing recently received payload summaries.

## Project structure

```text
github-webhook-monitor/
├── app/
│   ├── __init__.py
│   ├── config.py
│   ├── main.py
│   ├── security.py
│   └── store.py
├── .env.example
├── .gitignore
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md
├── LICENSE
├── NOTICE
├── SECURITY.md
└── requirements.txt
```

## Requirements

- Python 3.12+
- pip
- GitHub repository access for webhook setup
- A webhook secret stored in environment variables

## Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/Franklindot04/github-webhook-monitor.git
   cd github-webhook-monitor
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Create your environment file:
   ```bash
   cp .env.example .env
   ```

5. Update `.env` with your values:
   ```env
   WEBHOOK_SECRET=replace-with-a-random-secret
   APP_HOST=0.0.0.0
   APP_PORT=8000
   MAX_EVENTS=50
   ```

## Tech stack

- Python
- FastAPI
- Uvicorn
- python-dotenv

## Running the app

Start the development server with Uvicorn:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Once the server is running, you can open:

- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/events`
- `http://127.0.0.1:8000/docs`

## API endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check endpoint |
| GET | `/events` | Returns recent stored webhook event summaries |
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
SIG=$(python - <<'PY'
import hmac
import hashlib
from app.config import WEBHOOK_SECRET

payload = open("payload.json", "rb").read()
print("sha256=" + hmac.new(WEBHOOK_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest())
PY
)

curl -X POST http://127.0.0.1:8000/webhook/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-GitHub-Delivery: test-delivery-001" \
  -H "X-Hub-Signature-256: $SIG" \
  --data-binary @payload.json
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
- Validate the signature before processing any payload.
- Reject invalid deliveries immediately.
- Avoid placing secrets or credentials in the payload URL.

## Repository files

This repository includes additional project health and governance files:

- `CODE_OF_CONDUCT.md`
- `CONTRIBUTING.md`
- `SECURITY.md`
- `LICENSE`
- `NOTICE`

## Current status

This is the initial MVP release of the project. The current implementation focuses on secure webhook ingestion, event validation, and lightweight event visibility rather than persistence, retries, or deployment automation.

## License

This project is licensed under the MIT License. See `LICENSE` for details.