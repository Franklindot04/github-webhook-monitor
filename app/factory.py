from fastapi import FastAPI

from app.api.routes import create_router
from app.config import Settings
from app.services.webhooks import WebhookIngestionService
from app.storage.deliveries import DeliveryStore, InMemoryDeliveryStore


def create_app(
    settings: Settings | None = None,
    delivery_store: DeliveryStore | None = None,
) -> FastAPI:
    app_settings = settings or Settings()
    app_delivery_store = delivery_store or InMemoryDeliveryStore(max_events=app_settings.max_events)
    app_webhook_service = WebhookIngestionService(
        delivery_store=app_delivery_store,
        webhook_secret=app_settings.webhook_secret.get_secret_value(),
    )

    app = FastAPI(title="GitHub Webhook Monitor", version="0.1.0")
    app.state.settings = app_settings
    app.state.delivery_store = app_delivery_store
    app.state.webhook_service = app_webhook_service
    app.include_router(
        create_router(
            app_webhook_service,
            app_delivery_store,
            max_webhook_body_bytes=app_settings.max_webhook_body_bytes,
        )
    )
    return app
