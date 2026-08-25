"""Tests for the jobs table. No network, no key, no spend.

The questions here are the ones the queue exists to answer: does a claimed job
leave the queue exactly once, does the paid id survive, and does a job land in a
terminal state with the numbers the ledger needs. Same contract as the other
suites -- every test replaces something we would otherwise only learn in
production, where the thing at stake is a paid render, not a unit.

Run: python -m unittest discover -s tests -v
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from cryptography.fernet import Fernet

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jobs  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "jobs.db")
        self.patch = mock.patch.object(jobs, "DB_PATH", self.db)
        self.patch.start()
        # Deterministic vault key for BYOK encryption; reset the cached cipher so
        # it is picked up (the cache is process-global).
        self.envkey = mock.patch.dict(
            os.environ, {"HUB_VAULT_KEY": Fernet.generate_key().decode()})
        self.envkey.start()
        jobs._cipher = None
        jobs.init_db()

    def tearDown(self):
        self.patch.stop()
        self.envkey.stop()
        jobs._cipher = None

    def _enqueue(self, **kw):
        kw.setdefault("prompt", "walking is underrated")
        kw.setdefault("own_script", None)
        kw.setdefault("engine", "runpod")
        kw.setdefault("folder", os.path.join(self.tmp, "j"))
        kw.setdefault("img_path", os.path.join(self.tmp, "j", "photo.jpg"))
        return jobs.enqueue(**kw)


class TestEnqueueAndGet(Base):
    def test_new_job_is_queued(self):
        jid = self._enqueue()
        job = jobs.get(jid)
        self.assertEqual(job["status"], jobs.QUEUED)
        self.assertEqual(job["stage"], jobs.STAGE_QUEUED)
        self.assertEqual(job["engine"], "runpod")

    def test_user_id_is_present_but_nullable(self):
        """The tenant seam exists now even though auth is Phase 2."""
        job = jobs.get(self._enqueue())
        self.assertIn("user_id", job)
        self.assertIsNone(job["user_id"])
        tagged = jobs.get(self._enqueue(user_id="u-42"))
        self.assertEqual(tagged["user_id"], "u-42")

    def test_get_unknown_is_none(self):
        self.assertIsNone(jobs.get("nope"))


class TestClaim(Base):
    def test_claim_marks_running_and_advances_stage(self):
        jid = self._enqueue()
        claimed = jobs.claim_next()
        self.assertEqual(claimed["id"], jid)
        self.assertEqual(jobs.get(jid)["status"], jobs.RUNNING)
        self.assertEqual(jobs.get(jid)["stage"], jobs.STAGE_SCRIPT)

    def test_a_job_is_claimed_exactly_once(self):
        """The property a queue lives or dies on: two claims never yield the same
        row, or two workers would pay for the same render."""
        jid = self._enqueue()
        first = jobs.claim_next()
        second = jobs.claim_next()
        self.assertEqual(first["id"], jid)
        self.assertIsNone(second)  # queue now empty, not the same job again

    def test_claim_is_fifo(self):
        a = self._enqueue()
        b = self._enqueue()
        self.assertEqual(jobs.claim_next()["id"], a)
        self.assertEqual(jobs.claim_next()["id"], b)

    def test_claim_empty_queue_returns_none(self):
        self.assertIsNone(jobs.claim_next())


class TestPersistenceAndTerminals(Base):
    def test_runpod_id_survives(self):
        jid = self._enqueue()
        jobs.claim_next()
        jobs.set_runpod_job_id(jid, "rp-123", predicted_wall_s=176.0, duration_s=20.0)
        job = jobs.get(jid)
        self.assertEqual(job["runpod_job_id"], "rp-123")
        self.assertEqual(job["predicted_wall_s"], 176.0)
        self.assertEqual(job["duration_s"], 20.0)

    def test_mark_done_records_cost_for_the_ledger(self):
        jid = self._enqueue()
        jobs.claim_next()
        jobs.mark_done(jid, "static/jobs/x/video.mp4",
                       {"est_cost": 0.25, "total_cost": 0.27})
        job = jobs.get(jid)
        self.assertEqual(job["status"], jobs.DONE)
        self.assertEqual(job["stage"], jobs.STAGE_DONE)
        self.assertEqual(job["video_path"], "static/jobs/x/video.mp4")
        self.assertEqual(job["metrics"]["est_cost"], 0.25)
        # total_cost (voice + render) is what the ledger persists, not the render
        # half alone.
        self.assertEqual(job["est_cost"], 0.27)

    def test_mark_failed_keeps_the_reason(self):
        jid = self._enqueue()
        jobs.claim_next()
        jobs.mark_failed(jid, "Fish rejected the API key")
        job = jobs.get(jid)
        self.assertEqual(job["status"], jobs.FAILED)
        self.assertIn("rejected", job["error"])

    def test_update_rejects_unknown_columns(self):
        """A misspelled column must fail loudly, not silently no-op."""
        jid = self._enqueue()
        with self.assertRaises(KeyError):
            jobs.update(jid, stauts="done")  # typo


class TestRecoverySupport(Base):
    def test_interrupted_lists_running_jobs(self):
        jid = self._enqueue()
        jobs.claim_next()  # -> running
        interrupted = jobs.interrupted()
        self.assertEqual([j["id"] for j in interrupted], [jid])

    def test_requeue_returns_a_job_to_the_queue(self):
        jid = self._enqueue()
        jobs.claim_next()
        jobs.requeue(jid)
        job = jobs.get(jid)
        self.assertEqual(job["status"], jobs.QUEUED)
        self.assertEqual(job["stage"], jobs.STAGE_QUEUED)
        # and it can be claimed again
        self.assertEqual(jobs.claim_next()["id"], jid)


class TestBYOKKeys(Base):
    """Per-job keys: encrypted at rest, readable only by the worker, wiped on
    completion, and never leaked through the ordinary row accessor."""

    def test_keys_round_trip_but_never_leak_through_get(self):
        jid = self._enqueue(keys={"runpod": "rp-secret", "fish": "fish-secret"})
        # the worker can read them back
        self.assertEqual(jobs.read_keys(jid),
                         {"runpod": "rp-secret", "fish": "fish-secret"})
        # but get() (what the /jobs API uses) never carries them
        job = jobs.get(jid)
        self.assertNotIn("keys_blob", job)
        self.assertNotIn("rp-secret", json.dumps(job))

    def test_keys_are_encrypted_at_rest(self):
        jid = self._enqueue(keys={"runpod": "rp-secret"})
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        blob = con.execute("SELECT keys_blob FROM jobs WHERE id=?",
                           (jid,)).fetchone()["keys_blob"]
        con.close()
        self.assertIsNotNone(blob)
        # the plaintext must not appear in the stored bytes
        self.assertNotIn(b"rp-secret", bytes(blob))

    def test_clear_keys_wipes_them(self):
        jid = self._enqueue(keys={"runpod": "rp-secret"})
        jobs.clear_keys(jid)
        self.assertEqual(jobs.read_keys(jid), {})

    def test_no_keys_reads_as_empty(self):
        self.assertEqual(jobs.read_keys(self._enqueue()), {})

    def test_blank_keys_are_not_stored(self):
        """Empty strings (a field left blank) must not create a ciphertext."""
        jid = self._enqueue(keys={"runpod": "", "fish": None})
        self.assertEqual(jobs.read_keys(jid), {})


if __name__ == "__main__":
    unittest.main()
