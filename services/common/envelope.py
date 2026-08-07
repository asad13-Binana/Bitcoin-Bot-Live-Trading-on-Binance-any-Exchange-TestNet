from __future__ import annotations
"""HMAC-authenticated message envelopes for runtime inter-service buses.

Signals and owner commands cross container boundaries through a shared
filesystem. Every message is therefore signed and release
bound so a compromised sibling cannot forge a trade signal or privileged
command:

    {"envelope_version": 1, "producer": ..., "purpose": ..., "nonce": ...,
     "created_at": ..., "expires_at": ..., "release_hash": ...,
     "payload": {...}, "signature": hex(HMAC-SHA256(key, canonical(body)))}

Rules enforced by ``verify_envelope`` (all fail closed):
  * signature must verify with the shared per-bus secret (constant-time);
  * producer must be one of the expected producers for that bus;
  * purpose must match the consumer's bus exactly;
  * the envelope must not be expired and not be from the far future;
  * ``release_hash`` must be non-empty and equal to the installed release
    hash, so an envelope captured under one release cannot be replayed
    against a different installed release.

Replay of a *valid* envelope is separately neutralised by the durable
command/signal/request idempotency claims (StateStore / queue store).
Only Python stdlib is used so the Freqtrade container can import this module
from a read-only mount.
"""
import hashlib
import hmac
import json
import math
import os
import secrets
import time
from pathlib import Path


class EnvelopeError(ValueError):
    """Any authentication/validation failure. Consumers must fail closed."""


ENVELOPE_VERSION = 1
MIN_KEY_LENGTH = 32

# Bus definitions: purpose -> (env var holding the shared secret, producers).
BUS_SIGNAL = 'signal'
BUS_COMMAND = 'command'

# An authenticated envelope is deliberately short-lived.  The downstream
# consumers apply stricter signal/command age limits; this one-hour ceiling is
# the outer cryptographic replay window and prevents a correctly signed but
# absurdly long-lived message from bypassing those expectations.
MAX_TTL_SECONDS = {BUS_SIGNAL: 3600, BUS_COMMAND: 3600}

KEY_ENV = {
    BUS_SIGNAL: 'SIGNAL_HMAC_KEY',
    BUS_COMMAND: 'COMMAND_HMAC_KEY',
}


def canonical_bytes(payload) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str).encode('utf-8')


def installed_release_hash() -> str:
    """The installed release identity every envelope is bound to.

    ``ENVELOPE_RELEASE_HASH`` (set by the installer/compose) wins; otherwise
    the repository-root RELEASE_SHA256.txt next to this source tree is used.
    Returns '' when neither exists — signing/verification then fails closed.
    """
    explicit = os.getenv('ENVELOPE_RELEASE_HASH', '').strip()
    if explicit:
        return explicit
    path = Path(__file__).resolve().parents[2] / 'RELEASE_SHA256.txt'
    try:
        return path.read_text(encoding='utf-8').split()[0]
    except Exception:
        return ''


def load_key(purpose: str) -> bytes:
    """Load the shared secret for one bus. Missing/short keys fail closed."""
    env_name = KEY_ENV.get(purpose)
    if not env_name:
        raise EnvelopeError(f'unknown envelope purpose: {purpose}')
    value = os.getenv(env_name, '').strip()
    if not value:
        raise EnvelopeError(f'{env_name} is required and empty — bus is fail-closed')
    if len(value) < MIN_KEY_LENGTH:
        raise EnvelopeError(f'{env_name} must be at least {MIN_KEY_LENGTH} characters')
    return value.encode('utf-8')


def sign_envelope(*, producer: str, purpose: str, payload: dict, ttl_seconds: int,
                  key: bytes | str | None = None, nonce: str | None = None,
                  now: float | None = None) -> dict:
    if key is None:
        key = load_key(purpose)
    if isinstance(key, str):
        key = key.encode('utf-8')
    if not key:
        raise EnvelopeError('signing key is empty')
    release = installed_release_hash()
    if not release:
        raise EnvelopeError('installed release hash unavailable; refusing to sign an unbound envelope')
    created = float(now if now is not None else time.time())
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EnvelopeError('envelope TTL must be an integer') from exc
    if not math.isfinite(created):
        raise EnvelopeError('envelope created_at must be finite')
    maximum_ttl = MAX_TTL_SECONDS.get(purpose)
    if maximum_ttl is None:
        raise EnvelopeError(f'unknown envelope purpose: {purpose}')
    if ttl < 1 or ttl > maximum_ttl:
        raise EnvelopeError(f'envelope TTL must be between 1 and {maximum_ttl} seconds')
    body = {
        'envelope_version': ENVELOPE_VERSION,
        'producer': str(producer),
        'purpose': str(purpose),
        'nonce': nonce or secrets.token_hex(16),
        'created_at': created,
        'expires_at': created + ttl,
        'release_hash': release,
        'payload': payload,
    }
    signature = hmac.new(key, canonical_bytes(body), hashlib.sha256).hexdigest()
    return dict(body, signature=signature)


def verify_envelope(envelope, *, purpose: str, expected_producers,
                    key: bytes | str | None = None, now: float | None = None,
                    max_future_skew: float = 30.0) -> dict:
    """Validate one envelope and return its payload. Raises EnvelopeError."""
    if key is None:
        key = load_key(purpose)
    if isinstance(key, str):
        key = key.encode('utf-8')
    if not isinstance(envelope, dict):
        raise EnvelopeError('envelope must be a JSON object')
    if envelope.get('envelope_version') != ENVELOPE_VERSION:
        raise EnvelopeError('unsupported envelope version')
    supplied_signature = str(envelope.get('signature', ''))
    if not supplied_signature:
        raise EnvelopeError('missing signature')
    body = {k: v for k, v in envelope.items() if k != 'signature'}
    expected_signature = hmac.new(key, canonical_bytes(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise EnvelopeError('signature verification failed')
    if str(envelope.get('purpose')) != purpose:
        raise EnvelopeError(f'purpose mismatch: {envelope.get("purpose")!r} != {purpose!r}')
    producers = {expected_producers} if isinstance(expected_producers, str) else set(expected_producers)
    if str(envelope.get('producer')) not in producers:
        raise EnvelopeError(f'unexpected producer: {envelope.get("producer")!r}')
    current = float(now if now is not None else time.time())
    try:
        created_at = float(envelope['created_at'])
        expires_at = float(envelope['expires_at'])
    except Exception as exc:
        raise EnvelopeError('malformed envelope timestamps') from exc
    if not all(math.isfinite(value) for value in (current, created_at, expires_at, max_future_skew)):
        raise EnvelopeError('envelope timestamps and skew must be finite')
    if max_future_skew < 0:
        raise EnvelopeError('max_future_skew must not be negative')
    if expires_at <= created_at:
        raise EnvelopeError('envelope expires_at must be after created_at')
    maximum_ttl = MAX_TTL_SECONDS.get(purpose)
    if maximum_ttl is None or expires_at - created_at > maximum_ttl:
        raise EnvelopeError('envelope validity window exceeds the allowed maximum')
    if created_at > current + max_future_skew:
        raise EnvelopeError('envelope created_at is in the future')
    if current > expires_at:
        raise EnvelopeError('envelope expired')
    release = installed_release_hash()
    envelope_release = str(envelope.get('release_hash', ''))
    if not release or not envelope_release or envelope_release != release:
        raise EnvelopeError('release hash binding failed (cross-release replay rejected)')
    payload = envelope.get('payload')
    if not isinstance(payload, dict):
        raise EnvelopeError('payload must be a JSON object')
    return payload


def read_verified_file(path, *, purpose: str, expected_producers,
                       key: bytes | str | None = None) -> dict:
    """Read a JSON envelope file and return its verified payload."""
    raw = Path(path).read_text(encoding='utf-8')
    try:
        envelope = json.loads(raw)
    except Exception as exc:
        raise EnvelopeError(f'envelope file is not valid JSON: {exc}') from exc
    return verify_envelope(envelope, purpose=purpose, expected_producers=expected_producers, key=key)
