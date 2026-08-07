from __future__ import annotations
"""Ed25519 signatures for offline live-promotion evidence.

The private key belongs on an offline certifier workstation.  Runtime receives
only the DER-encoded public key, so a compromised execution container cannot
forge its own promotion record.
"""

import base64
import json
import math
import os
import secrets
import time


EVIDENCE_VERSION = 1
ALGORITHM = "Ed25519"
PUBLIC_KEY_ENV = "LIVE_EVIDENCE_PUBLIC_KEY"


class EvidenceSignatureError(ValueError):
    pass


def canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def public_key_b64(key) -> str:
    return base64.b64encode(key.public_key().export_key(format="DER")).decode("ascii")


def import_public_key(encoded: str | None = None):
    try:
        from Crypto.PublicKey import ECC
    except ImportError as exc:
        raise EvidenceSignatureError("pycryptodome is required for live-evidence verification") from exc
    value = str(encoded if encoded is not None else os.getenv(PUBLIC_KEY_ENV, "")).strip()
    if not value:
        raise EvidenceSignatureError(f"{PUBLIC_KEY_ENV} is required")
    try:
        raw = base64.b64decode(value, validate=True)
        key = ECC.import_key(raw)
    except Exception as exc:
        raise EvidenceSignatureError("live-evidence public key is not valid base64 DER") from exc
    if key.curve != "Ed25519" or key.has_private():
        raise EvidenceSignatureError("live-evidence verifier requires an Ed25519 public key")
    return key


def sign_document(*, payload: dict, private_key, producer: str,
                  valid_seconds: int, now: float | None = None,
                  nonce: str | None = None) -> dict:
    try:
        from Crypto.Signature import eddsa
    except ImportError as exc:
        raise EvidenceSignatureError("pycryptodome is required for live-evidence signing") from exc
    if not isinstance(payload, dict):
        raise EvidenceSignatureError("evidence payload must be an object")
    if not private_key.has_private() or private_key.curve != "Ed25519":
        raise EvidenceSignatureError("an Ed25519 private key is required")
    issued = float(time.time() if now is None else now)
    ttl = int(valid_seconds)
    if not math.isfinite(issued) or ttl < 300 or ttl > 90 * 86400:
        raise EvidenceSignatureError("evidence validity must be between 5 minutes and 90 days")
    body = {
        "evidence_version": EVIDENCE_VERSION,
        "algorithm": ALGORITHM,
        "producer": str(producer),
        "nonce": nonce or secrets.token_hex(16),
        "issued_at": issued,
        "expires_at": issued + ttl,
        "payload": payload,
    }
    signature = eddsa.new(private_key, "rfc8032").sign(canonical_bytes(body))
    return dict(body, signature=base64.b64encode(signature).decode("ascii"))


def verify_document(document, *, expected_producer: str,
                    public_key=None, now: float | None = None,
                    max_future_skew: float = 30.0,
                    min_remaining_seconds: float = 0.0) -> dict:
    try:
        from Crypto.Signature import eddsa
    except ImportError as exc:
        raise EvidenceSignatureError("pycryptodome is required for live-evidence verification") from exc
    if not isinstance(document, dict):
        raise EvidenceSignatureError("evidence document must be an object")
    if document.get("evidence_version") != EVIDENCE_VERSION:
        raise EvidenceSignatureError("unsupported evidence version")
    if document.get("algorithm") != ALGORITHM:
        raise EvidenceSignatureError("unsupported evidence signature algorithm")
    if document.get("producer") != expected_producer:
        raise EvidenceSignatureError("unexpected evidence producer")
    try:
        issued, expires = float(document["issued_at"]), float(document["expires_at"])
    except Exception as exc:
        raise EvidenceSignatureError("evidence timestamps are malformed") from exc
    current = float(time.time() if now is None else now)
    minimum_remaining = float(min_remaining_seconds)
    future_skew = float(max_future_skew)
    if not all(math.isfinite(value) for value in (issued, expires, current)):
        raise EvidenceSignatureError("evidence timestamps must be finite")
    if (not math.isfinite(minimum_remaining) or minimum_remaining < 0
            or minimum_remaining > 7 * 86400):
        raise EvidenceSignatureError("minimum remaining validity is outside the allowed range")
    if not math.isfinite(future_skew) or future_skew < 0:
        raise EvidenceSignatureError("future-skew allowance is invalid")
    if issued > current + future_skew:
        raise EvidenceSignatureError("evidence was issued in the future")
    if expires <= issued or current > expires:
        raise EvidenceSignatureError("evidence is expired")
    if expires - current < minimum_remaining:
        raise EvidenceSignatureError("evidence expires before the required transaction margin")
    try:
        signature = base64.b64decode(str(document.get("signature", "")), validate=True)
    except Exception as exc:
        raise EvidenceSignatureError("evidence signature is malformed") from exc
    body = {key: value for key, value in document.items() if key != "signature"}
    try:
        eddsa.new(public_key or import_public_key(), "rfc8032").verify(
            canonical_bytes(body), signature)
    except Exception as exc:
        raise EvidenceSignatureError("evidence signature verification failed") from exc
    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise EvidenceSignatureError("evidence payload must be an object")
    return payload
