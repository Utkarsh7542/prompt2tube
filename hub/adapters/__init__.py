"""Adapter registry: one platform = one module behind one contract.

The contract (mirrors make_video() on the render side):

    publish(video_path, caption, integration_id) -> dict
        caption: {"title": str, "text": str, "tags": [str], "privacy": str}
        returns: {"ok": bool, "url": str | None, "error": str | None}

Adding Facebook/Instagram later (PRD Phase 1b) = one new module here
plus one line in this dict. Nothing else in the app changes.
"""

from . import youtube, linkedin

ADAPTERS = {
    "youtube": youtube.publish,
    "linkedin": linkedin.publish,
}
