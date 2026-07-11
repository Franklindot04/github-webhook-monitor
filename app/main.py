from datetime import datetime, timezone
import json

from fastapi import FastAPI, Header, HTTPException, Request
from app.config import WEBHOOK_SECRET
from app.security import verify_github_signature
from app.store import events_store

app = FastAPI(title="GitHub Webhook Monitor", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/events")
def get_events():
    return {"count": len(events_store), "events": list(events_store)}


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
):
    raw_body = await request.body()

    if not verify_github_signature(raw_body, x_hub_signature_256, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_record = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "event": x_github_event,
        "delivery_id": x_github_delivery,
        "repository": payload.get("repository", {}).get("full_name"),
        "sender": payload.get("sender", {}).get("login"),
        "action": payload.get("action"),
    }

    events_store.appendleft(event_record)

    return {"message": "Webhook received", "event": event_record}