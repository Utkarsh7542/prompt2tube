"""WaveSpeed renderer: photo + audio in, lip-synced mp4 out.

WaveSpeed hosts open models behind one REST API (same category as Replicate or
fal.ai). This adapter targets its audio-driven talking-head models.

Flow. Inference runs on WaveSpeed's GPUs and the payload takes URLs, not bytes
-- a local file behind NAT is unreachable to them -- so each render is:

    photo --upload--> URL  ┐
    audio --upload--> URL  ├-> submit -> poll -> download mp4
                           ┘

Privacy note: uploads return unauthenticated CDN URLs and are retained 7 days,
so both the subject's likeness and the generated voice are held by a third
party for that window. (Contrast heygen.py, which returns an account-scoped
asset id and does its own TTS, so it never receives an audio file.)

One adapter, many models: these models differ only in data -- endpoint path,
rate, duration limits, whether the image is required -- so that lives in the
MODELS spec dict and the code stays generic. Adding another lipsync model is a
few lines of data.

Pricing (as of 2026-07, per the model pages):
  wavespeed-ai/infinitetalk-fast : $0.015/s, 5s billing floor, audio up to 600s
  wavespeed-ai/ltx-2.3/lipsync   : $0.03/s at 720p, audio must be 5-20s
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time

import requests

API = "https://api.wavespeed.ai/api/v3"
UPLOAD_URL = API + "/media/upload/binary"
CACHE_FILE = os.path.join("static", "wavespeed_cache.json")
RECEIPTS_FILE = os.path.join("static", "wavespeed-receipts.jsonl")

# WaveSpeed keeps uploaded media for 7 days. Expire our cache a day early so we
# never hand a dead URL to the API and pay for a failed submit.
UPLOAD_TTL_S = 6 * 24 * 3600

POLL_FIRST_S = 2       # their docs suggest ~2s; these renders take minutes
POLL_MAX_INTERVAL_S = 8
TERMINAL_BAD = {"failed", "cancelled", "timeout"}

# How long to wait for a render before giving up.
#
# This MUST stay below the web server's request timeout, because rendering is
# synchronous: the browser holds an HTTP request open for the whole job. If the
# server gives up first, the client sees a dead connection while the job keeps
# running and still gets billed. render.yaml runs gunicorn with --timeout 900,
# so the default here is 840 -- we give up first, with a message and a receipt.
#
# Known ceiling this implies: infinitetalk-fast needs roughly 10-30s of wall
# time per second of video, so a 60s video can exceed any sane web timeout.
# Long videos need the render moved off the request path (a job queue) rather
# than a bigger number here. Documented, not solved.
POLL_MAX_S = int(os.environ.get("WAVESPEED_POLL_MAX_S", "840"))


class WaveSpeedError(RuntimeError):
    """Any failure on the WaveSpeed path.

    Carries `prediction_id` when one exists. That id is the RECEIPT: if the
    render completed and only our download failed, the money is already spent
    and the video is still fetchable with this id. Losing it loses the video.
    """

    def __init__(self, message, prediction_id=None):
        super().__init__(message)
        self.prediction_id = prediction_id


# --- the per-model part: data, not code --------------------------------------
#
# rates: usd per second of OUTPUT video, keyed by resolution (None = the model
#        has no resolution knob).
# min_billed_s: billed for at least this much even if the clip is shorter.
# min_audio_s / max_audio_s: hard model limits. We check BEFORE submitting, so
#        an unusable request never costs anything.

MODELS = {
    # Fast is the distilled tier: cheapest, and it exposes no resolution knob
    # because it renders at a fixed small size. Measured 2026-07-28: a
    # 1408x768 input came back 640x352, a 2.2x downscale. That is the tier's
    # defining limit, not a bad render -- at that size the mouth is ~60px wide
    # and lip detail has nowhere to live. Use it for cheap iteration, not for
    # judging whether a model can do the job.
    "infinitetalk": {
        "path": "wavespeed-ai/infinitetalk-fast",
        "label": "InfiniteTalk Fast",
        "rates": {None: 0.015},
        "resolution": None,
        "min_billed_s": 5,
        "min_audio_s": 0,
        "max_audio_s": 600,
        "image_required": True,
    },
    # The full model: same architecture, selectable output resolution. This is
    # the tier the self-hosted RunPod campaign proved production-grade.
    "infinitetalk-hd": {
        "path": "wavespeed-ai/infinitetalk",
        "label": "InfiniteTalk",
        "rates": {"480p": 0.03, "720p": 0.06},
        "resolution": "720p",
        "min_billed_s": 5,
        "min_audio_s": 0,
        "max_audio_s": 600,
        "image_required": True,
    },
    "ltx": {
        "path": "wavespeed-ai/ltx-2.3/lipsync",
        "label": "LTX-2.3 Lipsync",
        "rates": {"480p": 0.02, "720p": 0.03, "1080p": 0.04},
        "resolution": "720p",
        "min_billed_s": 5,
        "min_audio_s": 5,
        "max_audio_s": 20,
        "image_required": False,
    },
}

DEFAULT_MODEL = "infinitetalk"


def spec_for(model):
    """Resolve a model key to its spec, with a message that lists the options."""
    key = (model or DEFAULT_MODEL).strip().lower()
    if key not in MODELS:
        raise WaveSpeedError("unknown WaveSpeed model {!r}; known: {}".format(
            key, ", ".join(sorted(MODELS))))
    return key, MODELS[key]


def resolve_key(request_key=None):
    """BYOK rule, same as heygen.py: a key from the form beats the server env."""
    key = (request_key or "").strip() or os.environ.get("WAVESPEED_API_KEY", "").strip()
    if not key:
        raise WaveSpeedError(
            "no WaveSpeed API key (set WAVESPEED_API_KEY or paste one in the form)")
    return key


def _call(method, url, key, **kwargs):
    """One place for auth, timeout and WaveSpeed's {code, message, data} envelope."""
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = "Bearer " + key
    try:
        r = requests.request(method, url, headers=headers,
                             timeout=kwargs.pop("timeout", 120), **kwargs)
    except requests.RequestException as e:
        raise WaveSpeedError("could not reach WaveSpeed: {}".format(str(e)[:200]))
    if r.status_code in (401, 403):
        raise WaveSpeedError("WaveSpeed rejected the API key (HTTP {})".format(r.status_code))
    try:
        body = r.json()
    except ValueError:
        raise WaveSpeedError("WaveSpeed returned a non-JSON response (HTTP {}): {}".format(
            r.status_code, r.text[:200]))
    if r.status_code >= 400:
        raise WaveSpeedError("WaveSpeed error (HTTP {}): {}".format(
            r.status_code, body.get("message") or r.text[:300]))
    # Success envelope is {"code": 200, "message": "success", "data": {...}};
    # some endpoints answer with the bare object. Tolerate both.
    code = body.get("code")
    if code is not None and code != 200:
        raise WaveSpeedError("WaveSpeed error (code {}): {}".format(
            code, body.get("message", "")))
    return body.get("data", body)


# --- media upload -------------------------------------------------------------

def _key_tag(key):
    """Non-reversible fingerprint, so cached URLs are never crossed between
    accounts (the same trick heygen.py uses for asset ids)."""
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


def _cache_enabled():
    """Reuse is an efficiency win and a privacy choice: a cached URL means the
    face stays live on their CDN longer. Set WAVESPEED_UPLOAD_CACHE=0 to opt out."""
    val = os.environ.get("WAVESPEED_UPLOAD_CACHE", "1").strip().strip('"').lower()
    return val not in ("0", "false", "no")


def upload(path, key, cache=True):
    """Push one file to WaveSpeed, return a URL their GPUs can fetch.

    Images are cached by (content hash, account) because the same headshot is
    re-rendered constantly. Audio is never worth caching -- a new script means
    new bytes every time -- so callers pass cache=False for it.
    """
    with open(path, "rb") as f:
        data = f.read()
    cache_id = None
    if cache and _cache_enabled():
        cache_id = hashlib.sha256(data).hexdigest()[:16] + ":" + _key_tag(key)
        store = _load_cache()
        hit = store.get(cache_id)
        # An entry past WaveSpeed's 7-day retention is a dead URL, and a dead
        # URL is a submit that fails AFTER we have been charged nothing but
        # have burned a round trip. Treat stale as absent.
        if isinstance(hit, dict) and hit.get("expires", 0) > time.time():
            return hit["url"]

    with open(path, "rb") as f:
        body = _call("POST", UPLOAD_URL, key,
                     files={"file": (os.path.basename(path), f)}, timeout=180)
    url = body.get("download_url") or body.get("url")
    if not url:
        raise WaveSpeedError("upload returned no download_url")

    if cache_id:
        store = _load_cache()
        store[cache_id] = {"url": url, "expires": time.time() + UPLOAD_TTL_S}
        _save_cache(store)
    return url


# --- duration + cost ----------------------------------------------------------

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")


def audio_duration(path):
    """Seconds of audio, measured from the file.

    heygen.py estimates from a word count at 150 wpm because it never sees the
    audio. Here the mp3 exists before anything is spent, and WaveSpeed bills
    audio duration rather than words, so measure instead of guessing.
    """
    probe = shutil.which("ffprobe")
    if probe:
        out = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True)
        try:
            return float(out.stdout.strip())
        except ValueError:
            pass
    # No ffprobe (imageio-ffmpeg ships only ffmpeg): read it off ffmpeg's stderr.
    from video import find_ffmpeg
    out = subprocess.run([find_ffmpeg(), "-i", path], capture_output=True, text=True)
    m = _DURATION_RE.search(out.stderr or "")
    if not m:
        raise WaveSpeedError("could not read the audio duration of " + os.path.basename(path))
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def rate_for(spec, resolution=None):
    rates = spec["rates"]
    return rates.get(resolution if resolution in rates else spec["resolution"],
                     next(iter(rates.values())))


def estimate_cost(duration_s, spec, resolution=None):
    """Predicted price: billed seconds x published rate.

    The duration is measured; the price is inferred from a published rate. The
    prediction record holds the actual charge, which is one reason the
    prediction id is logged. The 5s floor is why a 3s and a 5s clip cost alike.
    """
    billed = max(duration_s, spec["min_billed_s"])
    return round(billed * rate_for(spec, resolution), 3)


WORDS_PER_MIN = 150  # pre-flight guess only, never used for billing


def max_words(model=None):
    """Rough word ceiling for a model, for a pre-flight check only.

    The real check is check_duration(), which measures the finished audio. This
    one has to run before the audio exists -- ideally before the script is even
    written -- so it can only guess, using the same 150 wpm proxy heygen.py
    used. Deliberately generous: better to let a borderline script through and
    fail honestly on the measured duration than to refuse one that would have
    fit. Returns None for models with no practical ceiling.
    """
    _, spec = spec_for(model)
    if spec["max_audio_s"] >= 300:
        return None
    return int(spec["max_audio_s"] / 60.0 * WORDS_PER_MIN)


def check_duration(duration_s, model_key, spec):
    """Refuse an impossible render before spending anything.

    LTX-2.3 caps at 20s of audio while app.py permits ~90s of script. Silently
    truncating to fit would ship a video that stops mid-sentence, so this fails
    instead, naming both numbers and the alternative. Splitting long scripts
    into stitched clips is a legitimate answer, but it belongs above
    make_video(), not in an adapter whose unit of work is one clip.
    """
    if duration_s > spec["max_audio_s"]:
        raise WaveSpeedError(
            "{} caps at {}s of audio; this script is {:.0f}s. Shorten the script, "
            "or render with a model that has no practical cap (infinitetalk, 600s).".format(
                spec["label"], spec["max_audio_s"], duration_s))
    if duration_s < spec["min_audio_s"]:
        raise WaveSpeedError(
            "{} needs at least {}s of audio; this script is only {:.1f}s. "
            "Write a longer script.".format(spec["label"], spec["min_audio_s"], duration_s))


# --- receipts -----------------------------------------------------------------

def _log_receipt(record):
    """Append-only record of every billable call, keyed by prediction id.

    Written when the job is accepted, not when it succeeds. If the render
    completes and the download fails, the charge has already happened and this
    line is the only route back to the output.
    """
    try:
        os.makedirs(os.path.dirname(RECEIPTS_FILE), exist_ok=True)
        with open(RECEIPTS_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass  # a receipt we failed to write must never kill a render in flight


# --- the render ---------------------------------------------------------------

def submit(spec, key, image_url, audio_url, prompt=None, seed=None, resolution=None):
    """Start the job. Returns (prediction_id, result_url)."""
    payload = {"audio": audio_url}
    if image_url:
        payload["image"] = image_url
    if prompt:
        payload["prompt"] = prompt
    if seed is not None:
        payload["seed"] = seed
    if spec["resolution"]:
        payload["resolution"] = resolution or spec["resolution"]

    body = _call("POST", API + "/" + spec["path"], key, json=payload,
                 headers={"Content-Type": "application/json"})
    prediction_id = body.get("id")
    if not prediction_id:
        raise WaveSpeedError("submit returned no prediction id")
    result_url = (body.get("urls") or {}).get("get") or \
        "{}/predictions/{}/result".format(API, prediction_id)
    return prediction_id, result_url


def poll(result_url, key, prediction_id, max_s=POLL_MAX_S):
    """Wait for a terminal status. Returns the output video URL."""
    waited = 0
    interval = POLL_FIRST_S
    while True:
        time.sleep(interval)
        waited += interval
        body = _call("GET", result_url, key, timeout=60)
        status = body.get("status")
        if status == "completed":
            outputs = body.get("outputs") or []
            if not outputs:
                raise WaveSpeedError("render completed but returned no output",
                                     prediction_id)
            return outputs[0]
        if status in TERMINAL_BAD:
            raise WaveSpeedError("render {}: {}".format(
                status, str(body.get("error") or body.get("message") or "")[:200]),
                prediction_id)
        if waited >= max_s:
            raise WaveSpeedError(
                "render still {} after {}s (prediction {} -- it may finish later; "
                "the charge stands either way)".format(status, max_s, prediction_id),
                prediction_id)
        interval = min(interval + 1, POLL_MAX_INTERVAL_S)


def download(url, out, prediction_id=None):
    try:
        with requests.get(url, stream=True, timeout=180) as r:
            r.raise_for_status()
            with open(out, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
    except (requests.RequestException, OSError) as e:
        raise WaveSpeedError(
            "render succeeded but the download failed ({}). The job is already "
            "paid for; re-fetch it with prediction {}.".format(str(e)[:150], prediction_id),
            prediction_id)
    return out


def wavespeed_render(img_path, audio_path, folder, model=None, request_key=None,
                     prompt=None, resolution=None):
    """The whole pipeline. Returns (video_path, metrics dict).

    Takes audio, not a script, unlike heygen_render(): WaveSpeed does no TTS.
    Voice generation therefore happens in make_video(), ahead of engine
    dispatch, which also means two models compared here speak identical bytes.
    """
    model_key, spec = spec_for(model)
    key = resolve_key(request_key)
    started = time.time()

    if spec["image_required"] and not img_path:
        raise WaveSpeedError("{} requires a photo".format(spec["label"]))

    # Measure and validate first. Everything below this line can cost money.
    duration = audio_duration(audio_path)
    check_duration(duration, model_key, spec)
    est = estimate_cost(duration, spec, resolution)

    image_url = upload(img_path, key) if img_path else None
    audio_url = upload(audio_path, key, cache=False)

    prompt = prompt if prompt is not None else os.environ.get("WAVESPEED_PROMPT", "").strip() or None
    prediction_id, result_url = submit(spec, key, image_url, audio_url,
                                       prompt=prompt, resolution=resolution)
    _log_receipt({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "prediction_id": prediction_id,
        "model": spec["path"],
        "audio_s": round(duration, 2),
        "est_cost": est,
        "result_url": result_url,
        "folder": folder,
    })

    video_url = poll(result_url, key, prediction_id)
    out = os.path.join(folder, "video.mp4")
    download(video_url, out, prediction_id)

    return out, {
        "est_cost": est,
        "render_s": round(time.time() - started),
        "duration_s": round(duration, 1),
        "engine_detail": "{}{}".format(
            spec["label"],
            " " + (resolution or spec["resolution"]) if spec["resolution"] else ""),
        "prediction_id": prediction_id,
    }
