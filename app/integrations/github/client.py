from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx2

from app.integrations.github.models import GitHubDeliveryPage, GitHubDeliverySummary


GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_USER_AGENT = "github-webhook-monitor"
REPOSITORY_DELIVERIES_PER_PAGE = 100


class GitHubUpstreamError(Exception):
    pass


class GitHubUpstreamUnavailableError(GitHubUpstreamError):
    pass


class GitHubUpstreamProtocolError(GitHubUpstreamError):
    pass


class GitHubRepositoryWebhookDeliveriesClient:
    def __init__(
        self,
        *,
        token: str,
        timeout_seconds: int,
        http_client: httpx2.AsyncClient | None = None,
    ):
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx2.AsyncClient(
            base_url=GITHUB_API_BASE_URL,
            timeout=timeout_seconds,
        )
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": GITHUB_USER_AGENT,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def list_repository_webhook_deliveries(
        self,
        *,
        owner: str,
        repository: str,
        hook_id: int,
        cursor: str | None = None,
    ) -> GitHubDeliveryPage:
        path = repository_deliveries_path(owner=owner, repository=repository, hook_id=hook_id)
        params: dict[str, str | int] = {"per_page": REPOSITORY_DELIVERIES_PER_PAGE}
        if cursor is not None:
            params["cursor"] = cursor

        try:
            response = await self._http_client.get(path, params=params, headers=self._headers)
        except (httpx2.TimeoutException, httpx2.NetworkError, httpx2.RequestError) as exc:
            raise GitHubUpstreamUnavailableError("GitHub upstream unavailable") from exc

        if response.status_code == 429 or response.status_code >= 500 or _is_rate_limited(response):
            raise GitHubUpstreamUnavailableError("GitHub upstream unavailable")
        if response.status_code != 200:
            raise GitHubUpstreamProtocolError("GitHub upstream request failed")

        try:
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError
            deliveries = [parse_delivery_summary(item) for item in payload]
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubUpstreamProtocolError("GitHub upstream response was invalid") from exc

        return GitHubDeliveryPage(
            deliveries=deliveries,
            next_cursor=extract_next_cursor(
                response.headers.get("link"),
                expected_path=path,
            ),
        )


def repository_deliveries_path(*, owner: str, repository: str, hook_id: int) -> str:
    return (
        f"/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
        f"/hooks/{hook_id}/deliveries"
    )


def _is_rate_limited(response: httpx2.Response) -> bool:
    if response.status_code != 403:
        return False
    if response.headers.get("retry-after") is not None:
        return True
    return response.headers.get("x-ratelimit-remaining", "").strip() == "0"


def extract_next_cursor(link_header: str | None, *, expected_path: str) -> str | None:
    if not link_header:
        return None
    for link_part in link_header.split(","):
        section, *parameter_parts = link_part.split(";")
        if not any(part.strip() == 'rel="next"' for part in parameter_parts):
            continue
        section = section.strip()
        if not section.startswith("<") or not section.endswith(">"):
            continue
        parsed = urlparse(section[1:-1])
        if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.path != expected_path:
            return None
        cursor_values = parse_qs(parsed.query).get("cursor")
        if not cursor_values:
            return None
        return cursor_values[0]
    return None


def parse_delivery_summary(value: Any) -> GitHubDeliverySummary:
    if not isinstance(value, dict):
        raise ValueError
    return GitHubDeliverySummary(
        github_delivery_id=require_int(value, "id"),
        delivery_guid=require_str(value, "guid"),
        delivered_at=parse_required_datetime(value, "delivered_at"),
        redelivery=require_bool(value, "redelivery"),
        duration=optional_number(value.get("duration")),
        status=optional_str(value.get("status")),
        status_code=optional_int(value.get("status_code")),
        event=optional_str(value.get("event")),
        action=optional_str(value.get("action")),
        installation_id=optional_int(value.get("installation_id")),
        repository_id=optional_int(value.get("repository_id")),
        throttled_at=parse_optional_datetime(value.get("throttled_at")),
    )


def require_int(value: dict[str, Any], key: str) -> int:
    item = value[key]
    if not isinstance(item, int):
        raise ValueError
    return item


def require_str(value: dict[str, Any], key: str) -> str:
    item = value[key]
    if not isinstance(item, str):
        raise ValueError
    return item


def require_bool(value: dict[str, Any], key: str) -> bool:
    item = value[key]
    if not isinstance(item, bool):
        raise ValueError
    return item


def optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError
    return value


def optional_number(value: Any) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float):
        raise ValueError
    return float(value)


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError
    return value


def parse_required_datetime(value: dict[str, Any], key: str) -> datetime:
    return parse_datetime(value[key])


def parse_optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    return parse_datetime(value)


def parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return parsed
