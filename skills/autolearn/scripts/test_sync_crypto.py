"""Tests for sync_crypto.py.

Run with:
    uv run test_sync_crypto.py
or:
    uv run --with pytest --with cryptography --with keyring pytest test_sync_crypto.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "cryptography", "keyring"]
# ///
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

# Make sync_crypto.py importable when run via uv run / pytest.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sync_crypto as sc  # noqa: E402


# --- PBKDF2 master key derivation --------------------------------------

def test_derive_master_key_is_deterministic():
    salt = b"\x00" * 32
    k1 = sc.derive_master_key("hunter2", salt)
    k2 = sc.derive_master_key("hunter2", salt)
    assert k1 == k2
    assert len(k1) == 32


def test_derive_master_key_differs_per_password():
    salt = b"\x00" * 32
    assert sc.derive_master_key("hunter2", salt) != sc.derive_master_key("hunter3", salt)


def test_derive_master_key_differs_per_salt():
    assert sc.derive_master_key("hunter2", b"\x00" * 32) != sc.derive_master_key("hunter2", b"\x01" * 32)


def test_derive_master_key_is_256_bits():
    assert len(sc.derive_master_key("x", b"\x00" * 32)) == 32


# --- HMAC key hierarchy ------------------------------------------------

def test_persona_key_is_deterministic():
    master = b"\xab" * 32
    assert sc.derive_persona_key(master, "default") == sc.derive_persona_key(master, "default")


def test_persona_key_differs_per_persona():
    master = b"\xab" * 32
    assert sc.derive_persona_key(master, "default") != sc.derive_persona_key(master, "work")


def test_file_key_differs_per_path():
    persona = b"\xcd" * 32
    assert sc.derive_file_key(persona, "memory.md") != sc.derive_file_key(persona, "user-profile.md")


def test_file_key_differs_from_persona_key():
    persona = b"\xcd" * 32
    assert sc.derive_file_key(persona, "memory.md") != persona


def test_persona_isolation_via_hmac_chain():
    """Compromising one persona's file_key should not reveal another persona's file_key."""
    master = b"\xab" * 32
    work_key = sc.derive_file_key(sc.derive_persona_key(master, "work"), "memory.md")
    personal_key = sc.derive_file_key(sc.derive_persona_key(master, "personal"), "memory.md")
    assert work_key != personal_key


# --- AES-256-GCM -------------------------------------------------------

def test_encrypt_decrypt_round_trip():
    key = sc.derive_master_key("pw", b"\x00" * 32)
    plaintext = b"hello world"
    record = sc.encrypt(key, plaintext)
    assert sc.decrypt(key, record["nonce"], record["ciphertext"]) == plaintext


def test_encrypt_uses_random_nonce():
    key = sc.derive_master_key("pw", b"\x00" * 32)
    plaintext = b"same plaintext"
    r1 = sc.encrypt(key, plaintext)
    r2 = sc.encrypt(key, plaintext)
    assert r1["nonce"] != r2["nonce"]
    # Ciphertext also differs because GCM is a stream cipher + tag includes nonce influence
    assert r1["ciphertext"] != r2["ciphertext"]


def test_decrypt_wrong_key_raises_tamper():
    key1 = sc.derive_master_key("pw1", b"\x00" * 32)
    key2 = sc.derive_master_key("pw2", b"\x00" * 32)
    record = sc.encrypt(key1, b"secret")
    try:
        sc.decrypt(key2, record["nonce"], record["ciphertext"])
        assert False, "expected TamperError"
    except sc.TamperError:
        pass


def test_decrypt_tampered_ciphertext_raises():
    key = sc.derive_master_key("pw", b"\x00" * 32)
    record = sc.encrypt(key, b"secret")
    # Flip a bit in the ciphertext body (not the tag) -- should still fail auth.
    raw = bytearray(base64.b64decode(record["ciphertext"]))
    raw[0] ^= 0x01
    tampered = base64.b64encode(bytes(raw)).decode("ascii")
    try:
        sc.decrypt(key, record["nonce"], tampered)
        assert False, "expected TamperError"
    except sc.TamperError:
        pass


def test_decrypt_tampered_nonce_raises():
    key = sc.derive_master_key("pw", b"\x00" * 32)
    record = sc.encrypt(key, b"secret")
    raw = bytearray(base64.b64decode(record["nonce"]))
    raw[0] ^= 0x01
    tampered_nonce = base64.b64encode(bytes(raw)).decode("ascii")
    try:
        sc.decrypt(key, tampered_nonce, record["ciphertext"])
        assert False, "expected TamperError"
    except sc.TamperError:
        pass


def test_encrypt_empty_plaintext():
    key = sc.derive_master_key("pw", b"\x00" * 32)
    record = sc.encrypt(key, b"")
    assert sc.decrypt(key, record["nonce"], record["ciphertext"]) == b""


# --- base58 ------------------------------------------------------------

def test_base58_round_trip_random():
    for _ in range(20):
        data = os.urandom(32)
        encoded = sc.to_base58(data)
        decoded = sc.from_base58(encoded)
        assert decoded == data


def test_base58_leading_zeros():
    # bytes starting with 0x00 must round-trip the leading '1' prefix
    for n_zeros in (1, 2, 3, 4):
        data = b"\x00" * n_zeros + b"\xff" * 30
        assert sc.from_base58(sc.to_base58(data)) == data


def test_base58_empty():
    assert sc.to_base58(b"") == ""
    assert sc.from_base58("") == b""


def test_base58_invalid_char_raises():
    try:
        sc.from_base58("0OIl")
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- recovery key (base58 of master key) -------------------------------

def test_recovery_key_round_trip():
    master = os.urandom(32)
    text = sc.encode_recovery_key(master)
    assert sc.decode_recovery_key(text) == master


# --- persona_id + api_key_id + machine_id ------------------------------

def test_default_persona_id_deterministic_per_salt():
    salt = b"\xab" * 32
    assert sc.default_persona_id(salt) == sc.default_persona_id(salt)


def test_default_persona_id_differs_per_salt():
    assert sc.default_persona_id(b"\x00" * 32) != sc.default_persona_id(b"\x01" * 32)


def test_default_persona_id_is_uuid_string():
    pid = sc.default_persona_id(b"\x00" * 32)
    # uuid5 produces a string with 4 hyphens, length 36
    assert pid.count("-") == 4
    assert len(pid) == 36


def test_api_key_id_is_sha256_hex():
    h = sc.api_key_id("my-secret-key")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_api_key_id_differs_per_key():
    assert sc.api_key_id("a") != sc.api_key_id("b")


def test_machine_id_is_stable_across_calls():
    a = sc.machine_id()
    b = sc.machine_id()
    assert a == b
    assert len(a) == 16  # truncated to 16 hex chars


# --- salt storage ------------------------------------------------------

def test_salt_save_and_load(tmp_path):
    salt_path = tmp_path / "salt"
    salt = sc.generate_salt()
    sc.save_salt(salt_path, salt)
    assert sc.load_salt(salt_path) == salt


def test_salt_file_permissions_are_0600(tmp_path):
    salt_path = tmp_path / "salt"
    sc.save_salt(salt_path, sc.generate_salt())
    mode = salt_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_generate_salt_is_32_bytes():
    assert len(sc.generate_salt()) == 32


def test_generate_salt_is_random():
    assert sc.generate_salt() != sc.generate_salt()


# --- end-to-end: password -> master -> persona -> file -> encrypt ------

def test_full_pipeline_round_trip():
    """Simulates the full Phase 1 sync crypto pipeline."""
    salt = sc.generate_salt()
    master = sc.derive_master_key("correct horse battery staple", salt)
    persona_id = sc.default_persona_id(salt)
    persona_key = sc.derive_persona_key(master, persona_id)
    file_key = sc.derive_file_key(persona_key, "memory.md")

    plaintext = b"# Autolearn Memory\n\n- always use uv\n"
    record = sc.encrypt(file_key, plaintext)

    # Independent re-derivation on a second machine with same password + salt:
    master2 = sc.derive_master_key("correct horse battery staple", salt)
    persona_key2 = sc.derive_persona_key(master2, persona_id)
    file_key2 = sc.derive_file_key(persona_key2, "memory.md")

    assert file_key == file_key2
    assert sc.decrypt(file_key2, record["nonce"], record["ciphertext"]) == plaintext
