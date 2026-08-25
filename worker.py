"""The one worker loop. Run it beside the web app: `python worker.py`.

WHAT IT IS (PRODUCT-PLAN 2026-08-14, section 3)
-----------------------------------------------
A single process that claims queued jobs from jobs.py and runs them to
completion, off the HTTP request path. No broker, no Redis: the queue is the
SQLite table, and Runpod holds the actual render. This process just walks a job
through its stages and remembers the paid id.

WHY THE RUNPOD PATH IS HAND-ORCHESTRATED, NOT make_video()
----------------------------------------------------------
video.make_video() does voice AND render in one call. Convenient, but it submits
to Runpod and polls to completion INTERNALLY, so there is no moment in between
where the caller can persist the runpod_job_id. That moment is the entire point
of the queue: the id has to be in SQLite before the long poll, or a crash loses
a render we already paid for.

So for the runpod engine the worker does the stages itself --
    say()  ->  runpod.prepare_and_submit()  ->  PERSIST id  ->  collect()  ->  download()
-- which also gives the progress UI a real "generating voice" then "rendering"
transition instead of one opaque wait.

Every OTHER engine (motion, hf, wavespeed, heygen) still goes through
make_video() wholesale. That is not laziness, it is the property difference
stated plainly: those engines are free (hf, motion), or short, or work-in-
progress and not in the first release. None is both long AND paid, so none earns
the persist-and-resume machinery. If wavespeed ever ships as a primary paid
renderer it gets the same treatment; until then, wholesale is honest and simpler.
The win they all share regardless: the render is off the request path.

RESTART SURVIVAL
----------------
On startup, recover() looks at jobs left 'running' (a worker can only die, never
hand off, since there is one). A job that already has a runpod_job_id is RESUMED
-- re-attach to the paid render and finish it. A job without one is REQUEUED --
nothing was paid, so redoing script+voice is free. This is the concrete payoff
of storing the id: an abandoned hour-long render, already on our invoice, is
collected rather than lost.

KEYS COME FROM THE ENVIRONMENT, NOT THE JOB ROW
-----------------------------------------------
BYOK keys are deliberately NOT persisted in the jobs table. Storing a live API
key in the job DB would violate both the vault rule (never hold a real
credential at rest outside the encrypted vault) and the product rule (no API
keys in the user panel). The worker resolves keys from the server environment,
which is where the product plan puts them anyway -- admin-managed, out of the
user's hands. resolve_key() still falls back to env exactly as before.
"""

import os
import time

from env import load_env

# The worker is its own process and is not launched through run.bat, so it must
# load .env itself or it inherits none of the API keys (this is what caused
# "GEMINI_API_KEY is not set" mid-render). Done before importing jobs/runpod/
# video so every downstream os.environ.get sees the keys.
load_env()

import jobs
import runpod
import video

POLL_IDLE_S = float(os.environ.get("WORKER_IDLE_S", "3"))


def _voice_kwargs(job):
    return dict(
        engine=job.get("voice_engine"),
        voice_id=job.get("voice_id"),
        voice_model=job.get("voice_model"),
        speed=job.get("voice_speed"),
        fish_personal_use=bool(job.get("fish_personal_use")),
    )


def resolve_script(job):
    """Stage 1, 'writing the script'. Moved off the request path so POST returns
    instantly even when Gemini is slow, and so the UI can show this as its own
    step. Mirrors app.py's old inline logic: bring-your-own script skips the
    writer and only gets metadata; a topic gets a full generation."""
    from generate import make_script, make_meta
    own = (job.get("own_script") or "").strip()
    if own:
        data = make_meta(own, job.get("prompt") or "")
    else:
        data = make_script(job.get("prompt") or "")
    jobs.set_meta(job["id"], data["script"], data.get("title", ""),
                  data.get("description", ""), data.get("tags", []))
    return data["script"]


def _run_runpod(job, script):
    """The resumable path. Returns (video_path, metrics)."""
    folder = job["folder"]
    engine = job["engine"]
    model = engine.split("runpod-", 1)[1] if engine.startswith("runpod-") else None
    voice = os.path.join(folder, "voice.mp3")

    # If we are RESUMING (id already persisted), the paid render is in flight and
    # the voice was made before the crash -- skip straight to collecting it.
    if job.get("runpod_job_id"):
        job_id = job["runpod_job_id"]
        prep = runpod.prep_from_row(model, job.get("duration_s"),
                                    job.get("predicted_wall_s"))
        started = time.time()
        video_url, actual_cost, _ = runpod.collect(job_id, runpod.resolve_key())
        jobs.set_stage(job["id"], jobs.STAGE_FINALIZE)
        out = os.path.join(folder, "video.mp4")
        runpod.download(video_url, out, job_id)
        # No voice metrics to fold in on resume: the voice mp3 exists on disk but
        # its billing receipt was written by the process that died. The render
        # cost -- the one that matters and the one just confirmed -- is reported.
        return out, runpod.render_metrics(prep, job_id, actual_cost,
                                          time.time() - started)

    # Fresh render.
    jobs.set_stage(job["id"], jobs.STAGE_VOICE)
    voice_metrics = video.say(script, voice, **_voice_kwargs(job)) or {}

    jobs.set_stage(job["id"], jobs.STAGE_RENDER)
    job_id, prep = runpod.prepare_and_submit(
        job["img_path"], voice, folder, model=model,
        prompt=job.get("render_prompt"))
    # PERSIST BEFORE POLLING. This write is the crash boundary: everything after
    # it is recoverable, everything before it was free to redo.
    jobs.set_runpod_job_id(job["id"], job_id,
                           predicted_wall_s=prep["predicted_wall_s"],
                           duration_s=prep["duration"])

    started = time.time()
    video_url, actual_cost, _ = runpod.collect(job_id, runpod.resolve_key())
    jobs.set_stage(job["id"], jobs.STAGE_FINALIZE)
    out = os.path.join(folder, "video.mp4")
    runpod.download(video_url, out, job_id)

    metrics = video._with_voice(
        runpod.render_metrics(prep, job_id, actual_cost, time.time() - started),
        voice_metrics)
    return out, metrics


def _run_other(job, script):
    """Every non-runpod engine, run wholesale. Off the request path, but without
    the persist-and-resume machinery those engines do not need. make_video()
    regenerates the voice itself, so nothing is generated twice here."""
    jobs.set_stage(job["id"], jobs.STAGE_RENDER)
    video_path, _engine, metrics = video.make_video(
        job["img_path"], script, job["folder"], engine=job["engine"],
        **_voice_kwargs_for_make_video(job))
    return video_path, metrics


def _voice_kwargs_for_make_video(job):
    # make_video takes voice_* under slightly different names than say().
    return dict(
        voice_engine=job.get("voice_engine"),
        voice_id=job.get("voice_id"),
        voice_model=job.get("voice_model"),
        voice_speed=job.get("voice_speed"),
        fish_personal_use=bool(job.get("fish_personal_use")),
    )


def process(job):
    """Run one claimed job to a terminal state. Any exception is caught and
    recorded as a failure with its reason -- never swallowed, never turned into a
    lesser result."""
    try:
        # A resumed job already has its script; only a fresh one writes it.
        if job.get("script"):
            script = job["script"]
        else:
            script = resolve_script(job)

        if (job["engine"] or "").startswith("runpod"):
            video_path, metrics = _run_runpod(job, script)
        else:
            video_path, metrics = _run_other(job, script)

        jobs.mark_done(job["id"], video_path, metrics)
    except Exception as e:  # noqa: BLE001 -- a worker must not die on one bad job
        jobs.mark_failed(job["id"], "Render failed: {}".format(e))


def recover():
    """Re-attach to or requeue jobs interrupted by a previous crash. See the
    module docstring: paid renders (have a runpod_job_id) are resumed, unpaid
    ones are put back in the queue."""
    for job in jobs.interrupted():
        if job.get("runpod_job_id"):
            process(job)          # resume: process() detects the id and collects
        else:
            jobs.requeue(job["id"])


def run_once():
    """Claim and process one job. Returns True if it did work, False if idle.
    Split out so tests can drive the worker one job at a time without a loop."""
    job = jobs.claim_next()
    if job is None:
        return False
    process(job)
    return True


def main():
    jobs.init_db()
    recover()
    print("worker up; polling every {:.0f}s".format(POLL_IDLE_S), flush=True)
    while True:
        try:
            if not run_once():
                time.sleep(POLL_IDLE_S)
        except KeyboardInterrupt:
            print("worker stopping", flush=True)
            break
        except Exception as e:  # noqa: BLE001 -- the loop outlives any one error
            print("worker loop error: {}".format(e), flush=True)
            time.sleep(POLL_IDLE_S)


if __name__ == "__main__":
    main()
