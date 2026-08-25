"""Load .env into os.environ, for any process that starts without run.bat.

WHY THIS EXISTS
---------------
run.bat parses .env and exports the keys before launching `python app.py`. The
worker (worker.py) is a SEPARATE process, usually started with a bare
`python worker.py`, so it never went through run.bat and inherited none of those
keys -- which is why script generation failed with "GEMINI_API_KEY is not set"
even though .env was filled in correctly.

Rather than make people remember to export the environment twice, both the web
app and the worker call load_env() on startup. It is a ~15-line reader, so it
adds no dependency (python-dotenv would work too, but the project has kept its
dependency list short on purpose).

RULES
-----
- Never override a variable that is ALREADY set. So run.bat's values, and real
  system/production environment variables, always win over the .env file. The
  file is only a fallback for keys the process does not already have.
- Skip blank lines and comments (# ...). Strip one layer of surrounding quotes,
  matching how a value pasted as KEY="value" is meant to be read.
"""

import os


def load_env(path=".env"):
    """Populate os.environ from `path` for keys not already present. Silent and
    harmless if the file is missing -- a deployment that sets real env vars needs
    no .env at all."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            # `export KEY=val` shows up in some .env files; tolerate it.
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if not key or key in os.environ:
                continue  # already set -> real env wins over the file
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ[key] = value
