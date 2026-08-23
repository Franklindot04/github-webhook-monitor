from datetime import datetime, timezone
import hashlib
import json
from typing import Callable
from uuid import UUID, uuid4

from app.domain.deliveries import DeliveryAttempt, GitHubDeliveryIdentity
from app.security import verify_github_signature
from app.storage.deliveries import DeliveryStore


class InvalidWebhookSignatureError(Exception):
    pass


class MalformedWebhookPayloadError(Exception):
    pass


class WebhookIngestionService:
    def __init__(
        self,
        delivery_store: DeliveryStore,
        webhook_secret: str,
        attempt_id_factory: Callable[[], UUID] = uuid4,
        clock: Callable[[], datetime] | None = None,
    ):
        self._delivery_store = delivery_store
        self._webhook_secret = webhook_secret
        self._attempt_id_factory = attempt_id_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def ingest(
        self,
        raw_body: bytes,
        signature: str | None,
        github_event: str,
        github_delivery: str,
        github_hook_id: str,
        installation_target_id: str | None = None,
        installation_target_type: str | None = None,
    ) -> DeliveryAttempt:
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

        delivery_identity = GitHubDeliveryIdentity(
            delivery_guid=github_delivery,
            hook_id=int(github_hook_id),
        )
        attempt = DeliveryAttempt(
            attempt_id=self._attempt_id_factory(),
            delivery_identity=delivery_identity,
            received_at=self._clock(),
            payload_sha256=hashlib.sha256(raw_body).hexdigest(),
            event_type=github_event,
            installation_target_id=installation_target_id,
            installation_target_type=installation_target_type,
            repository=repository.get("full_name") if isinstance(repository, dict) else None,
            sender=sender.get("login") if isinstance(sender, dict) else None,
            action=payload.get("action"),
        )
        self._delivery_store.add(attempt)
        return attempt
