from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from starlette.concurrency import run_in_threadpool

from app.api.v1.models import (
    DeliveryAttemptResponse,
    DeliveryAttemptsListResponse,
    GitHubDeliveriesReconciliationResponse,
    GitHubDeliverySummaryResponse,
    GitHubRedeliveryResponse,
    RecoveryActionResponse,
    RecoveryActionsListResponse,
)
from app.domain.deliveries import DeliveryAttempt
from app.domain.recovery_actions import RecoveryAction
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
from app.services.github_redelivery import (
    GitHubRedeliveryDisabledError,
    GitHubRedeliveryJournalUnavailableError,
    GitHubRedeliveryOutcomeUnknownError,
    GitHubRedeliveryService,
    UnverifiedGitHubRedeliveryTargetError,
)
from app.services.recovery_actions import (
    InvalidRecoveryActionsCursorError,
    InvalidRecoveryActionsLimitError,
    RecoveryActionQueryService,
    parse_recovery_actions_limit,
)
from app.storage.deliveries import DeliveryStoreError
from app.storage.recovery_actions import RecoveryActionStoreError


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


def recovery_action_to_response(action: RecoveryAction) -> RecoveryActionResponse:
    return RecoveryActionResponse(
        action_id=action.action_id,
        action_type=action.action_type,
        requested_at=action.requested_at,
        completed_at=action.completed_at,
        attempt_id=action.attempt_id,
        delivery_guid=action.delivery_guid,
        hook_id=action.hook_id,
        repository=action.repository,
        github_delivery_id=action.github_delivery_id,
        authentication_method=action.authentication_method,
        state=action.state,
        upstream_status_code=action.upstream_status_code,
        failure_category=action.failure_category,
    )


def create_delivery_attempts_router(
    query_service: DeliveryQueryService,
    management_access_dependency,
    reconciliation_service: GitHubReconciliationService | None = None,
    redelivery_service: GitHubRedeliveryService | None = None,
    recovery_action_query_service: RecoveryActionQueryService | None = None,
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

    @router.post(
        "/delivery-attempts/{attempt_id}/github-deliveries/{github_delivery_id}/redelivery",
        response_model=GitHubRedeliveryResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="Request GitHub to redeliver one verified repository webhook delivery",
    )
    async def request_github_redelivery(
        attempt_id: str = Path(
            description="Application-owned delivery attempt UUID.",
            json_schema_extra={"format": "uuid"},
        ),
        github_delivery_id: str = Path(
            description="GitHub upstream numeric delivery-history record ID.",
        ),
    ):
        if redelivery_service is None or not redelivery_service.enabled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        try:
            parsed_attempt_id = UUID(attempt_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid attempt_id")
        try:
            parsed_github_delivery_id = int(github_delivery_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid github_delivery_id")
        if parsed_github_delivery_id <= 0:
            raise HTTPException(status_code=422, detail="Invalid github_delivery_id")

        try:
            attempt = await run_in_threadpool(redelivery_service.get_local_attempt, parsed_attempt_id)
        except DeliveryStoreError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable")
        if attempt is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery attempt not found")

        try:
            result = await redelivery_service.request_redelivery(
                attempt=attempt,
                github_delivery_id=parsed_github_delivery_id,
            )
        except GitHubRedeliveryDisabledError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        except GitHubRedeliveryJournalUnavailableError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable")
        except UnsupportedGitHubReconciliationTargetError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Delivery attempt is not eligible for repository webhook redelivery",
            )
        except UnverifiedGitHubRedeliveryTargetError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="GitHub delivery could not be verified for this local attempt",
            )
        except GitHubRedeliveryOutcomeUnknownError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GitHub redelivery submission outcome could not be confirmed; reconcile before retrying",
            )
        except GitHubUpstreamUnavailableError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable")
        except GitHubUpstreamProtocolError:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Upstream service unavailable")

        return GitHubRedeliveryResponse(
            action_id=result.action_id,
            attempt_id=result.attempt.attempt_id,
            delivery_guid=result.attempt.delivery_identity.delivery_guid,
            hook_id=result.attempt.delivery_identity.hook_id,
            github_delivery_id=result.github_delivery_id,
            status=result.status,
        )

    @router.get("/recovery-actions", response_model=RecoveryActionsListResponse)
    async def list_recovery_actions(
        limit: str | None = Query(
            default=None,
            description="Page size as an integer from 1 to 100. Defaults to 50.",
        ),
        cursor: str | None = Query(
            default=None,
            description="Opaque recovery-action pagination cursor returned as next_cursor. Do not parse or construct.",
        ),
    ):
        if recovery_action_query_service is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        try:
            parsed_limit = parse_recovery_actions_limit(limit)
            page = await run_in_threadpool(
                recovery_action_query_service.list_actions,
                limit=parsed_limit,
                cursor=cursor,
            )
        except InvalidRecoveryActionsLimitError:
            raise HTTPException(status_code=422, detail="Invalid limit")
        except InvalidRecoveryActionsCursorError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired cursor")
        except RecoveryActionStoreError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable")

        return RecoveryActionsListResponse(
            items=[recovery_action_to_response(action) for action in page.items],
            next_cursor=page.next_cursor,
        )

    @router.get("/recovery-actions/{action_id}", response_model=RecoveryActionResponse)
    async def get_recovery_action(
        action_id: str = Path(
            description="Application-owned recovery action UUID.",
            json_schema_extra={"format": "uuid"},
        ),
    ):
        if recovery_action_query_service is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        try:
            parsed_action_id = UUID(action_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid action_id")

        try:
            action = await run_in_threadpool(recovery_action_query_service.get_action, parsed_action_id)
        except RecoveryActionStoreError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable")
        if action is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recovery action not found")
        return recovery_action_to_response(action)

    return router
