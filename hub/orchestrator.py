"""Publish orchestrator: fan a video out to N platforms, independently.

House rule: REPORT EVERY OUTCOME, NEVER SUBSTITUTE SILENTLY. Each platform
runs in its own thread with its own try/except (inside the adapter), reports
its own status, and one failure never blocks the others. Note the difference
from the render side: publishing to three platforms and having one fail is
partial success worth reporting per-platform, whereas a renderer quietly
swapping in a different engine would misreport what was produced. Independent
failure domains, yes; substitution, no.

Status lives in an in-memory dict keyed by a publish id; the UI polls
/hub/status/<id>. In-memory is a deliberate POC choice: statuses are
worth minutes, tokens are worth protecting — the vault got a database,
this got a dict. (If we later need publish history, this grows a table.)
"""

import threading
import uuid

from .adapters import ADAPTERS

_publishes = {}  # publish_id -> {platform: {"state": ..., "url": ..., "error": ...}}


def start_publish(video_path: str, captions: dict, targets: list) -> str:
    """targets: [{"integration_id": int, "platform": str}, ...]
    captions: {platform: caption_dict} — per-platform text, PRD 4.7.
    Returns a publish_id to poll."""
    publish_id = uuid.uuid4().hex[:10]
    status = {}
    _publishes[publish_id] = status

    for t in targets:
        platform = t["platform"]
        status[platform] = {"state": "uploading", "url": None, "error": None}
        thread = threading.Thread(
            target=_run_one,
            args=(status, platform, video_path,
                  captions.get(platform, captions.get("default", {})),
                  t["integration_id"]),
            daemon=True)
        thread.start()
    return publish_id


def _run_one(status, platform, video_path, caption, integration_id):
    result = ADAPTERS[platform](video_path, caption, integration_id)
    entry = status[platform]
    entry["state"] = "done" if result["ok"] else "failed"
    entry["url"] = result["url"]
    entry["error"] = result["error"]


def get_status(publish_id: str) -> dict:
    return _publishes.get(publish_id, {})
