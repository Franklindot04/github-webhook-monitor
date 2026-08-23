from fastapi import FastAPI

from app.api.routes import create_router
from app.config import Settings
from app.services.webhooks import WebhookIngestionService
from app.storage.events import EventStore, InMemoryEventStore


def create_app(
    settings: Settings | None = None,
    event_store: EventStore | None = None,
) -> FastAPI:
    app_settings = settings or Settings()
    app_event_store = event_store or InMemoryEventStore(max_events=app_settings.max_events)
    app_webhook_service = WebhookIngestionService(
        event_store=app_event_store,
        webhook_secret=app_settings.webhook_secret.get_secret_value(),
    )

    app = FastAPI(title="GitHub Webhook Monitor", version="0.1.0")
    app.state.settings = app_settings
    app.state.event_store = app_event_store
    app.state.webhook_service = app_webhook_service
    app.include_router(create_router(app_webhook_service, app_event_store))
    return app
