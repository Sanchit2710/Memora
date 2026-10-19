"""Assert-based checks. No framework, no deps: run `python3 test_memora.py`.

Covers the logic that, if it broke, would either re-upload everything (the bug this tool
exists to fix) or delete a photo nobody agreed to delete.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memora import dedup, store, sync  # noqa: E402

GB = store.GB


def acct(email, used_gb, limit_gb=15, reserve_gb=2):
    return {"email": email, "quota_used": int(used_gb * GB), "enabled": 1,
            "quota_limit": int(limit_gb * GB), "reserve_bytes": int(reserve_gb * GB)}


def test_reserve_is_never_crossed():
    a = acct("a@x", used_gb=12.5)                 # 2.5 GB raw free, 0.5 GB inside reserve
    assert sync.free(a) == int(0.5 * GB)
    assert sync.choose([a], int(0.4 * GB)) is not None, "fits inside reserve"
    assert sync.choose([a], int(1 * GB)) is None, "1 GB fits in raw quota but eats reserve"
    assert sync.choose([a], int(2.4 * GB)) is None


def test_switches_account_at_reserve():
    full, empty = acct("full@x", 13), acct("empty@x", 0)
    assert sync.choose([full, empty], int(1 * GB))["email"] == "empty@x"
    # most-headroom wins, and in-flight bytes of the current run count
    big, small = acct("big@x", 1), acct("small@x", 9)
    assert sync.choose([big, small], 1)["email"] == "big@x"
    inflight = {"big@x": int(11.9 * GB)}
    assert sync.choose([big, small], int(1 * GB), inflight)["email"] == "small@x"


def test_known_content_is_skipped_not_reuploaded():
    db = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "a.jpg").write_bytes(b"photo-one")
        (root / "b.jpg").write_bytes(b"photo-two")
        accts = [acct("a@x", 0)]
        first = list(sync.plan(db, root, accts))
        assert [p[0] for p in first] == ["place", "place"], first
        # pretend the run completed
        for action, path, md5, size, email, _ in first:
            store.record_copy(db, md5, size, email, "drive", "id-" + md5)
        second = list(sync.plan(db, root, accts))
        assert [p[0] for p in second] == ["skip", "skip"], second
        # the same bytes under a new name, in a new account's turn: still skipped
        (root / "copy-of-a.jpg").write_bytes(b"photo-one")
        third = list(sync.plan(db, root, [acct("b@x", 0)] + accts))
        assert sorted(p[0] for p in third) == ["skip", "skip", "skip"], third


def test_blocked_when_every_account_is_at_reserve():
    db = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "a.jpg").write_bytes(b"x" * 1024)
        out = list(sync.plan(db, root, [acct("a@x", 13), acct("b@x", 14)]))
        assert out[0][0] == "blocked", out
        assert "a@x" in out[0][5] and "free" in out[0][5]


def test_photos_backend_is_tracked_separately():
    db = store.connect(":memory:")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "a.jpg").write_bytes(b"photo-one")
        accts = [acct("a@x", 0)]
        _, path, md5, size, email, _ = list(sync.plan(db, root, accts))[0]
        store.record_copy(db, md5, size, email, "drive", "d1")
        # drive-only run skips it; a run that also wants Photos still has work to do
        assert list(sync.plan(db, root, accts))[0][0] == "skip"
        out = list(sync.plan(db, root, accts, backends=("drive", "photos")))
        assert out[0][0] == "place" and out[0][5] == "photos", out


def test_dedup_groups_need_two_copies_and_matching_size():
    db = store.connect(":memory:")
    store.upsert_account(db, "a@x", 15 * GB, GB)
    store.upsert_account(db, "b@x", 15 * GB, 2 * GB)
    store.record_copy(db, "m1", 100, "a@x", "drive", "id1", "p.jpg", None, "2020")
    store.record_copy(db, "m1", 100, "b@x", "drive", "id2", "p.jpg", None, "2021")
    store.record_copy(db, "lone", 100, "a@x", "drive", "id3")
    store.record_copy(db, "m2", 100, "a@x", "drive", "id4")
    store.record_copy(db, "m2", 999, "b@x", "drive", "id5")   # same md5, different size
    gs = dedup.groups(db)
    assert len(gs) == 1, [g["md5"] for g in gs]
    assert gs[0]["suggest_keep"] == "id1", "oldest copy is the suggested keeper"


def test_decisions_stick_and_only_keep_one_deletes():
    db = store.connect(":memory:")
    store.upsert_account(db, "a@x", 15 * GB, GB)
    store.record_copy(db, "m1", 100, "a@x", "drive", "id1", created_time="2020")
    store.record_copy(db, "m1", 100, "b@x", "drive", "id2", created_time="2021")
    g = dedup.groups(db)[0]

    for passive in ("skip", "keep_all", "not_duplicates"):
        db2 = store.connect(":memory:")
        store.record_copy(db2, "m1", 100, "a@x", "drive", "id1")
        store.record_copy(db2, "m1", 100, "b@x", "drive", "id2")
        gg = dedup.groups(db2)[0]
        assert dedup.apply_decision(db2, gg, passive) == [], passive
        assert len(store.copies_of(db2, "m1", 100)) == 2, f"{passive} deleted something"
        # skip must stay in the queue; the other two must not come back
        assert len(dedup.groups(db2)) == (1 if passive == "skip" else 0), passive

    try:
        dedup.apply_decision(db, g, "keep_one", keep_id="not-in-group")
        raise AssertionError("accepted a keeper from outside the group")
    except ValueError:
        pass


def test_review_survives_hostile_filenames():
    """Photo names routinely contain apostrophes. They used to be interpolated straight
    into onclick='...' and broke every handler on the page, so no file data may appear in
    an HTML attribute: handlers carry indices only, data rides in one JSON blob."""
    import json
    import re

    from memora import dedup, review
    db = store.connect(":memory:")
    nasty = "beach day's `x` \"q\" </script>.jpg"
    store.record_copy(db, "md5a", 4182331, "a@x", "drive", "id1", nasty, "http://d/1", "2020")
    store.record_copy(db, "md5a", 4182331, "b@x", "drive", "id2", "b.jpg", None, None)
    page = review.render(dedup.groups(db))

    ok = re.compile(r"^(flip|keepOne)\(\d+,\d+\)$"
                    r"|^decide\(\d+,(?:'|&#x27;)(keep_all|not_duplicates|skip)(?:'|&#x27;)\)$"
                    r"|^this\.replaceWith")
    handlers = re.findall(r'on(?:click|error)="([^"]*)"', page)
    assert handlers, "no handlers rendered"
    for h in handlers:
        assert ok.match(h), f"user data leaked into an attribute: {h[:80]}"

    blob = re.search(r"const G = (\[.*?\]);", page, re.S).group(1)
    data = json.loads(blob.replace("<\\/", "</"))
    names = {c["id"]: c["name"] for c in data[0]["copies"]}
    assert names["id1"] == nasty, f"filename mangled in the JSON blob: {names}"
    assert "</script>" not in blob, "unescaped </script> would end the script early"
    assert "date unknown" in page, "a copy with no createdTime must still render"


def test_no_delete_path_outside_review():
    """The CLI must have no way to trash a file; only a review POST may."""
    src = Path(__file__).parent / "memora"
    assert src.is_dir(), f"{src} missing - this check would pass vacuously"
    hits = subprocess.run(["grep", "-rn", r"trashed.*True\|files()\.delete",
                           str(src)], capture_output=True, text=True).stdout
    files = {line.split(":")[0].split("/")[-1] for line in hits.splitlines() if line}
    assert files <= {"dedup.py"}, f"delete call outside dedup.py: {hits}"
    main = (src / "__main__.py").read_text()
    assert "trash" not in main and "apply_decision" not in main, \
        "__main__ can delete without the review gate"


def test_repo_carries_no_credentials():
    """Anyone cloning this must supply their own Google client. Nothing in the tree may
    contain a client id, client secret, refresh token or account name."""
    import re
    root = Path(__file__).parent
    bad = re.compile(r"apps\.googleusercontent\.com|\"client_secret\"|refresh_token\"\s*:"
                     r"|BEGIN [A-Z ]*PRIVATE KEY|AIza[0-9A-Za-z_-]{30}")
    tracked = [f for f in root.rglob("*")
               if f.is_file() and "__pycache__" not in f.parts and ".git" not in f.parts]
    assert tracked, "no files found"
    for f in tracked:
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        hit = bad.search(text)
        assert not hit, f"credential-shaped string in {f.name}: {hit.group()[:40]}"
    # and the paths the code reads secrets from must sit outside the repo
    from memora import accounts
    assert root not in accounts.CLIENT_SECRET.parents, "client secret path is inside the repo"
    assert root not in accounts.TOKENS.parents, "token store is inside the repo"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} checks passed")
