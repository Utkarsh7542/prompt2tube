import json
import os
import re
import requests

INSTRUCTION = """Write a short spoken monologue for a talking-head video about this topic:

{topic}

Rules:
- 40 to 70 words, natural spoken English, no stage directions, no emojis
- first person, like someone talking straight into the camera

Reply with only a JSON object in this exact shape:
{{"script": "...", "title": "...", "description": "...", "tags": ["...", "..."]}}
The title must be under 90 characters. Give 3 to 6 tags."""


def make_script(topic):
    text = INSTRUCTION.format(topic=topic)
    return parse(ask_gemini(text), topic)


def ask_gemini(text):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    name = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
    url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent".format(name)
    last = None
    for attempt in range(2):
        try:
            r = requests.post(
                url,
                headers={"x-goog-api-key": key},
                json={"contents": [{"parts": [{"text": text}]}]},
                timeout=150,
            )
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except requests.exceptions.Timeout as e:
            last = e
    raise RuntimeError("Gemini timed out twice, try again in a moment") from last


def parse(raw, topic):
    match = re.search(r"\{.*\}", raw, re.S)
    if match:
        try:
            data = json.loads(match.group())
            script = str(data.get("script", "")).strip()
            if script:
                return {
                    "script": script,
                    "title": str(data.get("title", topic))[:95],
                    "description": str(data.get("description", "")),
                    "tags": [str(t) for t in data.get("tags", [])][:8],
                }
        except ValueError:
            pass
    cleaned = raw.strip().strip("`")
    if not cleaned:
        raise RuntimeError("the model returned an empty response")
    return {"script": cleaned, "title": topic[:95], "description": "", "tags": []}
