"""SQLite index: what content exists, where each copy lives, what the human decided."""
import hashlib
import os
import sqlite3
import time
from pathlib import Path

GB = 1 << 30
DEFAULT_RESERVE = 2 * GB          # stop filling a 15 GB account at 13 GB
HOME = Path(os.environ.get("SPREAD_HOME", Path.home() / ".spread"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  email TEXT PRIMARY KEY, enabled INT DEFAULT 1,
  quota_limit INT, quota_used INT, checked_at INT,
  reserve_bytes INT DEFAULT %d,
  scopes TEXT, has_photos INT DEFAULT 0);

CREATE TABLE IF NOT EXISTS files(
  md5 TEXT, size INT, local_path TEXT, mtime INT, first_seen INT,
  PRIMARY KEY(md5, size));

CREATE TABLE IF NOT EXISTS copies(
  md5 TEXT, size INT, account TEXT, backend TEXT,
  remote_id TEXT, name TEXT, product_url TEXT, created_time TEXT, uploaded_at INT,
  PRIMARY KEY(md5, size, account, backend, remote_id));

CREATE TABLE IF NOT EXISTS verdicts(
  group_key TEXT PRIMARY KEY, verdict TEXT, decided_at INT);
""" % DEFAULT_RESERVE


def connect(path=None):
    if path is None:
        HOME.mkdir(parents=True, exist_ok=True)
        path = HOME / "index.db"
    # check_same_thread=False so the review server can serve on a thread other than the
    # one that opened the index. HTTPServer handles requests serially, so there is no
    # concurrent access to serialise.
    # ponytail: if this ever becomes ThreadingHTTPServer, add a lock around writes.
    db = sqlite3.connect(str(path), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def hash_file(path, chunk=1 << 20):
    """md5 + size. md5 because Drive hands it back on every file for free, so the same
    key compares local files against remote ones with no download.
    # ponytail: md5+size key, matches Drive's md5Checksum; swap to sha256 only if
    # collision resistance ever matters here (it does not for personal photos)."""
    h = hashlib.md5()
    size = 0
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


# --- accounts ---

def upsert_account(db, email, limit=None, used=None, scopes=None, has_photos=None):
    db.execute("INSERT OR IGNORE INTO accounts(email) VALUES(?)", (email,))
    sets, vals = [], []
    for col, val in (("quota_limit", limit), ("quota_used", used),
                     ("scopes", scopes), ("has_photos", has_photos)):
        if val is not None:
            sets.append(f"{col}=?")
            vals.append(val)
    if limit is not None or used is not None:
        sets.append("checked_at=?")
        vals.append(int(time.time()))
    if sets:
        db.execute(f"UPDATE accounts SET {','.join(sets)} WHERE email=?", vals + [email])
    db.commit()


def accounts(db, enabled_only=True):
    q = "SELECT * FROM accounts" + (" WHERE enabled=1" if enabled_only else "")
    return [dict(r) for r in db.execute(q + " ORDER BY email")]


def set_enabled(db, email, on):
    db.execute("UPDATE accounts SET enabled=? WHERE email=?", (1 if on else 0, email))
    db.commit()


def set_reserve(db, email, reserve_bytes):
    db.execute("UPDATE accounts SET reserve_bytes=? WHERE email=?", (reserve_bytes, email))
    db.commit()


# --- content + copies ---

def record_file(db, md5, size, local_path, mtime):
    db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,COALESCE("
               "(SELECT first_seen FROM files WHERE md5=? AND size=?), ?))",
               (md5, size, str(local_path), int(mtime), md5, size, int(time.time())))
    db.commit()


def copies_of(db, md5, size, backend=None):
    q = "SELECT * FROM copies WHERE md5=? AND size=?"
    args = [md5, size]
    if backend:
        q += " AND backend=?"
        args.append(backend)
    return [dict(r) for r in db.execute(q, args)]


def record_copy(db, md5, size, account, backend, remote_id,
                name=None, product_url=None, created_time=None):
    db.execute("INSERT OR REPLACE INTO copies VALUES(?,?,?,?,?,?,?,?,?)",
               (md5, size, account, backend, remote_id, name, product_url,
                created_time, int(time.time())))
    db.commit()


def forget_copy(db, remote_id):
    db.execute("DELETE FROM copies WHERE remote_id=?", (remote_id,))
    db.commit()


# --- human decisions ---

def group_key(remote_ids):
    return ",".join(sorted(remote_ids))


def set_verdict(db, key, verdict):
    db.execute("INSERT OR REPLACE INTO verdicts VALUES(?,?,?)",
               (key, verdict, int(time.time())))
    db.commit()


def verdicts(db):
    return {r["group_key"]: r["verdict"] for r in db.execute("SELECT * FROM verdicts")}
