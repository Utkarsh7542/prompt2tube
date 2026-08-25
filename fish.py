"""Fish Audio voice engine: text in, mp3 out.

A third voice engine alongside edge-tts (free) and ElevenLabs (paid), selected
the same way and substituted for neither. The 2026-08-02 rule stands: a failed
Fish call raises rather than quietly becoming edge-tts, because the voice is
part of the product and a different voice is a different product.

WHY IT IS WORTH ADDING AT ALL
-----------------------------
Rian already pays for ElevenLabs, so a cheaper TTS would normally be a
nice-to-have. What changed on 2026-08-06 is the renderer: Runpod's InfiniteTalk
endpoint charges a FLAT $0.25 per video at any length, measured live from 4
seconds to 10 minutes. Once the render stops scaling with duration and the voice
does not, the voice becomes the dominant cost of a long video.

    10-minute video          voice     render     total
    ElevenLabs Creator       $1.80     $0.25      $2.05   (voice = 88%)
    Fish s2.1-pro            $0.15     $0.25      $0.40

That inverts the cost-menu note of 2026-08-02 which said "voice is not the
expensive part". It was true when renderers billed per second.

BILLING IS UTF-8 BYTES, NOT CHARACTERS
--------------------------------------
$15.00 per million UTF-8 bytes (fact, docs.fish.audio pricing, 2026-08-06).
elevenlabs.py bills input CHARACTERS, so the two are only comparable for ASCII.
An accented or non-Latin script costs 2-4 bytes per character here, so a Hindi
or Arabic client script is several times its character count. credits_for()
therefore measures bytes, deliberately, and must not be "simplified" to len().

THE FREE MODEL IS NOT FREE FOR CLIENT WORK
------------------------------------------
`s2.1-pro-free` is the same model at $0. Their developer docs describe it as
suited to "testing, prototyping, development, and smaller businesses", which
reads as permitting business use. Their Terms of Service say otherwise, and the
Terms win:

    "You will only use the Services for your own internal, personal,
     non-commercial use, and not on behalf of or for the benefit of any third
     party ... Notwithstanding the foregoing, if you are a user of Paid
     Services, you are licensed to use the Services for commercial uses"
    (fish.audio/terms, fetched 2026-08-06)

So the free model is for our own testing only. It is selectable, because
refusing to expose it would just mean someone uses it by hand instead, but it
carries commercial_ok=False and speak() refuses it unless the caller explicitly
says the output is not for a client. Same shape as every other guardrail here:
name the constraint, refuse before it matters, make the override deliberate.

THEY TRAIN ON YOUR CONTENT
--------------------------
    "Usage Data and Content may be used to develop, train, or enhance
     artificial intelligence or machine learning models that are part of
     Fish.Audio's products and services"  (fish.audio/terms, 2026-08-06)

For prompt2tube that means a client's script and, if cloning is used, a client's
voice. Same family as the Higgsfield licence note and the WaveSpeed CDN note in
renderer-notes.md. It belongs in the PRD's data-handling section. Not a blocker;
not something to discover later either.
"""

import hashlib
import json
import os
import shutil
import subprocess
import time

import requests

API = "https://api.fish.audio"
TTS_URL = API + "/v1/tts"
CACHE_DIR = os.path.join("static", "fish-cache")
RECEIPTS_FILE = os.path.join("static", "fish-receipts.jsonl")

# $15.00 per 1,000,000 UTF-8 bytes (docs.fish.audio, 2026-08-06).
USD_PER_MILLION_BYTES = 15.00

# Their docs say 1M bytes is "approximately 180,000 English words, or about 12
# hours of speech", which implies 250 wpm. That is fast for narration and
# disagrees with the 150 wpm this codebase uses everywhere else. Used for
# display only; the byte count is what is billed and what we measure.
BYTES_PER_MIN = 1000


class FishError(RuntimeError):
    """Any failure on the Fish path.

    Propagates, like ElevenLabsError. A failed call must NOT become an edge-tts
    voiceover: the substitution would only be discovered after the renderer had
    been paid to lip-sync the wrong voice.
    """


MODELS = {
    # The recommended production model. 83 languages, free-form [bracket]
    # expression tags rather than a fixed vocabulary.
    "s2.1-pro": {
        "model_id": "s2.1-pro",
        "label": "Fish S2.1-Pro",
        "usd_per_m_bytes": 15.00,
        "commercial_ok": True,
    },
    # Same model, $0, and NOT licensed for client work. See the module docstring.
    "s2.1-pro-free": {
        "model_id": "s2.1-pro-free",
        "label": "Fish S2.1-Pro Free (personal use only)",
        "usd_per_m_bytes": 0.00,
        "commercial_ok": False,
    },
    # Previous generation, documented as open-source, 100ms time-to-first-audio.
    "s2-pro": {
        "model_id": "s2-pro",
        "label": "Fish S2-Pro",
        "usd_per_m_bytes": 15.00,
        "commercial_ok": True,
    },
    "s1": {
        "model_id": "s1",
        "label": "Fish S1",
        "usd_per_m_bytes": 15.00,
        "commercial_ok": True,
    },
}

DEFAULT_MODEL = "s2.1-pro"


# Model keys belonging to the OTHER voice engine. A request carrying one of
# these did not choose a bad Fish model, it leaked a setting across engines --
# which happened for real on 2026-08-06, when the hidden ElevenLabs model
# dropdown kept submitting "multilingual" after the engine was switched to Fish.
# Naming the cause is worth more than listing the valid options.
_ELEVENLABS_MODEL_KEYS = {"multilingual", "flash", "flash-multi", "v3"}


def spec_for(model):
    key = (model or DEFAULT_MODEL).strip().lower()
    if key not in MODELS:
        if key in _ELEVENLABS_MODEL_KEYS:
            raise FishError(
                "{!r} is an ElevenLabs model, not a Fish one -- the voice engine "
                "and the model picker have got out of step. Pick a Fish model "
                "({}), or switch the voice engine back to ElevenLabs.".format(
                    key, ", ".join(sorted(MODELS))))
        raise FishError("unknown Fish model {!r}; known: {}".format(
            key, ", ".join(sorted(MODELS))))
    return key, MODELS[key]


def resolve_key(request_key=None):
    """BYOK, same rule as heygen.py, wavespeed.py, elevenlabs.py and runpod.py."""
    key = (request_key or "").strip() or os.environ.get("FISH_API_KEY", "").strip()
    if not key:
        raise FishError("no Fish Audio API key (set FISH_API_KEY or paste one in the form)")
    return key


# --- cost ---------------------------------------------------------------------

def credits_for(text):
    """The exact charge unit: UTF-8 BYTES, not characters.

    Not an estimate. Fish bills input bytes, so this is knowable before anything
    is spent -- the same property that let elevenlabs.py make its guardrail
    exact rather than probabilistic.
    """
    return len(text.encode("utf-8"))


def estimate_usd(text, spec):
    return round(credits_for(text) / 1_000_000.0 * spec["usd_per_m_bytes"], 6)


def check_commercial(spec, personal_use):
    """Refuse the free model for anything client-facing.

    Costs nothing to run, which is exactly why it is easy to get wrong: there is
    no invoice to notice. The failure mode is a licence breach discovered by
    somebody else, so it fails loudly here instead.
    """
    if not spec["commercial_ok"] and not personal_use:
        raise FishError(
            "{} is free but Fish's Terms licence it for personal, non-commercial "
            "use only; commercial use requires Paid Services. Use model "
            "'s2.1-pro' for anything client-facing, or pass personal_use=True if "
            "this really is our own testing.".format(spec["label"]))


# --- receipts + cache ---------------------------------------------------------

def _log_receipt(record):
    try:
        os.makedirs(os.path.dirname(RECEIPTS_FILE), exist_ok=True)
        with open(RECEIPTS_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _key_tag(key):
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _fingerprint(text, spec, reference_id, key):
    """Cache key. Includes the account, so one workspace's audio is never served
    to another -- the same precaution heygen.py and wavespeed.py take."""
    h = hashlib.sha256()
    for part in (text, spec["model_id"], reference_id or "", _key_tag(key)):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:24]


def _cache_enabled():
    val = os.environ.get("FISH_CACHE", "1").strip().strip('"').lower()
    return val not in ("0", "false", "no")


def _measure_duration(path):
    """Seconds of audio, measured. Reused from the WaveSpeed path rather than
    reimplemented -- the renderer bills on duration and needs the same number."""
    try:
        from wavespeed import audio_duration
        return round(audio_duration(path), 2)
    except Exception:
        return None


# --- the call -----------------------------------------------------------------

def speak(text, out_path, request_key=None, voice_id=None, model=None,
          use_cache=True, personal_use=False, timeout=300):
    """Generate the voiceover. Returns (out_path, metrics dict).

    Order matters, same as elevenlabs.py: everything that can refuse runs before
    anything that can charge, and the cache is checked first because it is free.

    `voice_id` is Fish's `reference_id` -- a cloned or library voice. Omitted,
    the model uses its default.
    """
    if not (text or "").strip():
        raise FishError("nothing to speak")

    key = resolve_key(request_key)
    model_key, spec = spec_for(model)
    started = time.time()

    fp = _fingerprint(text, spec, voice_id, key)
    cached = os.path.join(CACHE_DIR, fp + ".mp3")
    if use_cache and _cache_enabled() and os.path.isfile(cached):
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        shutil.copy(cached, out_path)
        return out_path, {
            "engine_detail": "{} (cached)".format(spec["label"]),
            "model": spec["model_id"],
            "voice_id": voice_id,
            "bytes": credits_for(text),
            "est_cost": 0.0,
            "cached": True,
            "duration_s": _measure_duration(out_path),
            "render_s": round(time.time() - started),
            "fingerprint": fp,
        }

    # Guardrails. Nothing below this line is free -- or, for the free model,
    # nothing below this line is licensed.
    check_commercial(spec, personal_use)
    est = estimate_usd(text, spec)

    body = {"text": text}
    if voice_id:
        body["reference_id"] = voice_id

    try:
        r = requests.post(TTS_URL, headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            # Fish selects the model by HEADER, not in the body. Putting it in
            # the body silently gets the default model, which is the kind of
            # substitution this codebase exists to avoid.
            "model": spec["model_id"],
        }, json=body, timeout=timeout)
    except requests.RequestException as e:
        raise FishError("could not reach Fish Audio: {}".format(str(e)[:200]))

    if r.status_code in (401, 403):
        raise FishError("Fish Audio rejected the API key (HTTP {})".format(r.status_code))
    if r.status_code == 402:
        raise FishError("Fish Audio reports insufficient balance (HTTP 402). "
                        "Top up, or use model 's2.1-pro-free' for our own testing.")
    if r.status_code >= 400:
        detail = r.text[:300]
        raise FishError("Fish Audio error (HTTP {}): {}".format(r.status_code, detail))

    audio = r.content
    if not audio:
        raise FishError("Fish Audio returned an empty response")
    # A JSON body where audio was expected means an error envelope slipped past
    # the status check. Better to say so than to write a .mp3 full of JSON.
    if audio[:1] == b"{":
        raise FishError("Fish Audio returned JSON, not audio: {}".format(
            audio[:200].decode("utf-8", "replace")))

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(audio)

    if use_cache and _cache_enabled():
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            shutil.copy(out_path, cached)
        except OSError:
            pass

    duration = _measure_duration(out_path)
    _log_receipt({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": spec["model_id"],
        "voice_id": voice_id,
        "bytes": credits_for(text),
        "est_cost": est,
        "duration_s": duration,
        "fingerprint": fp,
    })

    return out_path, {
        "engine_detail": "{}{}".format(
            spec["label"], " · " + voice_id[:8] if voice_id else ""),
        "model": spec["model_id"],
        "voice_id": voice_id,
        "bytes": credits_for(text),
        "est_cost": est,
        "cached": False,
        "commercial_ok": spec["commercial_ok"],
        "duration_s": duration,
        "render_s": round(time.time() - started),
        "fingerprint": fp,
    }


# --- voices -------------------------------------------------------------------

def list_voices(key=None, page_size=50):
    """The account's voice models, for the UI dropdown."""
    key = resolve_key(key)
    try:
        r = requests.get(API + "/model", headers={"Authorization": "Bearer " + key},
                         params={"page_size": page_size, "self": "true"}, timeout=60)
    except requests.RequestException as e:
        raise FishError("could not reach Fish Audio: {}".format(str(e)[:200]))
    if r.status_code >= 400:
        raise FishError("Fish Audio error (HTTP {}): {}".format(r.status_code, r.text[:200]))
    try:
        body = r.json()
    except ValueError:
        raise FishError("Fish Audio returned a non-JSON voice list")
    items = body.get("items") if isinstance(body, dict) else body
    out = []
    for it in items or []:
        vid = it.get("_id") or it.get("id")
        if vid:
            out.append({"voice_id": vid,
                        "name": it.get("title") or it.get("name") or vid[:8],
                        "state": it.get("state")})
    return out
