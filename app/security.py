import hashlib
import hmac

from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials


MIN_MANAGEMENT_API_TOKEN_LENGTH = 32
MANAGEMENT_UNAUTHORIZED_DETAIL = "Unauthorized"


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


def require_management_access(
    *,
    management_api_enabled: bool,
    expected_token: str | None,
    credentials: HTTPAuthorizationCredentials | None,
) -> None:
    if not management_api_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

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
