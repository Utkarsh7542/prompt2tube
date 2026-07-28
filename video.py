import asyncio
import glob
import os
import shutil
import subprocess
from gtts import gTTS

HF_SPACE = "multimodalart/MoDA-fast-talking-head"

MOTION = (
    "scale=1500:1500:force_original_aspect_ratio=increase,"
    "rotate='0.035*sin(2*PI*t/3.1)':c=none,"
    "crop=1280:720:(iw-1280)/2+18*sin(2*PI*t/4.3):(ih-720)/3+12*sin(2*PI*t/2.6),"
    "format=yuv420p"
)


def find_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    candidates = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "**", "bin", "ffmpeg.exe"), recursive=True)
    candidates += glob.glob(r"C:\ffmpeg*\bin\ffmpeg.exe")
    candidates += glob.glob(r"C:\Program Files\ffmpeg*\bin\ffmpeg.exe")
    if candidates:
        return candidates[0]
    raise RuntimeError("ffmpeg was not found, install it and reopen the terminal")


def say(text, path):
    name = os.environ.get("VOICE", "en-US-ChristopherNeural")
    try:
        import edge_tts
        asyncio.run(edge_tts.Communicate(text, name).save(path))
    except Exception:
        gTTS(text).save(path)


def hf_render(img_path, voice, folder):
    """Lip-synced render on a free Hugging Face ZeroGPU Space (MoDA)."""
    from gradio_client import Client, handle_file
    wav = os.path.join(folder, "voice.wav")
    subprocess.run([find_ffmpeg(), "-y", "-i", voice, "-ar", "16000", "-ac", "1", wav], capture_output=True)
    space = os.environ.get("HF_SPACE", HF_SPACE)
    token = os.environ.get("HF_TOKEN") or None
    if token:
        try:
            client = Client(space, token=token)
        except TypeError:
            client = Client(space, hf_token=token)
    else:
        client = Client(space)
    result = client.predict(
        source_image_path=handle_file(os.path.abspath(img_path)),
        driving_audio_path=handle_file(os.path.abspath(wav)),
        emotion_name="None",
        cfg_scale=1.2,
        api_name="/generate_motion",
    )
    made = result.get("video") if isinstance(result, dict) else result
    if not made or not os.path.isfile(made):
        raise RuntimeError("the Space returned no video file")
    out = os.path.join(folder, "video.mp4")
    shutil.copy(made, out)
    return out


def motion_render(img_path, voice, folder):
    """Free fallback: the still photo with a gentle head-sway, plus the voiceover."""
    out = os.path.join(folder, "video.mp4")
    cmd = [
        find_ffmpeg(), "-y",
        "-loop", "1", "-i", img_path,
        "-i", voice,
        "-filter_complex", "[0:v]" + MOTION + "[v]",
        "-map", "[v]", "-map", "1:a",
        "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast",
        "-c:a", "aac",
        "-shortest", "-movflags", "+faststart",
        out,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip().splitlines()[-1])
    return out


def make_video(img_path, script, folder, engine=None, heygen_key=None):
    """Render with the requested engine, degrading down the chain on failure:
    heygen (paid, only when asked) -> hf (free ZeroGPU) -> motion (local ffmpeg).
    Returns (video_path, engine_used, note, metrics). note carries a fallback
    reason; metrics carries per-render cost and timing for the UI."""
    import time
    engine = (engine or os.environ.get("RENDERER", "hf")).lower()
    note = ""
    started = time.time()
    if engine == "heygen":
        try:
            from heygen import heygen_render
            path, metrics = heygen_render(img_path, script, folder, heygen_key)
            return path, "heygen", "", metrics
        except Exception as e:
            note = str(e)[:300]
            print("HeyGen render failed, falling back to free path:", note)
        engine = "hf"  # degrade, never die
    voice = os.path.join(folder, "voice.mp3")
    say(script, voice)
    if engine != "motion":
        try:
            path = hf_render(img_path, voice, folder)
            metrics = {"est_cost": 0.0, "render_s": round(time.time() - started),
                       "engine_detail": "free ZeroGPU"}
            return path, "hf", note, metrics
        except Exception as e:
            note = (note + " | " if note else "") + str(e)[:300]
            print("HF Space render failed, falling back to motion:", note)
    path = motion_render(img_path, voice, folder)
    metrics = {"est_cost": 0.0, "render_s": round(time.time() - started),
               "engine_detail": "local ffmpeg"}
    return path, "motion", note, metrics
