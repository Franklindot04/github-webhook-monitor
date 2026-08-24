from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from app.api.routes import create_router
from app.config import Settings
from app.integrations.github.client import GitHubRepositoryWebhookDeliveriesClient
from app.runtime import RuntimeResources, build_runtime_resources
from app.services.delivery_queries import DeliveryQueryService
from app.services.github_reconciliation import GitHubReconciliationService
from app.services.webhooks import WebhookIngestionService
from app.storage.deliveries import DeliveryStore


def create_app(
    settings: Settings | None = None,
    delivery_store: DeliveryStore | None = None,
    github_delivery_client: GitHubRepositoryWebhookDeliveriesClient | None = None,
) -> FastAPI:
    app_settings = settings or Settings()
    runtime_resources = (
        RuntimeResources(delivery_store=delivery_store, readiness_check=lambda: None)
        if delivery_store is not None
        else build_runtime_resources(app_settings)
    )
    app_delivery_store = runtime_resources.delivery_store
    app_webhook_service = WebhookIngestionService(
        delivery_store=app_delivery_store,
        webhook_secret=app_settings.webhook_secret.get_secret_value(),
    )
    app_github_delivery_client = github_delivery_client
    owns_github_delivery_client = False
    if app_settings.github_reconciliation_enabled and app_github_delivery_client is None:
        if app_settings.github_repository_webhook_token is None:
            raise ValueError("GITHUB_REPOSITORY_WEBHOOK_TOKEN is required when reconciliation is enabled")
        app_github_delivery_client = GitHubRepositoryWebhookDeliveriesClient(
            token=app_settings.github_repository_webhook_token.get_secret_value(),
            timeout_seconds=app_settings.github_api_timeout_seconds,
        )
        owns_github_delivery_client = True
    app_github_reconciliation_service = GitHubReconciliationService(
        enabled=app_settings.github_reconciliation_enabled,
        delivery_query_service=DeliveryQueryService(app_delivery_store),
        github_client=app_github_delivery_client,
        max_pages=app_settings.github_reconciliation_max_pages,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await run_in_threadpool(runtime_resources.readiness_check)
            yield
        finally:
            if owns_github_delivery_client and app_github_delivery_client is not None:
                await app_github_delivery_client.aclose()
            if runtime_resources.owns_engine and runtime_resources.engine is not None:
                await run_in_threadpool(runtime_resources.engine.dispose)

    app = FastAPI(title="GitHub Webhook Monitor", version="0.1.0", lifespan=lifespan)
    app.state.settings = app_settings
    app.state.delivery_store = app_delivery_store
    app.state.runtime_resources = runtime_resources
    app.state.webhook_service = app_webhook_service
    app.state.github_delivery_client = app_github_delivery_client
    app.state.github_reconciliation_service = app_github_reconciliation_service
    app.include_router(
        create_router(
            app_webhook_service,
            app_delivery_store,
            readiness_check=runtime_resources.readiness_check,
            max_webhook_body_bytes=app_settings.max_webhook_body_bytes,
            management_api_enabled=app_settings.management_api_enabled,
            management_api_token=(
                app_settings.management_api_token.get_secret_value()
                if app_settings.management_api_token is not None
                else None
            ),
            github_reconciliation_service=app_github_reconciliation_service,
        )
    )
    return app
