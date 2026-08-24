from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.domain.deliveries import DeliveryAttempt, GitHubDeliveryIdentity
from app.integrations.github.models import GitHubDeliveryPage, GitHubDeliverySummary
from app.services.delivery_queries import DeliveryQueryService
from app.services.github_reconciliation import (
    GitHubReconciliationService,
    InvalidGitHubReconciliationCursorError,
    UnsupportedGitHubReconciliationTargetError,
    decode_reconciliation_cursor,
    encode_reconciliation_cursor,
)
from app.storage.deliveries import InMemoryDeliveryStore


ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ATTEMPT_ID = UUID("00000000-0000-0000-0000-000000000002")


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


class RecordingGitHubClient:
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


def service_with_store(store: InMemoryDeliveryStore, github_client, *, max_pages: int = 5):
    return GitHubReconciliationService(
        enabled=True,
        delivery_query_service=DeliveryQueryService(store),
        github_client=github_client,
        max_pages=max_pages,
    )


@pytest.mark.anyio
async def test_missing_local_attempt_does_not_call_github():
    store = InMemoryDeliveryStore(max_events=10)
    github_client = RecordingGitHubClient([])
    service = service_with_store(store, github_client)

    assert service.get_local_attempt(ATTEMPT_ID) is None
    assert github_client.calls == []


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
async def test_unsupported_repository_targets_fail_before_github_call(attempt):
    store = InMemoryDeliveryStore(max_events=10)
    store.add(attempt)
    github_client = RecordingGitHubClient([])
    service = service_with_store(store, github_client)

    with pytest.raises(UnsupportedGitHubReconciliationTargetError):
        await service.reconcile(attempt=attempt, cursor=None)

    assert github_client.calls == []


@pytest.mark.anyio
async def test_reconciliation_returns_multiple_same_guid_upstream_records():
    attempt = make_attempt()
    store = InMemoryDeliveryStore(max_events=10)
    store.add(attempt)
    github_client = RecordingGitHubClient(
        [
            GitHubDeliveryPage(
                deliveries=[
                    make_delivery(github_delivery_id=100, redelivery=False),
                    make_delivery(github_delivery_id=101, redelivery=True),
                    make_delivery(github_delivery_id=102, delivery_guid="other-guid"),
                ],
                next_cursor=None,
            )
        ]
    )
    service = service_with_store(store, github_client)

    result = await service.reconcile(attempt=attempt, cursor=None)

    assert [delivery.github_delivery_id for delivery in result.matches] == [100, 101]
    assert [delivery.redelivery for delivery in result.matches] == [False, True]
    assert result.search_complete is True
    assert result.next_cursor is None
    assert github_client.calls == [
        {"owner": "octo", "repository": "example", "hook_id": 12345, "cursor": None}
    ]


@pytest.mark.anyio
async def test_reconciliation_follows_cursor_pages_within_bound():
    attempt = make_attempt()
    store = InMemoryDeliveryStore(max_events=10)
    store.add(attempt)
    github_client = RecordingGitHubClient(
        [
            GitHubDeliveryPage(deliveries=[make_delivery(github_delivery_id=1, delivery_guid="other")], next_cursor="c2"),
            GitHubDeliveryPage(deliveries=[make_delivery(github_delivery_id=2)], next_cursor=None),
        ]
    )
    service = service_with_store(store, github_client)

    result = await service.reconcile(attempt=attempt, cursor=None)

    assert [delivery.github_delivery_id for delivery in result.matches] == [2]
    assert result.search_complete is True
    assert [call["cursor"] for call in github_client.calls] == [None, "c2"]


@pytest.mark.anyio
async def test_bounded_search_returns_attempt_bound_continuation_cursor():
    attempt = make_attempt()
    store = InMemoryDeliveryStore(max_events=10)
    store.add(attempt)
    github_client = RecordingGitHubClient(
        [GitHubDeliveryPage(deliveries=[make_delivery(github_delivery_id=1)], next_cursor="continue-here")]
    )
    service = service_with_store(store, github_client, max_pages=1)

    result = await service.reconcile(attempt=attempt, cursor=None)

    assert [delivery.github_delivery_id for delivery in result.matches] == [1]
    assert result.search_complete is False
    assert result.next_cursor is not None
    assert decode_reconciliation_cursor(result.next_cursor, attempt_id=attempt.attempt_id) == "continue-here"


@pytest.mark.anyio
async def test_continuation_cursor_resumes_without_restarting():
    attempt = make_attempt()
    cursor = encode_reconciliation_cursor(attempt_id=attempt.attempt_id, upstream_cursor="second-page")
    store = InMemoryDeliveryStore(max_events=10)
    store.add(attempt)
    github_client = RecordingGitHubClient(
        [GitHubDeliveryPage(deliveries=[make_delivery(github_delivery_id=2)], next_cursor=None)]
    )
    service = service_with_store(store, github_client, max_pages=5)

    result = await service.reconcile(attempt=attempt, cursor=cursor)

    assert [call["cursor"] for call in github_client.calls] == ["second-page"]
    assert [delivery.github_delivery_id for delivery in result.matches] == [2]


@pytest.mark.anyio
async def test_cursor_bound_to_different_attempt_is_rejected_before_github_call():
    attempt = make_attempt()
    cursor = encode_reconciliation_cursor(attempt_id=OTHER_ATTEMPT_ID, upstream_cursor="second-page")
    github_client = RecordingGitHubClient([])
    service = service_with_store(InMemoryDeliveryStore(max_events=10), github_client)

    with pytest.raises(InvalidGitHubReconciliationCursorError):
        await service.reconcile(attempt=attempt, cursor=cursor)

    assert github_client.calls == []


@pytest.mark.anyio
async def test_completed_search_with_no_matches_returns_empty_matches():
    attempt = make_attempt()
    github_client = RecordingGitHubClient(
        [GitHubDeliveryPage(deliveries=[make_delivery(github_delivery_id=1, delivery_guid="other")], next_cursor=None)]
    )
    service = service_with_store(InMemoryDeliveryStore(max_events=10), github_client)

    result = await service.reconcile(attempt=attempt, cursor=None)

    assert result.matches == []
    assert result.search_complete is True
    assert result.next_cursor is None
