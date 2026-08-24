from dataclasses import dataclass


AUTHENTICATION_METHOD_MANAGEMENT_BEARER = "management_bearer"
AUTHENTICATION_METHOD_OIDC_JWT = "oidc_jwt"
AUTHORIZATION_METHOD_OIDC_SCOPE = "oidc_scope"
AUTHORIZATION_METHOD_SHARED_MANAGEMENT_TOKEN = "shared_management_token"
MANAGEMENT_CAPABILITY_DIAGNOSTICS_READ = "diagnostics.read"
MANAGEMENT_CAPABILITY_RECOVERY_READ = "recovery.read"
MANAGEMENT_CAPABILITY_RECOVERY_EXECUTE = "recovery.execute"
MANAGEMENT_CAPABILITIES = frozenset(
    {
        MANAGEMENT_CAPABILITY_DIAGNOSTICS_READ,
        MANAGEMENT_CAPABILITY_RECOVERY_READ,
        MANAGEMENT_CAPABILITY_RECOVERY_EXECUTE,
    }
)


@dataclass(frozen=True)
class ManagementPrincipal:
    authentication_method: str
    issuer: str | None
    subject: str | None
    client_id: str | None
    scopes: frozenset[str]


@dataclass(frozen=True)
class ManagementScopePolicy:
    full_management_scope: str
    diagnostics_read_scope: str
    recovery_read_scope: str
    recovery_execute_scope: str

    def scope_for(self, capability: str) -> str:
        if capability == MANAGEMENT_CAPABILITY_DIAGNOSTICS_READ:
            return self.diagnostics_read_scope
        if capability == MANAGEMENT_CAPABILITY_RECOVERY_READ:
            return self.recovery_read_scope
        if capability == MANAGEMENT_CAPABILITY_RECOVERY_EXECUTE:
            return self.recovery_execute_scope
        raise ValueError("Unknown management capability")


@dataclass(frozen=True)
class ManagementAuthorization:
    principal: ManagementPrincipal
    capability: str
    authorization_method: str
    matched_scope: str | None


SHARED_TOKEN_PRINCIPAL = ManagementPrincipal(
    authentication_method=AUTHENTICATION_METHOD_MANAGEMENT_BEARER,
    issuer=None,
    subject=None,
    client_id=None,
    scopes=frozenset(),
)
