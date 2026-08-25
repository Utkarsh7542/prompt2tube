"""Runpod renderer: photo + audio in, lip-synced mp4 out, at a FLAT price.

Same model as the WaveSpeed path -- MeiGen-AI's InfiniteTalk, the one proven
production-grade on the 2026-07-23 self-host campaign -- but bought differently,
and the difference in how it is sold changes how this adapter is shaped.

MEASURED 2026-08-06, live, on Utkarsh's own credit:

    audio      charged     wall time
      4.3s      $0.2500        88s
     24.8s      $0.2500       202s
     97.8s      $0.2500      ~10 min
    ~300s       $0.2500      ~29 min
    ~600s       $0.2500      ~57 min   (quality visibly degrades by the end)

The charge does not move. Fit those points and the wall time is roughly
64 seconds of fixed overhead plus 5.6 seconds of compute per second of video,
against WaveSpeed's measured 31.2 s/s at 720p -- about 5.6x faster.

WHAT FLAT PRICING CHANGES
-------------------------
wavespeed.py's guardrails exist to stop a long script costing a fortune: check
the duration, refuse if it exceeds the model cap, estimate the bill before
spending. Every one of those instincts is inverted here.

At $0.25 per video regardless of length, the cost per minute FALLS the longer
the clip:

    1 minute   $0.25 / min
    5 minutes  $0.05 / min
    10 minutes $0.025 / min

So money no longer argues for short renders. Two other things do.

CORRECTION, 2026-08-06, and it overturns what this file said first. The initial
version of this docstring concluded "render in the LONGEST single call the
endpoint will accept". A 10-minute render was then run successfully -- flat
$0.25, confirming the pricing all the way up -- and Utkarsh watched it back:
**quality degrades as the video proceeds.** The economic argument was sound and
the conclusion was still wrong, because it was reasoning about price with no
evidence about output. His read is that 3-5 minute segments are the practical
unit. Nothing here should be taken as advice to maximise clip length.

What that leaves is a genuine tension rather than a rule:

  - longer clips are cheaper per minute, and avoid the seam between segments
  - longer clips visibly degrade, and the degradation is inside one clip, which
    no cut can hide

Note this is a DIFFERENT failure from the one in renderer-notes.md. That one is
drift BETWEEN separately rendered clips, because each regenerates the face from
the photo. This is drift WITHIN a single clip. Segmenting trades the second for
the first, and at $0.25 a segment the trade costs almost nothing: a 10-minute
video in three 3-4 minute parts is $0.75.

The binding constraints are therefore WALL TIME and QUALITY, not money, and only
the first of those is machine-checkable, which is why the guardrails below only
check that one. The quality ceiling is a judgement and belongs to a human until
somebody measures it properly.

NO THIRD-PARTY CDN
------------------
Runpod fetches `image` and `audio` from URLs and publishes no upload endpoint.
Rather than host the files somewhere, this adapter inlines them as base64 data
URIs, which was confirmed working on 2026-08-06. That is a real improvement on
wavespeed.py, whose note reads: "the subject's face sits unauthenticated on a
third-party CDN for 7 days ... and this time the voice goes too." Here neither
ever leaves the request. The cost is a request-size ceiling of about 10 MB.

The OUTPUT still lands on a Runpod URL that lives 7 days, so download promptly.

MEASURED CEILING
----------------
A 10-minute render SUCCEEDED on 2026-08-06 at $0.25. An earlier 11.9-minute
attempt failed with "executionTimeout exceeded" after ~52 minutes and cost
$0.00, which is why submit() now sends an explicit execution policy. So the
length ceiling is not the model's -- it is whatever wall-clock budget the job is
given, and that is now ours to set.
"""

import base64
import json
import mimetypes
import os
import time

import requests

from wavespeed import audio_duration  # measuring audio is the same problem twice

API_BASE = "https://api.runpod.ai/v2/infinitetalk"
RECEIPTS_FILE = os.path.join("static", "runpod-receipts.jsonl")

# Runpod's request limit is ~10 MB and base64 inflates by about a third. Both
# the photo and the audio ride inside one request, so the check has to be on the
# total, not per file.
MAX_REQUEST_BYTES = 9 * 1024 * 1024

# Fitted from the four live measurements above. Used to predict wall time and
# to warn before a render that cannot finish inside a web request.
OVERHEAD_S = 64.0
SECONDS_PER_SECOND = 5.6

POLL_INTERVAL_S = 10

# Deliberately NOT tied to the gunicorn timeout the way wavespeed.py's 840 is.
# A 10-minute video needs roughly an hour here, which no web request can hold.
# Long renders must be submitted and collected later -- see submit()/collect()
# below, which exist so the caller can detach. This default only bounds the
# convenience path.
POLL_MAX_S = int(os.environ.get("RUNPOD_POLL_MAX_S", "5400"))

TERMINAL_BAD = {"FAILED", "CANCELLED", "TIMED_OUT"}


class RunpodError(RuntimeError):
    """Any failure on the Runpod path.

    Carries `job_id` when one exists. Same reasoning as WaveSpeedError: a
    completed render whose download failed is money already spent, and the job
    id is the only route back to the video for the 7 days it lives.
    """

    def __init__(self, message, job_id=None):
        super().__init__(message)
        self.job_id = job_id


# --- the per-model part: data, not code ---------------------------------------
#
# price: FLAT dollars per video. Not per second. This is the whole point.
# max_audio_s: conservative until the long probe lands (see module docstring).

MODELS = {
    "infinitetalk": {
        "label": "InfiniteTalk 480p",
        "size": "480p",
        "price": 0.25,
        "max_audio_s": 900,
    },
    "infinitetalk-720p": {
        "label": "InfiniteTalk 720p",
        "size": "720p",
        "price": 0.50,
        # 720p is slower per second, so the same wall-clock budget buys less
        # audio. Untested; keep it below the 480p ceiling until measured.
        "max_audio_s": 600,
    },
}

DEFAULT_MODEL = "infinitetalk"


def spec_for(model):
    key = (model or DEFAULT_MODEL).strip().lower()
    if key not in MODELS:
        raise RunpodError("unknown Runpod model {!r}; known: {}".format(
            key, ", ".join(sorted(MODELS))))
    return key, MODELS[key]


def resolve_key(request_key=None):
    """BYOK rule, same as heygen.py and wavespeed.py."""
    key = (request_key or "").strip() or os.environ.get("RUNPOD_API_KEY", "").strip()
    if not key:
        raise RunpodError(
            "no Runpod API key (set RUNPOD_API_KEY or paste one in the form)")
    return key


def _call(method, url, key, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = "Bearer " + key
    try:
        r = requests.request(method, url, headers=headers,
                             timeout=kwargs.pop("timeout", 180), **kwargs)
    except requests.RequestException as e:
        raise RunpodError("could not reach Runpod: {}".format(str(e)[:200]))
    if r.status_code in (401, 403):
        raise RunpodError("Runpod rejected the API key (HTTP {})".format(r.status_code))
    try:
        body = r.json()
    except ValueError:
        raise RunpodError("Runpod returned non-JSON (HTTP {}): {}".format(
            r.status_code, r.text[:200]))
    if r.status_code >= 400:
        raise RunpodError("Runpod error (HTTP {}): {}".format(
            r.status_code, str(body)[:300]))
    return body


# --- inlining -----------------------------------------------------------------

def data_uri(path):
    with open(path, "rb") as f:
        raw = f.read()
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return "data:{};base64,{}".format(mime, base64.b64encode(raw).decode())


def check_payload_size(image_uri, audio_uri, duration_s):
    """Refuse an over-size request before spending anything.

    Both files ride inside one JSON body, so the ceiling applies to the pair.
    Measured 2026-08-06: audio encodes at roughly 7.8 KB per second, so ten
    minutes is about 4.7 MB and fits; the photo is what usually breaks this.
    """
    total = len(image_uri) + len(audio_uri)
    if total > MAX_REQUEST_BYTES:
        raise RunpodError(
            "request would be {:.1f} MB inlined, over Runpod's ~10 MB limit "
            "({:.0f}s of audio plus the photo). Shrink the photo -- the model "
            "renders at 480p/720p, so a large source buys nothing.".format(
                total / 1e6, duration_s))


# --- time, which is the real constraint here ----------------------------------

def predict_wall_s(duration_s):
    """Fitted from the 2026-08-06 measurements. An estimate, not a promise."""
    return OVERHEAD_S + SECONDS_PER_SECOND * duration_s


# The prediction is a fit through five points, so it will be wrong sometimes.
# Ask for double, because being killed at 95% of a 68-minute render wastes an
# hour, while asking for headroom we do not use costs nothing: Runpod bills the
# flat per-video price, not the time allowed.
TIMEOUT_SAFETY_FACTOR = 2.0
MIN_EXECUTION_TIMEOUT_S = 900
QUEUE_HEADROOM_S = 3600


def execution_policy(duration_s):
    """Per-job execution policy, in milliseconds, as Runpod's API wants it.

    executionTimeout starts when a worker picks the job up. ttl starts at
    submission and covers queue time too, and is a HARD limit -- if it expires
    mid-render the job is deleted and /status returns 404 -- so it must always
    exceed executionTimeout by enough to absorb a queue.
    """
    exec_s = max(MIN_EXECUTION_TIMEOUT_S,
                 predict_wall_s(duration_s) * TIMEOUT_SAFETY_FACTOR)
    return {
        "executionTimeout": int(exec_s * 1000),
        "ttl": int((exec_s + QUEUE_HEADROOM_S) * 1000),
    }


def check_duration(duration_s, spec):
    """Guard on LENGTH and TIME, not on money.

    There is no cost check here and that is deliberate: the price is flat, so a
    longer render is strictly better value. What a long render can do is exceed
    the caller's patience or an HTTP timeout, and that is worth naming before
    the wait rather than after it.
    """
    if duration_s > spec["max_audio_s"]:
        raise RunpodError(
            "{} is configured for at most {}s of audio; this script is {:.0f}s. "
            "That ceiling is a conservative guess, not a measured limit -- raise "
            "MAX_AUDIO_S in runpod.py once a longer render is confirmed.".format(
                spec["label"], spec["max_audio_s"], duration_s))
    if duration_s <= 0:
        raise RunpodError("the audio is empty")


def cost_per_minute(duration_s, spec):
    """What this render actually costs per minute of output.

    Exists to make the inversion visible in the metrics: the same $0.25 is
    $0.25/min at one minute and $0.025/min at ten.
    """
    if duration_s <= 0:
        return None
    return round(spec["price"] / (duration_s / 60.0), 4)


# --- receipts -----------------------------------------------------------------

def _log_receipt(record):
    try:
        os.makedirs(os.path.dirname(RECEIPTS_FILE), exist_ok=True)
        with open(RECEIPTS_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass  # a receipt we failed to write must never kill a render in flight


# --- the render ---------------------------------------------------------------

def submit(spec, key, image_uri, audio_uri, prompt=None, safety=True,
           duration_s=None):
    """Start the job, return its id. Uses /run, so the caller can detach.

    /runsync exists and is tempting for short clips, but a 10-minute render
    takes about an hour here, and an adapter that only works below the HTTP
    timeout is an adapter that fails exactly when the flat price matters most.

    THE EXECUTION POLICY IS NOT OPTIONAL. Measured 2026-08-06: a 712.5s script
    was killed at roughly 3120s of wall time with "executionTimeout exceeded",
    having needed about 4054s. Runpod's default job execution timeout is 10
    minutes and is overridable per request, in MILLISECONDS, via `policy`. Left
    unset, every render longer than a few minutes dies -- which is precisely the
    range where a flat price is worth having. The failed job cost $0.00, so this
    is a correctness problem rather than a money one, but it is still the
    difference between the endpoint being useful and useless.
    """
    payload = {"input": {
        "prompt": prompt or "a person speaking to camera",
        "image": image_uri,
        "audio": audio_uri,
        "size": spec["size"],
        "enable_safety_checker": bool(safety),
    }}
    if duration_s:
        payload["policy"] = execution_policy(duration_s)
    body = _call("POST", API_BASE + "/run", key, json=payload,
                 headers={"Content-Type": "application/json"})
    job_id = body.get("id")
    if not job_id:
        raise RunpodError("submit returned no job id: {}".format(str(body)[:200]))
    return job_id


def _find_video_url(output):
    """Pull the video URL out of whatever shape Runpod actually returns.

    Their docs publish `output.video_url`. The first live run on 2026-08-06
    returned `output.cost` correctly but no value at that key, so the documented
    shape is wrong or conditional. Rather than guess one name, walk the object
    and take the first http URL that is not obviously an image. Tighten this to
    a literal key the moment a real response is recorded.
    """
    def walk(obj):
        if isinstance(obj, str):
            if obj.startswith("http") and not obj.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                return [obj]
            return []
        if isinstance(obj, dict):
            out = []
            for v in obj.values():
                out += walk(v)
            return out
        if isinstance(obj, list):
            out = []
            for v in obj:
                out += walk(v)
            return out
        return []

    urls = walk(output)
    return urls[0] if urls else None


def collect(job_id, key, max_s=POLL_MAX_S):
    """Poll until terminal. Returns (video_url, actual_cost, raw_output)."""
    waited = 0
    while True:
        time.sleep(POLL_INTERVAL_S)
        waited += POLL_INTERVAL_S
        body = _call("GET", "{}/status/{}".format(API_BASE, job_id), key, timeout=60)
        status = (body.get("status") or "").upper()
        if status == "COMPLETED":
            output = body.get("output") or {}
            url = _find_video_url(output)
            if not url:
                raise RunpodError(
                    "render completed and was charged, but no video URL was found "
                    "in the response. Raw output: {}".format(str(output)[:300]),
                    job_id)
            return url, output.get("cost"), output
        if status in TERMINAL_BAD:
            raise RunpodError("render {}: {}".format(
                status, str(body.get("error") or body.get("output") or "")[:200]), job_id)
        if waited >= max_s:
            raise RunpodError(
                "render still {} after {}s (job {}). It will probably finish and "
                "will be charged either way -- collect it later with this id.".format(
                    status, max_s, job_id), job_id)


def download(url, out, job_id=None):
    try:
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(out, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
    except (requests.RequestException, OSError) as e:
        raise RunpodError(
            "render succeeded but the download failed ({}). The job is already "
            "paid for; the URL lives 7 days, re-fetch with job {}.".format(
                str(e)[:150], job_id), job_id)
    return out


def prepare_and_submit(img_path, audio_path, folder, model=None,
                       request_key=None, prompt=None, safety=True):
    """Everything up to AND INCLUDING submit(): validate, inline, fit the photo,
    start the job, write the receipt. Returns (job_id, prep).

    WHY THIS IS SPLIT OUT OF runpod_render
    --------------------------------------
    submit()/collect()/download() were always separate "so the caller can
    detach" (see submit's docstring). This function is what makes that promise
    usable: it does all the pre-flight and hands back the job id at the exact
    moment the render becomes MONEY ALREADY SPENT and Runpod's problem, not ours.

    The job queue worker calls this, PERSISTS the returned job_id to SQLite, and
    only then starts polling. So if the worker dies mid-render, restart re-reads
    the id and re-attaches to a render we have already paid for -- instead of
    losing an hour-long paid job because it only lived in one process's memory.
    runpod_render() below is now just this + collect() + download(): the
    convenience path for a caller that does NOT need to detach.

    `prep` carries the numbers collect-time metrics need, so a resuming caller
    that reconstructs prep from a DB row produces the same metrics shape.
    """
    model_key, spec = spec_for(model)
    key = resolve_key(request_key)

    if not img_path:
        raise RunpodError("{} requires a photo".format(spec["label"]))

    # Measure and validate first. Everything below this line can cost money.
    duration = audio_duration(audio_path)
    check_duration(duration, spec)

    # The audio is fixed -- it is the product. The photo is not: the model
    # discards resolution above 480p/720p anyway, so if the pair will not fit
    # in one request, the photo is the thing that gives. See
    # video.fit_image_to_budget for why this one rewrite is allowed to be
    # automatic when nothing else in this codebase is.
    audio_uri = data_uri(audio_path)
    fitted = None
    budget = MAX_REQUEST_BYTES - len(audio_uri)
    if budget <= 0:
        raise RunpodError(
            "the audio alone is {:.1f} MB inlined, over Runpod's ~10 MB request "
            "limit ({:.0f}s). Split the script into shorter segments.".format(
                len(audio_uri) / 1e6, duration))
    # base64 inflates by 4/3, so convert the encoded budget back to raw bytes.
    raw_budget = int(budget * 3 / 4) - 512
    from video import fit_image_to_budget
    try:
        img_path, fitted = fit_image_to_budget(img_path, folder, raw_budget)
    except RuntimeError as e:
        raise RunpodError(str(e))

    image_uri = data_uri(img_path)
    check_payload_size(image_uri, audio_uri, duration)

    predicted_wall = predict_wall_s(duration)

    job_id = submit(spec, key, image_uri, audio_uri, prompt=prompt,
                    safety=safety, duration_s=duration)
    _log_receipt({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "job_id": job_id,
        "model": model_key,
        "size": spec["size"],
        "audio_s": round(duration, 2),
        "price": spec["price"],
        "predicted_wall_s": round(predicted_wall),
        "folder": folder,
    })
    return job_id, {
        "model_key": model_key,
        "spec": spec,
        "duration": duration,
        "predicted_wall_s": predicted_wall,
        "photo_fitted": fitted,
    }


def prep_from_row(model, duration_s, predicted_wall_s):
    """Rebuild the minimal `prep` a resuming caller needs for render_metrics().

    A worker resuming a render after a restart has only what it wrote to the
    jobs row: the model, the audio duration and the predicted wall time. That is
    exactly enough to report cost and timing. `photo_fitted` is not recoverable
    (the fitting happened in the process that died), so it is reported as None --
    honestly absent rather than invented.
    """
    _, spec = spec_for(model)
    return {
        "model_key": (model or DEFAULT_MODEL),
        "spec": spec,
        "duration": duration_s,
        "predicted_wall_s": predicted_wall_s
        if predicted_wall_s is not None else predict_wall_s(duration_s or 0),
        "photo_fitted": None,
    }


def render_metrics(prep, job_id, actual_cost, elapsed_s):
    """The metrics dict, built from prep. Shared by runpod_render and the worker
    so both report identically -- the number the cost ledger persists must not
    depend on which caller produced the video."""
    spec = prep["spec"]
    duration = prep["duration"]
    return {
        # The charge Runpod reported, not a rate we multiplied out. If it is
        # missing for any reason, fall back to the published flat price.
        "est_cost": actual_cost if actual_cost is not None else spec["price"],
        "cost_is_actual": actual_cost is not None,
        "cost_per_min": cost_per_minute(duration, spec),
        "render_s": round(elapsed_s),
        "predicted_render_s": round(prep["predicted_wall_s"]),
        "duration_s": round(duration, 1) if duration is not None else None,
        "engine_detail": "{} (flat ${:.2f}/video)".format(spec["label"], spec["price"]),
        "prediction_id": job_id,
        # Present only when the photo had to be shrunk. Reported rather than
        # hidden: the rewrite is automatic, not secret.
        "photo_fitted": prep.get("photo_fitted"),
    }


def runpod_render(img_path, audio_path, folder, model=None, request_key=None,
                  prompt=None, safety=True, poll_max_s=POLL_MAX_S):
    """The whole pipeline in one call. Returns (video_path, metrics dict).

    This is prepare_and_submit + collect + download -- it does not detach, so it
    holds through the whole render. Fine for a caller that can wait (a test, a
    short clip); the job queue worker uses the three pieces separately instead so
    it can persist the job id between submit and poll.
    """
    started = time.time()
    key = resolve_key(request_key)
    job_id, prep = prepare_and_submit(img_path, audio_path, folder, model=model,
                                      request_key=key, prompt=prompt, safety=safety)
    video_url, actual_cost, _ = collect(job_id, key, max_s=poll_max_s)
    out = os.path.join(folder, "video.mp4")
    download(video_url, out, job_id)
    return out, render_metrics(prep, job_id, actual_cost, time.time() - started)
