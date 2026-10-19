"""Cross-account duplicate detection.

This module only ever *proposes*. Trashing happens solely when review.py posts back an
explicit per-group choice made by a human.
"""
from . import accounts, store

LIST_FIELDS = ("nextPageToken,files(id,name,size,md5Checksum,createdTime,"
               "webViewLink,mimeType,trashed)")


def sweep_account(db, email, media_only=True):
    """Fold every visible Drive file into the index. With the backup profile that is the
    files this app uploaded; with the sweep profile it is everything in the account."""
    svc = accounts.drive(email)
    seen, token = 0, None
    while True:
        res = accounts.retry(lambda: svc.files().list(
            q="trashed=false", fields=LIST_FIELDS, pageSize=1000,
            pageToken=token, spaces="drive").execute())
        for f in res.get("files", []):
            if not f.get("md5Checksum") or not f.get("size"):
                continue                      # folders, Google-native docs: no content hash
            if media_only and not f.get("mimeType", "").startswith(("image/", "video/")):
                continue
            store.record_copy(db, f["md5Checksum"], int(f["size"]), email, "drive",
                              f["id"], f.get("name"), f.get("webViewLink"),
                              f.get("createdTime"))
            seen += 1
        token = res.get("nextPageToken")
        if not token:
            return seen


def groups(db, include_decided=False):
    """Duplicate groups: same content, more than one copy. Groups the human already
    settled are filtered out, which is what stops the queue re-asking forever."""
    decided = store.verdicts(db)
    rows = db.execute(
        "SELECT md5, size FROM copies WHERE backend='drive' "
        "GROUP BY md5, size HAVING COUNT(DISTINCT account || remote_id) > 1")
    free = {a["email"]: a["quota_limit"] - a["quota_used"]
            for a in store.accounts(db, enabled_only=False)}
    out = []
    for md5, size in [(r["md5"], r["size"]) for r in rows]:
        copies = store.copies_of(db, md5, size, backend="drive")
        if len(copies) < 2:
            continue
        key = store.group_key(c["remote_id"] for c in copies)
        if not include_decided and key in decided:
            continue
        # suggest keeping the oldest copy; tie-break toward the fuller account, so
        # cleaning up also rebalances quota toward the emptier one
        copies.sort(key=lambda c: (c["created_time"] or "", free.get(c["account"], 0)))
        out.append({"key": key, "md5": md5, "size": size,
                    "suggest_keep": copies[0]["remote_id"], "copies": copies,
                    "verdict": decided.get(key),
                    "photos": store.copies_of(db, md5, size, backend="photos")})
    out.sort(key=lambda g: -g["size"])
    return out


def trash(email, remote_id):
    """Trash, never purge: recoverable from the account's own Drive trash for 30 days."""
    svc = accounts.drive(email)
    return accounts.retry(lambda: svc.files().update(
        fileId=remote_id, body={"trashed": True}, fields="id,trashed").execute())


def apply_decision(db, group, action, keep_id=None):
    """action: 'keep_one' (trash the rest) | 'keep_all' | 'not_duplicates' | 'skip'."""
    if action == "skip":
        return []
    if action in ("keep_all", "not_duplicates"):
        store.set_verdict(db, group["key"], action)
        return []
    if action != "keep_one":
        raise ValueError(f"unknown action {action}")
    if not any(c["remote_id"] == keep_id for c in group["copies"]):
        raise ValueError("keep_id is not in this group")
    trashed = []
    for c in group["copies"]:
        if c["remote_id"] == keep_id:
            continue
        trash(c["account"], c["remote_id"])
        store.forget_copy(db, c["remote_id"])
        trashed.append(c)
    store.set_verdict(db, group["key"], "resolved")
    return trashed
