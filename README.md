# prompt2tube

Give it a photo and a prompt. It writes a short script with a free AI model, turns the photo into a talking-head style video with a voiceover, and uploads it to YouTube.

## How it works

1. You upload a photo and type a topic
2. Gemini (free tier) writes a 40-70 word script plus a title and description
3. edge-tts (free) reads the script out loud
4. MoDA, an open talking-head model running on a free Hugging Face ZeroGPU Space, turns the photo + voiceover into a lip-synced video. If the Space is busy or the daily GPU quota runs out, ffmpeg falls back to animating the photo with a gentle head sway, so a video always comes out
5. The video uploads to YouTube through the Data API v3 (unlisted by default so you can check it first)

Every stage is free: free Gemini tier, free TTS, free community GPU, free YouTube API quota.

## Setup

You need Python 3.10+ and ffmpeg on your PATH (`winget install ffmpeg` on Windows).

```
pip install -r requirements.txt
```

### Gemini (writes the script)

Get a free key at https://aistudio.google.com/apikey, then

```
set GEMINI_API_KEY=your-key
```

### Hugging Face (renders the lip-synced video, free)

Works without an account, but the anonymous GPU quota is tiny. Make a free account at https://huggingface.co, create a read token at https://huggingface.co/settings/tokens, then

```
set HF_TOKEN=your-token
```

Optional knobs: `set RENDERER=motion` skips the GPU entirely; `set HF_SPACE=owner/space` points at a different Space.

### YouTube API

1. Go to https://console.cloud.google.com, create a project
2. APIs & Services > Library > enable "YouTube Data API v3"
3. APIs & Services > OAuth consent screen > External > add yourself as a test user
4. Credentials > Create Credentials > OAuth client ID > Desktop app
5. Download the JSON and save it as `client_secret.json` in this folder

The first upload opens a browser window asking you to sign in. After that a `token.json` is saved and it stops asking.

## Run

Copy `.env.example` to `.env`, fill in your keys, then double-click `run.bat` (or `python app.py` with the env vars set). Open http://127.0.0.1:5000

## Running it on another machine (demo transfer)

What the new machine needs: Python 3.10+, ffmpeg (`winget install ffmpeg`), `pip install -r requirements.txt`.

What to copy: this folder, minus `__pycache__/` and `static/jobs/`. The three credentials:

- `.env` (Gemini + HF keys): either the new person creates their own free keys via `.env.example`, or you lend yours and rotate them afterwards (regenerate the Gemini key in AI Studio, revoke the HF token in settings).
- `client_secret.json`: identifies the Google Cloud project. Copy it as-is.
- `token.json`: the YouTube channel login. Copy it too and uploads go to YOUR channel with no sign-in (simplest for a supervised demo — delete it from their machine afterwards). Leave it out and their first upload triggers a Google sign-in instead; their Google account must first be added as a test user under OAuth consent screen in your Google Cloud console, and uploads then go to their channel.

## Deploying to Render (free)

The repo includes `render.yaml`. Steps:

1. Push to GitHub, then on https://render.com: New > Blueprint > pick the repo. It reads `render.yaml` automatically.
2. Set three env vars when prompted: `GEMINI_API_KEY`, `HF_TOKEN`, and `GOOGLE_TOKEN_JSON` — get the last one by running `python print_token.py` locally (needs a `token.json` from one successful local upload) and pasting the single-line output.
3. Deploy. First load after 15 idle minutes takes ~1 minute (free tier wakes from sleep).

Server notes: uploads go to the channel that owns the token. The motion fallback is slow on the free 0.1-CPU instance; the normal lip-sync path is unaffected (rendering happens on Hugging Face's GPU).

## Notes

- Default API quota allows about 6 uploads per day (1600 units each out of 10000)
- While the OAuth app is in testing mode, videos uploaded through it stay locked as private until Google verifies the app. Fine for an internship demo, just pick Private or check the video in YouTube Studio
- Generated videos land in `static/jobs/`, delete the folder whenever
