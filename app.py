import os
import shutil
import time
import uuid
from flask import Flask, render_template, request, jsonify
from env import load_env

# Load .env for the case where app.py is started directly (e.g. `python app.py`
# or a WSGI server) rather than through run.bat. Harmless when run.bat already
# exported the keys, since load_env never overrides a variable that is set.
load_env()

import jobs

app = Flask(__name__)

# The jobs table backs the async render path (PRODUCT-PLAN section 3). Created on
# import so the web app never depends on the worker having started first.
jobs.init_db()

# Publish hub (PRD v0.2 §4): connect-once accounts + multi-platform
# publishing live behind /hub/*. app.py knows the prefix, nothing else —
# that seam is what lets the hub become its own service later.
from hub import bp as hub_bp
app.register_blueprint(hub_bp)

JOBS = os.path.join("static", "jobs")
os.makedirs(JOBS, exist_ok=True)

MAX_JOB_AGE_H = float(os.environ.get("MAX_JOB_AGE_H", "6"))


def sweep_jobs():
    """Working files are scratch space: anything older than MAX_JOB_AGE_H goes."""
    now = time.time()
    for name in os.listdir(JOBS):
        p = os.path.join(JOBS, name)
        try:
            if os.path.isdir(p) and now - os.path.getmtime(p) > MAX_JOB_AGE_H * 3600:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


@app.route("/")
def home():
    try:
        import gradio_client  # noqa: F401
        lipsync = os.environ.get("RENDERER", "hf").lower() != "motion"
    except ImportError:
        lipsync = False
    heygen_ready = bool(os.environ.get("HEYGEN_API_KEY", "").strip())
    try:
        from heygen import avatar_iv_enabled
        heygen_iv = avatar_iv_enabled()
    except Exception:
        heygen_iv = False
    wavespeed_ready = bool(os.environ.get("WAVESPEED_API_KEY", "").strip())
    elevenlabs_ready = bool(os.environ.get("ELEVENLABS_API_KEY", "").strip())
    return render_template("index.html", lipsync=lipsync,
                           heygen_ready=heygen_ready, heygen_iv=heygen_iv,
                           wavespeed_ready=wavespeed_ready,
                           elevenlabs_ready=elevenlabs_ready)


@app.route("/quota", methods=["POST"])
def quota():
    """Remaining HeyGen credits. The key comes from the request (BYOK) or the
    server env; it is used for this one call and never stored anywhere."""
    data = request.get_json(silent=True) or {}
    try:
        from heygen import remaining_credits
        return jsonify(credits=remaining_credits(data.get("heygen_key")))
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.route("/voices", methods=["POST"])
def voices():
    """The account's voices, plus its plan and remaining credits.

    This is the whole of "connect your ElevenLabs account". Unlike LinkedIn or
    YouTube there is no OAuth dance: an ElevenLabs key is scoped to a workspace,
    so possessing it IS the connection. One paste, two GETs, and the dropdown
    holds whatever that account actually has — premade voices on a fresh free
    key, their own clones on a paid one.

    Capability comes back from the API rather than being asked of the user, so
    the UI never has to guess which plan somebody is on. BYOK as everywhere
    else: used for these calls, never stored.
    """
    data = request.get_json(silent=True) or {}
    key_in = data.get("elevenlabs_key")
    try:
        from elevenlabs import resolve_key, list_voices, account_safe
        key = resolve_key(key_in)
        acct = account_safe(key)
        return jsonify(voices=list_voices(key), account=acct)
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.route("/voice-preview", methods=["POST"])
def voice_preview():
    """Generate ONLY the voiceover, so it can be heard before rendering.

    The economics make this near-mandatory rather than a nicety: a 90-second
    script costs roughly $0.30 of speech and $1.35-$5.40 to lip-sync. Any
    mispronounced name caught here instead of after the render saves an order
    of magnitude, and a pronunciation fix cannot be patched into a finished
    video — the lip sync is bound to the waveform, so correcting a word means
    rendering the whole thing again.
    """
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(error="Nothing to preview: write or generate a script first."), 400
    sweep_jobs()
    folder = os.path.join(JOBS, "preview-" + uuid.uuid4().hex[:8])
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, "voice.mp3")
    try:
        from video import say
        metrics = say(text, out,
                      engine=data.get("voice_engine") or "elevenlabs",
                      elevenlabs_key=data.get("elevenlabs_key"),
                      voice_id=data.get("voice_id"),
                      voice_model=data.get("voice_model"),
                      speed=data.get("voice_speed"),
                      fish_key=data.get("fish_key"),
                      fish_personal_use=bool(data.get("fish_personal_use")))
    except Exception as e:
        return jsonify(error="Voice generation failed: " + str(e)), 400
    # What this audio will cost to lip-sync, which is the number that actually
    # moves when somebody drags the speed slider.
    try:
        from elevenlabs import render_cost_delta
        from wavespeed import MODELS, rate_for
        spec = MODELS["infinitetalk-hd"]
        metrics["render_cost_at_720p"] = render_cost_delta(
            metrics.get("duration_s"), rate_for(spec, "720p"))
    except Exception:
        pass
    return jsonify(audio="/" + out.replace(os.sep, "/"), metrics=metrics)


# Reels are the first release: 60-90 seconds, ~150-225 words. The cap is stated
# as an OUTPUT fact ("a reel is 60-90 seconds") rather than a machine limit,
# because that is the honest reason now that the render is async -- the browser
# no longer waits, so wall time is no longer what binds here. Longer scripts
# belong to the explainer/announcement formats, which come later.
REEL_MAX_WORDS = int(os.environ.get("REEL_MAX_WORDS", "260"))


@app.route("/generate", methods=["POST"])
def generate():
    """Enqueue a render and return its id immediately. The actual work -- script,
    voice, the ~9-minute render -- happens in worker.py, off this request.

    This is the change the whole product plan gates on (section 3): rendering no
    longer holds an HTTP request open. POST returns in milliseconds; the browser
    then polls GET /jobs/<id>.
    """
    photo = request.files.get("photo")
    prompt = request.form.get("prompt", "").strip()
    own_script = request.form.get("script", "").strip()
    if not photo or not photo.filename or not (prompt or own_script):
        return jsonify(error="A photo plus a topic or a script is required."), 400
    engine = request.form.get("engine", "").strip() or None

    if own_script and len(own_script.split()) > REEL_MAX_WORDS:
        return jsonify(error="That script is longer than a reel: keep it under "
                             "{} words (about 90 seconds). Longer videos are a "
                             "different format, coming soon.".format(
                                 REEL_MAX_WORDS)), 400

    sweep_jobs()
    job = uuid.uuid4().hex[:10]
    folder = os.path.join(JOBS, job)
    os.makedirs(folder)
    ext = os.path.splitext(photo.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        return jsonify(error="Use a jpg, png or webp image."), 400
    img_path = os.path.join(folder, "photo" + ext)
    photo.save(img_path)

    # NOTE ON KEYS: BYOK keys are intentionally NOT read from the form here. An
    # async job would have to persist them to run later, and a live key in the
    # jobs DB breaks both the vault rule and the product's "no API keys in the
    # user panel" rule. The worker resolves keys from the server environment;
    # BYOK moves to the admin panel in Phase 2. The key <input>s in the current
    # form are therefore no-ops on this path -- flagged, to be removed with the
    # Phase 1 product UI, not silently half-wired.
    fish_personal_use = request.form.get("fish_personal_use", "").strip() in ("1", "true", "on")
    job_id = jobs.enqueue(
        prompt=prompt or None,
        own_script=own_script or None,
        engine=engine or "runpod",
        folder=folder,
        img_path=img_path,
        voice_engine=request.form.get("voice_engine", "").strip() or None,
        voice_id=request.form.get("voice_id", "").strip() or None,
        voice_model=request.form.get("voice_model", "").strip() or None,
        voice_speed=request.form.get("voice_speed", "").strip() or None,
        fish_personal_use=fish_personal_use,
        render_prompt=request.form.get("render_prompt", "").strip() or None,
    )
    return jsonify(job_id=job_id), 202


# Human-facing stage labels. The web layer owns these, not the worker: the
# worker records honest machine keys and this is where they become sentences the
# UI shows. There is no "captions" stage because captions are not built yet.
_STAGE_LABELS = {
    jobs.STAGE_QUEUED: "Queued",
    jobs.STAGE_SCRIPT: "Writing the script",
    jobs.STAGE_VOICE: "Generating the voice",
    jobs.STAGE_RENDER: "Rendering the video",
    jobs.STAGE_FINALIZE: "Finishing up",
    jobs.STAGE_DONE: "Done",
}


@app.route("/jobs/<job_id>")
def job_status(job_id):
    """What the browser polls. Returns coarse status, the current stage as a
    sentence, and -- only once done -- the video and everything the result panel
    needs. A failure returns its reason verbatim: the render failed, and why."""
    job = jobs.get(job_id)
    if job is None:
        return jsonify(error="No such job."), 404

    out = {
        "id": job["id"],
        "status": job["status"],
        "stage": job["stage"],
        "stage_label": _STAGE_LABELS.get(job["stage"], job["stage"]),
        "engine": job["engine"],
        "predicted_wall_s": job.get("predicted_wall_s"),
    }
    if job["status"] == jobs.FAILED:
        out["error"] = job.get("error") or "The render failed."
    if job["status"] == jobs.DONE and job.get("video_path"):
        out.update(
            video="/" + job["video_path"].replace(os.sep, "/"),
            path=job["video_path"],
            metrics=job.get("metrics") or {},
            title=job.get("title") or "",
            description=job.get("description") or "",
            tags=job.get("tags") or [],
            script=job.get("script") or "",
        )
    return jsonify(out)


@app.route("/upload", methods=["POST"])
def upload():
    """Upload to YouTube through the hub, not through v1's yt.py.

    yt.py kept its credentials in a plaintext token.json beside the code, and
    its refresh token does not expire on a schedule — so that file was a
    standing liability for as long as this route depended on it. The hub's
    adapter reads from the encrypted vault instead, with the key stored apart
    from the database, which is the whole reason the vault exists.

    yt.py stays in the tree for now: it is what the first OAuth grant was made
    with, and deleting it is a separate decision from stopping using it.
    """
    data = request.get_json(silent=True) or {}
    path = data.get("path", "")
    title = (data.get("title") or "").strip()
    if not os.path.isfile(path) or not path.startswith(JOBS):
        return jsonify(error="Video file not found. Generate one first."), 400
    if not title:
        return jsonify(error="The video needs a title."), 400

    integration_id = data.get("integration_id")
    if not integration_id:
        from hub import vault
        youtube = [i for i in vault.list_integrations() if i["platform"] == "youtube"]
        if not youtube:
            return jsonify(error="No YouTube account is connected. Open /hub/ and "
                                 "click Connect YouTube first."), 400
        if len(youtube) > 1:
            return jsonify(error="More than one YouTube account is connected; "
                                 "say which one (integration_id)."), 400
        integration_id = youtube[0]["id"]

    from hub.adapters import ADAPTERS
    result = ADAPTERS["youtube"](path, {
        "title": title,
        "text": data.get("description", ""),
        "tags": data.get("tags", []),
        "privacy": data.get("privacy", "unlisted"),
    }, integration_id)
    if not result.get("ok"):
        return jsonify(error="Upload failed: " + str(result.get("error"))), 500
    # Keep the local file so Download still works after upload; the 6h sweep
    # cleans the folder anyway.
    return jsonify(url=result["url"])


if __name__ == "__main__":
    app.run(debug=True)
