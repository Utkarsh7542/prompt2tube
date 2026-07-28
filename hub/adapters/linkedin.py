"""LinkedIn adapter: video post to a personal profile via official APIs.

LinkedIn's upload is a three-step dance (this is normal for video APIs —
Instagram's works the same way, which is why the media step is its own
function you'll recognize again in Phase 1b):

  1. REGISTER: tell LinkedIn "I'm about to upload a video for member X"
     -> LinkedIn returns a one-time upload URL + an asset URN (the
     video's permanent id in their system).
  2. UPLOAD: PUT the raw bytes to that URL.
  3. POST: create the feed post (ugcPost) referencing the asset URN.

Uses the v2 assets + ugcPosts API, which the self-serve w_member_social
scope covers. LinkedIn is migrating to a newer "Posts API"; when we add
company pages (deferred, PRD non-goal) we revisit — noted, not built.
"""

import requests

from .. import vault

API = "https://api.linkedin.com/v2"


def _register_upload(token: str, person_urn: str) -> tuple:
    r = requests.post(
        API + "/assets?action=registerUpload",
        headers={"Authorization": "Bearer " + token},
        json={"registerUploadRequest": {
            "recipes": ["urn:li:digitalmediaRecipe:feedshare-video"],
            "owner": person_urn,
            "serviceRelationships": [{
                "relationshipType": "OWNER",
                "identifier": "urn:li:userGeneratedContent",
            }],
        }},
        timeout=30)
    r.raise_for_status()
    v = r.json()["value"]
    upload_url = (v["uploadMechanism"]
                   ["com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]
                   ["uploadUrl"])
    return upload_url, v["asset"]


def publish(video_path: str, caption: dict, integration_id: int) -> dict:
    try:
        t = vault.read_tokens(integration_id)
        token, person = t["access_token"], t["person_urn"]

        upload_url, asset_urn = _register_upload(token, person)

        with open(video_path, "rb") as f:  # step 2: raw bytes
            up = requests.put(upload_url, data=f,
                              headers={"Authorization": "Bearer " + token},
                              timeout=600)
        up.raise_for_status()

        # Step 3: the visible post. Title goes into the text body —
        # LinkedIn posts have no separate title field like YouTube.
        text = (caption.get("title", "") + "\n\n" + caption.get("text", "")).strip()
        post = requests.post(
            API + "/ugcPosts",
            headers={"Authorization": "Bearer " + token,
                     "X-Restli-Protocol-Version": "2.0.0"},
            json={
                "author": person,
                "lifecycleState": "PUBLISHED",
                "specificContent": {"com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text[:2900]},
                    "shareMediaCategory": "VIDEO",
                    "media": [{"status": "READY", "media": asset_urn}],
                }},
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
            },
            timeout=60)
        post.raise_for_status()
        post_id = post.headers.get("x-restli-id", "")
        url = "https://www.linkedin.com/feed/update/" + post_id if post_id else None
        return {"ok": True, "url": url, "error": None}
    except requests.HTTPError as e:
        # Readable errors, v1 house rule: surface what the platform said.
        detail = e.response.text[:300] if e.response is not None else str(e)
        return {"ok": False, "url": None, "error": "LinkedIn API: " + detail}
    except Exception as e:
        return {"ok": False, "url": None, "error": str(e)}
