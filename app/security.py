import hashlib
import hmac
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx2
import jwt
from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials

from app.domain.management import (
    AUTHENTICATION_METHOD_OIDC_JWT,
    SHARED_TOKEN_PRINCIPAL,
    ManagementPrincipal,
)


MIN_MANAGEMENT_API_TOKEN_LENGTH = 32
MANAGEMENT_UNAUTHORIZED_DETAIL = "Unauthorized"
MANAGEMENT_FORBIDDEN_DETAIL = "Forbidden"
OIDC_ACCESS_TOKEN_TYPES = frozenset({"at+jwt", "application/at+jwt"})
ASYMMETRIC_JWT_ALGORITHM_PREFIXES = ("RS", "PS", "ES")
CACHE_TTL_SECONDS = 300
CLOCK_SKEW_SECONDS = 60


class ManagementIdentityProviderUnavailableError(Exception):
    pass


class InvalidManagementTokenError(Exception):
    pass


class InsufficientManagementScopeError(Exception):
    pass


@dataclass(frozen=True)
class OidcJwtConfig:
    issuer: str
    audience: str
    required_scope: str
    allowed_algorithms: tuple[str, ...]


@dataclass(frozen=True)
class OidcMetadata:
    issuer: str
    jwks_uri: str


def verify_github_signature(payload_body: bytes, signature_header: str | None, secret: str) -> bool:
    if not signature_header or not secret:
        return False

    expected_signature = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        payload_body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected_signature, signature_header)


def verify_management_token(provided_token: str, expected_token: str) -> bool:
    if not provided_token or not expected_token:
        return False
    return hmac.compare_digest(
        provided_token.encode("utf-8"),
        expected_token.encode("utf-8"),
    )


def authenticate_shared_management_token(
    *,
    expected_token: str | None,
    credentials: HTTPAuthorizationCredentials | None,
) -> ManagementPrincipal:
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or expected_token is None
        or not verify_management_token(credentials.credentials, expected_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=MANAGEMENT_UNAUTHORIZED_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return SHARED_TOKEN_PRINCIPAL


def require_management_access(
    *,
    management_api_enabled: bool,
    expected_token: str | None,
    credentials: HTTPAuthorizationCredentials | None,
) -> ManagementPrincipal:
    if not management_api_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return authenticate_shared_management_token(expected_token=expected_token, credentials=credentials)


def management_unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=MANAGEMENT_UNAUTHORIZED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


def management_forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MANAGEMENT_FORBIDDEN_DETAIL)


def management_identity_unavailable() -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable")


class OidcJwtManagementAuthenticator:
    def __init__(
        self,
        *,
        config: OidcJwtConfig,
        http_client: httpx2.AsyncClient,
        cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    ):
        self._config = config
        self._http_client = http_client
        self._cache_ttl_seconds = cache_ttl_seconds
        self._metadata: OidcMetadata | None = None
        self._metadata_expires_at = 0.0
        self._jwks: dict[str, Any] | None = None
        self._jwks_expires_at = 0.0

    async def authenticate(self, credentials: HTTPAuthorizationCredentials | None) -> ManagementPrincipal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise InvalidManagementTokenError("Missing bearer token")
        token = credentials.credentials
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidManagementTokenError("Malformed JWT") from exc

        typ = header.get("typ")
        alg = header.get("alg")
        kid = header.get("kid")
        if typ not in OIDC_ACCESS_TOKEN_TYPES:
            raise InvalidManagementTokenError("Wrong token type")
        if not isinstance(alg, str) or alg not in self._config.allowed_algorithms:
            raise InvalidManagementTokenError("Unsupported algorithm")
        if alg == "none" or alg.startswith("HS") or not alg.startswith(ASYMMETRIC_JWT_ALGORITHM_PREFIXES):
            raise InvalidManagementTokenError("Unsupported algorithm")
        if kid is not None and not isinstance(kid, str):
            raise InvalidManagementTokenError("Invalid key id")

        key = await self._signing_key_for(kid=kid, alg=alg, refresh=False)
        if key is None and kid is not None:
            key = await self._signing_key_for(kid=kid, alg=alg, refresh=True)
        if key is None:
            raise InvalidManagementTokenError("Unknown signing key")

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self._config.allowed_algorithms),
                audience=self._config.audience,
                issuer=self._config.issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["iss", "sub", "aud", "exp", "iat", "client_id", "jti"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidManagementTokenError("Invalid JWT") from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise InvalidManagementTokenError("Missing subject")
        client_id = claims.get("client_id")
        if not isinstance(client_id, str) or not client_id.strip():
            raise InvalidManagementTokenError("Missing client id")
        jwt_id = claims.get("jti")
        if not isinstance(jwt_id, str) or not jwt_id.strip():
            raise InvalidManagementTokenError("Missing JWT id")
        scopes = parse_scope_claim(claims.get("scope"))
        if self._config.required_scope not in scopes:
            raise InsufficientManagementScopeError("Missing required management scope")
        return ManagementPrincipal(
            authentication_method=AUTHENTICATION_METHOD_OIDC_JWT,
            issuer=self._config.issuer,
            subject=subject,
            client_id=client_id,
            scopes=frozenset(scopes),
        )

    async def _signing_key_for(self, *, kid: str | None, alg: str, refresh: bool) -> Any | None:
        jwks = await self._jwks_document(refresh=refresh)
        keys = jwks.get("keys")
        if not isinstance(keys, list) or not keys:
            raise ManagementIdentityProviderUnavailableError("JWKS was invalid")
        if kid is None and len(keys) != 1:
            raise InvalidManagementTokenError("Missing key id")

        for jwk in keys:
            if not isinstance(jwk, dict):
                continue
            if kid is not None and jwk.get("kid") != kid:
                continue
            if jwk.get("kty") == "oct":
                raise InvalidManagementTokenError("Unsupported symmetric key")
            jwk_alg = jwk.get("alg")
            if jwk_alg is not None and jwk_alg != alg:
                continue
            try:
                return jwt.PyJWK.from_dict(jwk, algorithm=alg).key
            except jwt.PyJWTError as exc:
                raise ManagementIdentityProviderUnavailableError("JWKS key was invalid") from exc
        return None

    async def _metadata_document(self) -> OidcMetadata:
        now = time.monotonic()
        if self._metadata is not None and now < self._metadata_expires_at:
            return self._metadata

        metadata_url = urljoin(self._config.issuer.rstrip("/") + "/", ".well-known/openid-configuration")
        try:
            response = await self._http_client.get(metadata_url)
        except (httpx2.TimeoutException, httpx2.NetworkError, httpx2.RequestError) as exc:
            raise ManagementIdentityProviderUnavailableError("OIDC discovery unavailable") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise ManagementIdentityProviderUnavailableError("OIDC discovery unavailable")
        if response.status_code != 200:
            raise InvalidManagementTokenError("OIDC discovery rejected")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ManagementIdentityProviderUnavailableError("OIDC discovery invalid") from exc
        if not isinstance(payload, dict):
            raise ManagementIdentityProviderUnavailableError("OIDC discovery invalid")
        if payload.get("issuer") != self._config.issuer:
            raise ManagementIdentityProviderUnavailableError("OIDC issuer mismatch")
        jwks_uri = payload.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri.startswith("https://"):
            raise ManagementIdentityProviderUnavailableError("OIDC JWKS URI invalid")
        metadata = OidcMetadata(issuer=self._config.issuer, jwks_uri=jwks_uri)
        self._metadata = metadata
        self._metadata_expires_at = now + self._cache_ttl_seconds
        return metadata

    async def _jwks_document(self, *, refresh: bool) -> dict[str, Any]:
        now = time.monotonic()
        if not refresh and self._jwks is not None and now < self._jwks_expires_at:
            return self._jwks
        metadata = await self._metadata_document()
        try:
            response = await self._http_client.get(metadata.jwks_uri)
        except (httpx2.TimeoutException, httpx2.NetworkError, httpx2.RequestError) as exc:
            raise ManagementIdentityProviderUnavailableError("OIDC JWKS unavailable") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise ManagementIdentityProviderUnavailableError("OIDC JWKS unavailable")
        if response.status_code != 200:
            raise InvalidManagementTokenError("OIDC JWKS rejected")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ManagementIdentityProviderUnavailableError("OIDC JWKS invalid") from exc
        if not isinstance(payload, dict):
            raise ManagementIdentityProviderUnavailableError("OIDC JWKS invalid")
        self._jwks = payload
        self._jwks_expires_at = now + self._cache_ttl_seconds
        return payload


def parse_scope_claim(value: object) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, str):
        raise InvalidManagementTokenError("Invalid scope")
    return {scope for scope in value.split() if scope}


def parse_allowed_jwt_algorithms(value: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        algorithms = tuple(item.strip() for item in value.split(",") if item.strip())
    else:
        algorithms = tuple(item.strip() for item in value if item.strip())
    if not algorithms:
        raise ValueError("MANAGEMENT_OIDC_ALLOWED_ALGORITHMS must not be empty")
    for algorithm in algorithms:
        if algorithm == "none" or algorithm.startswith("HS") or not algorithm.startswith(ASYMMETRIC_JWT_ALGORITHM_PREFIXES):
            raise ValueError("MANAGEMENT_OIDC_ALLOWED_ALGORITHMS must contain only asymmetric algorithms")
    return algorithms
