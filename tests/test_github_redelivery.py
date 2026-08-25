from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.domain.deliveries import DeliveryAttempt, GitHubDeliveryIdentity
from app.domain.management import (
    AUTHORIZATION_METHOD_SHARED_MANAGEMENT_TOKEN,
    MANAGEMENT_CAPABILITY_RECOVERY_EXECUTE,
    SHARED_TOKEN_PRINCIPAL,
    ManagementAuthorization,
)
from app.integrations.github.models import GitHubDeliveryPage, GitHubDeliverySummary
from app.services.delivery_queries import DeliveryQueryService
from app.services.github_reconciliation import (
    GitHubReconciliationService,
    UnsupportedGitHubReconciliationTargetError,
)
from app.services.github_redelivery import (
    GitHubRedeliveryJournalUnavailableError,
    GitHubRedeliveryService,
    UnverifiedGitHubRedeliveryTargetError,
)
from app.storage.deliveries import InMemoryDeliveryStore
from app.storage.recovery_actions import InMemoryRecoveryActionStore, RecoveryActionStoreError


ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000001")


SHARED_EXECUTE_AUTHORIZATION = ManagementAuthorization(
    principal=SHARED_TOKEN_PRINCIPAL,
    capability=MANAGEMENT_CAPABILITY_RECOVERY_EXECUTE,
    authorization_method=AUTHORIZATION_METHOD_SHARED_MANAGEMENT_TOKEN,
    matched_scope=None,
)


def make_attempt(
    *,
    attempt_id: UUID = ATTEMPT_ID,
    delivery_guid: str = "guid-001",
    repository: str | None = "octo/example",
    installation_target_type: str | None = "repository",
) -> DeliveryAttempt:
    return DeliveryAttempt(
        attempt_id=attempt_id,
        delivery_identity=GitHubDeliveryIdentity(delivery_guid=delivery_guid, hook_id=12345),
        received_at=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        payload_sha256="a" * 64,
        event_type="pull_request",
        action="opened",
        repository=repository,
        sender="octocat",
        installation_target_id="111",
        installation_target_type=installation_target_type,
    )


def make_delivery(
    *,
    github_delivery_id: int,
    delivery_guid: str = "guid-001",
    redelivery: bool = False,
) -> GitHubDeliverySummary:
    return GitHubDeliverySummary(
        github_delivery_id=github_delivery_id,
        delivery_guid=delivery_guid,
        delivered_at=datetime(2026, 8, 24, 12, 1, tzinfo=timezone.utc),
        redelivery=redelivery,
        duration=0.2,
        status="OK",
        status_code=200,
        event="pull_request",
        action="opened",
        installation_id=111,
        repository_id=222,
        throttled_at=None,
    )


class RecordingGitHubDeliveryClient:
    def __init__(self, pages: list[GitHubDeliveryPage]):
        self.pages = list(pages)
        self.calls: list[dict[str, object]] = []

    async def list_repository_webhook_deliveries(self, *, owner, repository, hook_id, cursor=None):
        self.calls.append(
            {
                "owner": owner,
                "repository": repository,
                "hook_id": hook_id,
                "cursor": cursor,
            }
        )
        return self.pages.pop(0)


class RecordingGitHubRedeliveryClient:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    async def request_repository_webhook_redelivery(self, *, owner, repository, hook_id, github_delivery_id):
        self.calls.append(
            {
                "owner": owner,
                "repository": repository,
                "hook_id": hook_id,
                "github_delivery_id": github_delivery_id,
            }
        )


class FailingRecoveryActionStore(InMemoryRecoveryActionStore):
    def create_initiated_github_redelivery(self, **kwargs):
        raise RecoveryActionStoreError("synthetic journal outage")


class FailingFinalizeRecoveryActionStore(InMemoryRecoveryActionStore):
    def finalize(self, **kwargs):
        raise RecoveryActionStoreError("synthetic journal outage")


def service_with_store(
    store: InMemoryDeliveryStore,
    github_delivery_client,
    github_redelivery_client,
    *,
    max_pages: int = 5,
    recovery_action_store=None,
) -> GitHubRedeliveryService:
    reconciliation_service = GitHubReconciliationService(
        enabled=True,
        delivery_query_service=DeliveryQueryService(store),
        github_client=github_delivery_client,
        max_pages=max_pages,
    )
    return GitHubRedeliveryService(
        enabled=True,
        reconciliation_service=reconciliation_service,
        github_redelivery_client=github_redelivery_client,
        recovery_action_store=recovery_action_store or InMemoryRecoveryActionStore(max_actions=10),
    )


@pytest.mark.anyio
async def test_missing_local_attempt_does_not_call_upstream():
    store = InMemoryDeliveryStore(max_events=10)
    github_delivery_client = RecordingGitHubDeliveryClient([])
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    service = service_with_store(store, github_delivery_client, github_redelivery_client)

    assert service.get_local_attempt(ATTEMPT_ID) is None
    assert github_delivery_client.calls == []
    assert github_redelivery_client.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "attempt",
    [
        make_attempt(repository=None),
        make_attempt(repository="octo"),
        make_attempt(repository="octo/example/extra"),
        make_attempt(installation_target_type="organization"),
    ],
)
async def test_unsupported_repository_target_fails_before_upstream_calls(attempt):
    store = InMemoryDeliveryStore(max_events=10)
    store.add(attempt)
    github_delivery_client = RecordingGitHubDeliveryClient([])
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    service = service_with_store(store, github_delivery_client, github_redelivery_client)

    with pytest.raises(UnsupportedGitHubReconciliationTargetError):
        await service.request_redelivery(
            attempt=attempt,
            github_delivery_id=100,
            authorization=SHARED_EXECUTE_AUTHORIZATION,
        )

    assert github_delivery_client.calls == []
    assert github_redelivery_client.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "page",
    [
        GitHubDeliveryPage(deliveries=[make_delivery(github_delivery_id=101)], next_cursor=None),
        GitHubDeliveryPage(deliveries=[make_delivery(github_delivery_id=100, delivery_guid="other")], next_cursor=None),
        GitHubDeliveryPage(deliveries=[make_delivery(github_delivery_id=101)], next_cursor="more-history"),
    ],
)
async def test_unverified_github_delivery_id_does_not_mutate(page):
    attempt = make_attempt()
    store = InMemoryDeliveryStore(max_events=10)
    store.add(attempt)
    github_delivery_client = RecordingGitHubDeliveryClient([page])
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    service = service_with_store(store, github_delivery_client, github_redelivery_client, max_pages=1)

    with pytest.raises(UnverifiedGitHubRedeliveryTargetError):
        await service.request_redelivery(
            attempt=attempt,
            github_delivery_id=100,
            authorization=SHARED_EXECUTE_AUTHORIZATION,
        )

    assert len(github_delivery_client.calls) == 1
    assert github_redelivery_client.calls == []


@pytest.mark.anyio
async def test_verified_upstream_target_requests_one_redelivery():
    attempt = make_attempt()
    store = InMemoryDeliveryStore(max_events=10)
    store.add(attempt)
    github_delivery_client = RecordingGitHubDeliveryClient(
        [GitHubDeliveryPage(deliveries=[make_delivery(github_delivery_id=100)], next_cursor=None)]
    )
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    service = service_with_store(store, github_delivery_client, github_redelivery_client)

    result = await service.request_redelivery(
        attempt=attempt,
        github_delivery_id=100,
        authorization=SHARED_EXECUTE_AUTHORIZATION,
    )

    assert result.status == "accepted"
    assert result.action_id
    assert result.attempt is attempt
    assert result.github_delivery_id == 100
    assert github_delivery_client.calls == [
        {"owner": "octo", "repository": "example", "hook_id": 12345, "cursor": None}
    ]
    assert github_redelivery_client.calls == [
        {"owner": "octo", "repository": "example", "hook_id": 12345, "github_delivery_id": 100}
    ]
    assert len(store.list_recent()) == 1


@pytest.mark.anyio
async def test_journal_create_failure_prevents_mutation():
    attempt = make_attempt()
    store = InMemoryDeliveryStore(max_events=10)
    store.add(attempt)
    github_delivery_client = RecordingGitHubDeliveryClient(
        [GitHubDeliveryPage(deliveries=[make_delivery(github_delivery_id=100)], next_cursor=None)]
    )
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    service = service_with_store(
        store,
        github_delivery_client,
        github_redelivery_client,
        recovery_action_store=FailingRecoveryActionStore(max_actions=10),
    )

    with pytest.raises(GitHubRedeliveryJournalUnavailableError):
        await service.request_redelivery(
            attempt=attempt,
            github_delivery_id=100,
            authorization=SHARED_EXECUTE_AUTHORIZATION,
        )

    assert len(github_delivery_client.calls) == 1
    assert github_redelivery_client.calls == []


@pytest.mark.anyio
async def test_journal_finalize_failure_does_not_repeat_mutation():
    attempt = make_attempt()
    store = InMemoryDeliveryStore(max_events=10)
    store.add(attempt)
    github_delivery_client = RecordingGitHubDeliveryClient(
        [GitHubDeliveryPage(deliveries=[make_delivery(github_delivery_id=100)], next_cursor=None)]
    )
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    recovery_action_store = FailingFinalizeRecoveryActionStore(max_actions=10)
    service = service_with_store(
        store,
        github_delivery_client,
        github_redelivery_client,
        recovery_action_store=recovery_action_store,
    )

    with pytest.raises(GitHubRedeliveryJournalUnavailableError):
        await service.request_redelivery(
            attempt=attempt,
            github_delivery_id=100,
            authorization=SHARED_EXECUTE_AUTHORIZATION,
        )

    assert github_redelivery_client.calls == [
        {"owner": "octo", "repository": "example", "hook_id": 12345, "github_delivery_id": 100}
    ]
    assert len(recovery_action_store.list_recent(limit=10)) == 1


@pytest.mark.anyio
async def test_multiple_same_guid_records_mutate_exact_requested_id():
    attempt = make_attempt()
    store = InMemoryDeliveryStore(max_events=10)
    store.add(attempt)
    github_delivery_client = RecordingGitHubDeliveryClient(
        [
            GitHubDeliveryPage(
                deliveries=[
                    make_delivery(github_delivery_id=100, redelivery=False),
                    make_delivery(github_delivery_id=101, redelivery=True),
                ],
                next_cursor=None,
            )
        ]
    )
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    service = service_with_store(store, github_delivery_client, github_redelivery_client)

    result = await service.request_redelivery(
        attempt=attempt,
        github_delivery_id=101,
        authorization=SHARED_EXECUTE_AUTHORIZATION,
    )

    assert result.github_delivery_id == 101
    assert github_redelivery_client.calls == [
        {"owner": "octo", "repository": "example", "hook_id": 12345, "github_delivery_id": 101}
    ]
