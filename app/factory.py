from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from app.api.routes import create_router
from app.config import Settings
from app.integrations.github.client import (
    GitHubRepositoryWebhookDeliveriesClient,
    GitHubRepositoryWebhookRedeliveryClient,
)
from app.domain.management import ManagementScopePolicy
from app.runtime import RuntimeResources, build_runtime_resources
from app.security import OidcJwtConfig, OidcJwtManagementAuthenticator, parse_allowed_jwt_algorithms
from app.services.delivery_queries import DeliveryQueryService
from app.services.recovery_actions import RecoveryActionQueryService
from app.services.github_reconciliation import GitHubReconciliationService
from app.services.github_redelivery import GitHubRedeliveryService
from app.services.webhooks import WebhookIngestionService
from app.storage.deliveries import DeliveryStore
from app.storage.recovery_actions import InMemoryRecoveryActionStore, RecoveryActionStore


def create_app(
    settings: Settings | None = None,
    delivery_store: DeliveryStore | None = None,
    recovery_action_store: RecoveryActionStore | None = None,
    github_delivery_client: GitHubRepositoryWebhookDeliveriesClient | None = None,
    github_redelivery_client: GitHubRepositoryWebhookRedeliveryClient | None = None,
    management_identity_http_client: httpx2.AsyncClient | None = None,
) -> FastAPI:
    app_settings = settings or Settings()
    runtime_resources = (
        RuntimeResources(
            delivery_store=delivery_store,
            recovery_action_store=recovery_action_store or InMemoryRecoveryActionStore(max_actions=app_settings.max_events),
            readiness_check=lambda: None,
        )
        if delivery_store is not None
        else build_runtime_resources(app_settings)
    )
    app_delivery_store = runtime_resources.delivery_store
    app_recovery_action_store = runtime_resources.recovery_action_store
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
    app_github_redelivery_client = github_redelivery_client
    owns_github_redelivery_client = False
    if app_settings.github_redelivery_enabled and app_github_redelivery_client is None:
        if app_settings.github_repository_webhook_write_token is None:
            raise ValueError("GITHUB_REPOSITORY_WEBHOOK_WRITE_TOKEN is required when redelivery is enabled")
        app_github_redelivery_client = GitHubRepositoryWebhookRedeliveryClient(
            token=app_settings.github_repository_webhook_write_token.get_secret_value(),
            timeout_seconds=app_settings.github_api_timeout_seconds,
        )
        owns_github_redelivery_client = True
    app_github_redelivery_service = GitHubRedeliveryService(
        enabled=app_settings.github_redelivery_enabled,
        reconciliation_service=app_github_reconciliation_service,
        github_redelivery_client=app_github_redelivery_client,
        recovery_action_store=app_recovery_action_store,
    )
    app_management_identity_http_client = management_identity_http_client
    owns_management_identity_http_client = False
    app_oidc_authenticator = None
    if app_settings.management_api_enabled and app_settings.management_auth_mode == "oidc_jwt":
        if app_settings.management_oidc_issuer is None or app_settings.management_oidc_audience is None:
            raise ValueError("OIDC management settings are required when MANAGEMENT_AUTH_MODE=oidc_jwt")
        if app_management_identity_http_client is None:
            app_management_identity_http_client = httpx2.AsyncClient(
                timeout=app_settings.management_oidc_http_timeout_seconds,
            )
            owns_management_identity_http_client = True
        app_oidc_authenticator = OidcJwtManagementAuthenticator(
            config=OidcJwtConfig(
                issuer=app_settings.management_oidc_issuer,
                audience=app_settings.management_oidc_audience,
                allowed_algorithms=parse_allowed_jwt_algorithms(app_settings.management_oidc_allowed_algorithms),
            ),
            http_client=app_management_identity_http_client,
        )
    app_management_scope_policy = ManagementScopePolicy(
        full_management_scope=app_settings.management_oidc_full_management_scope,
        diagnostics_read_scope=app_settings.management_oidc_diagnostics_read_scope,
        recovery_read_scope=app_settings.management_oidc_recovery_read_scope,
        recovery_execute_scope=app_settings.management_oidc_recovery_execute_scope,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await run_in_threadpool(runtime_resources.readiness_check)
            yield
        finally:
            if owns_github_redelivery_client and app_github_redelivery_client is not None:
                await app_github_redelivery_client.aclose()
            if owns_github_delivery_client and app_github_delivery_client is not None:
                await app_github_delivery_client.aclose()
            if owns_management_identity_http_client and app_management_identity_http_client is not None:
                await app_management_identity_http_client.aclose()
            if runtime_resources.owns_engine and runtime_resources.engine is not None:
                await run_in_threadpool(runtime_resources.engine.dispose)

    app = FastAPI(title="GitHub Webhook Monitor", version="0.1.0", lifespan=lifespan)
    app.state.settings = app_settings
    app.state.delivery_store = app_delivery_store
    app.state.recovery_action_store = app_recovery_action_store
    app.state.runtime_resources = runtime_resources
    app.state.webhook_service = app_webhook_service
    app.state.github_delivery_client = app_github_delivery_client
    app.state.github_reconciliation_service = app_github_reconciliation_service
    app.state.github_redelivery_client = app_github_redelivery_client
    app.state.github_redelivery_service = app_github_redelivery_service
    app.state.management_identity_http_client = app_management_identity_http_client
    app.state.oidc_management_authenticator = app_oidc_authenticator
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
            management_auth_mode=app_settings.management_auth_mode,
            management_scope_policy=app_management_scope_policy,
            oidc_management_authenticator=app_oidc_authenticator,
            github_reconciliation_service=app_github_reconciliation_service,
            github_redelivery_service=app_github_redelivery_service,
            recovery_action_query_service=RecoveryActionQueryService(app_recovery_action_store),
        )
    )
    return app
