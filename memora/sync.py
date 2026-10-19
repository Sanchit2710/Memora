"""Placement planner and uploader.

The planner is the whole point of this tool: a file already held by any enabled account is
never uploaded again, and an account is filled only up to its reserve before the next one
takes over. It is pure logic over dicts so it can be tested without touching Google.
"""
from pathlib import Path

from . import accounts, store

FOLDER = "memora-backup"
RESUMABLE_MIN = 5 << 20
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".tif", ".tiff",
             ".dng", ".cr2", ".nef", ".arw", ".raf", ".orf", ".rw2",
             ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".mts", ".webm"}


def walk(root, media_only=True):
    for p in sorted(Path(root).rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        if media_only and p.suffix.lower() not in MEDIA_EXT:
            continue
        yield p


def free(acct, inflight=0):
    """Bytes still placeable in this account, honouring its reserve."""
    return acct["quota_limit"] - acct["reserve_bytes"] - acct["quota_used"] - inflight


def choose(accts, size, inflight=None):
    """Account with the most headroom that can still take `size` inside its reserve."""
    inflight = inflight or {}
    best = None
    for a in accts:
        room = free(a, inflight.get(a["email"], 0))
        if room >= size and (best is None or room > best[1]):
            best = (a, room)
    return best[0] if best else None


def plan(db, root, accts, backends=("drive",), media_only=True):
    """Yield (action, path, md5, size, target, detail).

    action is 'skip' (already backed up), 'place' (upload here) or 'blocked'
    (every account is at its reserve).
    """
    enabled = {a["email"] for a in accts}
    inflight = {}
    for p in walk(root, media_only):
        md5, size = store.hash_file(p)
        store.record_file(db, md5, size, p, p.stat().st_mtime)
        held = [c for c in store.copies_of(db, md5, size) if c["account"] in enabled]
        missing = set(backends) - {c["backend"] for c in held}
        if not missing:
            where = ", ".join(sorted({f"{c['account']}/{c['backend']}" for c in held}))
            yield ("skip", p, md5, size, None, where)
            continue
        target = choose(accts, size, inflight)
        if target is None:
            yield ("blocked", p, md5, size, None,
                   "; ".join(f"{a['email']} {free(a, inflight.get(a['email'], 0))}B free"
                             for a in accts))
            continue
        inflight[target["email"]] = inflight.get(target["email"], 0) + size
        yield ("place", p, md5, size, target["email"], ",".join(sorted(missing)))


# --- Drive upload ---

def folder_id(svc):
    q = (f"name='{FOLDER}' and mimeType='application/vnd.google-apps.folder' "
         "and trashed=false")
    hits = accounts.retry(lambda: svc.files().list(
        q=q, fields="files(id)", pageSize=1).execute())["files"]
    if hits:
        return hits[0]["id"]
    return accounts.retry(lambda: svc.files().create(
        body={"name": FOLDER, "mimeType": "application/vnd.google-apps.folder"},
        fields="id").execute())["id"]


def upload_drive(svc, parent, path, relpath, size, on_progress=None, expect_md5=None):
    """Upload one file. If Drive hashes the stored bytes differently from what we hashed
    locally, the upload is discarded and this raises: an unrecorded file left behind would
    be re-uploaded on every future run, duplicating forever. The only file this ever
    deletes is the one it created moments earlier in this same call - user data is only
    ever deleted through the review page.
    """
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(path), resumable=size >= RESUMABLE_MIN,
                            chunksize=8 << 20)
    req = svc.files().create(
        body={"name": path.name, "parents": [parent],
              "appProperties": {"relpath": str(relpath)}},
        media_body=media,
        fields="id,name,size,md5Checksum,createdTime,webViewLink")
    if not media.resumable():
        return _verify(svc, accounts.retry(lambda: req.execute()), expect_md5, path)
    # ponytail: resume is within-run only (num_retries per chunk). A run killed mid-upload
    # abandons the session, Drive creates no file, and the next run re-uploads it - correct,
    # just not byte-efficient. Persist resumable_uri across runs if that ever stings.
    resp = None
    while resp is None:
        status, resp = req.next_chunk(num_retries=5)
        if status and on_progress:
            on_progress(status.resumable_progress, size)
    return _verify(svc, resp, expect_md5, path)


def _verify(svc, f, expect_md5, path):
    got = f.get("md5Checksum")
    if expect_md5 and got and got != expect_md5:
        accounts.retry(lambda: svc.files().update(
            fileId=f["id"], body={"trashed": True}).execute())
        raise ValueError(f"{path.name}: Drive stored different bytes "
                         f"(expected md5 {expect_md5}, got {got}); upload discarded")
    return f


def push_photos(creds, path, mime="application/octet-stream"):
    """Upload bytes then create the media item. appendonly can create, never list or
    delete - so a Photos copy is recorded for reporting only."""
    from google.auth.transport.requests import AuthorizedSession
    s = AuthorizedSession(creds)
    with open(path, "rb") as f:
        r = s.post("https://photoslibrary.googleapis.com/v1/uploads",
                   data=f, headers={"Content-type": mime,
                                    "X-Goog-Upload-Protocol": "raw",
                                    "X-Goog-Upload-File-Name": path.name})
    r.raise_for_status()
    r2 = s.post("https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate",
                json={"newMediaItems": [{"simpleMediaItem": {
                    "uploadToken": r.text, "fileName": path.name}}]})
    r2.raise_for_status()
    res = r2.json()["newMediaItemResults"][0]
    if res.get("status", {}).get("message") not in (None, "Success", "OK"):
        raise RuntimeError(f"photos rejected {path.name}: {res['status']}")
    item = res["mediaItem"]
    return item["id"], item.get("productUrl")


def mirror_index(svc, db_path):
    """Keep a copy of the index in the account's hidden appDataFolder, so the index is
    not the single point of failure."""
    from googleapiclient.http import MediaFileUpload
    name = "memora-index.db"
    hits = accounts.retry(lambda: svc.files().list(
        spaces="appDataFolder", q=f"name='{name}'", fields="files(id)").execute())["files"]
    media = MediaFileUpload(str(db_path), resumable=False)
    if hits:
        return accounts.retry(lambda: svc.files().update(
            fileId=hits[0]["id"], media_body=media, fields="id").execute())["id"]
    return accounts.retry(lambda: svc.files().create(
        body={"name": name, "parents": ["appDataFolder"]},
        media_body=media, fields="id").execute())["id"]
