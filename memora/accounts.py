"""Per-account OAuth, token storage, and the one quota call that matters.

Google client libraries are imported inside functions so the planner and its tests run
with nothing installed.
"""
import json
import random
import time
from pathlib import Path

from . import store

CLIENT_SECRET = store.HOME / "client_secret.json"
TOKENS = store.HOME / "tokens"

DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
DRIVE_APPDATA = "https://www.googleapis.com/auth/drive.appdata"
DRIVE_METADATA_RO = "https://www.googleapis.com/auth/drive.metadata.readonly"
PHOTOS_APPEND = "https://www.googleapis.com/auth/photoslibrary.appendonly"

# backup profile: all non-sensitive -> no Google verification, no 7-day token expiry.
# sweep profile: adds read-only metadata so files this app did NOT upload are also visible.
PROFILES = {
    "backup": [DRIVE_FILE, DRIVE_APPDATA],
    "sweep": [DRIVE_FILE, DRIVE_APPDATA, DRIVE_METADATA_RO],
}


def scopes(profile="backup", photos=False):
    s = list(PROFILES[profile])
    if photos:
        s.append(PHOTOS_APPEND)
    return s


def token_path(email):
    return TOKENS / f"{email}.json"


def add(profile="backup", photos=False, port=0):
    """Run the consent flow, then name the account from Drive itself (about.get returns
    the email, so no extra identity scope is needed)."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not CLIENT_SECRET.exists():
        raise SystemExit(f"missing {CLIENT_SECRET} - see README phase 0")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), scopes(profile, photos))
    creds = flow.run_local_server(port=port, prompt="consent")
    about = _drive(creds).about().get(
        fields="user(emailAddress),storageQuota").execute()
    email = about["user"]["emailAddress"]
    TOKENS.mkdir(parents=True, exist_ok=True)
    token_path(email).write_text(creds.to_json())
    token_path(email).chmod(0o600)
    return email, _quota(about), scopes(profile, photos)


def load(email):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    p = token_path(email)
    if not p.exists():
        raise SystemExit(f"no token for {email} - run: memora auth add")
    creds = Credentials.from_authorized_user_file(str(p))
    if not creds.valid:
        creds.refresh(Request())
        p.write_text(creds.to_json())
    return creds


def _drive(creds):
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def drive(email):
    return _drive(load(email))


def _quota(about):
    q = about["storageQuota"]
    # "limit" is absent on unlimited (Workspace) accounts; treat as effectively infinite.
    limit = int(q["limit"]) if "limit" in q else 1 << 60
    return limit, int(q["usage"])


def quota(email):
    about = drive(email).about().get(fields="user(emailAddress),storageQuota").execute()
    return _quota(about)


def refresh_quotas(db, emails=None):
    out = {}
    for a in store.accounts(db):
        if emails and a["email"] not in emails:
            continue
        limit, used = quota(a["email"])
        store.upsert_account(db, a["email"], limit=limit, used=used)
        out[a["email"]] = (limit, used)
    return out


def retry(fn, tries=5):
    """Backoff on rate limits. Quota overruns return 403/429 and are never billed, so
    waiting is always the right response."""
    from googleapiclient.errors import HttpError
    for i in range(tries):
        try:
            return fn()
        except HttpError as e:
            transient = e.resp.status in (429, 500, 502, 503) or (
                e.resp.status == 403 and b"ateLimit" in (e.content or b""))
            if not transient or i == tries - 1:
                raise
            time.sleep(2 ** i + random.random())
