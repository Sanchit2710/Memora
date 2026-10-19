"""memora - back one photo library up across several Google accounts, once each.

Commands:
  memora auth add [--profile sweep] [--photos]   authorise an account
  memora status                                  quota + headroom per account
  memora backup DIR [--dry-run] [--also-photos]  place files, switching at the reserve
  memora sweep                                   index remote files, find duplicates
  memora review                                  localhost page to decide each duplicate

There is deliberately no delete flag here. Deletions happen only through `review`.
"""
import argparse
import sys
from pathlib import Path

from . import accounts, dedup, store, sync
from .review import human


def cmd_auth(args, db):
    if args.sub == "add":
        email, (limit, used), sc = accounts.add(args.profile, args.photos)
        store.upsert_account(db, email, limit=limit, used=used, scopes=",".join(sc),
                             has_photos=int(args.photos))
        print(f"added {email}  {human(used)} of {human(limit)} used")
    elif args.sub == "list":
        for a in store.accounts(db, enabled_only=False):
            print(f"{'on ' if a['enabled'] else 'off'} {a['email']}  "
                  f"photos={'yes' if a['has_photos'] else 'no'}  {a['scopes'] or ''}")
    elif args.sub in ("enable", "disable"):
        store.set_enabled(db, args.email, args.sub == "enable")
        print(f"{args.email} {args.sub}d")
    elif args.sub == "reserve":
        store.set_reserve(db, args.email, int(args.gb * store.GB))
        print(f"{args.email} reserve set to {args.gb} GB")


def cmd_status(args, db):
    accts = store.accounts(db, enabled_only=False)
    if not accts:
        sys.exit("no accounts yet - run: memora auth add")
    if not args.cached:
        accounts.refresh_quotas(db)
        accts = store.accounts(db, enabled_only=False)
    print(f"{'account':34} {'used':>10} {'limit':>10} {'reserve':>9} {'placeable':>11}  on")
    for a in accts:
        print(f"{a['email']:34} {human(a['quota_used']):>10} {human(a['quota_limit']):>10} "
              f"{human(a['reserve_bytes']):>9} {human(max(0, sync.free(a))):>11}  "
              f"{'yes' if a['enabled'] else 'no'}")
    on = [a for a in accts if a["enabled"]]
    print(f"\ntotal placeable across {len(on)} enabled account(s): "
          f"{human(sum(max(0, sync.free(a)) for a in on))}")


def cmd_backup(args, db):
    accts = store.accounts(db)
    if not accts:
        sys.exit("no enabled accounts - run: memora auth add")
    accounts.refresh_quotas(db)
    accts = store.accounts(db)
    backends = ("drive", "photos") if args.also_photos else ("drive",)
    if args.also_photos and not all(a["has_photos"] for a in accts):
        sys.exit("--also-photos needs every enabled account authorised with --photos")

    root = Path(args.dir).expanduser()
    svc, parent, creds = {}, {}, {}
    placed = skipped = blocked = 0
    bytes_up = bytes_skipped = 0

    for action, path, md5, size, email, detail in sync.plan(db, root, accts, backends,
                                                            not args.all_files):
        rel = path.relative_to(root)
        if action == "skip":
            skipped += 1
            bytes_skipped += size
            if args.verbose:
                print(f"skip   {rel}  (already in {detail})")
            continue
        if action == "blocked":
            blocked += 1
            print(f"BLOCKED {rel} ({human(size)}) - every account is at its reserve: {detail}")
            continue
        if args.dry_run:
            placed += 1
            bytes_up += size
            print(f"would upload {rel}  {human(size)}  -> {email} [{detail}]")
            continue

        if email not in svc:
            creds[email] = accounts.load(email)
            svc[email] = accounts._drive(creds[email])
            parent[email] = sync.folder_id(svc[email])
        if "drive" in detail:
            f = sync.upload_drive(svc[email], parent[email], path, rel, size)
            if f.get("md5Checksum") and f["md5Checksum"] != md5:
                print(f"WARNING {rel}: Drive reports a different md5, not recording")
            else:
                store.record_copy(db, md5, size, email, "drive", f["id"], f.get("name"),
                                  f.get("webViewLink"), f.get("createdTime"))
        if "photos" in detail:
            pid, purl = sync.push_photos(creds[email], path)
            store.record_copy(db, md5, size, email, "photos", pid, path.name, purl)
        placed += 1
        bytes_up += size
        print(f"up     {rel}  {human(size)}  -> {email}")

    print(f"\n{placed} placed ({human(bytes_up)}), {skipped} already backed up "
          f"({human(bytes_skipped)} not re-uploaded), {blocked} blocked")
    if not args.dry_run and placed:
        accounts.refresh_quotas(db)
        for email in svc:
            sync.mirror_index(svc[email], store.HOME / "index.db")


def cmd_sweep(args, db):
    accts = store.accounts(db)
    for a in accts:
        n = dedup.sweep_account(db, a["email"], not args.all_files)
        print(f"{a['email']}: indexed {n} file(s)")
    gs = dedup.groups(db)
    waste = sum(g["size"] * (len(g["copies"]) - 1) for g in gs)
    print(f"\n{len(gs)} duplicate group(s), {human(waste)} reclaimable")
    for g in gs[:args.show]:
        print(f"  {human(g['size'])}  " +
              " | ".join(f"{c['account']}/{c['name']}" for c in g["copies"]))
    if gs:
        print("\nnothing deleted. decide each group: memora review")


def cmd_review(args, db):
    from .review import serve
    serve(db, args.port, not args.no_open)


def main(argv=None):
    p = argparse.ArgumentParser(prog="memora", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)

    a = sp.add_parser("auth")
    asub = a.add_subparsers(dest="sub", required=True)
    add = asub.add_parser("add")
    add.add_argument("--profile", choices=list(accounts.PROFILES), default="backup")
    add.add_argument("--photos", action="store_true",
                     help="also request Photos append access (doubles quota burn)")
    asub.add_parser("list")
    for name in ("enable", "disable"):
        s = asub.add_parser(name)
        s.add_argument("email")
    r = asub.add_parser("reserve")
    r.add_argument("email")
    r.add_argument("gb", type=float, help="headroom to leave for mail, e.g. 2")

    s = sp.add_parser("status")
    s.add_argument("--cached", action="store_true", help="skip the quota API call")

    b = sp.add_parser("backup")
    b.add_argument("dir")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--also-photos", action="store_true",
                   help="second copy into Google Photos: undedupable, undeletable, 2x quota")
    b.add_argument("--all-files", action="store_true", help="not just photos and videos")
    b.add_argument("-v", "--verbose", action="store_true")

    w = sp.add_parser("sweep")
    w.add_argument("--all-files", action="store_true")
    w.add_argument("--show", type=int, default=20)

    v = sp.add_parser("review")
    v.add_argument("--port", type=int, default=8731)
    v.add_argument("--no-open", action="store_true")

    args = p.parse_args(argv)
    db = store.connect()
    {"auth": cmd_auth, "status": cmd_status, "backup": cmd_backup,
     "sweep": cmd_sweep, "review": cmd_review}[args.cmd](args, db)


if __name__ == "__main__":
    main()
