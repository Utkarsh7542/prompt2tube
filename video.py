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


def fit_image_to_budget(path, folder, max_bytes, widths=(1536, 1024, 768, 512)):
    """Shrink a photo until it fits `max_bytes`, or explain why it cannot.

    WHY THIS ONE IS ALLOWED TO BE AUTOMATIC
    ---------------------------------------
    This codebase refuses to modify a request silently -- the renderer fallback
    chain went on 2026-07-28, and edge-tts is never substituted for ElevenLabs --
    so an automatic rewrite of the user's input needs a reason.

    The reason is that this one does not change the output. The renderer emits
    480p or 720p, and "720p" is a pixel budget with aspect preserved: measured
    2026-08-02, a 4480x6600 input came back 784x1152. Every pixel above that is
    discarded by the model before it renders. Downscaling to fit therefore
    produces the SAME video, which is exactly what distinguishes it from
    substituting a renderer or a voice, where the output is materially different.

    It is still not invisible. It only runs when the file genuinely does not
    fit, it never upscales, and it returns a record of what it did so the caller
    can report it. Silent would be wrong; automatic is fine.

    Returns (path, info) where info is None if nothing was done.
    """
    original = os.path.getsize(path)
    if original <= max_bytes:
        return path, None

    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, "photo_fitted.jpg")
    last_err = ""
    for width in widths:
        # -2 keeps the height even (h264/jpeg encoders want it) and preserves
        # aspect. scale' with force_original_aspect_ratio=decrease means a photo
        # already narrower than `width` is never enlarged.
        vf = "scale='min({w},iw)':-2".format(w=width)
        r = subprocess.run(
            [find_ffmpeg(), "-y", "-i", path, "-vf", vf, "-q:v", "3", out],
            capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isfile(out):
            last_err = (r.stderr or "")[-300:]
            continue
        size = os.path.getsize(out)
        if size <= max_bytes:
            return out, {
                "original_bytes": original,
                "fitted_bytes": size,
                "width": width,
                "note": "photo downscaled to fit the request limit; the model "
                        "renders at 480p/720p so the output is unaffected",
            }

    raise RuntimeError(
        "could not shrink {} ({:.1f} MB) below {:.1f} MB even at {}px wide{}".format(
            os.path.basename(path), original / 1e6, max_bytes / 1e6, widths[-1],
            (": " + last_err) if last_err else ""))


def _edge_say(text, path):
    """The free path: edge-tts, falling back to gTTS.

    FLAGGED, NOT RESOLVED (2026-07-31): by the rule that removed the renderer
    fallback chain, this fallback is also a substitution -- gTTS is a different
    voice from the one edge-tts would have produced. It survives here because
    the stakes differ in kind: nobody selected a specific free synthetic voice,
    no money is spent either way, and no renderer bill depends on the outcome.
    That is an argument for treating it differently, not a proof that it is
    fine. Left as-is deliberately rather than changed unilaterally.
    """
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
    return {"engine_detail": "edge-tts", "est_cost": 0.0, "cached": False}


def say(text, path, engine=None, elevenlabs_key=None, voice_id=None,
        voice_model=None, speed=None, fish_key=None, fish_personal_use=False):
    """Produce the voiceover. Returns a metrics dict.

    Voice is a selectable ENGINE, exactly like the renderer, and for the same
    reason: the voice is part of the product. So there is no automatic
    substitution in either direction. A failed ElevenLabs call raises rather
    than quietly becoming edge-tts, because the user chose a specific voice and
    a different voice is a different product -- and because the substitution
    would only be discovered after the renderer had been paid to lip-sync the
    wrong one. Recovery is one dropdown change.

    There is deliberately no "auto" setting. Auto is where silent substitution
    reappears wearing a friendlier name.
    """
    engine = (engine or os.environ.get("VOICE_ENGINE", "edge")).strip().lower()

    if engine in ("elevenlabs", "11labs"):
        from elevenlabs import speak
        _, metrics = speak(text, path, request_key=elevenlabs_key,
                           voice_id=voice_id, model=voice_model, speed=speed)
        return metrics

    if engine in ("fish", "fishaudio", "fish-audio"):
        from fish import speak as fish_speak
        _, metrics = fish_speak(text, path, request_key=fish_key,
                                voice_id=voice_id, model=voice_model,
                                personal_use=fish_personal_use)
        return metrics

    if engine in ("edge", "edge-tts", "free", ""):
        return _edge_say(text, path)

    raise ValueError(
        "unknown voice engine {!r}; known: edge, elevenlabs, fish".format(engine))


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


def _with_voice(metrics, voice_metrics):
    """Fold the voice stage's numbers into the render's, without collision.

    Both stages report est_cost, engine_detail, duration_s and render_s, so the
    voice keys are prefixed rather than merged -- silently overwriting the
    renderer's cost with the voice's would understate a bill by about 20x.

    total_cost exists because neither number alone is what anyone wants to know,
    and because the voice's own settings move the renderer's half of it: audio
    seconds are what the renderer bills, and speed decides how many there are.
    """
    if not voice_metrics:
        return metrics
    merged = dict(metrics)
    for k, v in voice_metrics.items():
        merged["voice_" + k] = v
    render_cost = metrics.get("est_cost")
    voice_cost = voice_metrics.get("est_cost")
    if render_cost is not None and voice_cost is not None:
        merged["total_cost"] = round(render_cost + voice_cost, 4)
    return merged


def make_video(img_path, script, folder, engine=None, heygen_key=None,
               wavespeed_key=None, voice_engine=None, elevenlabs_key=None,
               voice_id=None, voice_model=None, voice_speed=None,
               runpod_key=None, fish_key=None, fish_personal_use=False):
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
    voice_metrics = {}
    if engine != "heygen":
        voice_metrics = say(script, voice, engine=voice_engine,
                            elevenlabs_key=elevenlabs_key, voice_id=voice_id,
                            voice_model=voice_model, speed=voice_speed,
                            fish_key=fish_key,
                            fish_personal_use=fish_personal_use) or {}

    if engine.startswith("wavespeed"):
        # Everything after "wavespeed-" is the model key in wavespeed.MODELS, so
        # adding a model there makes it selectable here with no change to this
        # file. Bare "wavespeed" means the adapter's default. Every model
        # consumes the same voice.mp3, which is what makes comparing them fair.
        from wavespeed import wavespeed_render
        model = engine.split("wavespeed-", 1)[1] if engine.startswith("wavespeed-") else None
        path, metrics = wavespeed_render(img_path, voice, folder,
                                         model=model, request_key=wavespeed_key)
        return path, engine, _with_voice(metrics, voice_metrics)

    if engine.startswith("runpod"):
        # Same model as the wavespeed path, bought at a flat price per video
        # rather than per second (measured 2026-08-06: $0.25 whether the clip is
        # 4s or 5 minutes). That inverts the economics -- longer single renders
        # are cheaper per minute, not more expensive -- so nothing here should
        # ever split a script to save money. See runpod.py.
        from runpod import runpod_render
        model = engine.split("runpod-", 1)[1] if engine.startswith("runpod-") else None
        path, metrics = runpod_render(img_path, voice, folder,
                                      model=model, request_key=runpod_key)
        return path, engine, _with_voice(metrics, voice_metrics)

    if engine == "heygen":
        from heygen import heygen_render
        path, metrics = heygen_render(img_path, script, folder, heygen_key)
        # No voice metrics to fold in: HeyGen does its own TTS, so say() was
        # skipped above rather than paying for an mp3 it would ignore.
        return path, "heygen", metrics

    if engine == "hf":
        path = hf_render(img_path, voice, folder)
        return path, "hf", _with_voice({"est_cost": 0.0,
                                        "render_s": round(time.time() - started),
                                        "engine_detail": "free ZeroGPU"},
                                       voice_metrics)

    if engine == "motion":
        path = motion_render(img_path, voice, folder)
        return path, "motion", _with_voice({"est_cost": 0.0,
                                            "render_s": round(time.time() - started),
                                            "engine_detail": "local ffmpeg"},
                                           voice_metrics)

    raise ValueError("unknown renderer {!r}; known: runpod, runpod-infinitetalk-720p, "
                     "wavespeed, wavespeed-ltx, heygen, hf, motion".format(engine))
