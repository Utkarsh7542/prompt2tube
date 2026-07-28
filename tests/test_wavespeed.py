"""Mock-level tests for the WaveSpeed adapter. No API key, no network, no spend.

Every test here answers a question we would otherwise only learn by paying:
does an over-length script cost money before it is rejected? does a lost
download lose the receipt? does a failed paid render quietly ship a worse video?

Run: python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wavespeed  # noqa: E402


def _resp(status=200, payload=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = json.dumps(payload or {})
    return r


def _write_stub(text, path):
    """Stand-in for say(): make a file, close it, no TTS engine involved."""
    with open(path, "wb") as f:
        f.write(b"x")


class FakeAPI:
    """Stands in for requests.request. Records calls, replays scripted answers."""

    def __init__(self, poll_statuses=("processing", "completed"), outputs=None):
        self.calls = []
        self.poll_statuses = list(poll_statuses)
        self.outputs = outputs if outputs is not None else ["https://cdn/out.mp4"]

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/media/upload/binary"):
            n = sum(1 for c in self.calls if c[1].endswith("/media/upload/binary"))
            return _resp(200, {"code": 200, "data": {"download_url": "https://cdn/up%d" % n}})
        if "/predictions/" in url and url.endswith("/result"):
            status = self.poll_statuses.pop(0) if self.poll_statuses else "completed"
            data = {"status": status}
            if status == "completed":
                data["outputs"] = self.outputs
            if status == "failed":
                data["error"] = "gpu exploded"
            return _resp(200, {"code": 200, "data": data})
        # submit
        return _resp(200, {"code": 200, "data": {"id": "pred_123"}})

    @property
    def submitted(self):
        return [c for c in self.calls
                if c[0] == "POST" and "/media/upload/" not in c[1]]

    def payload(self):
        return self.submitted[0][2]["json"]


class WaveSpeedTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.img = os.path.join(self.tmp, "photo.jpg")
        self.audio = os.path.join(self.tmp, "voice.mp3")
        for p in (self.img, self.audio):
            with open(p, "wb") as f:
                f.write(b"bytes")
        # Keep the cache and receipts out of the real static/ folder.
        self.patches = [
            mock.patch.object(wavespeed, "CACHE_FILE", os.path.join(self.tmp, "cache.json")),
            mock.patch.object(wavespeed, "RECEIPTS_FILE", os.path.join(self.tmp, "r.jsonl")),
            mock.patch.object(wavespeed, "POLL_FIRST_S", 0),
            mock.patch.object(wavespeed, "POLL_MAX_INTERVAL_S", 0),
            mock.patch.dict(os.environ, {"WAVESPEED_API_KEY": "test-key"}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _render(self, api, duration=12.0, model="infinitetalk", download_ok=True):
        with mock.patch.object(wavespeed.requests, "request", api), \
             mock.patch.object(wavespeed, "audio_duration", return_value=duration), \
             mock.patch.object(wavespeed, "download",
                               side_effect=(lambda url, out, pid=None: out) if download_ok
                               else wavespeed.download):
            return wavespeed.wavespeed_render(self.img, self.audio, self.tmp, model=model)

    # --- the happy path ----------------------------------------------------

    def test_infinitetalk_happy_path(self):
        api = FakeAPI()
        path, metrics = self._render(api, duration=12.0)

        self.assertTrue(path.endswith("video.mp4"))
        # Photo and audio each uploaded exactly once, and BOTH URLs reach the model.
        payload = api.payload()
        self.assertEqual(payload["image"], "https://cdn/up1")
        self.assertEqual(payload["audio"], "https://cdn/up2")
        # 12s x $0.015 = $0.18, above the 5s floor.
        self.assertEqual(metrics["est_cost"], 0.18)
        self.assertEqual(metrics["duration_s"], 12.0)
        self.assertEqual(metrics["prediction_id"], "pred_123")

    def test_billing_floor_applies_to_short_clips(self):
        """A 3s clip and a 5s clip cost the same. The estimate must say so."""
        api = FakeAPI()
        _, metrics = self._render(api, duration=3.0)
        self.assertEqual(metrics["est_cost"], round(5 * 0.015, 3))

    def test_ltx_sends_resolution_and_its_own_rate(self):
        api = FakeAPI()
        _, metrics = self._render(api, duration=10.0, model="ltx")
        self.assertEqual(api.payload()["resolution"], "720p")
        self.assertEqual(metrics["est_cost"], 0.30)  # 10s x $0.03

    # --- guardrails: refuse BEFORE spending ---------------------------------

    def test_ltx_rejects_over_cap_without_submitting(self):
        api = FakeAPI()
        with self.assertRaises(wavespeed.WaveSpeedError) as cm:
            self._render(api, duration=90.0, model="ltx")
        msg = str(cm.exception)
        self.assertIn("20s", msg)          # names the limit
        self.assertIn("90s", msg)          # names the actual
        self.assertIn("infinitetalk", msg)  # names the way out
        self.assertEqual(api.calls, [])     # and cost nothing: no upload, no submit

    def test_ltx_rejects_under_minimum_without_submitting(self):
        api = FakeAPI()
        with self.assertRaises(wavespeed.WaveSpeedError):
            self._render(api, duration=2.0, model="ltx")
        self.assertEqual(api.calls, [])

    def test_infinitetalk_accepts_what_ltx_rejects(self):
        api = FakeAPI()
        _, metrics = self._render(api, duration=90.0, model="infinitetalk")
        self.assertEqual(metrics["est_cost"], round(90 * 0.015, 3))

    def test_unknown_model_is_rejected(self):
        with self.assertRaises(wavespeed.WaveSpeedError):
            wavespeed.spec_for("kling")

    def test_infinitetalk_hd_defaults_to_720p_at_the_hd_rate(self):
        """The full model exposes a resolution knob that Fast does not."""
        api = FakeAPI()
        _, metrics = self._render(api, duration=10.0, model="infinitetalk-hd")
        self.assertEqual(api.payload()["resolution"], "720p")
        self.assertEqual(metrics["est_cost"], 0.60)  # 10s x $0.06
        self.assertIn("720p", metrics["engine_detail"])

    def test_adding_a_model_needed_no_new_code_path(self):
        """The registry is data: every model runs the same generic pipeline."""
        for key in ("infinitetalk", "infinitetalk-hd", "ltx"):
            api = FakeAPI()
            path, metrics = self._render(api, duration=10.0, model=key)
            self.assertTrue(path.endswith("video.mp4"), key)
            self.assertIn("prediction_id", metrics)

    # --- receipts: never pay and lose the video -----------------------------

    def test_receipt_is_written_at_submit_not_at_success(self):
        api = FakeAPI(poll_statuses=["failed"])
        with self.assertRaises(wavespeed.WaveSpeedError):
            self._render(api, duration=10.0)
        with open(wavespeed.RECEIPTS_FILE) as f:
            rec = json.loads(f.read().strip())
        self.assertEqual(rec["prediction_id"], "pred_123")
        self.assertEqual(rec["est_cost"], 0.15)

    def test_failed_render_carries_the_prediction_id(self):
        api = FakeAPI(poll_statuses=["failed"])
        with self.assertRaises(wavespeed.WaveSpeedError) as cm:
            self._render(api, duration=10.0)
        self.assertEqual(cm.exception.prediction_id, "pred_123")

    def test_lost_download_still_names_the_receipt(self):
        api = FakeAPI()
        boom = mock.Mock(side_effect=wavespeed.requests.RequestException("socket died"))
        with mock.patch.object(wavespeed.requests, "get", boom):
            with self.assertRaises(wavespeed.WaveSpeedError) as cm:
                self._render(api, duration=10.0, download_ok=False)
        self.assertEqual(cm.exception.prediction_id, "pred_123")
        self.assertIn("already", str(cm.exception))

    def test_completed_but_empty_outputs_is_an_error(self):
        api = FakeAPI(outputs=[])
        with self.assertRaises(wavespeed.WaveSpeedError):
            self._render(api, duration=10.0)

    # --- keys and caching ---------------------------------------------------

    def test_missing_key_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {"WAVESPEED_API_KEY": ""}):
            with self.assertRaises(wavespeed.WaveSpeedError) as cm:
                wavespeed.resolve_key()
        self.assertIn("WAVESPEED_API_KEY", str(cm.exception))

    def test_form_key_beats_env_key(self):
        self.assertEqual(wavespeed.resolve_key("from-form"), "from-form")

    def test_photo_upload_is_cached_but_audio_is_not(self):
        api = FakeAPI()
        self._render(api, duration=10.0)
        api2 = FakeAPI()
        self._render(api2, duration=10.0)
        uploads = [c for c in api2.calls if c[1].endswith("/media/upload/binary")]
        self.assertEqual(len(uploads), 1)  # audio only; the photo URL was reused

    def test_expired_cache_entry_is_ignored(self):
        api = FakeAPI()
        self._render(api, duration=10.0)
        store = wavespeed._load_cache()
        for k in store:
            store[k]["expires"] = 0  # pretend WaveSpeed's 7 days elapsed
        wavespeed._save_cache(store)
        api2 = FakeAPI()
        self._render(api2, duration=10.0)
        uploads = [c for c in api2.calls if c[1].endswith("/media/upload/binary")]
        self.assertEqual(len(uploads), 2)  # re-uploaded rather than sent a dead URL


class NoSubstitutionTests(unittest.TestCase):
    """The chain-level contract: you get the engine you asked for, or an error.

    The old make_video() caught a failure and quietly tried something cheaper,
    which meant a request for lip sync could return a still photo and report
    success. These tests pin down that it no longer can. Each one replaces the
    engines that must NOT run with tripwires -- mocks whose only behaviour is to
    raise if called -- because that is how you assert something did not happen.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.img = os.path.join(self.tmp, "photo.jpg")
        with open(self.img, "wb") as f:
            f.write(b"bytes")
        import video
        self.video = video

    def _tripwires(self, *names):
        return [mock.patch.object(self.video, n,
                                  side_effect=AssertionError("must not run: " + n))
                for n in names]

    def test_failed_wavespeed_raises_and_runs_nothing_else(self):
        with mock.patch.object(self.video, "say", _write_stub), \
             mock.patch("wavespeed.wavespeed_render",
                        side_effect=wavespeed.WaveSpeedError("gpu on fire")), \
             self._tripwires("hf_render", "motion_render")[0], \
             self._tripwires("hf_render", "motion_render")[1]:
            with self.assertRaises(wavespeed.WaveSpeedError) as cm:
                self.video.make_video(self.img, "hello there", self.tmp,
                                      engine="wavespeed")
        self.assertIn("gpu on fire", str(cm.exception))

    def test_failed_heygen_raises_and_runs_nothing_else(self):
        with mock.patch.object(self.video, "say", _write_stub), \
             mock.patch("heygen.heygen_render", side_effect=RuntimeError("no credits")), \
             self._tripwires("hf_render", "motion_render")[0], \
             self._tripwires("hf_render", "motion_render")[1]:
            with self.assertRaises(RuntimeError):
                self.video.make_video(self.img, "hello there", self.tmp, engine="heygen")

    def test_failed_free_render_also_raises(self):
        """The point that widened the change: a FREE substitution is the same
        lie with a smaller invoice. Asking for lip sync and getting a slideshow
        is wrong whether or not money changed hands."""
        with mock.patch.object(self.video, "say", _write_stub), \
             mock.patch.object(self.video, "hf_render",
                               side_effect=RuntimeError("space out of quota")), \
             self._tripwires("motion_render")[0]:
            with self.assertRaises(RuntimeError):
                self.video.make_video(self.img, "hello there", self.tmp, engine="hf")

    def test_motion_still_works_when_chosen_on_purpose(self):
        """It stops being a fallback; it does not stop being an option."""
        with mock.patch.object(self.video, "say", _write_stub), \
             mock.patch.object(self.video, "motion_render", return_value="out.mp4"):
            path, engine, metrics = self.video.make_video(
                self.img, "hello there", self.tmp, engine="motion")
        self.assertEqual((path, engine), ("out.mp4", "motion"))
        self.assertEqual(metrics["est_cost"], 0.0)

    def test_unknown_engine_names_the_valid_ones(self):
        with mock.patch.object(self.video, "say", _write_stub):
            with self.assertRaises(ValueError) as cm:
                self.video.make_video(self.img, "hi", self.tmp, engine="kling")
        self.assertIn("wavespeed", str(cm.exception))

    def test_voice_is_generated_before_the_paid_call(self):
        """TTS moved above the dispatch, so the paid renderer receives audio."""
        seen = {}

        def fake_render(img, audio, folder, model=None, request_key=None, **kw):
            seen["audio_exists"] = os.path.isfile(audio)
            return os.path.join(folder, "video.mp4"), {"est_cost": 0.1}

        with mock.patch.object(self.video, "say", _write_stub), \
             mock.patch("wavespeed.wavespeed_render", fake_render):
            self.video.make_video(self.img, "hello there", self.tmp, engine="wavespeed")
        self.assertTrue(seen["audio_exists"])

    def test_heygen_does_not_pay_for_tts_it_ignores(self):
        """HeyGen does its own TTS, so ours is pure waste on that path."""
        said = []
        with mock.patch.object(self.video, "say", lambda t, p: said.append(p)), \
             mock.patch("heygen.heygen_render", return_value=("v.mp4", {})):
            self.video.make_video(self.img, "hello there", self.tmp, engine="heygen")
        self.assertEqual(said, [])


if __name__ == "__main__":
    unittest.main()
