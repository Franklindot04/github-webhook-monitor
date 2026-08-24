import base64
import binascii
from dataclasses import dataclass
import json
import re
from uuid import UUID

from app.domain.deliveries import DeliveryAttempt
from app.integrations.github.client import (
    GitHubRepositoryWebhookDeliveriesClient,
    GitHubUpstreamProtocolError,
    GitHubUpstreamUnavailableError,
)
from app.integrations.github.models import GitHubDeliverySummary
from app.services.delivery_queries import DeliveryQueryService


RECONCILIATION_CURSOR_VERSION = 1
REPOSITORY_FULL_NAME_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")


class GitHubReconciliationDisabledError(Exception):
    pass


class InvalidGitHubReconciliationCursorError(Exception):
    pass


class UnsupportedGitHubReconciliationTargetError(Exception):
    pass


@dataclass(frozen=True)
class GitHubReconciliationResult:
    attempt: DeliveryAttempt
    matches: list[GitHubDeliverySummary]
    search_complete: bool
    next_cursor: str | None


class GitHubReconciliationService:
    def __init__(
        self,
        *,
        enabled: bool,
        delivery_query_service: DeliveryQueryService,
        github_client: GitHubRepositoryWebhookDeliveriesClient | None,
        max_pages: int,
    ):
        self.enabled = enabled
        self._delivery_query_service = delivery_query_service
        self._github_client = github_client
        self._max_pages = max_pages

    def get_local_attempt(self, attempt_id: UUID) -> DeliveryAttempt | None:
        return self._delivery_query_service.get_attempt(attempt_id)

    async def reconcile(
        self,
        *,
        attempt: DeliveryAttempt,
        cursor: str | None,
    ) -> GitHubReconciliationResult:
        if not self.enabled or self._github_client is None:
            raise GitHubReconciliationDisabledError

        owner, repository = repository_coordinates(attempt)
        upstream_cursor = None
        if cursor is not None:
            upstream_cursor = decode_reconciliation_cursor(cursor, attempt_id=attempt.attempt_id)

        matches: list[GitHubDeliverySummary] = []
        pages_fetched = 0
        while pages_fetched < self._max_pages:
            page = await self._github_client.list_repository_webhook_deliveries(
                owner=owner,
                repository=repository,
                hook_id=attempt.delivery_identity.hook_id,
                cursor=upstream_cursor,
            )
            pages_fetched += 1
            matches.extend(
                delivery
                for delivery in page.deliveries
                if delivery.delivery_guid == attempt.delivery_identity.delivery_guid
            )
            if page.next_cursor is None:
                return GitHubReconciliationResult(
                    attempt=attempt,
                    matches=matches,
                    search_complete=True,
                    next_cursor=None,
                )
            upstream_cursor = page.next_cursor

        return GitHubReconciliationResult(
            attempt=attempt,
            matches=matches,
            search_complete=False,
            next_cursor=encode_reconciliation_cursor(
                attempt_id=attempt.attempt_id,
                upstream_cursor=upstream_cursor,
            ),
        )


def repository_coordinates(attempt: DeliveryAttempt) -> tuple[str, str]:
    if attempt.installation_target_type is not None and attempt.installation_target_type != "repository":
        raise UnsupportedGitHubReconciliationTargetError
    if attempt.repository is None or not REPOSITORY_FULL_NAME_PATTERN.fullmatch(attempt.repository):
        raise UnsupportedGitHubReconciliationTargetError
    owner, repository = attempt.repository.split("/", 1)
    return owner, repository


def encode_reconciliation_cursor(*, attempt_id: UUID, upstream_cursor: str) -> str:
    payload = {
        "v": RECONCILIATION_CURSOR_VERSION,
        "attempt_id": str(attempt_id),
        "github_cursor": upstream_cursor,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return encoded.rstrip(b"=").decode("ascii")


def decode_reconciliation_cursor(cursor: str, *, attempt_id: UUID) -> str:
    try:
        padded_cursor = cursor + ("=" * (-len(cursor) % 4))
        decoded = base64.urlsafe_b64decode(padded_cursor.encode("ascii"))
        payload = json.loads(decoded)
        if not isinstance(payload, dict) or payload.get("v") != RECONCILIATION_CURSOR_VERSION:
            raise ValueError
        cursor_attempt_id = UUID(payload["attempt_id"])
        upstream_cursor = payload["github_cursor"]
        if cursor_attempt_id != attempt_id or not isinstance(upstream_cursor, str) or not upstream_cursor:
            raise ValueError
    except (binascii.Error, KeyError, TypeError, UnicodeEncodeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidGitHubReconciliationCursorError("Invalid or expired reconciliation cursor") from exc
    return upstream_cursor


__all__ = [
    "GitHubReconciliationDisabledError",
    "GitHubReconciliationResult",
    "GitHubReconciliationService",
    "GitHubUpstreamProtocolError",
    "GitHubUpstreamUnavailableError",
    "InvalidGitHubReconciliationCursorError",
    "UnsupportedGitHubReconciliationTargetError",
    "decode_reconciliation_cursor",
    "encode_reconciliation_cursor",
]
