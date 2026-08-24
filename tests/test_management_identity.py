import json
from datetime import datetime, timedelta, timezone

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.management import (
    MANAGEMENT_CAPABILITY_DIAGNOSTICS_READ,
    MANAGEMENT_CAPABILITY_RECOVERY_EXECUTE,
    MANAGEMENT_CAPABILITY_RECOVERY_READ,
    ManagementPrincipal,
    ManagementScopePolicy,
)
from app.factory import create_app
from app.security import (
    InsufficientManagementScopeError,
    InvalidManagementTokenError,
    ManagementIdentityProviderUnavailableError,
    OidcJwtConfig,
    OidcJwtManagementAuthenticator,
    authorize_management_capability,
)
from app.storage.deliveries import InMemoryDeliveryStore
from app.storage.recovery_actions import InMemoryRecoveryActionStore


ISSUER = "https://identity.example.com/"
AUDIENCE = "https://github-webhook-monitor.example/"
REQUIRED_SCOPE = "webhook-monitor.manage"
DIAGNOSTICS_READ_SCOPE = "webhook-monitor.diagnostics.read"
RECOVERY_READ_SCOPE = "webhook-monitor.recovery.read"
RECOVERY_EXECUTE_SCOPE = "webhook-monitor.recovery.execute"
TEST_SECRET = "test-webhook-secret"
MANAGEMENT_TOKEN = "synthetic-management-token-000001"
SCOPE_POLICY = ManagementScopePolicy(
    full_management_scope=REQUIRED_SCOPE,
    diagnostics_read_scope=DIAGNOSTICS_READ_SCOPE,
    recovery_read_scope=RECOVERY_READ_SCOPE,
    recovery_execute_scope=RECOVERY_EXECUTE_SCOPE,
)


def make_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk(key, *, kid: str = "key-1", alg: str = "RS256") -> dict[str, object]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "alg": alg, "use": "sig"})
    return jwk


def token_for(
    key,
    *,
    kid: str = "key-1",
    alg: str = "RS256",
    typ: str = "at+jwt",
    issuer: str = ISSUER,
    audience: str | list[str] = AUDIENCE,
    subject: str | None = "principal-001",
    scope: object = REQUIRED_SCOPE,
    client_id: object = "client-001",
    jti: object = "jwt-id-001",
    expires_delta: timedelta = timedelta(minutes=5),
    not_before_delta: timedelta | None = None,
):
    now = datetime.now(timezone.utc)
    claims: dict[str, object] = {
        "iss": issuer,
        "aud": audience,
        "exp": now + expires_delta,
        "iat": now,
    }
    if scope is not None:
        claims["scope"] = scope
    if subject is not None:
        claims["sub"] = subject
    if client_id is not None:
        claims["client_id"] = client_id
    if jti is not None:
        claims["jti"] = jti
    if not_before_delta is not None:
        claims["nbf"] = now + not_before_delta
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid, "typ": typ})


class OidcProvider:
    def __init__(self, jwks_sequence: list[dict[str, object]], *, metadata_status: int = 200):
        self.jwks_sequence = list(jwks_sequence)
        self.metadata_status = metadata_status
        self.discovery_calls = 0
        self.jwks_calls = 0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == "https://identity.example.com/.well-known/openid-configuration":
            self.discovery_calls += 1
            return httpx2.Response(
                self.metadata_status,
                json={"issuer": ISSUER, "jwks_uri": "https://identity.example.com/jwks"},
            )
        if url == "https://identity.example.com/jwks":
            self.jwks_calls += 1
            payload = self.jwks_sequence[min(self.jwks_calls - 1, len(self.jwks_sequence) - 1)]
            return httpx2.Response(200, json=payload)
        raise AssertionError(f"unexpected identity URL: {url}")

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))


def authenticator(provider: OidcProvider) -> OidcJwtManagementAuthenticator:
    return OidcJwtManagementAuthenticator(
        config=OidcJwtConfig(
            issuer=ISSUER,
            audience=AUDIENCE,
            allowed_algorithms=("RS256",),
        ),
        http_client=provider.client(),
    )


@pytest.mark.anyio
async def test_valid_signed_access_token_returns_management_principal():
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])
    principal = await authenticator(provider).authenticate(credentials_for(token_for(key)))

    assert principal.authentication_method == "oidc_jwt"
    assert principal.issuer == ISSUER
    assert principal.subject == "principal-001"
    assert principal.client_id == "client-001"
    assert REQUIRED_SCOPE in principal.scopes


@pytest.mark.anyio
@pytest.mark.parametrize(
    "token_factory",
    [
        lambda key: token_for(make_key()),
        lambda key: token_for(key, kid="unknown"),
        lambda key: token_for(key, alg="RS512"),
        lambda key: jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
                "iat": datetime.now(timezone.utc),
                "sub": "principal-001",
                "client_id": "client-001",
                "jti": "jwt-id-001",
                "scope": REQUIRED_SCOPE,
            },
            key="synthetic-hs-confusion-secret-0001",
            algorithm="HS256",
            headers={"kid": "key-1", "typ": "at+jwt"},
        ),
        lambda key: token_for(key, issuer="https://other.example.com/"),
        lambda key: token_for(key, audience="https://other-api.example/"),
        lambda key: token_for(key, expires_delta=timedelta(minutes=-5)),
        lambda key: token_for(key, not_before_delta=timedelta(minutes=5)),
        lambda key: token_for(key, subject=None),
        lambda key: token_for(key, subject=""),
        lambda key: token_for(key, typ="JWT"),
        lambda key: "not-a-jwt",
    ],
)
async def test_invalid_access_tokens_are_rejected(token_factory):
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])

    with pytest.raises(InvalidManagementTokenError):
        await authenticator(provider).authenticate(credentials_for(token_factory(key)))


@pytest.mark.anyio
async def test_alg_none_is_rejected():
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": now + timedelta(minutes=5),
            "iat": now,
                "sub": "principal-001",
                "client_id": "client-001",
                "jti": "jwt-id-001",
                "scope": REQUIRED_SCOPE,
            },
        key="",
        algorithm="none",
        headers={"typ": "at+jwt"},
    )
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])

    with pytest.raises(InvalidManagementTokenError):
        await authenticator(provider).authenticate(credentials_for(token))


@pytest.mark.anyio
async def test_valid_token_without_recognized_scope_authenticates_as_principal():
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])

    principal = await authenticator(provider).authenticate(credentials_for(token_for(key, scope="other.scope")))

    assert principal.authentication_method == "oidc_jwt"
    assert principal.scopes == frozenset({"other.scope"})


@pytest.mark.anyio
@pytest.mark.parametrize("client_id", [None, "", "   ", 123, ["client-001"]])
async def test_client_id_is_required_as_non_empty_string(client_id):
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])

    with pytest.raises(InvalidManagementTokenError):
        await authenticator(provider).authenticate(credentials_for(token_for(key, client_id=client_id)))


@pytest.mark.anyio
@pytest.mark.parametrize("jti", [None, "", "   ", 123, ["jwt-id-001"]])
async def test_jti_is_required_as_non_empty_string(jti):
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])

    with pytest.raises(InvalidManagementTokenError):
        await authenticator(provider).authenticate(credentials_for(token_for(key, jti=jti)))


@pytest.mark.anyio
@pytest.mark.parametrize("scope", [None, ["read", REQUIRED_SCOPE], {"scope": REQUIRED_SCOPE}, 123, True])
async def test_scope_must_be_space_delimited_string(scope):
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])

    with pytest.raises(InvalidManagementTokenError):
        await authenticator(provider).authenticate(credentials_for(token_for(key, scope=scope)))


@pytest.mark.anyio
async def test_multiple_scopes_including_required_scope_are_accepted():
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])

    principal = await authenticator(provider).authenticate(
        credentials_for(token_for(key, scope=f"read {REQUIRED_SCOPE} other"))
    )

    assert REQUIRED_SCOPE in principal.scopes


@pytest.mark.anyio
async def test_unknown_kid_triggers_one_jwks_refresh_and_then_uses_cache():
    old_key = make_key()
    new_key = make_key()
    provider = OidcProvider(
        [
            {"keys": [public_jwk(old_key, kid="old")]},
            {"keys": [public_jwk(new_key, kid="new")]},
        ]
    )
    auth = authenticator(provider)

    await auth.authenticate(credentials_for(token_for(new_key, kid="new")))
    await auth.authenticate(credentials_for(token_for(new_key, kid="new")))

    assert provider.discovery_calls == 1
    assert provider.jwks_calls == 2


@pytest.mark.anyio
async def test_provider_discovery_outage_is_unavailable():
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}], metadata_status=503)

    with pytest.raises(ManagementIdentityProviderUnavailableError):
        await authenticator(provider).authenticate(credentials_for(token_for(key)))


def credentials_for(token: str):
    from fastapi.security import HTTPAuthorizationCredentials

    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def oidc_settings() -> Settings:
    return Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=True,
        management_auth_mode="oidc_jwt",
        management_oidc_issuer=ISSUER,
        management_oidc_audience=AUDIENCE,
        _env_file=None,
    )


@pytest.fixture
def delivery_store():
    return InMemoryDeliveryStore(max_events=50)


def test_oidc_jwt_mode_accepts_valid_token_for_management_api():
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])
    client = TestClient(create_app(settings=oidc_settings(), management_identity_http_client=provider.client()))

    response = client.get("/events", headers={"Authorization": f"Bearer {token_for(key)}"})

    assert response.status_code == 200


def test_oidc_jwt_mode_rejects_shared_token_fallback():
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])
    client = TestClient(create_app(settings=oidc_settings(), management_identity_http_client=provider.client()))

    response = client.get("/events", headers={"Authorization": f"Bearer {MANAGEMENT_TOKEN}"})

    assert response.status_code == 401


def test_shared_token_mode_rejects_oidc_jwt_fallback():
    key = make_key()
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=True,
        management_api_token=MANAGEMENT_TOKEN,
        _env_file=None,
    )
    client = TestClient(create_app(settings=settings))

    response = client.get("/events", headers={"Authorization": f"Bearer {token_for(key)}"})

    assert response.status_code == 401


def test_oidc_token_without_scope_returns_forbidden():
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])
    client = TestClient(create_app(settings=oidc_settings(), management_identity_http_client=provider.client()))

    response = client.get("/events", headers={"Authorization": f"Bearer {token_for(key, scope='other.scope')}"})

    assert response.status_code == 403


def test_oidc_insufficient_scope_challenge_names_required_scope_only():
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])
    client = TestClient(create_app(settings=oidc_settings(), management_identity_http_client=provider.client()))

    response = client.get("/events", headers={"Authorization": f"Bearer {token_for(key, scope=RECOVERY_READ_SCOPE)}"})

    assert response.status_code == 403
    challenge = response.headers["WWW-Authenticate"]
    assert 'Bearer error="insufficient_scope"' in challenge
    assert f'scope="{DIAGNOSTICS_READ_SCOPE}"' in challenge
    assert RECOVERY_READ_SCOPE not in challenge


@pytest.mark.parametrize(
    ("scope", "events_status", "recovery_status", "redelivery_status"),
    [
        (DIAGNOSTICS_READ_SCOPE, 200, 403, 403),
        (RECOVERY_READ_SCOPE, 403, 200, 403),
        (RECOVERY_EXECUTE_SCOPE, 403, 403, 404),
        (f"{DIAGNOSTICS_READ_SCOPE} {RECOVERY_READ_SCOPE} {RECOVERY_EXECUTE_SCOPE}", 200, 200, 404),
        (REQUIRED_SCOPE, 200, 200, 404),
    ],
)
def test_oidc_route_authorization_scope_matrix(scope, events_status, recovery_status, redelivery_status):
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])
    client = TestClient(create_app(settings=oidc_settings(), management_identity_http_client=provider.client()))
    headers = {"Authorization": f"Bearer {token_for(key, scope=scope)}"}

    assert client.get("/events", headers=headers).status_code == events_status
    assert client.get("/api/v1/recovery-actions", headers=headers).status_code == recovery_status
    assert client.post(
        "/api/v1/delivery-attempts/00000000-0000-0000-0000-000000000001/github-deliveries/1/redelivery",
        headers=headers,
    ).status_code == redelivery_status


def test_oidc_capabilities_are_independent_and_exactly_matched():
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])
    client = TestClient(create_app(settings=oidc_settings(), management_identity_http_client=provider.client()))
    headers = {"Authorization": f"Bearer {token_for(key, scope='webhook-monitor.recovery.execute-extra')}"}

    assert client.post(
        "/api/v1/delivery-attempts/00000000-0000-0000-0000-000000000001/github-deliveries/1/redelivery",
        headers=headers,
    ).status_code == 403


def test_authorizer_prefers_exact_scope_over_full_management_scope():
    principal = ManagementPrincipal(
        authentication_method="oidc_jwt",
        issuer=ISSUER,
        subject="principal-001",
        client_id="client-001",
        scopes=frozenset({REQUIRED_SCOPE, RECOVERY_EXECUTE_SCOPE}),
    )

    authorization = authorize_management_capability(
        principal=principal,
        capability=MANAGEMENT_CAPABILITY_RECOVERY_EXECUTE,
        scope_policy=SCOPE_POLICY,
    )

    assert authorization.capability == MANAGEMENT_CAPABILITY_RECOVERY_EXECUTE
    assert authorization.authorization_method == "oidc_scope"
    assert authorization.matched_scope == RECOVERY_EXECUTE_SCOPE


def test_authorizer_uses_full_management_scope_as_compatibility_umbrella():
    principal = ManagementPrincipal(
        authentication_method="oidc_jwt",
        issuer=ISSUER,
        subject="principal-001",
        client_id="client-001",
        scopes=frozenset({REQUIRED_SCOPE}),
    )

    authorization = authorize_management_capability(
        principal=principal,
        capability=MANAGEMENT_CAPABILITY_RECOVERY_READ,
        scope_policy=SCOPE_POLICY,
    )

    assert authorization.matched_scope == REQUIRED_SCOPE


def test_authorizer_rejects_unknown_scopes_for_application_capability():
    principal = ManagementPrincipal(
        authentication_method="oidc_jwt",
        issuer=ISSUER,
        subject="principal-001",
        client_id="client-001",
        scopes=frozenset({"unknown.scope"}),
    )

    with pytest.raises(InsufficientManagementScopeError):
        authorize_management_capability(
            principal=principal,
            capability=MANAGEMENT_CAPABILITY_DIAGNOSTICS_READ,
            scope_policy=SCOPE_POLICY,
        )


class FailingListRecentDeliveryStore(InMemoryDeliveryStore):
    def list_recent(self):
        raise AssertionError("delivery store should not be accessed")


class FailingDeliveryLookupStore(InMemoryDeliveryStore):
    def get_attempt(self, attempt_id):
        raise AssertionError("delivery store should not be accessed")


class FailingRecoveryActionStore(InMemoryRecoveryActionStore):
    def list_recent(self, *, limit, after=None):
        raise AssertionError("recovery action store should not be accessed")


class RecordingGitHubDeliveryClient:
    def __init__(self):
        self.calls = []

    async def list_repository_webhook_deliveries(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("GitHub read client should not be accessed")

    async def aclose(self):
        pass


class RecordingGitHubRedeliveryClient:
    def __init__(self):
        self.calls = []

    async def request_repository_webhook_redelivery(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("GitHub write client should not be accessed")

    async def aclose(self):
        pass


def test_insufficient_diagnostics_scope_blocks_delivery_store_access():
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])
    client = TestClient(
        create_app(
            settings=oidc_settings(),
            delivery_store=FailingListRecentDeliveryStore(max_events=50),
            management_identity_http_client=provider.client(),
        )
    )

    response = client.get("/events", headers={"Authorization": f"Bearer {token_for(key, scope=RECOVERY_READ_SCOPE)}"})

    assert response.status_code == 403


def test_insufficient_recovery_read_scope_blocks_recovery_store_access():
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])
    client = TestClient(
        create_app(
            settings=oidc_settings(),
            recovery_action_store=FailingRecoveryActionStore(max_actions=50),
            management_identity_http_client=provider.client(),
        )
    )

    response = client.get(
        "/api/v1/recovery-actions",
        headers={"Authorization": f"Bearer {token_for(key, scope=DIAGNOSTICS_READ_SCOPE)}"},
    )

    assert response.status_code == 403


def test_insufficient_redelivery_scope_blocks_all_business_side_effects():
    key = make_key()
    provider = OidcProvider([{"keys": [public_jwk(key)]}])
    github_delivery_client = RecordingGitHubDeliveryClient()
    github_redelivery_client = RecordingGitHubRedeliveryClient()
    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=True,
        management_auth_mode="oidc_jwt",
        management_oidc_issuer=ISSUER,
        management_oidc_audience=AUDIENCE,
        github_reconciliation_enabled=True,
        github_repository_webhook_token="synthetic-github-token",
        github_redelivery_enabled=True,
        github_repository_webhook_write_token="synthetic-github-write-token",
        _env_file=None,
    )
    client = TestClient(
        create_app(
            settings=settings,
            delivery_store=FailingDeliveryLookupStore(max_events=50),
            recovery_action_store=FailingRecoveryActionStore(max_actions=50),
            github_delivery_client=github_delivery_client,
            github_redelivery_client=github_redelivery_client,
            management_identity_http_client=provider.client(),
        )
    )

    response = client.post(
        "/api/v1/delivery-attempts/00000000-0000-0000-0000-000000000001/github-deliveries/1/redelivery",
        headers={"Authorization": f"Bearer {token_for(key, scope=RECOVERY_READ_SCOPE)}"},
    )

    assert response.status_code == 403
    assert github_delivery_client.calls == []
    assert github_redelivery_client.calls == []


def test_disabled_management_api_does_not_contact_oidc_provider():
    def fail_if_called(request):
        raise AssertionError("identity provider should not be contacted")

    settings = Settings(
        webhook_secret=TEST_SECRET,
        management_api_enabled=False,
        management_auth_mode="oidc_jwt",
        _env_file=None,
    )
    client = TestClient(
        create_app(
            settings=settings,
            management_identity_http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(fail_if_called)),
        )
    )

    response = client.get("/events", headers={"Authorization": "Bearer anything"})

    assert response.status_code == 404


def test_oidc_provider_outage_does_not_affect_health_ready_or_webhook(delivery_store):
    def outage(request):
        return httpx2.Response(503)

    client = TestClient(
        create_app(
            settings=oidc_settings(),
            delivery_store=delivery_store,
            management_identity_http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(outage)),
        )
    )
    payload = b'{"action":"opened"}'

    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.post(
        "/webhook/github",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-001",
            "X-GitHub-Hook-ID": "12345",
            "X-Hub-Signature-256": signature_for(payload),
        },
    ).status_code == 200
    assert client.get("/events", headers={"Authorization": f"Bearer {token_for(make_key())}"}).status_code == 503


def signature_for(payload: bytes) -> str:
    import hashlib
    import hmac

    return "sha256=" + hmac.new(TEST_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()
