"""Mock-level tests for the Fish Audio voice engine. No API key, no network, no spend.

Same contract as test_elevenlabs.py and test_runpod.py. The questions specific
to Fish are: does it bill the right unit (bytes, not characters), and does it
refuse the free model for client work?

That second one has no invoice attached, which is exactly why it needs a test.
A cost guardrail announces itself when it fails; a licence guardrail does not.

Run: python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fish  # noqa: E402


def _resp(status=200, content=b"ID3fakemp3bytes", text=None):
    r = mock.Mock()
    r.status_code = status
    r.content = content
    r.text = text if text is not None else ""
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "voice.mp3")
        self.env = mock.patch.dict(os.environ, {"FISH_API_KEY": "k", "FISH_CACHE": "0"})
        self.env.start()
        self.receipts = mock.patch.object(
            fish, "RECEIPTS_FILE", os.path.join(self.tmp, "r.jsonl"))
        self.receipts.start()
        self.cache = mock.patch.object(fish, "CACHE_DIR", os.path.join(self.tmp, "c"))
        self.cache.start()
        self.dur = mock.patch.object(fish, "_measure_duration", return_value=12.0)
        self.dur.start()

    def tearDown(self):
        for p in (self.env, self.receipts, self.cache, self.dur):
            p.stop()


class TestBillingUnit(Base):
    """Fish bills UTF-8 BYTES. ElevenLabs bills characters. Not the same thing."""

    def test_ascii_bytes_equal_characters(self):
        self.assertEqual(fish.credits_for("hello"), 5)

    def test_non_latin_costs_more_than_its_character_count(self):
        text = "नमस्ते"                      # 6 characters
        self.assertEqual(len(text), 6)
        self.assertGreater(fish.credits_for(text), len(text))

    def test_price_matches_the_published_rate(self):
        _, spec = fish.spec_for("s2.1-pro")
        # $15 per 1,000,000 bytes -> a 10,000 byte script is $0.15
        self.assertAlmostEqual(fish.estimate_usd("x" * 10000, spec), 0.15, places=4)

    def test_free_model_is_actually_zero(self):
        _, spec = fish.spec_for("s2.1-pro-free")
        self.assertEqual(fish.estimate_usd("x" * 100000, spec), 0.0)


class TestCommercialGuard(Base):
    """The guardrail with no invoice behind it."""

    def test_free_model_refused_for_client_work(self):
        _, spec = fish.spec_for("s2.1-pro-free")
        with self.assertRaises(fish.FishError) as cm:
            fish.check_commercial(spec, personal_use=False)
        self.assertIn("non-commercial", str(cm.exception))

    def test_free_model_allowed_when_explicitly_personal(self):
        _, spec = fish.spec_for("s2.1-pro-free")
        fish.check_commercial(spec, personal_use=True)   # must not raise

    def test_paid_model_never_needs_the_flag(self):
        _, spec = fish.spec_for("s2.1-pro")
        fish.check_commercial(spec, personal_use=False)  # must not raise

    def test_speak_refuses_free_model_by_default(self):
        with mock.patch("requests.post") as post:
            with self.assertRaises(fish.FishError):
                fish.speak("hi", self.out, model="s2.1-pro-free")
        post.assert_not_called()   # refused BEFORE the call, not after


class TestCrossEngineModelLeak(Base):
    """Regression, 2026-08-06. The ElevenLabs model dropdown is hidden when Fish
    is selected, but a hidden <select> still submits, so "multilingual" reached
    the Fish adapter and killed the render. The UI fix disables the inactive
    picker; this asserts the adapter still explains itself if one slips through."""

    def test_elevenlabs_model_key_names_the_real_problem(self):
        with self.assertRaises(fish.FishError) as cm:
            fish.spec_for("multilingual")
        msg = str(cm.exception)
        self.assertIn("ElevenLabs", msg)
        self.assertIn("out of step", msg)

    def test_every_elevenlabs_key_is_recognised_as_foreign(self):
        for key in ("multilingual", "flash", "flash-multi", "v3"):
            with self.assertRaises(fish.FishError) as cm:
                fish.spec_for(key)
            self.assertIn("ElevenLabs", str(cm.exception), key)

    def test_a_genuinely_unknown_model_still_lists_the_options(self):
        with self.assertRaises(fish.FishError) as cm:
            fish.spec_for("nonsense")
        self.assertIn("s2.1-pro", str(cm.exception))
        self.assertNotIn("ElevenLabs", str(cm.exception))

    def test_none_falls_back_to_the_default_model(self):
        key, spec = fish.spec_for(None)
        self.assertEqual(key, "s2.1-pro")
        self.assertTrue(spec["commercial_ok"])


class TestSpeak(Base):
    def test_writes_audio_and_reports_the_charge(self):
        with mock.patch("requests.post", return_value=_resp()) as post:
            path, m = fish.speak("hello world", self.out)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(m["bytes"], 11)
        self.assertEqual(m["model"], "s2.1-pro")
        self.assertFalse(m["cached"])
        # model goes in the HEADER; putting it in the body silently gets the default
        self.assertEqual(post.call_args.kwargs["headers"]["model"], "s2.1-pro")

    def test_voice_id_is_sent_as_reference_id(self):
        with mock.patch("requests.post", return_value=_resp()) as post:
            fish.speak("hi", self.out, voice_id="abc123")
        self.assertEqual(post.call_args.kwargs["json"]["reference_id"], "abc123")

    def test_empty_text_refused_before_any_call(self):
        with mock.patch("requests.post") as post:
            with self.assertRaises(fish.FishError):
                fish.speak("   ", self.out)
        post.assert_not_called()

    def test_missing_key_refused_before_any_call(self):
        with mock.patch.dict(os.environ, {"FISH_API_KEY": ""}, clear=False):
            with mock.patch("requests.post") as post:
                with self.assertRaises(fish.FishError):
                    fish.speak("hi", self.out)
            post.assert_not_called()

    def test_json_response_is_not_written_as_audio(self):
        """An error envelope that slipped past the status check must not become
        an .mp3 full of JSON that fails much later, inside the renderer."""
        with mock.patch("requests.post",
                        return_value=_resp(content=b'{"error":"nope"}')):
            with self.assertRaises(fish.FishError) as cm:
                fish.speak("hi", self.out)
        self.assertIn("JSON", str(cm.exception))

    def test_insufficient_balance_is_named_clearly(self):
        with mock.patch("requests.post", return_value=_resp(status=402, content=b"")):
            with self.assertRaises(fish.FishError) as cm:
                fish.speak("hi", self.out)
        self.assertIn("balance", str(cm.exception))

    def test_bad_key_is_named_clearly(self):
        with mock.patch("requests.post", return_value=_resp(status=401, content=b"")):
            with self.assertRaises(fish.FishError) as cm:
                fish.speak("hi", self.out)
        self.assertIn("key", str(cm.exception))

    def test_receipt_written_for_a_billable_call(self):
        with mock.patch("requests.post", return_value=_resp()):
            fish.speak("hello world", self.out)
        with open(fish.RECEIPTS_FILE) as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["bytes"], 11)
        self.assertEqual(rec["model"], "s2.1-pro")


class TestNoSilentSubstitution(Base):
    """The 2026-08-02 rule, applied to the third voice engine."""

    def test_failure_raises_rather_than_becoming_edge_tts(self):
        import video
        with mock.patch("requests.post", return_value=_resp(status=500, content=b"x")):
            with self.assertRaises(fish.FishError):
                video.say("hi", self.out, engine="fish")

    def test_personal_use_flag_reaches_the_adapter(self):
        """Without this the free model is unusable from the app, which is how a
        correct guardrail became a blocker on 2026-08-06."""
        import video
        with mock.patch("fish.speak", return_value=(self.out, {})) as sp:
            video.say("hi", self.out, engine="fish", fish_personal_use=True)
        self.assertTrue(sp.call_args.kwargs["personal_use"])

    def test_personal_use_defaults_to_false(self):
        import video
        with mock.patch("fish.speak", return_value=(self.out, {})) as sp:
            video.say("hi", self.out, engine="fish")
        self.assertFalse(sp.call_args.kwargs["personal_use"])

    def test_free_model_works_end_to_end_when_declared_personal(self):
        with mock.patch("requests.post", return_value=_resp()):
            _, m = fish.speak("hi", self.out, model="s2.1-pro-free",
                              personal_use=True)
        self.assertEqual(m["est_cost"], 0.0)
        self.assertFalse(m["commercial_ok"])

    def test_dispatch_reaches_fish_and_passes_the_key(self):
        import video
        with mock.patch("fish.speak", return_value=(self.out, {"ok": 1})) as sp:
            video.say("hi", self.out, engine="fish", fish_key="mykey",
                      voice_id="v1", voice_model="s1")
        self.assertEqual(sp.call_args.kwargs["request_key"], "mykey")
        self.assertEqual(sp.call_args.kwargs["voice_id"], "v1")
        self.assertEqual(sp.call_args.kwargs["model"], "s1")

    def test_unknown_engine_now_lists_fish(self):
        import video
        with self.assertRaises(ValueError) as cm:
            video.say("hi", self.out, engine="bogus")
        self.assertIn("fish", str(cm.exception))


class TestPipelineSeam(Base):
    """Fish -> Runpod end to end, both halves mocked. The point is that the two
    stages meet correctly: one audio file, two sets of metrics, no collision."""

    def test_fish_voice_feeds_runpod_render(self):
        import video
        img = os.path.join(self.tmp, "face.jpg")
        with open(img, "wb") as f:
            f.write(b"\xff\xd8\xff")

        def fake_fish(text, path, **kw):
            with open(path, "wb") as f:
                f.write(b"ID3audio")
            return path, {"est_cost": 0.15, "engine_detail": "Fish S2.1-Pro",
                          "duration_s": 60.0, "bytes": 10000}

        with mock.patch("fish.speak", side_effect=fake_fish), \
             mock.patch("runpod.runpod_render",
                        return_value=("/v.mp4", {"est_cost": 0.25,
                                                 "engine_detail": "InfiniteTalk 480p",
                                                 "duration_s": 60.0})):
            path, eng, m = video.make_video(img, "a sixty second reel script",
                                            self.tmp, engine="runpod",
                                            voice_engine="fish", fish_key="k")

        self.assertEqual(eng, "runpod")
        # Voice keys are prefixed so the voice's cost cannot overwrite the render's.
        self.assertEqual(m["est_cost"], 0.25)
        self.assertEqual(m["voice_est_cost"], 0.15)
        self.assertAlmostEqual(m["total_cost"], 0.40, places=4)

    def test_a_sixty_second_reel_costs_what_we_claim(self):
        """60s reel: ~1000 bytes of script on Fish, flat $0.25 render."""
        _, spec = fish.spec_for("s2.1-pro")
        voice = fish.estimate_usd("x" * 1000, spec)
        render = 0.25
        self.assertAlmostEqual(voice, 0.015, places=4)
        self.assertAlmostEqual(voice + render, 0.265, places=4)


if __name__ == "__main__":
    unittest.main()
