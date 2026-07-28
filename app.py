import os
import shutil
import time
import uuid
from flask import Flask, render_template, request, jsonify
from generate import make_script, make_meta
from video import make_video
from yt import upload_video

app = Flask(__name__)

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
    return render_template("index.html", lipsync=lipsync,
                           heygen_ready=heygen_ready, heygen_iv=heygen_iv,
                           wavespeed_ready=wavespeed_ready)


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


@app.route("/generate", methods=["POST"])
def generate():
    photo = request.files.get("photo")
    prompt = request.form.get("prompt", "").strip()
    own_script = request.form.get("script", "").strip()
    if not photo or not photo.filename or not (prompt or own_script):
        return jsonify(error="A photo plus a topic or a script is required."), 400
    if own_script and len(own_script.split()) > 220:
        return jsonify(error="Script too long: keep it under 220 words (~90s of video) to control render cost."), 400
    engine = request.form.get("engine", "").strip() or None
    # Per-model ceiling, checked here rather than at render time. The adapter
    # checks the real (measured) duration before spending anything, so this is
    # only about feedback speed: without it, an over-cap LTX script would run
    # Gemini and TTS first and fail a minute later. Pasted scripts only -- a
    # generated one does not exist yet, so that case still fails in the adapter.
    if own_script and (engine or "").startswith("wavespeed"):
        try:
            from wavespeed import max_words
            cap = max_words("ltx" if engine.endswith("-ltx") else "infinitetalk")
        except Exception:
            cap = None
        if cap and len(own_script.split()) > cap:
            return jsonify(error="That renderer caps at about {} words (~{}s of speech). "
                                 "Shorten the script or pick InfiniteTalk.".format(cap, cap * 60 // 150)), 400
    sweep_jobs()
    job = uuid.uuid4().hex[:10]
    folder = os.path.join(JOBS, job)
    os.makedirs(folder)
    ext = os.path.splitext(photo.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        return jsonify(error="Use a jpg, png or webp image."), 400
    img_path = os.path.join(folder, "photo" + ext)
    photo.save(img_path)
    if own_script:
        script = make_meta(own_script, prompt)
    else:
        try:
            script = make_script(prompt)
        except Exception as e:
            return jsonify(error="Script generation failed: " + str(e)), 500
    heygen_key = request.form.get("heygen_key", "").strip() or None
    wavespeed_key = request.form.get("wavespeed_key", "").strip() or None
    try:
        video_path, engine, metrics = make_video(
            img_path, script["script"], folder, engine=engine,
            heygen_key=heygen_key, wavespeed_key=wavespeed_key
        )
    except Exception as e:
        # No silent substitution: a failed renderer is an error the person sees,
        # not a quieter video they did not ask for.
        return jsonify(error="Video render failed: " + str(e)), 500
    return jsonify(
        video="/" + video_path.replace(os.sep, "/"),
        path=video_path,
        engine=engine,
        metrics=metrics,
        script=script["script"],
        title=script["title"],
        description=script["description"],
        tags=script["tags"],
    )


@app.route("/upload", methods=["POST"])
def upload():
    data = request.get_json(silent=True) or {}
    path = data.get("path", "")
    title = (data.get("title") or "").strip()
    if not os.path.isfile(path) or not path.startswith(JOBS):
        return jsonify(error="Video file not found. Generate one first."), 400
    if not title:
        return jsonify(error="The video needs a title."), 400
    try:
        url = upload_video(
            path,
            title,
            data.get("description", ""),
            data.get("tags", []),
            data.get("privacy", "unlisted"),
        )
    except Exception as e:
        return jsonify(error="Upload failed: " + str(e)), 500
    # Keep the local file so Download still works after upload; the 6h sweep
    # cleans the folder anyway.
    return jsonify(url=url)


if __name__ == "__main__":
    app.run(debug=True)
