"""Mock-level tests for the ElevenLabs voice adapter. No API key, no network, no spend.

Every test answers a question we would otherwise only learn by paying: does an
over-budget script get refused before the charge? does a failed voice call
quietly become edge-tts? does the cache ever serve the wrong voice? does the
voice's cost silently overwrite the renderer's?

Run: python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import elevenlabs  # noqa: E402
import video  # noqa: E402


def _resp(status=200, payload=None, content=b"ID3fake-mp3-bytes"):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = json.dumps(payload or {})
    r.content = content
    return r


SUB_OK = {
    "tier": "starter",
    "character_count": 1000,
    "character_limit": 30000,
    "can_use_instant_voice_cloning": True,
    "can_use_professional_voice_cloning": False,
    "next_character_count_reset_unix": 1738356858,
    "current_overage": {"amount": "0", "currency": "usd"},
    "has_open_invoices": False,
}

VOICES_OK = {"voices": [
    {"voice_id": "v_premade", "name": "Rachel", "category": "premade"},
    {"voice_id": "v_clone", "name": "Senior", "category": "cloned"},
]}


class FakeAPI:
    """Stands in for requests.request. Records calls, replays scripted answers."""

    def __init__(self, sub=None, voices=None, tts_status=200):
        self.calls = []
        self.sub = SUB_OK if sub is None else sub
        self.voices = VOICES_OK if voices is None else voices
        self.tts_status = tts_status

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/user/subscription"):
            return _resp(200, self.sub)
        if url.endswith("/voices"):
            return _resp(200, self.voices)
        if "/text-to-speech/" in url:
            return _resp(self.tts_status, {"detail": {"message": "nope"}})
        return _resp(200, {})

    def tts_calls(self):
        return [c for c in self.calls if "/text-to-speech/" in c[1]]


class Sandbox(unittest.TestCase):
    """Each test gets its own cwd, so cache and receipt files never leak."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.env = mock.patch.dict(os.environ, {
            "ELEVENLABS_API_KEY": "k_test",
            "ELEVENLABS_VOICE_ID": "v_premade",
            "ELEVENLABS_TTS_CACHE": "1",
        }, clear=False)
        self.env.start()
        # duration measurement shells out to ffprobe; irrelevant to these tests
        self.dur = mock.patch.object(elevenlabs, "_measure_duration", return_value=3.5)
        self.dur.start()

    def tearDown(self):
        self.dur.stop()
        self.env.stop()
        os.chdir(self.cwd)


# --- refuse before you spend --------------------------------------------------

class TestGuardrails(Sandbox):

    def test_over_character_limit_never_reaches_the_api(self):
        api = FakeAPI()
        with mock.patch("requests.request", api):
            with self.assertRaises(elevenlabs.ElevenLabsError) as cm:
                elevenlabs.speak("x" * 10001, "out.mp3", model="multilingual")
        self.assertIn("10000", str(cm.exception))
        self.assertIn("10001", str(cm.exception))
        self.assertEqual(api.tts_calls(), [], "an over-limit request was submitted")

    def test_over_limit_error_names_a_model_that_would_fit(self):
        api = FakeAPI()
        with mock.patch("requests.request", api):
            with self.assertRaises(elevenlabs.ElevenLabsError) as cm:
                elevenlabs.speak("x" * 10001, "out.mp3", model="multilingual")
        self.assertIn("flash", str(cm.exception))

    def test_insufficient_credits_refuses_before_spending(self):
        api = FakeAPI(sub=dict(SUB_OK, character_count=29900, character_limit=30000))
        with mock.patch("requests.request", api):
            with self.assertRaises(elevenlabs.ElevenLabsError) as cm:
                elevenlabs.speak("y" * 500, "out.mp3")
        msg = str(cm.exception)
        self.assertIn("100", msg)   # remaining
        self.assertIn("500", msg)   # needed
        self.assertEqual(api.tts_calls(), [], "we spent credits we did not have")

    def test_empty_script_is_refused(self):
        api = FakeAPI()
        with mock.patch("requests.request", api):
            with self.assertRaises(elevenlabs.ElevenLabsError):
                elevenlabs.speak("", "out.mp3")
        self.assertEqual(api.tts_calls(), [])

    def test_unknown_model_lists_the_known_ones(self):
        with self.assertRaises(elevenlabs.ElevenLabsError) as cm:
            elevenlabs.spec_for("nope")
        self.assertIn("multilingual", str(cm.exception))

    def test_missing_key_is_refused_not_defaulted(self):
        with mock.patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}, clear=False):
            with self.assertRaises(elevenlabs.ElevenLabsError):
                elevenlabs.resolve_key(None)

    def test_undocumented_char_limit_defers_to_the_api(self):
        """v3 has no published limit. Guessing one would refuse valid work; the
        API refusing costs nothing, so the local check is skipped on purpose."""
        self.assertIsNone(elevenlabs.MODELS["v3"]["char_limit"])
        elevenlabs.check_budget("z" * 999999, elevenlabs.MODELS["v3"], None)


# --- the silent-ignore trap ---------------------------------------------------

class TestPronunciationGuard(Sandbox):

    def test_phoneme_rules_on_a_deaf_model_are_refused(self):
        api = FakeAPI()
        with mock.patch("requests.request", api):
            with self.assertRaises(elevenlabs.ElevenLabsError) as cm:
                elevenlabs.speak("hello", "out.mp3", model="multilingual",
                                 dictionary_locators=[{"pronunciation_dictionary_id": "d",
                                                       "version_id": "1"}],
                                 has_phoneme_rules=True)
        msg = str(cm.exception)
        self.assertIn("silently ignores", msg)
        self.assertIn("flash", msg, "the error should name the model that works")
        self.assertIn("alias", msg, "the error should name the cheaper fix")
        self.assertEqual(api.tts_calls(), [],
                         "we paid to render a dictionary the model would drop")

    def test_alias_only_dictionary_passes_on_any_model(self):
        """Aliases are text substitution and work everywhere, so the guard must
        not fire on them -- otherwise it blocks the path clients actually use."""
        api = FakeAPI()
        with mock.patch("requests.request", api):
            elevenlabs.speak("hello", "out.mp3", model="multilingual",
                             dictionary_locators=[{"pronunciation_dictionary_id": "d",
                                                   "version_id": "1"}],
                             has_phoneme_rules=False)
        self.assertEqual(len(api.tts_calls()), 1)

    def test_phoneme_rules_on_a_capable_model_pass(self):
        api = FakeAPI()
        with mock.patch("requests.request", api):
            elevenlabs.speak("hello", "out.mp3", model="flash",
                             dictionary_locators=[{"pronunciation_dictionary_id": "d",
                                                   "version_id": "1"}],
                             has_phoneme_rules=True)
        self.assertEqual(len(api.tts_calls()), 1)


# --- cost, receipts, cache ----------------------------------------------------

class TestAccounting(Sandbox):

    def test_credits_are_exact_characters_not_an_estimate(self):
        self.assertEqual(elevenlabs.credits_for("hello world"), 11)

    def test_receipt_written_before_the_call(self):
        api = FakeAPI(tts_status=500)
        with mock.patch("requests.request", api):
            with self.assertRaises(elevenlabs.ElevenLabsError):
                elevenlabs.speak("hello", "out.mp3")
        with open(elevenlabs.RECEIPTS_FILE) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(rows), 1,
                         "a call that may have been billed left no receipt")
        self.assertEqual(rows[0]["chars"], 5)
        self.assertIn("fingerprint", rows[0])

    def test_cache_hit_costs_nothing_and_skips_the_api(self):
        api = FakeAPI()
        with mock.patch("requests.request", api):
            elevenlabs.speak("same words", "a.mp3")
            _, m2 = elevenlabs.speak("same words", "b.mp3")
        self.assertEqual(len(api.tts_calls()), 1, "identical audio was re-bought")
        self.assertTrue(m2["cached"])
        self.assertEqual(m2["credits_used"], 0)
        self.assertTrue(os.path.isfile("b.mp3"))

    def test_every_setting_that_changes_the_audio_changes_the_key(self):
        """A missed input means a stale hit, which is the wrong voiceover served
        silently -- the exact failure mode this project keeps designing out."""
        spec = elevenlabs.MODELS["multilingual"]
        base = dict(text="hi", voice_id="v1", spec=spec, speed=1.0,
                    stability=0.5, similarity=0.75, locators=None)
        ref = elevenlabs._fingerprint(**base)
        for field, other in [("text", "bye"), ("voice_id", "v2"), ("speed", 0.8),
                             ("stability", 0.9), ("similarity", 0.3),
                             ("locators", [{"pronunciation_dictionary_id": "d"}])]:
            self.assertNotEqual(ref, elevenlabs._fingerprint(**dict(base, **{field: other})),
                                "{} does not affect the cache key".format(field))
        self.assertNotEqual(ref, elevenlabs._fingerprint(
            **dict(base, spec=elevenlabs.MODELS["flash"])), "model does not affect the key")

    def test_cache_can_be_disabled(self):
        api = FakeAPI()
        with mock.patch.dict(os.environ, {"ELEVENLABS_TTS_CACHE": "0"}, clear=False):
            with mock.patch("requests.request", api):
                elevenlabs.speak("same words", "a.mp3")
                elevenlabs.speak("same words", "b.mp3")
        self.assertEqual(len(api.tts_calls()), 2)

    def test_metrics_carry_tier_and_remaining_credits(self):
        api = FakeAPI()
        with mock.patch("requests.request", api):
            _, m = elevenlabs.speak("hello", "out.mp3")
        self.assertEqual(m["tier"], "starter")
        self.assertEqual(m["credits_used"], 5)
        self.assertEqual(m["credits_left"], 30000 - 1000 - 5)

    def test_open_invoices_are_surfaced(self):
        api = FakeAPI(sub=dict(SUB_OK, has_open_invoices=True))
        with mock.patch("requests.request", api):
            _, m = elevenlabs.speak("hello", "out.mp3")
        self.assertIn("warning", m)


# --- account + voices ---------------------------------------------------------

class TestAccount(Sandbox):

    def test_capability_is_detected_not_assumed(self):
        api = FakeAPI()
        with mock.patch("requests.request", api):
            acct = elevenlabs.account("k")
        self.assertTrue(acct["can_clone_instant"])
        self.assertFalse(acct["can_clone_professional"])
        self.assertEqual(acct["remaining"], 29000)

    def test_free_tier_reads_as_no_cloning(self):
        api = FakeAPI(sub={"tier": "free", "character_count": 0,
                           "character_limit": 10000,
                           "can_use_instant_voice_cloning": False,
                           "can_use_professional_voice_cloning": False})
        with mock.patch("requests.request", api):
            acct = elevenlabs.account("k")
        self.assertFalse(acct["can_clone_instant"])
        self.assertEqual(acct["remaining"], 10000)

    def test_voice_list_includes_clones_and_premades(self):
        api = FakeAPI()
        with mock.patch("requests.request", api):
            voices = elevenlabs.list_voices("k")
        self.assertEqual([v["voice_id"] for v in voices], ["v_premade", "v_clone"])
        self.assertEqual(voices[1]["category"], "cloned")

    def test_voice_list_tolerates_an_unexpected_shape(self):
        """Field names are verify-on-first-run, so a surprise must degrade, not crash."""
        api = FakeAPI(voices={"voices": [{"id": "x", "name": "N"}, {"junk": 1}, "nope"]})
        with mock.patch("requests.request", api):
            voices = elevenlabs.list_voices("k")
        self.assertEqual(voices, [{"voice_id": "x", "name": "N",
                                   "category": "", "preview_url": ""}])

    def test_form_voice_beats_env(self):
        api = FakeAPI()
        with mock.patch("requests.request", api):
            self.assertEqual(elevenlabs.resolve_voice("k", "v_form"), "v_form")

    def test_a_failed_status_read_does_not_kill_a_generation(self):
        api = FakeAPI()
        with mock.patch("requests.request", api), \
                mock.patch.object(elevenlabs, "account",
                                  side_effect=RuntimeError("down")):
            _, m = elevenlabs.speak("hello", "out.mp3")
        self.assertNotIn("tier", m)
        self.assertEqual(len(api.tts_calls()), 1)


# --- speed, the cost lever ----------------------------------------------------

class TestSpeed(Sandbox):

    def test_speed_is_clamped_to_the_documented_range(self):
        self.assertEqual(elevenlabs.clamp_speed(0.1), 0.7)
        self.assertEqual(elevenlabs.clamp_speed(5.0), 1.2)
        self.assertEqual(elevenlabs.clamp_speed(1.0), 1.0)
        self.assertIsNone(elevenlabs.clamp_speed(None))

    def test_non_numeric_speed_is_refused(self):
        with self.assertRaises(elevenlabs.ElevenLabsError):
            elevenlabs.clamp_speed("fast")

    def test_speed_reaches_voice_settings(self):
        api = FakeAPI()
        with mock.patch("requests.request", api):
            elevenlabs.speak("hello", "out.mp3", speed=0.8)
        sent = api.tts_calls()[0][2]["json"]
        self.assertEqual(sent["voice_settings"]["speed"], 0.8)

    def test_render_cost_delta_prices_the_audio_length(self):
        # InfiniteTalk 720p is $0.06/s; 86s of slowed audio vs 50s of fast audio
        self.assertEqual(elevenlabs.render_cost_delta(86, 0.06), 5.16)
        self.assertEqual(elevenlabs.render_cost_delta(50, 0.06), 3.0)
        self.assertIsNone(elevenlabs.render_cost_delta(None, 0.06))


# --- no automatic substitution ------------------------------------------------

class TestNoSubstitution(Sandbox):

    def test_failed_elevenlabs_call_does_not_become_edge_tts(self):
        api = FakeAPI(tts_status=500)
        with mock.patch("requests.request", api), \
                mock.patch.object(video, "_edge_say") as edge:
            with self.assertRaises(elevenlabs.ElevenLabsError):
                video.say("hello", "out.mp3", engine="elevenlabs")
        edge.assert_not_called()

    def test_rate_limit_message_explains_concurrency(self):
        api = FakeAPI(tts_status=429)
        with mock.patch("requests.request", api):
            with self.assertRaises(elevenlabs.ElevenLabsError) as cm:
                elevenlabs.speak("hello", "out.mp3")
        self.assertIn("concurrent", str(cm.exception))

    def test_bad_key_is_reported_not_swapped(self):
        api = FakeAPI(tts_status=401)
        with mock.patch("requests.request", api):
            with self.assertRaises(elevenlabs.ElevenLabsError) as cm:
                elevenlabs.speak("hello", "out.mp3")
        self.assertIn("rejected the API key", str(cm.exception))

    def test_there_is_no_auto_voice_engine(self):
        """Auto is where silent substitution comes back wearing a nicer name."""
        with self.assertRaises(ValueError):
            video.say("hello", "out.mp3", engine="auto")

    def test_unknown_voice_engine_lists_the_known_ones(self):
        with self.assertRaises(ValueError) as cm:
            video.say("hello", "out.mp3", engine="espeak")
        self.assertIn("elevenlabs", str(cm.exception))

    def test_edge_remains_the_default(self):
        with mock.patch.object(video, "_edge_say", return_value={}) as edge:
            video.say("hello", "out.mp3")
        edge.assert_called_once()

    def test_a_voice_failure_aborts_before_the_renderer_is_paid(self):
        """say() runs above engine dispatch, so a voice failure must reach the
        caller without any upload happening -- the render is the expensive half."""
        with mock.patch.object(video, "say",
                               side_effect=elevenlabs.ElevenLabsError("no credits")), \
                mock.patch("wavespeed.wavespeed_render") as render:
            with self.assertRaises(elevenlabs.ElevenLabsError):
                video.make_video("p.jpg", "script", self.tmp, engine="wavespeed")
        render.assert_not_called()


# --- metrics merging ----------------------------------------------------------

class TestMetricsMerge(unittest.TestCase):

    def test_voice_cost_does_not_overwrite_render_cost(self):
        merged = video._with_voice(
            {"est_cost": 5.40, "engine_detail": "InfiniteTalk 720p", "duration_s": 90},
            {"est_cost": 0.30, "engine_detail": "Multilingual v2", "duration_s": 90})
        self.assertEqual(merged["est_cost"], 5.40)
        self.assertEqual(merged["voice_est_cost"], 0.30)
        self.assertEqual(merged["engine_detail"], "InfiniteTalk 720p")
        self.assertEqual(merged["voice_engine_detail"], "Multilingual v2")

    def test_total_cost_is_both_stages(self):
        merged = video._with_voice({"est_cost": 5.40}, {"est_cost": 0.30})
        self.assertEqual(merged["total_cost"], 5.70)

    def test_no_voice_metrics_leaves_render_metrics_untouched(self):
        m = {"est_cost": 1.0}
        self.assertEqual(video._with_voice(m, {}), m)


if __name__ == "__main__":
    unittest.main()
