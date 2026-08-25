# prompt2tube

Give it a photo and a prompt. It writes a short script with a free AI model, turns the photo into a talking-head style video with a voiceover, and uploads it to YouTube.

## How it works

1. You upload a photo and type a topic
2. Gemini (free tier) writes a 40-70 word script plus a title and description
3. edge-tts (free) reads the script out loud
4. The renderer you picked turns the photo + voiceover into a lip-synced video. Options: WaveSpeed (hosted InfiniteTalk or LTX-2.3, paid), HeyGen (paid), MoDA on a free Hugging Face ZeroGPU Space, or `motion` — an ffmpeg pass that animates the photo with a gentle head sway and no lip sync at all. Renderers never substitute for one another: if the one you chose fails, you get the reason and pick another, rather than silently receiving a different kind of video
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

### HeyGen (optional, paid — production-quality lip sync)

Pick "HeyGen" in the renderer dropdown. Key resolution: a key pasted in the form is used for that request only and never stored; otherwise the server's `HEYGEN_API_KEY` is used. Get a key at https://app.heygen.com/settings/api — pay-as-you-go, minimum $5 top-up (no free API credits since Feb 2026).

Cost: ~$1 per minute of 720p video, so ~$0.50 per short video here; `HEYGEN_AVATAR_IV=1` switches to the newer motion engine at $4/min. The UI shows remaining credits and an estimated cost per render. Uploaded photos are cached by content hash so the same face is never uploaded twice. If HeyGen fails (no credits, bad key, outage) the render fails with that reason; pick another renderer from the dropdown.

*(Superseded 2026-07-28: until then a failed render fell through to the free ZeroGPU renderer and finally to `motion`. That chain was removed because it substituted a visibly different product — a still photo with a pan-and-zoom — while reporting success.)*

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
2. Set `GEMINI_API_KEY` and `HF_TOKEN`, plus `WAVESPEED_API_KEY` / `HEYGEN_API_KEY` if you want the paid renderers server-side rather than pasted per request.
3. Set `HUB_VAULT_KEY` to a generated Fernet key (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`). Without it the vault generates a throwaway key on each boot and every connected account becomes unreadable on restart.
4. Set `HUB_REDIRECT_BASE` to the service's public HTTPS URL, and add `<that URL>/hub/callback/youtube` and `/hub/callback/linkedin` to the respective developer apps' allowed redirect URIs.
5. Deploy. First load after 15 idle minutes takes ~1 minute (free tier wakes from sleep).

**Known limitation on the free tier:** connected accounts live in `hub.db` on local disk, and Render's free instances have ephemeral filesystems — the file is wiped on redeploy and on wake-from-sleep. So social connections do not survive there. Fixing it properly means a persistent disk or an external database; until then, treat the hosted deploy as render-and-download only and publish from a local run.

*(Superseded 2026-07-28: this section previously told you to set `GOOGLE_TOKEN_JSON` from `python print_token.py`. That fed v1's `yt.py`, which stored a YouTube refresh token in plaintext. Uploads now go through the hub's encrypted vault instead, and nothing reads `GOOGLE_TOKEN_JSON` any more.)*

Server notes: uploads go to the channel that owns the token. The `motion` renderer is slow on the free 0.1-CPU instance because ffmpeg runs locally; the lip-sync paths are unaffected, since rendering happens on Hugging Face's or WaveSpeed's GPUs. Rendering is synchronous, so the gunicorn timeout (900s) is also the maximum render time — long videos need the render moved off the request path.

## Notes

- Default API quota allows about 6 uploads per day (1600 units each out of 10000)
- While the OAuth app is in testing mode, videos uploaded through it stay locked as private until Google verifies the app. Fine for an internship demo, just pick Private or check the video in YouTube Studio
- Generated videos land in `static/jobs/`, delete the folder whenever
