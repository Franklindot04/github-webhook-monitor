from datetime import datetime, timezone

import httpx2
import pytest

from app.integrations.github.client import (
    GITHUB_API_VERSION,
    GitHubRedeliveryOutcomeUnknownError,
    GitHubRepositoryWebhookDeliveriesClient,
    GitHubRepositoryWebhookRedeliveryClient,
    GitHubUpstreamProtocolError,
    GitHubUpstreamUnavailableError,
    extract_next_cursor,
)


TOKEN = "synthetic-github-token"


def delivery_payload(
    *,
    delivery_id: int = 123,
    guid: str = "guid-001",
    redelivery: bool = False,
) -> dict[str, object]:
    return {
        "id": delivery_id,
        "guid": guid,
        "delivered_at": "2026-08-24T12:00:00Z",
        "redelivery": redelivery,
        "duration": 0.42,
        "status": "OK",
        "status_code": 200,
        "event": "pull_request",
        "action": "opened",
        "installation_id": 111,
        "repository_id": 222,
        "throttled_at": None,
    }


@pytest.mark.anyio
async def test_client_sends_repository_webhook_delivery_request_headers_and_query():
    seen_requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_requests.append(request)
        return httpx2.Response(200, json=[delivery_payload()])

    client = GitHubRepositoryWebhookDeliveriesClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://api.github.com",
        ),
    )

    page = await client.list_repository_webhook_deliveries(
        owner="octo owner",
        repository="repo/name",
        hook_id=12345,
        cursor="upstream-cursor",
    )

    request = seen_requests[0]
    assert request.method == "GET"
    assert str(request.url).startswith(
        "https://api.github.com/repos/octo%20owner/repo%2Fname/hooks/12345/deliveries?"
    )
    assert request.url.params["per_page"] == "100"
    assert request.url.params["cursor"] == "upstream-cursor"
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == GITHUB_API_VERSION
    assert request.headers["authorization"].startswith("Bearer ")
    assert TOKEN not in request.headers["user-agent"]
    assert page.deliveries[0].github_delivery_id == 123
    assert page.deliveries[0].delivered_at == datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


@pytest.mark.anyio
async def test_client_extracts_next_cursor_without_following_link_url():
    link = (
        '<https://api.github.com/repos/octo/example/hooks/123/deliveries?cursor=next-cursor>; '
        'rel="next"'
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=[delivery_payload()], headers={"Link": link})

    client = GitHubRepositoryWebhookDeliveriesClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://api.github.com",
        ),
    )

    page = await client.list_repository_webhook_deliveries(owner="octo", repository="example", hook_id=123)

    assert page.next_cursor == "next-cursor"


def test_link_parser_ignores_arbitrary_hosts():
    link = '<https://evil.example/repos/octo/example/hooks/123/deliveries?cursor=next>; rel="next"'

    assert extract_next_cursor(link, expected_path="/repos/octo/example/hooks/123/deliveries") is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        {"X-RateLimit-Remaining": "0"},
        {"Retry-After": "60"},
    ],
)
async def test_client_maps_identifiable_403_rate_limits_to_unavailable(headers):
    seen_requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_requests.append(request)
        return httpx2.Response(403, json={"message": "hidden"}, headers=headers)

    client = GitHubRepositoryWebhookDeliveriesClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://api.github.com",
        ),
    )

    with pytest.raises(GitHubUpstreamUnavailableError):
        await client.list_repository_webhook_deliveries(owner="octo", repository="example", hook_id=123)

    assert len(seen_requests) == 1


@pytest.mark.anyio
async def test_client_maps_ordinary_403_to_protocol_failure():
    seen_requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_requests.append(request)
        return httpx2.Response(403, json={"message": "hidden"})

    client = GitHubRepositoryWebhookDeliveriesClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://api.github.com",
        ),
    )

    with pytest.raises(GitHubUpstreamProtocolError):
        await client.list_repository_webhook_deliveries(owner="octo", repository="example", hook_id=123)

    assert len(seen_requests) == 1


@pytest.mark.anyio
async def test_client_maps_malformed_rate_limit_header_to_controlled_protocol_failure():
    client = GitHubRepositoryWebhookDeliveriesClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(
                    403,
                    json={"message": "hidden"},
                    headers={"X-RateLimit-Remaining": "not-a-number"},
                )
            ),
            base_url="https://api.github.com",
        ),
    )

    with pytest.raises(GitHubUpstreamProtocolError):
        await client.list_repository_webhook_deliveries(owner="octo", repository="example", hook_id=123)


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [401, 404, 422])
async def test_client_maps_upstream_auth_or_protocol_failures(status_code):
    client = GitHubRepositoryWebhookDeliveriesClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda request: httpx2.Response(status_code, json={"message": "hidden"})),
            base_url="https://api.github.com",
        ),
    )

    with pytest.raises(GitHubUpstreamProtocolError):
        await client.list_repository_webhook_deliveries(owner="octo", repository="example", hook_id=123)


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_client_maps_unavailable_failures(status_code):
    seen_requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_requests.append(request)
        return httpx2.Response(status_code, json={"message": "hidden"})

    client = GitHubRepositoryWebhookDeliveriesClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://api.github.com",
        ),
    )

    with pytest.raises(GitHubUpstreamUnavailableError):
        await client.list_repository_webhook_deliveries(owner="octo", repository="example", hook_id=123)

    assert len(seen_requests) == 1


@pytest.mark.anyio
async def test_client_maps_timeout_or_network_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("synthetic network failure")

    client = GitHubRepositoryWebhookDeliveriesClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://api.github.com",
        ),
    )

    with pytest.raises(GitHubUpstreamUnavailableError):
        await client.list_repository_webhook_deliveries(owner="octo", repository="example", hook_id=123)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {"not": "a list"},
        [{"id": "not-int", "guid": "guid", "delivered_at": "2026-08-24T12:00:00Z", "redelivery": False}],
        [{"id": 123, "guid": "guid", "delivered_at": "not-a-date", "redelivery": False}],
    ],
)
async def test_client_rejects_malformed_success_payload(payload):
    client = GitHubRepositoryWebhookDeliveriesClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json=payload)),
            base_url="https://api.github.com",
        ),
    )

    with pytest.raises(GitHubUpstreamProtocolError):
        await client.list_repository_webhook_deliveries(owner="octo", repository="example", hook_id=123)


@pytest.mark.anyio
async def test_redelivery_client_sends_repository_webhook_redelivery_request():
    seen_requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_requests.append(request)
        return httpx2.Response(202)

    client = GitHubRepositoryWebhookRedeliveryClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://api.github.com",
        ),
    )

    await client.request_repository_webhook_redelivery(
        owner="octo owner",
        repository="repo/name",
        hook_id=12345,
        github_delivery_id=98765,
    )

    request = seen_requests[0]
    assert request.method == "POST"
    assert str(request.url) == (
        "https://api.github.com/repos/octo%20owner/repo%2Fname/hooks/12345/deliveries/98765/attempts"
    )
    assert request.content == b""
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == GITHUB_API_VERSION
    assert request.headers["authorization"].startswith("Bearer ")
    assert TOKEN not in request.headers["user-agent"]
    assert len(seen_requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [401, 403, 404, 422])
async def test_redelivery_client_maps_upstream_auth_or_protocol_failures(status_code):
    client = GitHubRepositoryWebhookRedeliveryClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(status_code, json={"message": "hidden"})
            ),
            base_url="https://api.github.com",
        ),
    )

    with pytest.raises(GitHubUpstreamProtocolError):
        await client.request_repository_webhook_redelivery(
            owner="octo",
            repository="example",
            hook_id=123,
            github_delivery_id=456,
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "headers",
    [
        {"X-RateLimit-Remaining": "0"},
        {"Retry-After": "60"},
    ],
)
async def test_redelivery_client_maps_identifiable_403_rate_limits_to_unavailable(headers):
    seen_requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_requests.append(request)
        return httpx2.Response(403, json={"message": "hidden"}, headers=headers)

    client = GitHubRepositoryWebhookRedeliveryClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://api.github.com",
        ),
    )

    with pytest.raises(GitHubUpstreamUnavailableError):
        await client.request_repository_webhook_redelivery(
            owner="octo",
            repository="example",
            hook_id=123,
            github_delivery_id=456,
        )

    assert len(seen_requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_redelivery_client_maps_unavailable_failures(status_code):
    seen_requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_requests.append(request)
        return httpx2.Response(status_code, json={"message": "hidden"})

    client = GitHubRepositoryWebhookRedeliveryClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://api.github.com",
        ),
    )

    with pytest.raises(GitHubUpstreamUnavailableError):
        await client.request_repository_webhook_redelivery(
            owner="octo",
            repository="example",
            hook_id=123,
            github_delivery_id=456,
        )

    assert len(seen_requests) == 1


@pytest.mark.anyio
async def test_redelivery_client_maps_read_timeout_to_unknown_outcome_without_retry():
    seen_requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_requests.append(request)
        raise httpx2.ReadTimeout("synthetic ambiguous timeout", request=request)

    client = GitHubRepositoryWebhookRedeliveryClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://api.github.com",
        ),
    )

    with pytest.raises(GitHubRedeliveryOutcomeUnknownError):
        await client.request_repository_webhook_redelivery(
            owner="octo",
            repository="example",
            hook_id=123,
            github_delivery_id=456,
        )

    assert len(seen_requests) == 1


@pytest.mark.anyio
async def test_redelivery_client_maps_connect_error_to_unavailable_without_retry():
    seen_requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_requests.append(request)
        raise httpx2.ConnectError("synthetic pre-connect failure", request=request)

    client = GitHubRepositoryWebhookRedeliveryClient(
        token=TOKEN,
        timeout_seconds=5,
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            base_url="https://api.github.com",
        ),
    )

    with pytest.raises(GitHubUpstreamUnavailableError):
        await client.request_repository_webhook_redelivery(
            owner="octo",
            repository="example",
            hook_id=123,
            github_delivery_id=456,
        )

    assert len(seen_requests) == 1
