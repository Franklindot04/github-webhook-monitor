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
        github_event: str,
        github_delivery: str,
        github_hook_id: str,
        installation_target_id: str | None = None,
        installation_target_type: str | None = None,
    ) -> EventSummary:
        if not verify_github_signature(raw_body, signature, self._webhook_secret):
            raise InvalidWebhookSignatureError

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise MalformedWebhookPayloadError from exc
        if not isinstance(payload, dict):
            payload = {}

        repository = payload.get("repository")
        sender = payload.get("sender")

        event = EventSummary(
            received_at=datetime.now(timezone.utc).isoformat(),
            event=github_event,
            delivery_id=github_delivery,
            hook_id=github_hook_id,
            installation_target_id=installation_target_id,
            installation_target_type=installation_target_type,
            repository=repository.get("full_name") if isinstance(repository, dict) else None,
            sender=sender.get("login") if isinstance(sender, dict) else None,
            action=payload.get("action"),
        )
        self._event_store.add(event)
        return event
