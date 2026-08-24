from dataclasses import dataclass
from uuid import UUID

from app.domain.deliveries import DeliveryAttempt
from app.integrations.github.client import (
    GitHubRedeliveryOutcomeUnknownError,
    GitHubRepositoryWebhookRedeliveryClient,
    GitHubUpstreamProtocolError,
    GitHubUpstreamUnavailableError,
)
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


@dataclass(frozen=True)
class GitHubRedeliveryResult:
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
    ):
        self.enabled = enabled
        self._reconciliation_service = reconciliation_service
        self._github_redelivery_client = github_redelivery_client

    def get_local_attempt(self, attempt_id: UUID) -> DeliveryAttempt | None:
        return self._reconciliation_service.get_local_attempt(attempt_id)

    async def request_redelivery(
        self,
        *,
        attempt: DeliveryAttempt,
        github_delivery_id: int,
    ) -> GitHubRedeliveryResult:
        if not self.enabled or self._github_redelivery_client is None:
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

        await self._github_redelivery_client.request_repository_webhook_redelivery(
            owner=owner,
            repository=repository,
            hook_id=attempt.delivery_identity.hook_id,
            github_delivery_id=github_delivery_id,
        )
        return GitHubRedeliveryResult(attempt=attempt, github_delivery_id=github_delivery_id)


__all__ = [
    "GitHubRedeliveryDisabledError",
    "GitHubRedeliveryOutcomeUnknownError",
    "GitHubRedeliveryResult",
    "GitHubRedeliveryService",
    "GitHubRedeliveryTargetNotFoundError",
    "GitHubUpstreamProtocolError",
    "GitHubUpstreamUnavailableError",
    "UnsupportedGitHubReconciliationTargetError",
    "UnverifiedGitHubRedeliveryTargetError",
]
