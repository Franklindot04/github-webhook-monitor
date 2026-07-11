import hashlib
import hmac


def verify_github_signature(payload_body: bytes, signature_header: str | None, secret: str) -> bool:
    if not signature_header or not secret:
        return False

    expected_signature = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        payload_body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected_signature, signature_header)