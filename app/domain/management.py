from dataclasses import dataclass


AUTHENTICATION_METHOD_MANAGEMENT_BEARER = "management_bearer"
AUTHENTICATION_METHOD_OIDC_JWT = "oidc_jwt"


@dataclass(frozen=True)
class ManagementPrincipal:
    authentication_method: str
    issuer: str | None
    subject: str | None
    client_id: str | None
    scopes: frozenset[str]


SHARED_TOKEN_PRINCIPAL = ManagementPrincipal(
    authentication_method=AUTHENTICATION_METHOD_MANAGEMENT_BEARER,
    issuer=None,
    subject=None,
    client_id=None,
    scopes=frozenset(),
)

