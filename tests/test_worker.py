"""Tests for the worker loop. No network, no key, no spend.

These assert the two properties the queue was built for:

  1. The paid id is persisted BEFORE the long poll. This is the crash boundary --
     if it regresses, an interrupted render is money we cannot get back.
  2. An interrupted render is RESUMED, and an interrupted-but-unpaid job is
     REQUEUED. This is restart survival, the reason the queue is a table and not
     the in-memory dict hub/orchestrator.py uses.

Every Runpod/Fish call is mocked, so this runs offline and spends nothing.

Run: python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

from cryptography.fernet import Fernet

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jobs  # noqa: E402
import worker  # noqa: E402

_SPEC = {"price": 0.25, "label": "InfiniteTalk 480p", "size": "480p"}
_PREP = {"spec": _SPEC, "duration": 20.0, "predicted_wall_s": 176.0,
         "photo_fitted": None, "model_key": "infinitetalk"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "jobs.db")
        self.dbpatch = mock.patch.object(jobs, "DB_PATH", self.db)
        self.dbpatch.start()
        self.envkey = mock.patch.dict(
            os.environ, {"HUB_VAULT_KEY": Fernet.generate_key().decode()})
        self.envkey.start()
        jobs._cipher = None
        jobs.init_db()
        # Script generation calls Gemini; stub it everywhere so nothing hits the
        # network. The worker treats an already-set script as "resume", so most
        # tests set the script directly and this guards the fresh path.
        self.script = mock.patch.object(
            worker, "resolve_script", return_value="hello from a script")
        self.script.start()

    def tearDown(self):
        self.dbpatch.stop()
        self.envkey.stop()
        jobs._cipher = None
        self.script.stop()

    def _enqueue(self, engine="runpod", keys=None):
        folder = os.path.join(self.tmp, "j")
        os.makedirs(folder, exist_ok=True)
        return jobs.enqueue(prompt="a topic", own_script=None, engine=engine,
                            folder=folder, img_path=os.path.join(folder, "p.jpg"),
                            voice_engine="fish", keys=keys)


class TestHappyPath(Base):
    def test_runpod_job_runs_to_done_with_folded_metrics(self):
        jid = self._enqueue()
        with mock.patch.object(worker.video, "say",
                               return_value={"est_cost": 0.015, "engine_detail": "Fish"}), \
             mock.patch.object(worker.runpod, "prepare_and_submit",
                               return_value=("rp-1", _PREP)), \
             mock.patch.object(worker.runpod, "resolve_key", return_value="k"), \
             mock.patch.object(worker.runpod, "collect",
                               return_value=("https://x/a.mp4", 0.25, {})), \
             mock.patch.object(worker.runpod, "download",
                               side_effect=lambda u, o, j=None: o):
            self.assertTrue(worker.run_once())

        job = jobs.get(jid)
        self.assertEqual(job["status"], jobs.DONE)
        self.assertEqual(job["runpod_job_id"], "rp-1")
        # render cost and voice cost both survive, folded not overwritten.
        self.assertEqual(job["metrics"]["est_cost"], 0.25)
        self.assertEqual(job["metrics"]["voice_est_cost"], 0.015)
        self.assertAlmostEqual(job["metrics"]["total_cost"], 0.265, places=3)

    def test_non_runpod_engine_goes_wholesale(self):
        """motion/hf/etc. do not get the resumable path; they run make_video."""
        jid = self._enqueue(engine="motion")
        with mock.patch.object(worker.video, "make_video",
                               return_value=("static/jobs/j/video.mp4", "motion",
                                             {"est_cost": 0.0})) as mv:
            self.assertTrue(worker.run_once())
        mv.assert_called_once()
        job = jobs.get(jid)
        self.assertEqual(job["status"], jobs.DONE)
        # no paid id was ever taken on this path
        self.assertIsNone(job["runpod_job_id"])


class TestCrashBoundary(Base):
    def test_id_is_persisted_before_polling(self):
        """The invariant that makes a crash survivable: by the time collect()
        (the long wait) starts, the id is already in the row."""
        jid = self._enqueue()
        seen = {}

        def spy_collect(job_id, key, **kw):
            seen["id_at_poll_time"] = jobs.get(jid)["runpod_job_id"]
            return ("https://x/a.mp4", 0.25, {})

        with mock.patch.object(worker.video, "say", return_value={"est_cost": 0.015}), \
             mock.patch.object(worker.runpod, "prepare_and_submit",
                               return_value=("rp-boundary", _PREP)), \
             mock.patch.object(worker.runpod, "resolve_key", return_value="k"), \
             mock.patch.object(worker.runpod, "collect", side_effect=spy_collect), \
             mock.patch.object(worker.runpod, "download",
                               side_effect=lambda u, o, j=None: o):
            worker.run_once()

        self.assertEqual(seen["id_at_poll_time"], "rp-boundary")


class TestFailuresAreReported(Base):
    def test_a_voice_failure_marks_failed_with_reason(self):
        """No substitution: a failed voice call ends the job as failed, with the
        reason, not as a quieter video."""
        jid = self._enqueue()
        with mock.patch.object(worker.video, "say",
                               side_effect=RuntimeError("Fish rejected the API key")):
            worker.run_once()
        job = jobs.get(jid)
        self.assertEqual(job["status"], jobs.FAILED)
        self.assertIn("Fish rejected", job["error"])
        # failed before submit, so no paid id was taken
        self.assertIsNone(job["runpod_job_id"])


class TestBYOK(Base):
    def test_worker_uses_the_testers_keys_then_wipes_them(self):
        """The tester's Fish key reaches the voice call and their Runpod key
        reaches the render, and both are wiped once the job finishes."""
        jid = self._enqueue(keys={"runpod": "rp-x", "fish": "fish-x"})
        seen = {}

        def cap_say(text, path, **kw):
            seen["fish"] = kw.get("fish_key")
            return {"est_cost": 0.015}

        def cap_prep(img, aud, folder, **kw):
            seen["runpod"] = kw.get("request_key")
            return ("rp-1", _PREP)

        with mock.patch.object(worker.video, "say", side_effect=cap_say), \
             mock.patch.object(worker.runpod, "prepare_and_submit", side_effect=cap_prep), \
             mock.patch.object(worker.runpod, "resolve_key",
                               side_effect=lambda k=None: k or "env"), \
             mock.patch.object(worker.runpod, "collect",
                               return_value=("https://x/a.mp4", 0.25, {})), \
             mock.patch.object(worker.runpod, "download",
                               side_effect=lambda u, o, j=None: o):
            worker.run_once()

        self.assertEqual(seen["fish"], "fish-x")     # their voice key
        self.assertEqual(seen["runpod"], "rp-x")     # their render key
        self.assertEqual(jobs.read_keys(jid), {})    # wiped on completion
        self.assertEqual(jobs.get(jid)["status"], jobs.DONE)

    def test_keys_are_wiped_even_when_the_render_fails(self):
        jid = self._enqueue(keys={"runpod": "rp-x", "fish": "fish-x"})
        with mock.patch.object(worker.video, "say",
                               side_effect=RuntimeError("boom")):
            worker.run_once()
        self.assertEqual(jobs.get(jid)["status"], jobs.FAILED)
        self.assertEqual(jobs.read_keys(jid), {})    # not left behind on failure

    def test_gemini_key_flows_to_the_script_writer(self):
        """The tester's Gemini key reaches script generation (full BYOK)."""
        import generate
        self.script.stop()  # use the real resolve_script for this one
        jid = self._enqueue(keys={"runpod": "rp", "fish": "fi", "gemini": "gk"})
        seen = {}

        def cap_make_script(topic, api_key=None):
            seen["gemini"] = api_key
            return {"script": "s", "title": "t", "description": "", "tags": []}

        try:
            with mock.patch.object(generate, "make_script", side_effect=cap_make_script), \
                 mock.patch.object(worker.video, "say", return_value={"est_cost": 0.015}), \
                 mock.patch.object(worker.runpod, "prepare_and_submit", return_value=("rp-1", _PREP)), \
                 mock.patch.object(worker.runpod, "resolve_key", side_effect=lambda k=None: k or "env"), \
                 mock.patch.object(worker.runpod, "collect", return_value=("u", 0.25, {})), \
                 mock.patch.object(worker.runpod, "download", side_effect=lambda u, o, j=None: o):
                worker.run_once()
        finally:
            self.script.start()  # restore so tearDown stays balanced

        self.assertEqual(seen["gemini"], "gk")
        self.assertEqual(jobs.get(jid)["status"], jobs.DONE)


class TestRestartSurvival(Base):
    def test_a_paid_interrupted_render_is_resumed(self):
        """A job left running WITH a runpod_job_id is re-attached and finished --
        the abandoned-but-paid render is collected, not lost."""
        jid = self._enqueue()
        jobs.claim_next()                       # -> running
        jobs.update(jid, script="already written")
        jobs.set_runpod_job_id(jid, "rp-orphan",
                               predicted_wall_s=176.0, duration_s=20.0)

        with mock.patch.object(worker.runpod, "resolve_key", return_value="k"), \
             mock.patch.object(worker.runpod, "collect",
                               return_value=("https://x/a.mp4", 0.25, {})) as coll, \
             mock.patch.object(worker.runpod, "download",
                               side_effect=lambda u, o, j=None: o):
            worker.recover()

        # It re-attached to the SAME paid id, and did not resubmit.
        coll.assert_called_once()
        self.assertEqual(coll.call_args.args[0], "rp-orphan")
        self.assertEqual(jobs.get(jid)["status"], jobs.DONE)

    def test_an_unpaid_interrupted_job_is_requeued(self):
        """A job left running WITHOUT a runpod_job_id never reached the money hop,
        so it goes back in the queue to be redone from scratch -- safe, because
        nothing was spent."""
        jid = self._enqueue()
        jobs.claim_next()  # -> running, no runpod_job_id
        worker.recover()
        self.assertEqual(jobs.get(jid)["status"], jobs.QUEUED)


if __name__ == "__main__":
    unittest.main()
