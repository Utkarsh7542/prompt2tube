# Deploying prompt2tube (full BYOK demo)

A single Render web service runs both the web app and the render worker in one
container, sharing a SQLite job database. Each tester enters their OWN keys, so
their accounts pay for their renders. You (the operator) set only one thing: a
vault key used to encrypt those keys.

## How keys work here

- **Testers bring:** Runpod API key (render), Fish Audio API key (voice), and
  Google Gemini API key (writes the script from a topic). They paste these into
  the form. The keys are encrypted, stored only for the length of that one
  render, and wiped when it finishes.
- **You provide (server-side):** `HUB_VAULT_KEY` only. It encrypts the testers'
  per-job keys.

## 1. Push the code (repo already exists)

From the project folder:

```
git add -A
git commit -m "async job queue + full BYOK + Render deploy config"
git push
```

## 2. Create the service on Render

1. render.com -> New -> Web Service -> connect the repo. Render reads
   `render.yaml` automatically (Blueprint); accept it.
2. Set ONE environment variable (marked `sync: false`, never in the repo):
   - `HUB_VAULT_KEY` — generate one and paste it:
     ```
     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
     ```
   Do NOT set `RUNPOD_API_KEY`, `FISH_API_KEY`, or `GEMINI_API_KEY`; leaving them
   unset is what makes the app require each tester's own keys.
3. Deploy. You get a URL like `https://prompt2tube.onrender.com`. Share it.

## 3. What testers do

Open the link, upload a photo, type a topic (or paste a script), **paste their
own Gemini, Fish, and Runpod keys**, and click Generate. They watch the progress
steps and download the finished video. Their keys pay for their render (~$0.27
each) and are deleted when the render completes.

Where testers get keys: Gemini (free) at aistudio.google.com/apikey, Runpod at
runpod.io, Fish at fish.audio. A tester without these cannot render.

## Things to know (free tier)

- **It sleeps when idle.** First request after a nap takes ~30s to wake. Keep the
  browser tab open during a render; the progress poll keeps it awake.
- **The disk is ephemeral.** The job database and finished videos are wiped on a
  redeploy, so download promptly.
- **Publishing is not durable here.** YouTube/LinkedIn connections live in a local
  DB a redeploy wipes. The core demo (make and download a video) does not need it.

## Running locally instead

Two processes, from the project root, with a `.env` (copy `.env.example`) holding
just `HUB_VAULT_KEY`:

```
python app.py       # terminal 1: the web app
python worker.py    # terminal 2: the render worker
```

Then enter your Gemini, Fish, and Runpod keys in the form as a tester would.
