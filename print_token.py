"""DEPRECATED 2026-07-28. Nothing reads GOOGLE_TOKEN_JSON any more.

This flattened token.json into one line for Render's GOOGLE_TOKEN_JSON env var,
which fed v1's yt.py — a plaintext YouTube refresh token in an environment
variable. Uploads now go through the hub's encrypted vault instead, so this
script's output has nowhere to go. Kept only so the history of the old deploy
path is legible; delete once yt.py goes.

Original usage (no longer useful):
    python print_token.py
"""
import json

with open("token.json") as f:
    print(json.dumps(json.load(f), separators=(",", ":")))
