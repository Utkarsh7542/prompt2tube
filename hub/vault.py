"""Token vault: encrypted-at-rest storage for OAuth tokens.

WHY THIS FILE EXISTS (PRD section 7, the Composio lesson):
tokens are the crown jewels. If someone steals hub.db they must get
ciphertext, not tokens. So:

  - Tokens are encrypted with Fernet (AES-128-CBC + HMAC, from the
    `cryptography` package) BEFORE they touch the database.
  - The encryption key lives OUTSIDE the database: in the HUB_VAULT_KEY
    env var, or in .secrets/vault.key (gitignored). DB file alone = useless.
  - Every read of a token is recorded in an access log table, so misuse
    leaves footprints.

The database is plain sqlite (stdlib) — a POC does not need Postgres,
and the interface below is small enough to swap later.
"""

import json
import os
import sqlite3
import time

from cryptography.fernet import Fernet

DB_PATH = os.environ.get("HUB_DB", "hub.db")
KEY_FILE = os.path.join(".secrets", "vault.key")


def _load_key() -> bytes:
    """Key lookup order: env var beats key file; key file is created on
    first run. Mirrors the BYOK rule used for renderers (explicit beats
    implicit, nothing silently invented)."""
    env_key = os.environ.get("HUB_VAULT_KEY", "").strip()
    if env_key:
        return env_key.encode()
    os.makedirs(".secrets", exist_ok=True)
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(key)
    return key


_fernet = None


def fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS integrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,          -- 'youtube' | 'linkedin' | later: 'facebook', 'instagram'
        display_name TEXT NOT NULL,      -- what the UI shows, e.g. the account name
        token_blob BLOB NOT NULL,        -- Fernet ciphertext of a JSON dict
        created REAL NOT NULL,
        updated REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS access_log (
        ts REAL NOT NULL,
        integration_id INTEGER NOT NULL,
        action TEXT NOT NULL)""")  # action: 'read' | 'refresh' | 'revoke'
    return conn


def save_integration(platform: str, display_name: str, token_dict: dict) -> int:
    """Encrypt the token dict and store it. Returns the integration id.
    token_dict shape is adapter-specific but always JSON-serializable,
    e.g. {access_token, refresh_token, expires_at, extra...}."""
    blob = fernet().encrypt(json.dumps(token_dict).encode())
    now = time.time()
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO integrations (platform, display_name, token_blob, created, updated) "
            "VALUES (?, ?, ?, ?, ?)",
            (platform, display_name, blob, now, now))
        return cur.lastrowid


def update_tokens(integration_id: int, token_dict: dict) -> None:
    """Called after a refresh: new ciphertext, old one gone."""
    blob = fernet().encrypt(json.dumps(token_dict).encode())
    with _db() as conn:
        conn.execute("UPDATE integrations SET token_blob=?, updated=? WHERE id=?",
                     (blob, time.time(), integration_id))
        conn.execute("INSERT INTO access_log VALUES (?, ?, 'refresh')",
                     (time.time(), integration_id))


def read_tokens(integration_id: int) -> dict:
    """The ONLY way tokens leave the vault. Every call is logged."""
    with _db() as conn:
        row = conn.execute("SELECT token_blob FROM integrations WHERE id=?",
                           (integration_id,)).fetchone()
        if row is None:
            raise KeyError("No integration with id %s" % integration_id)
        conn.execute("INSERT INTO access_log VALUES (?, ?, 'read')",
                     (time.time(), integration_id))
    return json.loads(fernet().decrypt(row["token_blob"]))


def list_integrations() -> list:
    """Metadata only — token ciphertext never leaves this module via list."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, platform, display_name, created, updated "
            "FROM integrations ORDER BY platform, id").fetchall()
    return [dict(r) for r in rows]


def delete_integration(integration_id: int) -> None:
    """Disconnect = hard-delete the ciphertext. (The user can also revoke
    app access on the platform side; we tell them to in the UI.)"""
    with _db() as conn:
        conn.execute("DELETE FROM integrations WHERE id=?", (integration_id,))
        conn.execute("INSERT INTO access_log VALUES (?, ?, 'revoke')",
                     (time.time(), integration_id))
