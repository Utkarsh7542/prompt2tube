import os
import shutil
import time
import uuid
from flask import Flask, render_template, request, jsonify
from generate import make_script
from video import make_video
from yt import upload_video

app = Flask(__name__)
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
    return render_template("index.html", lipsync=lipsync)


@app.route("/generate", methods=["POST"])
def generate():
    photo = request.files.get("photo")
    prompt = request.form.get("prompt", "").strip()
    if not photo or not photo.filename or not prompt:
        return jsonify(error="A photo and a prompt are both required."), 400
    sweep_jobs()
    job = uuid.uuid4().hex[:10]
    folder = os.path.join(JOBS, job)
    os.makedirs(folder)
    ext = os.path.splitext(photo.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        return jsonify(error="Use a jpg, png or webp image."), 400
    img_path = os.path.join(folder, "photo" + ext)
    photo.save(img_path)
    try:
        script = make_script(prompt)
    except Exception as e:
        return jsonify(error="Script generation failed: " + str(e)), 500
    try:
        video_path, engine, note = make_video(img_path, script["script"], folder)
    except Exception as e:
        return jsonify(error="Video render failed: " + str(e)), 500
    return jsonify(
        video="/" + video_path.replace(os.sep, "/"),
        path=video_path,
        engine=engine,
        note=note,
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
    # YouTube is now the permanent copy; the local working folder is no longer needed.
    shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    return jsonify(url=url)


if __name__ == "__main__":
    app.run(debug=True)
