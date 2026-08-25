# Deploying prompt2tube (BYOK demo)

A single Render web service runs both the web app and the render worker in one
container, sharing a SQLite job database. Each tester enters their OWN Runpod and
Fish keys, so their account pays for their renders. You (the operator) provide
only the Gemini key (script writing) and a vault key.

## How keys work here

- **Testers bring:** Runpod API key (render) + Fish Audio API key (voice). They
  paste these into the form. The keys are encrypted, stored only for the length
  of that one render, and wiped when it finishes.
- **You provide (server-side):** `GEMINI_API_KEY` (script writing, cheap/free)
  and `HUB_VAULT_KEY` (encrypts the testers' per-job keys).

## 1. Put this code in a GitHub repo

Create an empty GitHub repo and push this folder to it. The `.gitignore` keeps
secrets, logs, reports, and scratch files out.

```
git init
git add .
git commit -m "prompt2tube deploy"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

## 2. Create the service on Render

1. render.com -> New -> Web Service -> connect the repo. Render reads
   `render.yaml` automatically (Blueprint); accept it.
2. Set the two environment variables (marked `sync: false`, never in the repo):
   - `GEMINI_API_KEY` — your Google Gemini key (writes the script).
   - `HUB_VAULT_KEY` — generate one and paste it:
     ```
     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
     ```
   Do NOT set `RUNPOD_API_KEY` or `FISH_API_KEY`; leaving them unset is what
   makes the app require each tester's own keys.
3. Deploy. You get a URL like `https://prompt2tube.onrender.com`. Share it.

## 3. What testers do

Open the link, upload a photo, type a topic (or paste a script), **paste their
own Runpod and Fish keys**, and click Generate. They watch the progress steps and
download the finished video. Their keys pay for their render (~$0.27 each) and are
deleted when the render completes.

Where testers get keys: Runpod at runpod.io (API keys), Fish at fish.audio. If a
tester does not have these, they cannot render on this BYOK setup.

## Things to know (free tier)

- **It sleeps when idle.** First request after a nap takes ~30s to wake. Keep the
  browser tab open during a render; the progress poll keeps it awake.
- **The disk is ephemeral.** The job database and finished videos are wiped on a
  redeploy, so download promptly.
- **Publishing is not durable here.** YouTube/LinkedIn connections live in a local
  DB a redeploy wipes. The core demo (make and download a video) does not need it.

## Running locally instead

Two processes, from the project root, with a `.env` (copy `.env.example`) holding
`GEMINI_API_KEY` and `HUB_VAULT_KEY`:

```
python app.py       # terminal 1: the web app
python worker.py    # terminal 2: the render worker
```

Then enter your Runpod and Fish keys in the form as a tester would.
