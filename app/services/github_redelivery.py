from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from app.domain.recovery_actions import (
    RECOVERY_ACTION_STATE_ACCEPTED,
    RECOVERY_ACTION_STATE_FAILED,
    RECOVERY_ACTION_STATE_OUTCOME_UNKNOWN,
)
from app.domain.deliveries import DeliveryAttempt
from app.domain.management import (
    MANAGEMENT_CAPABILITY_RECOVERY_EXECUTE,
    ManagementAuthorization,
)
from app.integrations.github.client import (
    GitHubRedeliveryOutcomeUnknownError,
    GitHubRepositoryWebhookRedeliveryClient,
    GitHubUpstreamProtocolError,
    GitHubUpstreamUnavailableError,
)
from app.storage.recovery_actions import RecoveryActionStore, RecoveryActionStoreError
from app.services.github_reconciliation import (
    GitHubReconciliationService,
    UnsupportedGitHubReconciliationTargetError,
    repository_coordinates,
)


class GitHubRedeliveryDisabledError(Exception):
    pass


class GitHubRedeliveryTargetNotFoundError(Exception):
    pass


class UnverifiedGitHubRedeliveryTargetError(Exception):
    pass


class GitHubRedeliveryJournalUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class GitHubRedeliveryResult:
    action_id: UUID
    attempt: DeliveryAttempt
    github_delivery_id: int
    status: str = "accepted"


class GitHubRedeliveryService:
    def __init__(
        self,
        *,
        enabled: bool,
        reconciliation_service: GitHubReconciliationService,
        github_redelivery_client: GitHubRepositoryWebhookRedeliveryClient | None,
        recovery_action_store: RecoveryActionStore,
    ):
        self.enabled = enabled
        self._reconciliation_service = reconciliation_service
        self._github_redelivery_client = github_redelivery_client
        self._recovery_action_store = recovery_action_store

    def get_local_attempt(self, attempt_id: UUID) -> DeliveryAttempt | None:
        return self._reconciliation_service.get_local_attempt(attempt_id)

    async def request_redelivery(
        self,
        *,
        attempt: DeliveryAttempt,
        github_delivery_id: int,
        authorization: ManagementAuthorization,
    ) -> GitHubRedeliveryResult:
        if not self.enabled or self._github_redelivery_client is None:
            raise GitHubRedeliveryDisabledError
        if authorization.capability != MANAGEMENT_CAPABILITY_RECOVERY_EXECUTE:
            raise GitHubRedeliveryDisabledError

        owner, repository = repository_coordinates(attempt)
        reconciliation_result = await self._reconciliation_service.reconcile(attempt=attempt, cursor=None)
        verified_match = next(
            (
                delivery
                for delivery in reconciliation_result.matches
                if delivery.github_delivery_id == github_delivery_id
            ),
            None,
        )
        if verified_match is None:
            raise UnverifiedGitHubRedeliveryTargetError

        try:
            recovery_action = self._recovery_action_store.create_initiated_github_redelivery(
                attempt=attempt,
                repository=f"{owner}/{repository}",
                github_delivery_id=github_delivery_id,
                requested_at=datetime.now(timezone.utc),
                principal=authorization.principal,
                authorization=authorization,
            )
        except RecoveryActionStoreError as exc:
            raise GitHubRedeliveryJournalUnavailableError("Recovery action journal unavailable") from exc

        try:
            await self._github_redelivery_client.request_repository_webhook_redelivery(
                owner=owner,
                repository=repository,
                hook_id=attempt.delivery_identity.hook_id,
                github_delivery_id=github_delivery_id,
            )
        except GitHubRedeliveryOutcomeUnknownError as exc:
            self._finalize_action(
                action_id=recovery_action.action_id,
                state=RECOVERY_ACTION_STATE_OUTCOME_UNKNOWN,
                failure_category="outcome_unknown",
            )
            raise
        except GitHubUpstreamProtocolError as exc:
            self._finalize_action(
                action_id=recovery_action.action_id,
                state=RECOVERY_ACTION_STATE_FAILED,
                upstream_status_code=exc.status_code,
                failure_category=exc.failure_category or "upstream_protocol",
            )
            raise
        except GitHubUpstreamUnavailableError as exc:
            self._finalize_action(
                action_id=recovery_action.action_id,
                state=RECOVERY_ACTION_STATE_FAILED,
                upstream_status_code=exc.status_code,
                failure_category=exc.failure_category or "upstream_unavailable",
            )
            raise

        self._finalize_action(
            action_id=recovery_action.action_id,
            state=RECOVERY_ACTION_STATE_ACCEPTED,
            upstream_status_code=202,
        )
        return GitHubRedeliveryResult(
            action_id=recovery_action.action_id,
            attempt=attempt,
            github_delivery_id=github_delivery_id,
        )

    def _finalize_action(
        self,
        *,
        action_id: UUID,
        state: str,
        upstream_status_code: int | None = None,
        failure_category: str | None = None,
    ) -> None:
        try:
            self._recovery_action_store.finalize(
                action_id=action_id,
                state=state,
                completed_at=datetime.now(timezone.utc),
                upstream_status_code=upstream_status_code,
                failure_category=failure_category,
            )
        except RecoveryActionStoreError as exc:
            raise GitHubRedeliveryJournalUnavailableError("Recovery action journal unavailable") from exc


__all__ = [
    "GitHubRedeliveryDisabledError",
    "GitHubRedeliveryJournalUnavailableError",
    "GitHubRedeliveryOutcomeUnknownError",
    "GitHubRedeliveryResult",
    "GitHubRedeliveryService",
    "GitHubRedeliveryTargetNotFoundError",
    "GitHubUpstreamProtocolError",
    "GitHubUpstreamUnavailableError",
    "UnsupportedGitHubReconciliationTargetError",
    "UnverifiedGitHubRedeliveryTargetError",
]
