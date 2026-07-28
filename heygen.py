"""HeyGen renderer: photo + script in, lip-synced mp4 out, on HeyGen's paid API.

Flow: resolve key -> upload photo (cached by content hash) -> pick a voice
-> POST /v2/video/generate -> poll /v1/video_status.get -> download mp4.
Pricing (as of 2026-07): ~$1 per output minute standard, $4/min Avatar IV.
"""

import hashlib
import json
import os
import time

import requests

API = "https://api.heygen.com"
UPLOAD = "https://upload.heygen.com"
CACHE_FILE = os.path.join("static", "heygen_cache.json")
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
POLL_EVERY_S = 5
POLL_MAX_S = 600
WORDS_PER_MIN = 150  # average speaking rate, used only for the cost estimate


class HeyGenError(RuntimeError):
    """Any failure on the HeyGen path. Propagates to the caller: renderers do
    not substitute for one another, so a failure here is reported, not swapped
    for a cheaper render the user did not ask for."""


def avatar_iv_enabled():
    """Robust flag read: tolerates quotes and stray spaces from .env editing."""
    val = os.environ.get("HEYGEN_AVATAR_IV", "").strip().strip('"').strip("'").lower()
    return val in ("1", "true", "yes")


def resolve_key(request_key=None):
    """BYOK rule: a key pasted into the form wins over the server's env key."""
    key = (request_key or "").strip() or os.environ.get("HEYGEN_API_KEY", "").strip()
    if not key:
        raise HeyGenError("no HeyGen API key (set HEYGEN_API_KEY or paste one in the form)")
    return key


def _call(method, url, key, **kwargs):
    """One place for auth header, timeout, and HeyGen's error envelope."""
    headers = kwargs.pop("headers", {})
    headers["X-Api-Key"] = key
    r = requests.request(method, url, headers=headers, timeout=60, **kwargs)
    if r.status_code == 401:
        raise HeyGenError("HeyGen rejected the API key (401)")
    try:
        body = r.json()
    except ValueError:
        raise HeyGenError("HeyGen returned a non-JSON response (HTTP {})".format(r.status_code))
    err = body.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("detail") or str(err)
    elif err not in (None, "", "null"):
        msg = str(err)
    else:
        msg = ""
    if r.status_code >= 400:
        # top-level "message" is only an error detail when HTTP already failed;
        # successful responses also carry "message": "Success"
        raise HeyGenError("HeyGen error (HTTP {}): {}".format(
            r.status_code, msg or body.get("message") or r.text[:300]))
    if msg:
        raise HeyGenError("HeyGen error: " + msg)
    return body.get("data", body)


def _key_tag(key):
    """Short non-reversible fingerprint of the key, so cached asset ids
    (which belong to one account) are never reused with a different key."""
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)


def _cached_upload(img_path, key, prefix, uploader):
    """Upload the photo once per (photo content, account, engine); reuse after.

    Re-uploading the same face on every render is pure waste, so the cache key
    is sha256(photo bytes) + a fingerprint of the API key. The engine prefix
    matters because talking-photo ids and asset image_keys are different
    namespaces on HeyGen's side."""
    with open(img_path, "rb") as f:
        data = f.read()
    cache_id = prefix + hashlib.sha256(data).hexdigest()[:16] + ":" + _key_tag(key)
    cache = _load_cache()
    if cache_id in cache:
        return cache[cache_id]
    value = uploader(img_path, data, key)
    cache[cache_id] = value
    _save_cache(cache)
    return value


def upload_photo(img_path, key):
    """Standard engine: upload as a Talking Photo, returns talking_photo_id."""
    def up(path, data, key):
        mime = MIME.get(os.path.splitext(path)[1].lower())
        if not mime:
            raise HeyGenError("unsupported image type for talking photo")
        body = _call("POST", UPLOAD + "/v1/talking_photo", key,
                     headers={"Content-Type": mime}, data=data)
        photo_id = body.get("talking_photo_id")
        if not photo_id:
            raise HeyGenError("upload returned no talking_photo_id")
        return photo_id
    return _cached_upload(img_path, key, "tp:", up)


def upload_asset(img_path, key):
    """Avatar IV engine: upload via the Asset API, returns an image_key.
    Asset uploads only take png/jpeg, so webp gets converted first."""
    def up(path, data, key):
        ext = os.path.splitext(path)[1].lower()
        if ext == ".webp":
            from video import find_ffmpeg  # lazy: avoids import cost otherwise
            import subprocess
            jpg = os.path.splitext(path)[0] + ".jpg"
            subprocess.run([find_ffmpeg(), "-y", "-i", path, jpg], capture_output=True)
            path, ext = jpg, ".jpg"
            with open(path, "rb") as f:
                data = f.read()
        mime = "image/png" if ext == ".png" else "image/jpeg"
        body = _call("POST", UPLOAD + "/v1/asset", key,
                     headers={"Content-Type": mime}, data=data)
        image_key = body.get("image_key")
        if not image_key and body.get("id"):
            image_key = "image/{}/original".format(body["id"])
        if not image_key:
            raise HeyGenError("asset upload returned no image_key")
        return image_key
    return _cached_upload(img_path, key, "av4:", up)


def pick_voice(key):
    """HEYGEN_VOICE_ID env wins; otherwise take the first English voice."""
    voice = os.environ.get("HEYGEN_VOICE_ID", "").strip()
    if voice:
        return voice
    body = _call("GET", API + "/v2/voices", key)
    for v in body.get("voices", []):
        if "english" in str(v.get("language", "")).lower():
            return v["voice_id"]
    raise HeyGenError("no English voice found; set HEYGEN_VOICE_ID")


def estimate_cost(script, avatar_iv):
    """Rough pre-render price: spoken minutes x per-minute rate."""
    minutes = max(len(script.split()), 1) / WORDS_PER_MIN
    return round(minutes * (4.0 if avatar_iv else 1.0), 2)


def _credits(key):
    """Credits left on the account. HeyGen returns quota; quota / 60 = credits."""
    body = _call("GET", API + "/v2/user/remaining_quota", key)
    return round(body.get("remaining_quota", 0) / 60, 2)


def remaining_credits(request_key=None):
    return _credits(resolve_key(request_key))


def _credits_safe(key):
    """Metrics must never break a render; a failed quota read is just a blank stat."""
    try:
        return _credits(key)
    except Exception:
        return None


def heygen_render(img_path, script, folder, request_key=None):
    """The whole pipeline. Returns (video_path, metrics dict)."""
    key = resolve_key(request_key)
    avatar_iv = avatar_iv_enabled()
    started = time.time()
    credits_before = _credits_safe(key)  # measured cost = before - after
    voice_id = pick_voice(key)

    if avatar_iv:
        # Avatar IV is its own product with its own endpoint (v2/video/av4),
        # NOT a flag on the standard one. It supports motion prompts, which is
        # what gets gestures/body movement instead of a static torso. $4/min.
        image_key = upload_asset(img_path, key)
        payload = {
            "image_key": image_key,
            "video_title": "prompt2tube",
            "script": script,
            "voice_id": voice_id,
        }
        motion = os.environ.get(
            "HEYGEN_MOTION_PROMPT",
            "natural hand gestures while speaking, relaxed shoulders, "
            "subtle upper-body movement, engaged expressive face",
        ).strip()
        if motion:
            payload["custom_motion_prompt"] = motion
            payload["enhance_custom_motion_prompt"] = True
        body = _call("POST", API + "/v2/video/av4/generate", key, json=payload)
    else:
        photo_id = upload_photo(img_path, key)
        payload = {
            "title": "prompt2tube",
            "video_inputs": [{
                "character": {"type": "talking_photo", "talking_photo_id": photo_id},
                "voice": {"type": "text", "input_text": script, "voice_id": voice_id},
            }],
            "dimension": {"width": 1280, "height": 720},  # 720p, cheapest tier
        }
        body = _call("POST", API + "/v2/video/generate", key, json=payload)
    video_id = body.get("video_id")
    if not video_id:
        raise HeyGenError("generate returned no video_id")

    waited = 0
    while True:
        time.sleep(POLL_EVERY_S)
        waited += POLL_EVERY_S
        status = _call("GET", API + "/v1/video_status.get", key,
                       params={"video_id": video_id})
        state = status.get("status")
        if state == "completed":
            url = status.get("video_url")
            duration = status.get("duration")  # seconds, reported by HeyGen
            break
        if state == "failed":
            err = status.get("error") or {}
            raise HeyGenError("render failed: " + str(err.get("message", err))[:200])
        if waited >= POLL_MAX_S:
            raise HeyGenError("render timed out after {}s (video_id {})".format(POLL_MAX_S, video_id))

    out = os.path.join(folder, "video.mp4")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)

    credits_after = _credits_safe(key)
    metrics = {
        "est_cost": estimate_cost(script, avatar_iv),
        "render_s": round(time.time() - started),
        "engine_detail": "Avatar IV" if avatar_iv else "standard 720p",
    }
    if duration:
        metrics["duration_s"] = round(duration, 1)
    if credits_before is not None and credits_after is not None:
        metrics["credits_used"] = round(credits_before - credits_after, 2)
        metrics["credits_left"] = credits_after
    return out, metrics
