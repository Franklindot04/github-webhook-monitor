import hashlib
import hmac

from fastapi.testclient import TestClient

from app.config import Settings
from app.factory import create_app
from app.storage.events import InMemoryEventStore


def signature_for(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def test_independently_constructed_apps_do_not_share_events():
    secret = "synthetic-secret"
    first_client = TestClient(create_app(settings=Settings(webhook_secret=secret, _env_file=None)))
    second_client = TestClient(create_app(settings=Settings(webhook_secret=secret, _env_file=None)))
    payload = b'{"action":"opened"}'

    response = first_client.post(
        "/webhook/github",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-001",
            "X-GitHub-Hook-ID": "12345",
            "X-Hub-Signature-256": signature_for(payload, secret),
        },
    )

    assert response.status_code == 200
    assert first_client.get("/events").json()["count"] == 1
    assert second_client.get("/events").json() == {"count": 0, "events": []}


def test_create_app_accepts_explicit_settings_instance():
    settings = Settings(webhook_secret="synthetic-secret", max_events=3, _env_file=None)

    app = create_app(settings=settings)

    assert app.state.settings is settings


def test_create_app_uses_custom_settings_capacity():
    settings = Settings(webhook_secret="synthetic-secret", max_events=2, _env_file=None)
    client = TestClient(create_app(settings=settings))

    for index in range(3):
        payload = f'{{"action":"event-{index}"}}'.encode("utf-8")
        response = client.post(
            "/webhook/github",
            content=payload,
            headers={
                "X-GitHub-Delivery": f"delivery-{index}",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Hook-ID": "12345",
                "Content-Type": "application/json",
                "X-Hub-Signature-256": signature_for(payload, "synthetic-secret"),
            },
        )
        assert response.status_code == 200

    events = client.get("/events").json()["events"]
    assert len(events) == 2
    assert [event["delivery_id"] for event in events] == ["delivery-2", "delivery-1"]


def test_injected_event_store_is_used_for_ingestion_and_listing():
    secret = "synthetic-secret"
    event_store = InMemoryEventStore(max_events=10)
    settings = Settings(webhook_secret=secret, _env_file=None)
    client = TestClient(create_app(settings=settings, event_store=event_store))
    payload = b'{"action":"opened","repository":{"full_name":"octo/example"}}'

    response = client.post(
        "/webhook/github",
        content=payload,
        headers={
            "X-GitHub-Delivery": "delivery-001",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Hook-ID": "12345",
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature_for(payload, secret),
        },
    )

    assert response.status_code == 200
    stored_events = [event.to_dict() for event in event_store.list_recent()]
    assert stored_events == [response.json()["event"]]
    assert client.get("/events").json() == {"count": 1, "events": stored_events}
