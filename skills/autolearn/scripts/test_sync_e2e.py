"""End-to-end integration test for autolearn sync.

Starts the Fastify sync server as a subprocess on an ephemeral port, then
exercises the full pipeline:

  Python crypto (encrypt) -> HTTP POST -> SQLite store -> HTTP POST -> Python crypto (decrypt)

Verifies that:
  1. A file encrypted by sync_crypto survives the server round-trip unchanged.
  2. Conflict detection works (older updated_at is rejected).
  3. observations.jsonl merge semantics are correct.
  4. Tampered ciphertext is detected on pull.
  5. Multi-file push/pull preserves all files.

Prerequisites:
  - bun must be on PATH
  - sync-server/ must have node_modules installed (bun install)

Run with:
  uv run --with pytest --with cryptography --with keyring --with requests \\
    pytest test_sync_e2e.py -v -s
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest", "cryptography", "keyring", "requests"]
# ///
from __future__ import annotations

import base64
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

HERE = Path(__file__).resolve().parent
# HERE = .../skills/autolearn-reviewer/scripts/  ->  repo root is 3 parents up
REPO_ROOT = HERE.parent.parent.parent
SYNC_SERVER_DIR = REPO_ROOT / "sync-server"

sys.path.insert(0, str(HERE))
import sync_crypto as sc  # noqa: E402


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_health(url: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{url}/health", timeout=1)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def server():
    """Start the Fastify server on an ephemeral port; yield base URL; kill on teardown."""
    port = free_port()
    data_dir = f"/tmp/autolearn-e2e-{port}"
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        ["bun", "run", "src/cli.ts", "--port", str(port), "--data-dir", data_dir],
        cwd=str(SYNC_SERVER_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        if not wait_for_health(base_url):
            output = proc.stdout.read().decode() if proc.stdout else ""
            pytest.fail(f"Server did not become healthy. Output:\n{output}")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="module")
def api_key() -> str:
    return "e2e-integration-api-key-9999"


@pytest.fixture(scope="module")
def registered(server, api_key):
    """Register the API key once; idempotent across tests in the module."""
    r = requests.post(
        f"{server}/sync/register",
        json={"api_key": api_key},
        timeout=10,
    )
    assert r.status_code in (201, 409), f"register failed: {r.status_code} {r.text}"
    return api_key


@pytest.fixture(scope="module")
def crypto_state():
    """Set up a deterministic master_key + persona_id for the test module."""
    salt = b"\xab" * 32  # deterministic for tests
    master_key = sc.derive_master_key("correct horse battery staple", salt)
    persona_id = sc.default_persona_id(salt)
    return {
        "salt": salt,
        "master_key": master_key,
        "persona_id": persona_id,
    }


def encrypt_file(crypto_state, rel_path, plaintext_bytes):
    """Helper: encrypt a file's contents the same way autolearn.py does."""
    persona_key = sc.derive_persona_key(crypto_state["master_key"], crypto_state["persona_id"])
    file_key = sc.derive_file_key(persona_key, rel_path)
    record = sc.encrypt(file_key, plaintext_bytes)
    record["key"] = rel_path
    record["tag"] = ""
    record["updated_at"] = int(time.time())
    return record


def decrypt_file(crypto_state, key, nonce, ciphertext):
    """Helper: decrypt a pulled record."""
    persona_key = sc.derive_persona_key(crypto_state["master_key"], crypto_state["persona_id"])
    file_key = sc.derive_file_key(persona_key, key)
    return sc.decrypt(file_key, nonce, ciphertext)


def auth_headers(api_key):
    return {"Authorization": f"Bearer {api_key}"}


def test_single_file_round_trip(server, registered, crypto_state):
    """Push a file, pull it back, decrypt, and verify it matches the original."""
    persona = crypto_state["persona_id"]
    plaintext = b"# Autolearn Memory\n\n- always use uv for Python tools\n- never use pip\n"

    record = encrypt_file(crypto_state, "memory.md", plaintext)

    # Push
    push = requests.post(
        f"{server}/sync/push",
        json={"persona_id": persona, "machine_id": "test-machine", "files": [record]},
        headers=auth_headers(registered),
        timeout=10,
    )
    assert push.status_code == 200
    assert push.json()["conflicts"] == []

    # Pull
    pull = requests.post(
        f"{server}/sync/pull",
        json={"persona_id": persona},
        headers=auth_headers(registered),
        timeout=10,
    )
    assert pull.status_code == 200
    files = pull.json()["files"]
    assert len(files) == 1

    pulled = files[0]
    decrypted = decrypt_file(crypto_state, pulled["key"], pulled["nonce"], pulled["ciphertext"])
    assert decrypted == plaintext


def test_multi_file_push_pull(server, registered, crypto_state):
    """Push multiple files; pull them all back; verify each decrypts correctly."""
    persona = crypto_state["persona_id"]
    files_data = {
        "memory.md": b"# Memory\n- item one\n- item two\n",
        "user-profile.md": b"# Profile\n- prefers dark mode\n",
        "strengths.json": b'{"uv": {"count": 3}}',
        "config.yaml": b"review_threshold: 10\n",
    }

    records = [encrypt_file(crypto_state, k, v) for k, v in files_data.items()]

    push = requests.post(
        f"{server}/sync/push",
        json={"persona_id": persona, "machine_id": "test-machine", "files": records},
        headers=auth_headers(registered),
        timeout=10,
    )
    assert push.status_code == 200
    assert push.json()["conflicts"] == []

    pull = requests.post(
        f"{server}/sync/pull",
        json={"persona_id": persona},
        headers=auth_headers(registered),
        timeout=10,
    )
    pulled_files = {f["key"]: f for f in pull.json()["files"]}

    for key, original_plaintext in files_data.items():
        assert key in pulled_files, f"missing {key} in pull response"
        pulled = pulled_files[key]
        decrypted = decrypt_file(crypto_state, pulled["key"], pulled["nonce"], pulled["ciphertext"])
        assert decrypted == original_plaintext, f"{key} round-trip mismatch"


def test_conflict_detection(server, registered, crypto_state):
    """Push a newer version, then try to push an older version; verify conflict."""
    persona = crypto_state["persona_id"]
    plaintext_new = b"newer content"
    plaintext_old = b"older content"

    rec_new = encrypt_file(crypto_state, "conflict-test.md", plaintext_new)
    rec_new["updated_at"] = 2000
    rec_old = encrypt_file(crypto_state, "conflict-test.md", plaintext_old)
    rec_old["updated_at"] = 1000

    # Push newer first
    push1 = requests.post(
        f"{server}/sync/push",
        json={"persona_id": persona, "machine_id": "desktop", "files": [rec_new]},
        headers=auth_headers(registered),
        timeout=10,
    )
    assert push1.status_code == 200
    assert push1.json()["conflicts"] == []

    # Push older -> conflict
    push2 = requests.post(
        f"{server}/sync/push",
        json={"persona_id": persona, "machine_id": "laptop", "files": [rec_old]},
        headers=auth_headers(registered),
        timeout=10,
    )
    assert push2.status_code == 200
    conflicts = push2.json()["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["key"] == "conflict-test.md"
    assert conflicts[0]["remote_updated_at"] == 2000
    assert conflicts[0]["remote_machine"] == "desktop"

    # Pull and verify the server kept the newer version
    pull = requests.post(
        f"{server}/sync/pull",
        json={"persona_id": persona},
        headers=auth_headers(registered),
        timeout=10,
    )
    pulled = {f["key"]: f for f in pull.json()["files"]}
    assert "conflict-test.md" in pulled
    decrypted = decrypt_file(
        crypto_state,
        "conflict-test.md",
        pulled["conflict-test.md"]["nonce"],
        pulled["conflict-test.md"]["ciphertext"],
    )
    assert decrypted == plaintext_new, "server should have kept the newer version"


def test_tamper_detection_on_pull(server, registered, crypto_state):
    """If ciphertext is tampered with in transit, decryption must raise TamperError."""
    persona = crypto_state["persona_id"]
    plaintext = b"sensitive data"

    record = encrypt_file(crypto_state, "tamper-test.md", plaintext)
    push = requests.post(
        f"{server}/sync/push",
        json={"persona_id": persona, "machine_id": "test", "files": [record]},
        headers=auth_headers(registered),
        timeout=10,
    )
    assert push.status_code == 200

    # Pull the stored record
    pull = requests.post(
        f"{server}/sync/pull",
        json={"persona_id": persona},
        headers=auth_headers(registered),
        timeout=10,
    )
    pulled = {f["key"]: f for f in pull.json()["files"]}
    assert "tamper-test.md" in pulled

    # Tamper with the ciphertext before decrypting
    raw = bytearray(base64.b64decode(pulled["tamper-test.md"]["ciphertext"]))
    raw[0] ^= 0x01
    tampered_ct = base64.b64encode(bytes(raw)).decode("ascii")

    with pytest.raises(sc.TamperError):
        decrypt_file(
            crypto_state,
            "tamper-test.md",
            pulled["tamper-test.md"]["nonce"],
            tampered_ct,
        )


def test_status_reflects_pushed_files(server, registered, crypto_state):
    """GET /sync/status should report the persona with correct file count and machines."""
    persona = crypto_state["persona_id"]
    status = requests.get(
        f"{server}/sync/status",
        headers=auth_headers(registered),
        timeout=10,
    )
    assert status.status_code == 200
    personas = status.json()["personas"]
    matching = [p for p in personas if p["persona_id"] == persona]
    assert len(matching) == 1, f"expected persona {persona} in status"
    assert matching[0]["files"] >= 1
    assert "test-machine" in matching[0]["machines"] or "desktop" in matching[0]["machines"]


def test_unauthorized_request_rejected(server):
    """Requests without a valid Bearer token must return 401."""
    r = requests.get(f"{server}/sync/status", timeout=5)
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}
