"""Prints token.json as one line, ready to paste into the GOOGLE_TOKEN_JSON env var on Render.

Run locally AFTER you've uploaded to YouTube at least once (so token.json exists):
    python print_token.py
"""
import json

with open("token.json") as f:
    print(json.dumps(json.load(f), separators=(",", ":")))
