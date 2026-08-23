from fastapi import APIRouter, Header, HTTPException, Request

from app.services.webhooks import (
    InvalidWebhookSignatureError,
    MalformedWebhookPayloadError,
    WebhookIngestionService,
)
from app.storage.events import EventStore


def create_router(webhook_service: WebhookIngestionService, event_store: EventStore) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health():
        return {"status": "ok"}

    @router.get("/events")
    def get_events():
        events = [event.to_dict() for event in event_store.list_recent()]
        return {"count": len(events), "events": events}

    @router.post("/webhook/github")
    async def github_webhook(
        request: Request,
        x_github_event: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
        x_hub_signature_256: str | None = Header(default=None),
    ):
        raw_body = await request.body()

        try:
            event = webhook_service.ingest(
                raw_body=raw_body,
                signature=x_hub_signature_256,
                github_event=x_github_event,
                github_delivery=x_github_delivery,
            )
        except InvalidWebhookSignatureError:
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        except MalformedWebhookPayloadError:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        return {"message": "Webhook received", "event": event.to_dict()}

    return router
