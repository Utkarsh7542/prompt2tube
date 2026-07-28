"""YouTube adapter: vault-backed port of v1's yt.py.

What changed from yt.py and why:
  - Credentials come from the vault (per-integration row), not from a
    token.json file next to the code. Multi-account becomes possible and
    secrets leave the repo tree (PRD section 7 cleanup).
  - Refresh is handled here: Google access tokens die hourly; the
    refresh_token gets a new one and the vault row is updated.
  - Same resumable upload call as v1 — that part was already right.
"""

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from .. import vault


def _service(integration_id: int):
    t = vault.read_tokens(integration_id)
    creds = Credentials(
        token=t["token"],
        refresh_token=t.get("refresh_token"),
        token_uri=t["token_uri"],
        client_id=t["client_id"],
        client_secret=t["client_secret"],
        scopes=t["scopes"],
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        t["token"] = creds.token
        vault.update_tokens(integration_id, t)
    return build("youtube", "v3", credentials=creds)


def publish(video_path: str, caption: dict, integration_id: int) -> dict:
    try:
        yt = _service(integration_id)
        privacy = caption.get("privacy", "unlisted")
        if privacy not in ("private", "unlisted", "public"):
            privacy = "unlisted"
        body = {
            "snippet": {
                "title": caption.get("title", "")[:100],
                "description": caption.get("text", ""),
                "tags": caption.get("tags", []),
                "categoryId": "22",
            },
            "status": {"privacyStatus": privacy,
                       "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            _, response = req.next_chunk()
        return {"ok": True, "url": "https://youtu.be/" + response["id"], "error": None}
    except Exception as e:  # one platform failing must never crash the others
        return {"ok": False, "url": None, "error": str(e)}
