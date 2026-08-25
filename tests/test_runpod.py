"""Mock-level tests for the Runpod adapter. No API key, no network, no spend.

Same contract as test_wavespeed.py: every test answers a question we would
otherwise only learn by paying. The questions differ though, because the
economics differ. WaveSpeed's tests mostly ask "does this cost money before it
is refused?"; Runpod charges a flat $0.25 either way, so these ask "does a paid
render lose its output?" and "does anything here split a script and multiply
the bill?".

Run: python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runpod  # noqa: E402


def _resp(status=200, payload=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = json.dumps(payload or {})
    return r


class FakeAPI:
    """Stands in for requests.request. Records calls, replays scripted answers."""

    def __init__(self, statuses=("IN_PROGRESS", "COMPLETED"), output=None):
        self.statuses = list(statuses)
        self.output = output if output is not None else {
            "video_url": "https://video.runpod.ai/abc/out.mp4", "cost": 0.25}
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/run"):
            return _resp(200, {"id": "job-1"})
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        body = {"status": status}
        if status == "COMPLETED":
            body["output"] = self.output
        return _resp(200, body)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.img = os.path.join(self.tmp, "face.jpg")
        self.audio = os.path.join(self.tmp, "voice.mp3")
        with open(self.img, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"i" * 500)
        with open(self.audio, "wb") as f:
            f.write(b"a" * 500)
        runpod.POLL_INTERVAL_S = 0
        self.env = mock.patch.dict(os.environ, {"RUNPOD_API_KEY": "k"})
        self.env.start()
        self.receipts = mock.patch.object(
            runpod, "RECEIPTS_FILE", os.path.join(self.tmp, "r.jsonl"))
        self.receipts.start()

    def tearDown(self):
        self.env.stop()
        self.receipts.stop()


class TestPricingIsFlat(Base):
    """The defining property. If these break, the adapter's design is wrong."""

    def test_price_does_not_depend_on_duration(self):
        _, spec = runpod.spec_for("infinitetalk")
        self.assertEqual(spec["price"], 0.25)
        # Nothing in the spec is per-second. A rate key would be a design bug.
        self.assertNotIn("rates", spec)
        self.assertNotIn("min_billed_s", spec)

    def test_cost_per_minute_falls_as_the_clip_grows(self):
        _, spec = runpod.spec_for("infinitetalk")
        one = runpod.cost_per_minute(60, spec)
        ten = runpod.cost_per_minute(600, spec)
        self.assertAlmostEqual(one, 0.25, places=3)
        self.assertAlmostEqual(ten, 0.025, places=3)
        self.assertLess(ten, one)   # longer is CHEAPER per minute, not dearer

    def test_no_cost_guard_refuses_a_long_script(self):
        """WaveSpeed refuses long audio to protect the wallet. Here that would
        be actively wrong: the bill is identical and the per-minute cost better."""
        _, spec = runpod.spec_for("infinitetalk")
        runpod.check_duration(590, spec)   # must not raise


class TestGuardrails(Base):
    def test_over_length_is_refused_before_spending(self):
        _, spec = runpod.spec_for("infinitetalk")
        with self.assertRaises(runpod.RunpodError) as cm:
            runpod.check_duration(spec["max_audio_s"] + 1, spec)
        self.assertIn("conservative guess", str(cm.exception))

    def test_empty_audio_is_refused(self):
        _, spec = runpod.spec_for("infinitetalk")
        with self.assertRaises(runpod.RunpodError):
            runpod.check_duration(0, spec)

    def test_oversize_payload_is_refused_before_submitting(self):
        big = "d" * (runpod.MAX_REQUEST_BYTES + 10)
        with self.assertRaises(runpod.RunpodError) as cm:
            runpod.check_payload_size(big, "x", 30)
        self.assertIn("10 MB", str(cm.exception))

    def test_missing_key_refuses(self):
        with mock.patch.dict(os.environ, {"RUNPOD_API_KEY": ""}, clear=False):
            with self.assertRaises(runpod.RunpodError):
                runpod.resolve_key()

    def test_unknown_model_lists_the_known_ones(self):
        with self.assertRaises(runpod.RunpodError) as cm:
            runpod.spec_for("nope")
        self.assertIn("infinitetalk", str(cm.exception))


class TestNoThirdPartyUpload(Base):
    """The privacy improvement over wavespeed.py, asserted rather than assumed."""

    def test_files_are_inlined_not_uploaded(self):
        api = FakeAPI()
        with mock.patch("requests.request", api), \
             mock.patch.object(runpod, "audio_duration", return_value=20.0), \
             mock.patch.object(runpod, "download", side_effect=lambda u, o, j=None: o):
            runpod.runpod_render(self.img, self.audio, self.tmp)

        posts = [c for c in api.calls if c[1].endswith("/run")]
        self.assertEqual(len(posts), 1)
        payload = posts[0][2]["json"]["input"]
        self.assertTrue(payload["image"].startswith("data:"))
        self.assertTrue(payload["audio"].startswith("data:"))
        # No upload endpoint should ever be hit.
        self.assertFalse(any("upload" in c[1] for c in api.calls))


class TestAutomaticPhotoFitting(Base):
    """The one automatic rewrite this codebase allows, and why.

    Everything else here refuses to change a request silently. This is permitted
    because it does not change the OUTPUT: the model renders 480p/720p, so
    pixels above that are discarded before rendering. The tests assert it is
    narrow -- only when needed, never upscaling, never silent, and never
    pretending to have succeeded when the file still does not fit.
    """

    def test_small_photo_is_left_completely_alone(self):
        import video
        p, info = video.fit_image_to_budget(self.img, self.tmp, 10_000_000)
        self.assertEqual(p, self.img)
        self.assertIsNone(info)

    def test_render_reports_that_it_shrank_the_photo(self):
        import video
        api = FakeAPI()
        with mock.patch("requests.request", api), \
             mock.patch.object(runpod, "audio_duration", return_value=20.0), \
             mock.patch.object(runpod, "download", side_effect=lambda u, o, j=None: o), \
             mock.patch.object(video, "fit_image_to_budget",
                               return_value=(self.img, {"original_bytes": 14_000_000,
                                                        "fitted_bytes": 170_000,
                                                        "width": 1024,
                                                        "note": "n"})):
            _, m = runpod.runpod_render(self.img, self.audio, self.tmp)
        # Automatic, but visible in the metrics rather than hidden.
        self.assertIsNotNone(m["photo_fitted"])
        self.assertEqual(m["photo_fitted"]["width"], 1024)

    def test_untouched_photo_reports_nothing(self):
        api = FakeAPI()
        with mock.patch("requests.request", api), \
             mock.patch.object(runpod, "audio_duration", return_value=20.0), \
             mock.patch.object(runpod, "download", side_effect=lambda u, o, j=None: o):
            _, m = runpod.runpod_render(self.img, self.audio, self.tmp)
        self.assertIsNone(m["photo_fitted"])

    def test_audio_alone_over_the_limit_is_refused_and_blames_the_audio(self):
        """The photo is negotiable, the audio is the product. If the audio alone
        will not fit, shrinking the photo cannot help and must not be attempted."""
        # Under the duration cap, so the length guard passes and the SIZE guard
        # is the one under test. A very dense mp3 at 800s would do this for real.
        with mock.patch.object(runpod, "audio_duration", return_value=800.0), \
             mock.patch.object(runpod, "data_uri",
                               return_value="d" * (runpod.MAX_REQUEST_BYTES + 10)), \
             mock.patch("requests.request") as req:
            with self.assertRaises(runpod.RunpodError) as cm:
                runpod.runpod_render(self.img, self.audio, self.tmp)
        self.assertIn("audio alone", str(cm.exception))
        self.assertIn("shorter segments", str(cm.exception))
        req.assert_not_called()

    def test_unshrinkable_photo_fails_rather_than_sending_it(self):
        import video
        with mock.patch.object(runpod, "audio_duration", return_value=20.0), \
             mock.patch.object(video, "fit_image_to_budget",
                               side_effect=RuntimeError("could not shrink")), \
             mock.patch("requests.request") as req:
            with self.assertRaises(runpod.RunpodError) as cm:
                runpod.runpod_render(self.img, self.audio, self.tmp)
        self.assertIn("could not shrink", str(cm.exception))
        req.assert_not_called()


class TestReceiptsAndRecovery(Base):
    def test_receipt_is_written_at_submit_not_at_success(self):
        api = FakeAPI(statuses=("FAILED",))
        with mock.patch("requests.request", api), \
             mock.patch.object(runpod, "audio_duration", return_value=20.0):
            with self.assertRaises(runpod.RunpodError):
                runpod.runpod_render(self.img, self.audio, self.tmp)
        with open(runpod.RECEIPTS_FILE) as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["job_id"], "job-1")

    def test_failed_download_keeps_the_job_id(self):
        api = FakeAPI()
        import requests as _r
        with mock.patch("requests.request", api), \
             mock.patch.object(runpod, "audio_duration", return_value=20.0), \
             mock.patch("requests.get", side_effect=_r.RequestException("boom")):
            with self.assertRaises(runpod.RunpodError) as cm:
                runpod.runpod_render(self.img, self.audio, self.tmp)
        self.assertEqual(cm.exception.job_id, "job-1")
        self.assertIn("already", str(cm.exception))

    def test_completed_but_no_video_url_still_names_the_job(self):
        api = FakeAPI(output={"cost": 0.25})   # the 2026-08-06 live shape
        with mock.patch("requests.request", api), \
             mock.patch.object(runpod, "audio_duration", return_value=20.0):
            with self.assertRaises(runpod.RunpodError) as cm:
                runpod.runpod_render(self.img, self.audio, self.tmp)
        self.assertEqual(cm.exception.job_id, "job-1")
        self.assertIn("charged", str(cm.exception))


class TestVideoUrlExtraction(Base):
    """We do not yet know the real response shape, so the walker has to cope."""

    def test_finds_url_in_several_shapes(self):
        for shape in (
            {"video_url": "https://x/a.mp4"},
            {"video": {"url": "https://x/a.mp4"}},
            {"result": ["https://x/a.mp4"]},
            {"data": {"outputs": [{"url": "https://x/a.mp4"}]}},
        ):
            self.assertEqual(runpod._find_video_url(shape), "https://x/a.mp4", shape)

    def test_ignores_thumbnails(self):
        self.assertIsNone(runpod._find_video_url({"thumb": "https://x/a.jpg"}))

    def test_returns_none_when_absent(self):
        self.assertIsNone(runpod._find_video_url({"cost": 0.25}))


class TestMetrics(Base):
    def test_reports_the_actual_charge_not_a_computed_one(self):
        api = FakeAPI(output={"video_url": "https://x/a.mp4", "cost": 0.31})
        with mock.patch("requests.request", api), \
             mock.patch.object(runpod, "audio_duration", return_value=120.0), \
             mock.patch.object(runpod, "download", side_effect=lambda u, o, j=None: o):
            _, m = runpod.runpod_render(self.img, self.audio, self.tmp)
        self.assertEqual(m["est_cost"], 0.31)      # theirs, not ours
        self.assertTrue(m["cost_is_actual"])

    def test_falls_back_to_the_published_price_if_cost_is_missing(self):
        api = FakeAPI(output={"video_url": "https://x/a.mp4"})
        with mock.patch("requests.request", api), \
             mock.patch.object(runpod, "audio_duration", return_value=120.0), \
             mock.patch.object(runpod, "download", side_effect=lambda u, o, j=None: o):
            _, m = runpod.runpod_render(self.img, self.audio, self.tmp)
        self.assertEqual(m["est_cost"], 0.25)
        self.assertFalse(m["cost_is_actual"])

    def test_wall_time_prediction_matches_the_measured_fit(self):
        # 2026-08-06: 4.3s -> 88s, 24.8s -> 202s.
        self.assertAlmostEqual(runpod.predict_wall_s(4.3), 88, delta=8)
        self.assertAlmostEqual(runpod.predict_wall_s(24.8), 202, delta=8)


class TestDispatch(Base):
    def test_video_py_routes_runpod_and_passes_the_model(self):
        import video
        with mock.patch("runpod.runpod_render",
                        return_value=("/v.mp4", {"est_cost": 0.25})) as rr, \
             mock.patch.object(video, "say", return_value={}):
            video.make_video(self.img, "hello", self.tmp,
                             engine="runpod-infinitetalk-720p", runpod_key="k")
        self.assertEqual(rr.call_args.kwargs["model"], "infinitetalk-720p")

    def test_unknown_engine_names_runpod(self):
        import video
        # say() has to be stubbed even here: make_video generates the voiceover
        # before it dispatches, so an unknown engine would otherwise die in TTS
        # rather than at the check we are testing.
        with mock.patch.object(video, "say", return_value={}):
            with self.assertRaises(ValueError) as cm:
                video.make_video(self.img, "hi", self.tmp, engine="bogus")
        self.assertIn("runpod", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
