import asyncio
import glob
import os
import shutil
import subprocess

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
        # Lazy, like edge_tts above. gTTS only runs when edge-tts is missing or
        # fails, so importing it at module load made every consumer of this
        # module depend on a package the normal path never touches.
        from gtts import gTTS
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
    """The still photo with a gentle head-sway, plus the voiceover. No lip sync.

    Not a fallback: it is chosen explicitly. Its value is having zero
    dependencies -- no GPU, no network, no API key -- which makes it the way to
    verify the rest of the pipeline (script, voice, mux, upload) when every
    renderer is unavailable."""
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


def make_video(img_path, script, folder, engine=None, heygen_key=None,
               wavespeed_key=None):
    """Render with the engine that was requested. No automatic substitution.

    Until 2026-07-28 this was a chain (heygen -> hf -> motion), each rung
    catching the one above. That was removed: `motion` produces a still photo
    with a pan-and-zoom, so a request for lip sync could return a materially
    different output reported as success. Whether the failed engine was paid or
    free does not change that, which is why no rung survives.

    The selected engine runs and a failure propagates. `hf` and `motion` remain
    selectable; `motion` in particular is the only path requiring no GPU, no
    network and no key, so it verifies script -> voice -> mux -> upload when no
    renderer is reachable. Accepted trade-off: a transient renderer outage now
    returns an error rather than a lesser video.

    Returns (video_path, engine_used, metrics).
    """
    import time
    engine = (engine or os.environ.get("RENDERER", "hf")).lower()
    started = time.time()

    # Voice is a pipeline stage, not a detail of one branch. It used to sit
    # below the HeyGen block because HeyGen does its own TTS and only the free
    # path needed an mp3; WaveSpeed takes audio, so it has to happen first.
    # HeyGen is the one engine that does not consume ours, so it does not pay
    # for the TTS it will ignore.
    voice = os.path.join(folder, "voice.mp3")
    if engine != "heygen":
        say(script, voice)

    if engine.startswith("wavespeed"):
        # "wavespeed" -> InfiniteTalk Fast (default), "wavespeed-ltx" -> LTX-2.3.
        # Both consume the same voice.mp3, which is what makes comparing them fair.
        from wavespeed import wavespeed_render
        model = "ltx" if engine.endswith("-ltx") else "infinitetalk"
        path, metrics = wavespeed_render(img_path, voice, folder,
                                         model=model, request_key=wavespeed_key)
        return path, engine, metrics

    if engine == "heygen":
        from heygen import heygen_render
        path, metrics = heygen_render(img_path, script, folder, heygen_key)
        return path, "heygen", metrics

    if engine == "hf":
        path = hf_render(img_path, voice, folder)
        return path, "hf", {"est_cost": 0.0,
                            "render_s": round(time.time() - started),
                            "engine_detail": "free ZeroGPU"}

    if engine == "motion":
        path = motion_render(img_path, voice, folder)
        return path, "motion", {"est_cost": 0.0,
                                "render_s": round(time.time() - started),
                                "engine_detail": "local ffmpeg"}

    raise ValueError("unknown renderer {!r}; known: wavespeed, wavespeed-ltx, "
                     "heygen, hf, motion".format(engine))
