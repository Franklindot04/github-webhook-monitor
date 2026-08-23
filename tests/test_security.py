import hashlib
import hmac

from app.security import verify_github_signature


def signature_for(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def test_github_documented_hmac_sha256_vector():
    # Source: GitHub webhook delivery validation docs.
    payload = b"Hello, World!"
    secret = "It's a Secret to Everybody"
    signature = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"

    assert verify_github_signature(payload, signature, secret) is True


def test_correctly_signed_payload_succeeds():
    payload = b'{"action":"opened"}'
    secret = "test-secret"

    assert verify_github_signature(payload, signature_for(payload, secret), secret) is True


def test_incorrect_signature_fails():
    payload = b'{"action":"opened"}'

    assert verify_github_signature(payload, "sha256=incorrect", "test-secret") is False


def test_missing_signature_fails():
    assert verify_github_signature(b"{}", None, "test-secret") is False


def test_empty_signature_fails():
    assert verify_github_signature(b"{}", "", "test-secret") is False


def test_payload_modified_after_signing_fails():
    original_payload = b'{"action":"opened"}'
    transmitted_payload = b'{"action":"closed"}'
    secret = "test-secret"

    assert verify_github_signature(transmitted_payload, signature_for(original_payload, secret), secret) is False


def test_wrong_secret_fails():
    payload = b'{"action":"opened"}'

    assert verify_github_signature(payload, signature_for(payload, "right-secret"), "wrong-secret") is False


def test_correctly_signed_empty_body_succeeds():
    payload = b""
    secret = "test-secret"

    assert verify_github_signature(payload, signature_for(payload, secret), secret) is True


def test_unicode_payload_bytes_are_verified_without_reencoding():
    payload = '{"repository":{"full_name":"octo/repó"},"sender":{"login":"álîçé"}}'.encode("utf-8")
    secret = "test-secret"

    assert verify_github_signature(payload, signature_for(payload, secret), secret) is True


def test_arbitrary_raw_bytes_are_verified_without_decoding():
    payload = b"\x00\xffraw-bytes\nnot-json"
    secret = "test-secret"

    assert verify_github_signature(payload, signature_for(payload, secret), secret) is True
