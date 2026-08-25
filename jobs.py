"""The jobs table: one render, its status, and the id it was paid under.

WHY THIS FILE EXISTS (PRODUCT-PLAN 2026-08-14, section 3)
--------------------------------------------------------
Rendering used to hold an HTTP request open for the whole job. At the measured
~64s overhead + ~5.6s per second of video, a 90s reel needs ~9 minutes and a
10-minute video ~an hour -- past any web timeout. So the render has to move off
the request path, and this table is where it goes.

WHY NOT CELERY/RQ/REDIS
-----------------------
Because Runpod ALREADY IS the queue. `/run` returns a job id and holds the work
on their infrastructure; our side never executes the render. So we do not need a
broker to hold work -- we need to REMEMBER an id and come back for it. That is a
row in a table, not a message bus.

And it must be a table, not the in-memory dict hub/orchestrator.py uses for
publish status. The difference is money and time: a publish status is worth
minutes and is cheap to redo, but a Runpod render is PAID AT SUBMIT and can take
an hour. If the worker crashes, an in-memory queue forgets a job we already paid
for. The id in SQLite is what lets a restart re-attach to it instead. This is
the receipts discipline (runpod-receipts.jsonl) made first-class: a paid job is
recoverable by id.

Same stdlib sqlite3 as hub/vault.py -- small enough to swap later, big enough to
survive a restart. WAL mode is on because two processes touch this file at once:
Flask inserts new rows while the worker claims and updates them.

TENANT-AWARE FROM THE START (SaaS direction, 2026-08-15)
--------------------------------------------------------
The product is headed for Rian-as-admin + external clients-as-users, so every
row carries a user_id and an est_cost. Auth itself is Phase 2 and user_id is
nullable until then -- but the COLUMN is here now, because adding it later means
backfilling live rows, and designing it in now costs nothing. It is the cheap
kind of foresight; the expensive kind (billing, per-user key isolation) is
deliberately NOT here yet.
"""

import json
import os
import sqlite3
import time
import uuid

from cryptography.fernet import Fernet

DB_PATH = os.environ.get("JOBS_DB", "jobs.db")

# --- per-job key encryption (BYOK) --------------------------------------------
#
# Testers bring their own Runpod + Fish keys. Because the worker runs the render
# LATER, in a different process, the keys have to be stored with the job -- but
# never in plaintext. They are encrypted here on the way in, decrypted only
# inside the worker for the one render, and wiped the moment the job reaches a
# terminal state (see clear_keys / worker.process). This is a deliberate,
# demo-scoped exception to "never persist a credential", narrowed by: encryption
# at rest, a short lifetime (one render), and a hard wipe on completion.
#
# The key comes from the SAME source as hub/vault.py (HUB_VAULT_KEY env, else
# .secrets/vault.key) so there is one vault key for the whole app. Loaded lazily
# so importing jobs.py never requires the key to exist (tests, tooling).

_VAULT_KEY_FILE = os.path.join(".secrets", "vault.key")
_cipher = None


def _vault_key():
    env_key = os.environ.get("HUB_VAULT_KEY", "").strip()
    if env_key:
        return env_key.encode()
    os.makedirs(".secrets", exist_ok=True)
    if os.path.exists(_VAULT_KEY_FILE):
        with open(_VAULT_KEY_FILE, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    with open(_VAULT_KEY_FILE, "wb") as f:
        f.write(key)
    return key


def _fernet():
    global _cipher
    if _cipher is None:
        _cipher = Fernet(_vault_key())
    return _cipher

# Status is the coarse lifecycle; stage is the fine-grained thing the UI shows.
# Kept separate so the worker can move through stages without the web layer
# having to know the enumeration -- it only ever branches on status.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# Stages are phrased for a human because the progress UI shows them verbatim.
# The set is honest about what actually happens: there is no "adding captions"
# stage because captions are not built yet (Phase 1). Adding a lie to the
# progress bar would be exactly the substitution this codebase refuses.
STAGE_QUEUED = "queued"
STAGE_SCRIPT = "writing_script"
STAGE_VOICE = "generating_voice"
STAGE_RENDER = "rendering"
STAGE_FINALIZE = "finishing"
STAGE_DONE = "done"

# Columns a caller is allowed to update through update(). Anything not here is
# either immutable (id, created, the submitted request) or set through a named
# helper below, so a typo'd key fails loudly instead of silently doing nothing.
_UPDATABLE = {
    "status", "stage", "script", "title", "description", "tags",
    "runpod_job_id", "predicted_wall_s", "duration_s",
    "video_path", "error", "metrics", "est_cost",
}


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets the worker read/claim while Flask is writing a new job, without
    # one blocking the other the way the default rollback journal would.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        user_id TEXT,                    -- nullable until auth (Phase 2); tenant seam
        status TEXT NOT NULL,            -- queued | running | done | failed
        stage TEXT NOT NULL,             -- fine-grained, shown in the UI verbatim
        -- the submitted request (immutable once written):
        prompt TEXT,                     -- the topic, if the script is to be written
        own_script TEXT,                 -- the user's own words, if they brought them
        engine TEXT NOT NULL,            -- e.g. 'runpod', 'runpod-infinitetalk-720p'
        voice_engine TEXT,
        voice_id TEXT,
        voice_model TEXT,
        voice_speed TEXT,
        fish_personal_use INTEGER DEFAULT 0,
        render_prompt TEXT,              -- InfiniteTalk delivery prompt (presets, later)
        folder TEXT NOT NULL,            -- working dir; photo + voice + video live here
        img_path TEXT NOT NULL,
        -- filled in as the worker proceeds:
        script TEXT,                     -- the resolved script (worker writes it)
        title TEXT,
        description TEXT,
        tags TEXT,                       -- JSON list
        runpod_job_id TEXT,              -- THE crown jewel: the id we paid under
        predicted_wall_s REAL,           -- so a resumed job can still report timing
        duration_s REAL,
        video_path TEXT,
        error TEXT,
        metrics TEXT,                    -- JSON of the final metrics dict
        est_cost REAL,                   -- per-job cost, for the ledger (Phase 2)
        keys_blob BLOB,                  -- BYOK: Fernet ciphertext of the tester's
                                         -- keys, wiped when the job finishes
        created REAL NOT NULL,
        updated REAL NOT NULL)""")
    return conn


def init_db():
    """Create the table if absent. Safe to call repeatedly; the worker and the
    web app both call it on startup so neither depends on the other having run."""
    _db().close()


def _row(r):
    if r is None:
        return None
    d = dict(r)
    d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
    d["metrics"] = json.loads(d["metrics"]) if d.get("metrics") else None
    # The ciphertext never leaves this module through the normal row accessor,
    # so a stray get() or the /jobs API can never surface a tester's keys. The
    # worker reads them only through read_keys(), which is explicit.
    d.pop("keys_blob", None)
    return d


def enqueue(prompt, own_script, engine, folder, img_path, user_id=None,
            voice_engine=None, voice_id=None, voice_model=None, voice_speed=None,
            fish_personal_use=False, render_prompt=None, keys=None):
    """Write a queued row and return its id. This is all POST /generate does with
    the render -- it never touches Runpod or Fish, so it returns in milliseconds
    no matter how long the eventual render is.

    `keys` is an optional dict of the tester's BYOK keys (e.g. {"runpod": ...,
    "fish": ...}). It is encrypted before it touches the database; empty/None
    means the worker falls back to server env keys (the shared-key mode)."""
    job_id = uuid.uuid4().hex[:10]
    now = time.time()
    blob = None
    real = {k: v for k, v in (keys or {}).items() if v}
    if real:
        blob = _fernet().encrypt(json.dumps(real).encode())
    with _db() as conn:
        conn.execute(
            """INSERT INTO jobs (id, user_id, status, stage, prompt, own_script,
                 engine, voice_engine, voice_id, voice_model, voice_speed,
                 fish_personal_use, render_prompt, folder, img_path, keys_blob,
                 created, updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, user_id, QUEUED, STAGE_QUEUED, prompt, own_script,
             engine, voice_engine, voice_id, voice_model, voice_speed,
             1 if fish_personal_use else 0, render_prompt, folder, img_path, blob,
             now, now))
    return job_id


def read_keys(job_id):
    """Decrypt and return the tester's BYOK keys for one job, or {} if none.
    The ONLY way keys leave this module; called by the worker at render time."""
    with _db() as conn:
        row = conn.execute("SELECT keys_blob FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
    if not row or row["keys_blob"] is None:
        return {}
    try:
        return json.loads(_fernet().decrypt(row["keys_blob"]).decode())
    except Exception:
        return {}  # unreadable (rotated key etc.) -> behave as no keys


def clear_keys(job_id):
    """Wipe the stored keys. Called when a job reaches a terminal state, so a
    tester's credentials live only as long as their render."""
    with _db() as conn:
        conn.execute("UPDATE jobs SET keys_blob=NULL, updated=? WHERE id=?",
                     (time.time(), job_id))


def get(job_id):
    with _db() as conn:
        return _row(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def claim_next():
    """Atomically take the oldest queued job and mark it running. Returns the
    claimed row, or None if the queue is empty.

    The claim is one transaction under BEGIN IMMEDIATE: the row is selected and
    flipped to running before any other connection can grab the same lock. Today
    there is exactly one worker so contention is theoretical, but writing the
    claim atomically is what lets a second worker be added later (the RQ upgrade
    path) without a race where two workers pay for the same render.
    """
    with _db() as conn:
        conn.isolation_level = None  # take control of the transaction explicitly
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY created LIMIT 1",
                (QUEUED,)).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute("UPDATE jobs SET status=?, stage=?, updated=? WHERE id=?",
                         (RUNNING, STAGE_SCRIPT, time.time(), row["id"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return _row(row)


def update(job_id, **fields):
    """Set one or more updatable columns. Unknown keys raise -- a silent no-op on
    a misspelled column is the kind of bug that hides for a week."""
    bad = set(fields) - _UPDATABLE
    if bad:
        raise KeyError("not updatable: {}".format(", ".join(sorted(bad))))
    if not fields:
        return
    cols = ", ".join("{}=?".format(k) for k in fields)
    vals = list(fields.values()) + [time.time(), job_id]
    with _db() as conn:
        conn.execute("UPDATE jobs SET {}, updated=? WHERE id=?".format(cols), vals)


def set_stage(job_id, stage):
    update(job_id, stage=stage)


def set_runpod_job_id(job_id, runpod_job_id, predicted_wall_s=None, duration_s=None):
    """Persist the paid id the instant submit() returns. This single write is the
    whole restart-survival guarantee: after it, a crash loses no money because the
    id survives in the row and the worker can re-attach on startup."""
    update(job_id, runpod_job_id=runpod_job_id,
           predicted_wall_s=predicted_wall_s, duration_s=duration_s)


def mark_done(job_id, video_path, metrics):
    with _db() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, stage=?, video_path=?, metrics=?, "
            "est_cost=?, error=NULL, updated=? WHERE id=?",
            (DONE, STAGE_DONE, video_path, json.dumps(metrics or {}),
             (metrics or {}).get("total_cost", (metrics or {}).get("est_cost")),
             time.time(), job_id))


def mark_failed(job_id, error):
    """Record the failure and its reason. The reason is shown to the user
    verbatim -- 'the render failed' plus the actual cause -- because the whole
    point of the no-substitution rule is that a failure is reported, not hidden
    behind a lesser result."""
    update(job_id, status=FAILED, error=str(error))


def set_meta(job_id, script, title, description, tags):
    with _db() as conn:
        conn.execute(
            "UPDATE jobs SET script=?, title=?, description=?, tags=?, updated=? "
            "WHERE id=?",
            (script, title, description, json.dumps(tags or []),
             time.time(), job_id))


def interrupted():
    """Jobs left 'running' with no live worker -- i.e. a worker died mid-job.
    Since there is one worker, anything still 'running' at startup was
    interrupted. The caller decides what to do per row (resume the paid ones,
    requeue the rest)."""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE status=? ORDER BY created",
                            (RUNNING,)).fetchall()
    return [_row(r) for r in rows]


def requeue(job_id):
    """Put an interrupted job that had NOT yet been submitted back in the queue.
    Safe precisely because nothing was paid: no runpod_job_id means the money
    hop never happened, so redoing script+voice wastes only local work."""
    update(job_id, status=QUEUED, stage=STAGE_QUEUED, error=None)


def list_for_user(user_id=None, limit=100):
    """Recent jobs, optionally for one user. The seam the admin cost ledger
    (Phase 2) reads through; harmless to have now."""
    with _db() as conn:
        if user_id is None:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created DESC LIMIT ?",
                (limit,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE user_id=? ORDER BY created DESC LIMIT ?",
                (user_id, limit)).fetchall()
    return [_row(r) for r in rows]
