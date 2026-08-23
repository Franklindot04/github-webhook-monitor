from fastapi import APIRouter, Header, HTTPException, Request

from app.services.webhooks import (
    InvalidWebhookSignatureError,
    MalformedWebhookPayloadError,
    WebhookIngestionService,
)
from app.storage.events import EventStore


async def read_bounded_body(request: Request, max_body_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total_size = 0
    async for chunk in request.stream():
        total_size += len(chunk)
        if total_size > max_body_bytes:
            raise HTTPException(status_code=413, detail="Payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def is_json_content_type(content_type: str | None) -> bool:
    if content_type is None:
        return False
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json"


def require_non_empty_header(value: str | None, detail: str) -> str:
    if value is None or not value.strip():
        raise HTTPException(status_code=400, detail=detail)
    return value


def require_positive_integer_header(value: str | None, detail: str) -> str:
    header_value = require_non_empty_header(value, detail)
    try:
        parsed_value = int(header_value)
    except ValueError:
        raise HTTPException(status_code=400, detail=detail)
    if parsed_value <= 0:
        raise HTTPException(status_code=400, detail=detail)
    return header_value


def validate_content_length(content_length: str | None, max_body_bytes: int) -> None:
    if content_length is None:
        return
    try:
        declared_length = int(content_length)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Content-Length")
    if declared_length < 0:
        raise HTTPException(status_code=400, detail="Invalid Content-Length")
    if declared_length > max_body_bytes:
        raise HTTPException(status_code=413, detail="Payload too large")


def validate_installation_target(
    installation_target_id: str | None,
    installation_target_type: str | None,
) -> tuple[str | None, str | None]:
    if installation_target_id is None and installation_target_type is None:
        return None, None
    if installation_target_id is None or installation_target_type is None:
        raise HTTPException(status_code=400, detail="Invalid GitHub installation target metadata")
    target_id = require_positive_integer_header(
        installation_target_id,
        "Invalid GitHub installation target metadata",
    )
    target_type = require_non_empty_header(
        installation_target_type,
        "Invalid GitHub installation target metadata",
    )
    return target_id, target_type


def create_router(
    webhook_service: WebhookIngestionService,
    event_store: EventStore,
    max_webhook_body_bytes: int,
) -> APIRouter:
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
        content_type: str | None = Header(default=None),
        content_length: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
        x_github_hook_id: str | None = Header(default=None),
        x_github_hook_installation_target_id: str | None = Header(default=None),
        x_github_hook_installation_target_type: str | None = Header(default=None),
        x_hub_signature_256: str | None = Header(default=None),
    ):
        if not is_json_content_type(content_type):
            raise HTTPException(status_code=415, detail="Unsupported media type")

        github_event = require_non_empty_header(x_github_event, "Missing GitHub event")
        github_delivery = require_non_empty_header(x_github_delivery, "Missing GitHub delivery ID")
        github_hook_id = require_positive_integer_header(x_github_hook_id, "Invalid GitHub hook ID")
        installation_target_id, installation_target_type = validate_installation_target(
            x_github_hook_installation_target_id,
            x_github_hook_installation_target_type,
        )

        validate_content_length(content_length, max_webhook_body_bytes)
        raw_body = await read_bounded_body(request, max_webhook_body_bytes)

        try:
            event = webhook_service.ingest(
                raw_body=raw_body,
                signature=x_hub_signature_256,
                github_event=github_event,
                github_delivery=github_delivery,
                github_hook_id=github_hook_id,
                installation_target_id=installation_target_id,
                installation_target_type=installation_target_type,
            )
        except InvalidWebhookSignatureError:
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        except MalformedWebhookPayloadError:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        return {"message": "Webhook received", "event": event.to_dict()}

    return router
