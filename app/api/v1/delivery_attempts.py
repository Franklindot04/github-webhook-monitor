from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from starlette.concurrency import run_in_threadpool

from app.api.v1.models import (
    DeliveryAttemptResponse,
    DeliveryAttemptsListResponse,
    GitHubDeliveriesReconciliationResponse,
    GitHubDeliverySummaryResponse,
)
from app.domain.deliveries import DeliveryAttempt
from app.integrations.github.models import GitHubDeliverySummary
from app.services.delivery_queries import (
    DeliveryQueryService,
    InvalidDeliveryAttemptsCursorError,
    InvalidDeliveryAttemptsLimitError,
    parse_delivery_attempts_limit,
)
from app.services.github_reconciliation import (
    GitHubReconciliationDisabledError,
    GitHubReconciliationService,
    GitHubUpstreamProtocolError,
    GitHubUpstreamUnavailableError,
    InvalidGitHubReconciliationCursorError,
    UnsupportedGitHubReconciliationTargetError,
)
from app.storage.deliveries import DeliveryStoreError


def delivery_attempt_to_response(attempt: DeliveryAttempt) -> DeliveryAttemptResponse:
    return DeliveryAttemptResponse(
        attempt_id=attempt.attempt_id,
        delivery_guid=attempt.delivery_identity.delivery_guid,
        hook_id=attempt.delivery_identity.hook_id,
        received_at=attempt.received_at,
        payload_sha256=attempt.payload_sha256,
        event_type=attempt.event_type,
        action=attempt.action,
        repository=attempt.repository,
        sender=attempt.sender,
        installation_target_id=attempt.installation_target_id,
        installation_target_type=attempt.installation_target_type,
    )


def github_delivery_to_response(delivery: GitHubDeliverySummary) -> GitHubDeliverySummaryResponse:
    return GitHubDeliverySummaryResponse(
        github_delivery_id=delivery.github_delivery_id,
        delivery_guid=delivery.delivery_guid,
        delivered_at=delivery.delivered_at,
        redelivery=delivery.redelivery,
        duration=delivery.duration,
        status=delivery.status,
        status_code=delivery.status_code,
        event=delivery.event,
        action=delivery.action,
        installation_id=delivery.installation_id,
        repository_id=delivery.repository_id,
        throttled_at=delivery.throttled_at,
    )


def create_delivery_attempts_router(
    query_service: DeliveryQueryService,
    management_access_dependency,
    reconciliation_service: GitHubReconciliationService | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["management diagnostics"],
        dependencies=[Depends(management_access_dependency)],
    )

    @router.get("/delivery-attempts", response_model=DeliveryAttemptsListResponse)
    async def list_delivery_attempts(
        limit: str | None = Query(
            default=None,
            description="Page size as an integer from 1 to 100. Defaults to 50.",
        ),
        cursor: str | None = Query(
            default=None,
            description="Opaque pagination cursor returned as next_cursor. Do not parse or construct.",
        ),
    ):
        try:
            parsed_limit = parse_delivery_attempts_limit(limit)
            page = await run_in_threadpool(
                query_service.list_attempts,
                limit=parsed_limit,
                cursor=cursor,
            )
        except InvalidDeliveryAttemptsLimitError:
            raise HTTPException(status_code=422, detail="Invalid limit")
        except InvalidDeliveryAttemptsCursorError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired cursor")
        except DeliveryStoreError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable")

        return DeliveryAttemptsListResponse(
            items=[delivery_attempt_to_response(attempt) for attempt in page.items],
            next_cursor=page.next_cursor,
        )

    @router.get("/delivery-attempts/{attempt_id}", response_model=DeliveryAttemptResponse)
    async def get_delivery_attempt(
        attempt_id: str = Path(
            description="Application-owned delivery attempt UUID.",
            json_schema_extra={"format": "uuid"},
        ),
    ):
        try:
            parsed_attempt_id = UUID(attempt_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid attempt_id")

        try:
            attempt = await run_in_threadpool(query_service.get_attempt, parsed_attempt_id)
        except DeliveryStoreError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable")
        if attempt is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery attempt not found")
        return delivery_attempt_to_response(attempt)

    @router.get(
        "/delivery-attempts/{attempt_id}/github-deliveries",
        response_model=GitHubDeliveriesReconciliationResponse,
        summary="Reconcile one local delivery attempt with GitHub repository webhook history",
    )
    async def reconcile_github_deliveries(
        attempt_id: str = Path(
            description="Application-owned delivery attempt UUID.",
            json_schema_extra={"format": "uuid"},
        ),
        cursor: str | None = Query(
            default=None,
            description="Opaque reconciliation continuation cursor bound to this attempt_id.",
        ),
    ):
        if reconciliation_service is None or not reconciliation_service.enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        try:
            parsed_attempt_id = UUID(attempt_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid attempt_id")

        try:
            attempt = await run_in_threadpool(reconciliation_service.get_local_attempt, parsed_attempt_id)
        except DeliveryStoreError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable")
        if attempt is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery attempt not found")

        try:
            result = await reconciliation_service.reconcile(attempt=attempt, cursor=cursor)
        except GitHubReconciliationDisabledError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        except InvalidGitHubReconciliationCursorError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reconciliation cursor",
            )
        except UnsupportedGitHubReconciliationTargetError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Delivery attempt is not eligible for repository webhook reconciliation",
            )
        except GitHubUpstreamUnavailableError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable")
        except GitHubUpstreamProtocolError:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Upstream service unavailable")

        return GitHubDeliveriesReconciliationResponse(
            attempt_id=result.attempt.attempt_id,
            delivery_guid=result.attempt.delivery_identity.delivery_guid,
            hook_id=result.attempt.delivery_identity.hook_id,
            repository=result.attempt.repository or "",
            matches=[github_delivery_to_response(delivery) for delivery in result.matches],
            search_complete=result.search_complete,
            next_cursor=result.next_cursor,
        )

    return router
