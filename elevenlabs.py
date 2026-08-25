"""ElevenLabs voice stage: script in, mp3 out, in a voice someone chose.

Sits at the same level as edge-tts -- a voice ENGINE, selected explicitly. It is
never substituted for a failing one and never substitutes for one. A voice is
part of the product, so quietly delivering a different one is the same class of
lie the renderer fallback chain was removed for on 2026-07-28. See make_video().

Why this adapter looks different from wavespeed.py, despite copying its shape:

  wavespeed bills SECONDS OF OUTPUT, so the cost can only be known after the
  audio exists and has been measured. ElevenLabs bills INPUT CHARACTERS, so the
  exact charge is len(text) -- known before anything is spent. The guardrail is
  therefore stronger here: not an estimate, an arithmetic fact.

  wavespeed returns a prediction id, so a paid-for-but-lost render is
  recoverable. This returns raw mp3 bytes synchronously with no id and nothing
  stored server-side, so there is nothing to re-fetch. The receipt still exists,
  but its job is ACCOUNTING -- which generation ate which credits -- not
  recovery. See _log_receipt().

The second-order cost nobody expects (measured reasoning in
raw/prompt2tube-elevenlabs-voice-stage-2026-07-31.md): `speed` is documented as
a delivery setting, but the renderer bills per second of audio, so slowing the
voice down makes the VIDEO more expensive while the TTS charge does not move.
Across the legal 0.7-1.2 range that is roughly a 1.7x swing on the render bill.
This module therefore reports the measured audio duration in its metrics; the
caller multiplies by the renderer's rate. Neither stage can tell the truth alone.

Pricing (as of 2026-07-31, elevenlabs.io/pricing): 1 credit per input character,
~1000 characters per spoken minute. Free 10k credits/mo (~10 min, no commercial
licence, no cloning), Starter $6 (30k, cloning + commercial), Creator $22 (121k).
"""

import hashlib
import json
import os
import shutil
import time

import requests

API = "https://api.elevenlabs.io/v1"
CACHE_DIR = os.path.join("static", "tts-cache")
RECEIPTS_FILE = os.path.join("static", "elevenlabs-receipts.jsonl")

CHARS_PER_MIN = 1000  # docs' own credits<->minutes mapping; used for display only

# Marginal "extra minutes" price by tier, straight off the pricing page
# (2026-07-31). Applied per 1000 characters. This is the cost of the NEXT
# minute, not an allocation of the subscription, so treat it as an order of
# magnitude rather than an invoice line.
TIER_USD_PER_1K_CHARS = {
    "free": 0.36, "starter": 0.20, "creator": 0.18,
    "pro": 0.17, "scale": 0.17, "business": 0.17,
}

SPEED_MIN, SPEED_MAX = 0.7, 1.2  # documented hard range; outside this it is rejected


class ElevenLabsError(RuntimeError):
    """Any failure on the ElevenLabs path.

    Propagates. A failed call must NOT become an edge-tts voiceover: the user
    picked a specific voice, a different voice is a different product, and the
    substitution would be discovered only after the renderer had been paid to
    lip-sync the wrong one.
    """


# --- the per-model part: data, not code --------------------------------------
#
# char_limit:      hard per-request cap. None = undocumented; skip the local
#                  check and let the API refuse, which costs nothing.
# honours_phonemes: whether <phoneme> entries in a pronunciation dictionary fire.
#                  Models that do not honour them SKIP THEM SILENTLY -- no error,
#                  just the wrong pronunciation in a video reported as success.
# normalizes:      whether numbers/dates/currency get read the human way.

MODELS = {
    # The default. Docs name it for "professional content, audiobooks & video
    # narration" and it is the only listed model that normalizes numbers
    # properly ("$1,000,000" -> "one million dollars", where Flash v2.5 says
    # "one thousand thousand dollars"). It does NOT honour phoneme entries --
    # which is fine, because the client-facing pronunciation path emits ALIAS
    # entries, and aliases work on every model. Phonemes are the escape hatch,
    # not the mechanism.
    "multilingual": {
        "model_id": "eleven_multilingual_v2",
        "label": "Multilingual v2",
        "char_limit": 10000,
        "honours_phonemes": False,
        "normalizes": True,
    },
    # The phoneme-capable model, English only. Reach for it only when an alias
    # cannot express the sound.
    "flash": {
        "model_id": "eleven_flash_v2",
        "label": "Flash v2 (English, phoneme-capable)",
        "char_limit": 30000,
        "honours_phonemes": True,
        "normalizes": False,
    },
    # Fastest and cheapest per character, 32 languages. Normalization is
    # DISABLED to hold latency, so avoid it for scripts with prices or dates.
    "flash-multi": {
        "model_id": "eleven_flash_v2_5",
        "label": "Flash v2.5 (32 languages)",
        "char_limit": 40000,
        "honours_phonemes": False,
        "normalizes": False,
    },
    # v3 takes IPA inline in the text rather than via a dictionary, at a
    # documented 80-90% consistency. Included because it is the most capable
    # pronunciation path, flagged because the docs disagree about it: the
    # best-practices page says phoneme tags are flash_v2 ONLY, the API how-to
    # page says flash_v2 AND v3, and the models page does not list v3 at all or
    # give it a character limit. char_limit stays None until measured -- the API
    # rejecting an over-long request is free, guessing a limit is not.
    "v3": {
        "model_id": "eleven_v3",
        "label": "v3 (inline IPA)",
        "char_limit": None,
        "honours_phonemes": True,
        "normalizes": True,
    },
}

DEFAULT_MODEL = "multilingual"


def spec_for(model=None):
    """Resolve a model key to its spec, with a message that lists the options."""
    key = (model or os.environ.get("ELEVENLABS_MODEL", "") or DEFAULT_MODEL).strip().lower()
    if key not in MODELS:
        raise ElevenLabsError("unknown ElevenLabs model {!r}; known: {}".format(
            key, ", ".join(sorted(MODELS))))
    return key, MODELS[key]


def resolve_key(request_key=None):
    """BYOK rule, same as heygen.py and wavespeed.py: a key from the form beats
    the server env key. Used per request, never written to disk.

    Worth asking the account owner for a DEDICATED key rather than their
    personal one: unlike OAuth there is no per-app revocation, so the only way
    to withdraw access later is to delete the key -- which should not also break
    everything else they use ElevenLabs for.
    """
    key = (request_key or "").strip() or os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise ElevenLabsError(
            "no ElevenLabs API key (set ELEVENLABS_API_KEY or paste one in the form)")
    return key


def _call(method, path, key, **kwargs):
    """One place for auth, timeout and error shape. Header is xi-api-key."""
    headers = kwargs.pop("headers", {})
    headers["xi-api-key"] = key
    url = path if path.startswith("http") else API + path
    try:
        r = requests.request(method, url, headers=headers,
                             timeout=kwargs.pop("timeout", 120), **kwargs)
    except requests.RequestException as e:
        raise ElevenLabsError("could not reach ElevenLabs: {}".format(str(e)[:200]))
    if r.status_code in (401, 403):
        raise ElevenLabsError(
            "ElevenLabs rejected the API key (HTTP {})".format(r.status_code))
    if r.status_code == 429:
        raise ElevenLabsError(
            "ElevenLabs rate limit hit (HTTP 429). Free tier allows 4 concurrent "
            "requests, Starter 6 -- retry in a moment or upgrade.")
    if r.status_code >= 400:
        detail = ""
        try:
            body = r.json()
            d = body.get("detail")
            detail = (d.get("message") if isinstance(d, dict) else str(d)) or ""
        except ValueError:
            detail = r.text[:300]
        raise ElevenLabsError("ElevenLabs error (HTTP {}): {}".format(
            r.status_code, detail or "no detail"))
    return r


# --- account ------------------------------------------------------------------

def account(key):
    """Tier, credits and capability flags, from GET /v1/user/subscription.

    This one call answers three questions the UI would otherwise have to ASK the
    user: which plan they are on, how much they have left, and whether cloning
    is available to them. Capability is detected, never assumed.
    """
    body = _call("GET", "/user/subscription", key, timeout=30).json()
    used = body.get("character_count") or 0
    limit = body.get("character_limit") or 0
    return {
        "tier": body.get("tier") or "unknown",
        "used": used,
        "limit": limit,
        "remaining": max(limit - used, 0),
        "resets_unix": body.get("next_character_count_reset_unix"),
        "can_clone_instant": bool(body.get("can_use_instant_voice_cloning")),
        "can_clone_professional": bool(body.get("can_use_professional_voice_cloning")),
        # Surfaced because a render that quietly adds to somebody else's overdue
        # bill is a conversation nobody wants to have after the fact.
        "overage_usd": (body.get("current_overage") or {}).get("amount"),
        "has_open_invoices": bool(body.get("has_open_invoices")),
    }


def account_safe(key):
    """Never let a failed status read kill a render that would have worked."""
    try:
        return account(key)
    except Exception:
        return None


def list_voices(key):
    """Every voice on the account: their clones, designed voices, and premades.

    A brand-new free key still returns the stock voices, so this is never empty
    and the integration works with zero account setup. A paid account with
    clones on it simply makes the same list better -- which is why the plan
    question changes which voices exist, not how any of this works.

    Response field names are VERIFY-ON-FIRST-RUN: the endpoint and its purpose
    are confirmed from the docs, the exact keys are not. Parsing is deliberately
    tolerant rather than assuming a shape we have not seen.
    """
    body = _call("GET", "/voices", key, timeout=60).json()
    raw = body.get("voices") if isinstance(body, dict) else body
    out = []
    for v in raw or []:
        if not isinstance(v, dict):
            continue
        vid = v.get("voice_id") or v.get("id")
        if not vid:
            continue
        out.append({
            "voice_id": vid,
            "name": v.get("name") or vid,
            "category": v.get("category") or "",       # premade / cloned / generated
            "preview_url": v.get("preview_url") or "",
        })
    return out


def resolve_voice(key, request_voice=None):
    """Form voice beats env beats the account's first voice.

    Falling back to "whatever is first" is acceptable ONLY because every account
    has premade voices, so this cannot silently pick a clone of a real person.
    """
    voice = (request_voice or "").strip() or os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
    if voice:
        return voice
    voices = list_voices(key)
    if not voices:
        raise ElevenLabsError(
            "no voices on this ElevenLabs account; set ELEVENLABS_VOICE_ID")
    return voices[0]["voice_id"]


# --- cost + guardrails --------------------------------------------------------

def credits_for(text):
    """The exact charge. Not an estimate -- ElevenLabs bills input characters."""
    return len(text or "")


def estimate_usd(text, tier=None):
    """Order-of-magnitude price, from the tier's marginal per-minute rate."""
    rate = TIER_USD_PER_1K_CHARS.get((tier or "").lower(), TIER_USD_PER_1K_CHARS["free"])
    return round(credits_for(text) / 1000.0 * rate, 4)


def clamp_speed(speed):
    """Hold speed inside the documented range instead of letting the API reject it.

    Clamping rather than raising is the right call here ONLY because speed is a
    delivery preference with a continuous range, not a product promise -- 0.72
    when you asked for 0.6 is the nearest legal reading of the same intent. A
    wrong VOICE would not get the same treatment.
    """
    if speed is None:
        return None
    try:
        s = float(speed)
    except (TypeError, ValueError):
        raise ElevenLabsError("speed must be a number between {} and {}".format(
            SPEED_MIN, SPEED_MAX))
    return max(SPEED_MIN, min(SPEED_MAX, s))


def check_budget(text, spec, acct):
    """Refuse an impossible or unaffordable request BEFORE spending anything.

    Two ceilings, both named with their numbers so the error tells you what to
    do rather than that something went wrong -- same contract as
    wavespeed.check_duration().

    Deliberately NOT truncating the script to fit. That ships a video which
    stops mid-sentence and nobody notices until a client does.
    """
    chars = credits_for(text)
    if chars == 0:
        raise ElevenLabsError("nothing to say: the script is empty")

    limit = spec.get("char_limit")
    if limit and chars > limit:
        raise ElevenLabsError(
            "{} accepts {} characters per request; this script is {}. Shorten it, "
            "or switch to a model with more headroom ({}).".format(
                spec["label"], limit, chars,
                ", ".join(sorted(k for k, s in MODELS.items()
                                 if (s.get("char_limit") or 10 ** 9) > chars))))

    if acct and acct["limit"] and chars > acct["remaining"]:
        raise ElevenLabsError(
            "not enough ElevenLabs credits: this script needs {} but only {} of "
            "{} remain on the {} plan. Shorten the script, upgrade, or wait for "
            "the monthly reset.".format(
                chars, acct["remaining"], acct["limit"], acct["tier"]))


def check_pronunciation_support(spec, dictionary_locators, has_phoneme_rules=False):
    """Refuse a dictionary the chosen model would silently ignore.

    Models that do not support phoneme entries do not error on them -- they skip
    them and speak the default pronunciation. So the failure mode is a video
    that mispronounces the client's own product name, at full render cost,
    reported as success. That is exactly the silent-substitution pattern the
    fallback chain was removed for, so it is refused rather than warned about:
    refusing costs nothing, generating-with-a-warning costs the credits AND the
    render.

    Scoped narrowly on purpose. ALIAS entries (SQL -> "sequel") are plain text
    substitution and work on every model, so they never trigger this. Only
    phoneme entries are model-gated, and the client-facing path should be
    emitting aliases anyway -- which is why this guard is expected to be
    near-dead code in normal use.
    """
    if not dictionary_locators or not has_phoneme_rules:
        return
    if spec.get("honours_phonemes"):
        return
    raise ElevenLabsError(
        "{} silently ignores phoneme entries, so this dictionary would not take "
        "effect and the video would mispronounce the words anyway. Either render "
        "with a phoneme-capable model ({}), or rewrite those entries as aliases "
        "(a plain respelling like \"sequel\" works on every model).".format(
            spec["label"],
            ", ".join(sorted(k for k, s in MODELS.items() if s.get("honours_phonemes")))))


# --- receipts + cache ---------------------------------------------------------

def _fingerprint(text, voice_id, spec, speed, stability, similarity, locators):
    """One hash identifying exactly these bytes of audio.

    Doing two jobs on purpose. As a CACHE KEY it prevents re-buying identical
    audio -- which matters most on the free tier's 10k credits, and matters most
    of all for the renderer head-to-head, whose whole point is feeding two
    models the same audio. As a RECEIPT ID it is the only stable handle on a
    generation, since the API returns no id of its own.

    Every input that changes the output is in here. Anything omitted would cause
    a stale cache hit, which is a wrong voiceover served silently.
    """
    payload = json.dumps({
        "text": text,
        "voice": voice_id,
        "model": spec["model_id"],
        "speed": speed,
        "stability": stability,
        "similarity": similarity,
        "dict": locators or [],
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _cache_enabled():
    """Reuse saves credits, but a cached mp3 is a voice artifact sitting on disk.
    Set ELEVENLABS_TTS_CACHE=0 to opt out -- same choice wavespeed.py offers."""
    val = os.environ.get("ELEVENLABS_TTS_CACHE", "1").strip().strip('"').lower()
    return val not in ("0", "false", "no")


def _log_receipt(record):
    """Append-only record of every billable call, written BEFORE the request.

    Written before rather than after for the same reason wavespeed.py does it:
    once the request leaves, the charge may have happened whatever comes back.
    Unlike wavespeed there is nothing to re-fetch, so this does not recover a
    lost result -- it answers "what ate the credits", which on a 10k/month
    budget is the question that actually gets asked.
    """
    try:
        os.makedirs(os.path.dirname(RECEIPTS_FILE), exist_ok=True)
        with open(RECEIPTS_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass  # a receipt we failed to write must never kill a generation in flight


def _measure_duration(path):
    """Seconds of audio, measured. Feeds the caller's render-cost arithmetic.

    NOTE: audio_duration lives in wavespeed.py because that is where it was
    first needed. Importing a renderer from the voice stage is backwards and it
    should move to a shared module; kept lazy and optional so the wart cannot
    break a generation.
    """
    try:
        from wavespeed import audio_duration
        return round(audio_duration(path), 2)
    except Exception:
        return None


# --- the generation -----------------------------------------------------------

def speak(text, out_path, request_key=None, voice_id=None, model=None,
          speed=None, stability=0.5, similarity=0.75,
          dictionary_locators=None, has_phoneme_rules=False, use_cache=True):
    """Generate the voiceover. Returns (out_path, metrics dict).

    Order matters: everything that can refuse runs before anything that can
    charge, and the cache is checked before the account is even queried.
    """
    key = resolve_key(request_key)
    model_key, spec = spec_for(model)
    speed = clamp_speed(speed if speed is not None
                        else os.environ.get("ELEVENLABS_SPEED") or None)
    started = time.time()

    resolved_voice = voice_id or resolve_voice(key)
    fp = _fingerprint(text, resolved_voice, spec, speed, stability, similarity,
                      dictionary_locators)

    # 1. Cache. Free, so it goes first.
    cached_path = os.path.join(CACHE_DIR, fp + ".mp3")
    if use_cache and _cache_enabled() and os.path.isfile(cached_path):
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        shutil.copy(cached_path, out_path)
        return out_path, {
            "engine_detail": "{} ({}, cached)".format(spec["label"], resolved_voice[:8]),
            "voice_id": resolved_voice,
            "model": spec["model_id"],
            "chars": credits_for(text),
            "credits_used": 0,
            "est_cost": 0.0,
            "cached": True,
            "speed": speed,
            "duration_s": _measure_duration(out_path),
            "render_s": round(time.time() - started),
            "fingerprint": fp,
        }

    # 2. Guardrails. Nothing below this line is free.
    acct = account_safe(key)
    check_budget(text, spec, acct)
    check_pronunciation_support(spec, dictionary_locators, has_phoneme_rules)

    body = {
        "text": text,
        "model_id": spec["model_id"],
        "voice_settings": {"stability": stability, "similarity_boost": similarity},
    }
    if speed is not None:
        body["voice_settings"]["speed"] = speed
    if dictionary_locators:
        body["pronunciation_dictionary_locators"] = dictionary_locators

    _log_receipt({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "fingerprint": fp,
        "voice_id": resolved_voice,
        "model": spec["model_id"],
        "chars": credits_for(text),
        "est_usd": estimate_usd(text, acct["tier"] if acct else None),
        "tier": acct["tier"] if acct else None,
        "out": out_path,
    })

    r = _call("POST", "/text-to-speech/" + resolved_voice, key,
              json=body, params={"output_format": "mp3_44100_128"},
              headers={"Content-Type": "application/json"}, timeout=180)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(r.content)

    if use_cache and _cache_enabled():
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            shutil.copy(out_path, cached_path)
        except OSError:
            pass  # a cache we could not write is a slower next run, not a failure

    duration = _measure_duration(out_path)
    metrics = {
        "engine_detail": "{} ({})".format(spec["label"], resolved_voice[:8]),
        "voice_id": resolved_voice,
        "model": spec["model_id"],
        "chars": credits_for(text),
        "credits_used": credits_for(text),
        "est_cost": estimate_usd(text, acct["tier"] if acct else None),
        "cached": False,
        "speed": speed,
        "duration_s": duration,
        "render_s": round(time.time() - started),
        "fingerprint": fp,
    }
    if acct:
        metrics["tier"] = acct["tier"]
        metrics["credits_left"] = max(acct["remaining"] - credits_for(text), 0)
        if acct["has_open_invoices"]:
            metrics["warning"] = "this ElevenLabs account has open invoices"
    return out_path, metrics


def render_cost_delta(duration_s, usd_per_s):
    """What this audio length will cost to LIP-SYNC, at the renderer's rate.

    Here rather than in the renderer because the number only becomes meaningful
    once the voice settings are known -- speed is chosen in this module and paid
    for in that one. Returned for display; nothing depends on it.
    """
    if not duration_s or not usd_per_s:
        return None
    return round(duration_s * usd_per_s, 3)
