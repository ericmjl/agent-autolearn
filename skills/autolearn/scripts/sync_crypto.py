"""Autolearn sync crypto primitives.

Implements the E2E-encryption layer described in
docs/designs/sync/encryption-LLD.md:

  Master Password
        |
        v
  PBKDF2-SHA256 (600,000 iterations, per-installation random salt)
        |
        v
  master_key (256 bits) -- stored in OS keychain
        |
        +-- persona_key = HMAC-SHA256(master_key, persona_id)
                |
                +-- file_key = HMAC-SHA256(persona_key, file_path)

Encryption: AES-256-GCM with a random 12-byte nonce per operation.

This module has no CLI and performs no network I/O. It is imported by
autolearn.py for the `sync` subcommands.
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["cryptography", "keyring"]
# ///
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# @spec SYNC-ENC-001
PBKDF2_ITERATIONS = 600_000
KEY_BITS = 256
KEY_BYTES = KEY_BITS // 8  # 32
SALT_BYTES = 32
NONCE_BYTES = 12  # GCM standard
TAG_BYTES = 16    # GCM standard

KEYCHAIN_SERVICE = "autolearn-sync"
KEYCHAIN_USER = "master_key"

DEFAULT_PERSONA_ID_NAMESPACE = uuid.UUID("a4b1c2d3-e4f5-6789-abcd-ef0123456789")


class TamperError(Exception):
    """Raised when GCM authentication tag verification fails."""


# @spec SYNC-ENC-004
def generate_salt() -> bytes:
    """Return 32 cryptographically random bytes for use as the install salt."""
    return secrets.token_bytes(SALT_BYTES)


def load_salt(salt_path: Path) -> bytes:
    """Load the install salt, raising FileNotFoundError if missing."""
    return salt_path.read_bytes()


def save_salt(salt_path: Path, salt: bytes) -> None:
    """Persist the install salt (mode 0600)."""
    salt_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(salt_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, salt)
    finally:
        os.close(fd)


# @spec SYNC-ENC-001
def derive_master_key(password: str, salt: bytes) -> bytes:
    """Derive a 256-bit master key from a password and salt via PBKDF2-SHA256."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


# @spec SYNC-ENC-005
def derive_persona_key(master_key: bytes, persona_id: str) -> bytes:
    """Derive a per-persona key via HMAC-SHA256(master_key, persona_id)."""
    return hmac.new(master_key, persona_id.encode("utf-8"), hashlib.sha256).digest()


# @spec SYNC-ENC-006
def derive_file_key(persona_key: bytes, file_path: str) -> bytes:
    """Derive a per-file key via HMAC-SHA256(persona_key, file_path)."""
    return hmac.new(persona_key, file_path.encode("utf-8"), hashlib.sha256).digest()


# @spec SYNC-ENC-008, SYNC-ENC-009
def encrypt(file_key: bytes, plaintext: bytes) -> dict:
    """Encrypt plaintext with AES-256-GCM under file_key.

    Returns a dict with base64-encoded `ciphertext` (includes the GCM tag
    appended, per the cryptography library's AESGCM API) and `nonce`.
    """
    nonce = secrets.token_bytes(NONCE_BYTES)
    aesgcm = AESGCM(file_key)
    # AESGCM.encrypt returns ciphertext || tag (tag is the last 16 bytes).
    ct_and_tag = aesgcm.encrypt(nonce, plaintext, associated_data=None)
    return {
        "ciphertext": base64.b64encode(ct_and_tag).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }


# @spec SYNC-ENC-009, SYNC-ENC-010
def decrypt(file_key: bytes, nonce_b64: str, ciphertext_b64: str) -> bytes:
    """Decrypt and verify an AES-256-GCM record.

    Raises TamperError if the authentication tag does not verify.
    """
    nonce = base64.b64decode(nonce_b64)
    ct_and_tag = base64.b64decode(ciphertext_b64)
    aesgcm = AESGCM(file_key)
    try:
        return aesgcm.decrypt(nonce, ct_and_tag, associated_data=None)
    except Exception as exc:
        # @spec SYNC-ENC-010
        raise TamperError(f"GCM authentication tag verification failed: {exc}") from exc


# @spec SYNC-ENC-002
def store_master_key(master_key: bytes) -> None:
    """Persist master_key in the OS keychain."""
    import keyring
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USER, base64.b64encode(master_key).decode("ascii"))


def load_master_key() -> bytes | None:
    """Return the master_key from the keychain, or None if absent."""
    import keyring
    raw = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USER)
    if raw is None:
        return None
    return base64.b64decode(raw)


def delete_master_key() -> bool:
    """Remove the master_key from the keychain. Returns True if it was present."""
    import keyring
    try:
        stored = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USER)
    except Exception:
        return False
    if stored is None:
        return False
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_USER)
        return True
    except Exception:
        return False


def has_master_key() -> bool:
    """True if a master_key is present in the OS keychain."""
    try:
        import keyring
        return keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USER) is not None
    except Exception:
        return False


def keychain_available() -> bool:
    """Probe whether the keyring backend has a working set/get cycle.

    Used to decide between SYNC-ENC-002 (keychain path) and SYNC-ENC-003
    (prompt-on-every-operation fallback).
    """
    try:
        import keyring
        probe_service = "autolearn-sync-probe"
        probe_user = "probe"
        keyring.set_password(probe_service, probe_user, "x")
        retrieved = keyring.get_password(probe_service, probe_user)
        try:
            keyring.delete_password(probe_service, probe_user)
        except Exception:
            pass
        return retrieved == "x"
    except Exception:
        return False


BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def to_base58(data: bytes) -> str:
    """Encode bytes as a Bitcoin-style base58 string (no checksum)."""
    if not data:
        return ""
    n = int.from_bytes(data, "big")
    chars = []
    while n > 0:
        n, rem = divmod(n, 58)
        chars.append(BASE58_ALPHABET[rem])
    leading_zeros = 0
    for byte in data:
        if byte == 0:
            leading_zeros += 1
        else:
            break
    return "1" * leading_zeros + "".join(reversed(chars))


def from_base58(text: str) -> bytes:
    """Decode a base58 string to bytes (inverse of to_base58)."""
    if not text:
        return b""
    n = 0
    for char in text:
        digit = BASE58_ALPHABET.find(char)
        if digit == -1:
            raise ValueError(f"invalid base58 character: {char!r}")
        n = n * 58 + digit
    # Determine byte length, accounting for leading '1's (leading zero bytes).
    leading_ones = 0
    for char in text:
        if char == "1":
            leading_ones += 1
        else:
            break
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n > 0 else b""
    return b"\x00" * leading_ones + body


# @spec SYNC-ENC-012
def encode_recovery_key(master_key: bytes) -> str:
    """Render master_key as a base58 recovery string for offline backup."""
    return to_base58(master_key)


def decode_recovery_key(text: str) -> bytes:
    """Inverse of encode_recovery_key."""
    return from_base58(text)


def default_persona_id(salt: bytes) -> str:
    """Derive a deterministic persona_id for the implicit default persona.

    Uses UUID v5 over the install salt so that every machine logged in
    with the same password+salt pair derives the same default persona_id,
    and different installs get different IDs. This is forward-compatible
    with the Phase 3 multi-persona registry: that registry can simply
    adopt this deterministic value as the default persona's UUID.
    """
    return str(uuid.uuid5(DEFAULT_PERSONA_ID_NAMESPACE, salt.hex()))


def api_key_id(api_key: str) -> str:
    """Return a stable, non-reversible identifier for an API key.

    Used as the user_id on the server side (never store the raw API key).
    Mirrors the `user_id: sha256(api_key)` field in encryption-LLD.md.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def machine_id() -> str:
    """Return a stable per-machine identifier for sync telemetry."""
    import platform
    node = platform.node() or "unknown"
    # Mix in the macOS IOPlatformUUID / Linux /etc/machine-id when present
    # so two hostnames that happen to collide don't conflate.
    extra = ""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path) as f:
                extra = f.read().strip()
                break
        except OSError:
            continue
    if not extra and sys.platform == "darwin":
        try:
            import subprocess
            result = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=2,
            )
            for line in result.stdout.splitlines():
                if "IOPlatformUUID" in line:
                    extra = line.split('"')[-2]
                    break
        except Exception:
            pass
    fingerprint = f"{node}|{extra}" if extra else node
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]


# Imported here to avoid a top-level import on platforms without keyring.
import sys  # noqa: E402
