"""OAuth service: the "Connect account" flow, one function pair per provider.

THE MENTAL MODEL (read this before the code):
OAuth 2.0 authorization-code flow is a three-legged handshake:

  1. We redirect the user's BROWSER to the platform's login page,
     carrying our app's client_id and a redirect_uri pointing back at us.
  2. The user logs in ON THE PLATFORM'S PAGE (we never see the password)
     and approves the scopes. The platform redirects the browser back to
     our redirect_uri with a short-lived one-time ?code=...
  3. Our SERVER exchanges that code (plus our client_secret, which the
     browser never sees) for an access_token. That token — not a
     password — is what we store, encrypted, in the vault.

The `state` parameter is a random nonce we send out and check on return:
it proves the callback belongs to a flow WE started (CSRF protection).

Dev note: both Google and LinkedIn accept http://localhost redirect URIs
for development, so this runs on your laptop today; production needs a
public HTTPS URL (PRD section 9).
"""

import os
import secrets
import time

import requests

# In-flight state nonces: state -> platform. In-memory is fine for a
# single-process POC; a multi-worker deploy would move this to the DB.
_pending_states = {}

REDIRECT_BASE = os.environ.get("HUB_REDIRECT_BASE", "http://localhost:5000")


def new_state(platform: str) -> str:
    state = secrets.token_urlsafe(24)
    _pending_states[state] = platform
    return state


def check_state(state: str, platform: str) -> bool:
    return _pending_states.pop(state, None) == platform


# ---------------------------------------------------------------- LinkedIn
# Product: "Share on LinkedIn" (self-serve in the LinkedIn dev portal).
# Scopes: openid + profile let us fetch the member id and name for the
# vault's display_name; w_member_social is the posting permission.
# LinkedIn tokens live ~60 days; refresh tokens require an extra LinkedIn
# program, so v1 behavior on expiry is "ask the user to reconnect" —
# honest and simple beats silent and broken.

LINKEDIN_AUTH = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_TOKEN = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_SCOPES = "openid profile w_member_social"


def linkedin_auth_url() -> str:
    return (LINKEDIN_AUTH
            + "?response_type=code"
            + "&client_id=" + os.environ["LINKEDIN_CLIENT_ID"]
            + "&redirect_uri=" + REDIRECT_BASE + "/hub/callback/linkedin"
            + "&state=" + new_state("linkedin")
            + "&scope=" + LINKEDIN_SCOPES.replace(" ", "%20"))


def linkedin_exchange(code: str) -> dict:
    """Leg 3: code -> token, then fetch who this is so the UI can show a
    human name. Returns the token_dict the vault will encrypt."""
    r = requests.post(LINKEDIN_TOKEN, data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": os.environ["LINKEDIN_CLIENT_ID"],
        "client_secret": os.environ["LINKEDIN_CLIENT_SECRET"],
        "redirect_uri": REDIRECT_BASE + "/hub/callback/linkedin",
    }, timeout=30)
    r.raise_for_status()
    tok = r.json()
    # /v2/userinfo comes with the openid scope: gives us the member id
    # ("sub") that the Posts API needs as the author URN, plus the name.
    me = requests.get("https://api.linkedin.com/v2/userinfo",
                      headers={"Authorization": "Bearer " + tok["access_token"]},
                      timeout=30)
    me.raise_for_status()
    who = me.json()
    return {
        "access_token": tok["access_token"],
        "expires_at": time.time() + tok.get("expires_in", 0),
        "person_urn": "urn:li:person:" + who["sub"],
        "display_name": who.get("name", "LinkedIn account"),
    }


# ---------------------------------------------------------------- Google/YouTube
# We reuse the google-auth libraries already in requirements (yt.py used
# them too). Difference from v1: v1 ran a local throwaway server per
# login (InstalledAppFlow.run_local_server); the hub instead routes the
# callback through our own /hub/callback/youtube URL like every other
# provider — one consistent flow, and it works when the app is hosted.

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _google_flow():
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_secrets_file(
        "client_secret.json", scopes=YOUTUBE_SCOPES,
        redirect_uri=REDIRECT_BASE + "/hub/callback/youtube")


def youtube_auth_url() -> str:
    flow = _google_flow()
    # access_type=offline asks Google for a refresh_token so uploads keep
    # working after the 1h access token dies; prompt=consent forces Google
    # to actually send one (it omits it on repeat authorizations otherwise).
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    _pending_states[state] = "youtube"
    return url


def youtube_exchange(code: str) -> dict:
    flow = _google_flow()
    flow.fetch_token(code=code)
    c = flow.credentials
    return {
        "token": c.token,
        "refresh_token": c.refresh_token,
        "token_uri": c.token_uri,
        "client_id": c.client_id,
        "client_secret": c.client_secret,
        "scopes": list(c.scopes or YOUTUBE_SCOPES),
        "expires_at": c.expiry.timestamp() if c.expiry else 0,
        "display_name": "YouTube channel",
    }
