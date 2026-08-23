from datetime import datetime, timezone
import json

from app.domain.events import EventSummary
from app.security import verify_github_signature
from app.storage.events import EventStore


class InvalidWebhookSignatureError(Exception):
    pass


class MalformedWebhookPayloadError(Exception):
    pass


class WebhookIngestionService:
    def __init__(self, event_store: EventStore, webhook_secret: str):
        self._event_store = event_store
        self._webhook_secret = webhook_secret

    def ingest(
        self,
        raw_body: bytes,
        signature: str | None,
        github_event: str | None,
        github_delivery: str | None,
    ) -> EventSummary:
        if not verify_github_signature(raw_body, signature, self._webhook_secret):
            raise InvalidWebhookSignatureError

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise MalformedWebhookPayloadError from exc

        event = EventSummary(
            received_at=datetime.now(timezone.utc).isoformat(),
            event=github_event,
            delivery_id=github_delivery,
            repository=payload.get("repository", {}).get("full_name"),
            sender=payload.get("sender", {}).get("login"),
            action=payload.get("action"),
        )
        self._event_store.add(event)
        return event
