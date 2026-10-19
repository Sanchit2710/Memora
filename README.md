# spread

Back one photo library up across several Google accounts — each file uploaded **once**, each
account filled only to a reserve so mail keeps arriving, and cross-account duplicates removed
only after you look at them and decide.

```
python3 -m spread auth add   # authorise an account (repeat per account)
python3 -m spread status      # quota, reserve, placeable bytes per account
python3 -m spread backup DIR --dry-run
python3 -m spread backup DIR  # uploads, switching accounts at the reserve
python3 -m spread sweep       # index what is up there, find duplicates
python3 -m spread review      # decide each duplicate in the browser
```

There is no delete flag. Deletions happen only through `review`, one group at a time, and go
to that account's Drive trash (30-day recovery).

## Phase 0 — one-time Google setup (you must do this part)

1. <https://console.cloud.google.com> → create a project (free, no billing account needed).
2. **APIs & Services → Library** → enable **Google Drive API**. Enable **Photos Library API**
   too only if you want `--also-photos`.
3. **APIs & Services → OAuth consent screen** → External. Add yourself under *Test users*.
4. **Set publishing status to "In production".** Not optional: in *Testing* status Google
   issues refresh tokens that **die after 7 days**, which breaks unattended backup every week.
   With Drive-only scopes this needs no review. If you enabled the Photos scope you will see a
   one-time "Google hasn't verified this app" screen — *Advanced → go to … (unsafe)*. It is
   your own app and your own data.
5. **Credentials → Create credentials → OAuth client ID → Desktop app** → download the JSON.
6. Save it as `~/.spread/client_secret.json`.
7. `pip install -r requirements.txt`

Then, per account: `python3 -m spread auth add` (add `--photos` for Photos, `--profile sweep`
to also see files this app did not upload — see *Scopes* below).

## If you cloned this

It works without any credential from whoever wrote it, and it has to: this repo contains no
client id, no client secret, no token, no account name. A guard test (`test_repo_carries_no_
credentials`) fails the build if one ever lands there.

Everything per-user lives in `~/.spread/`, outside the checkout:

```
~/.spread/client_secret.json   your OAuth client, downloaded from your own GCP project
~/.spread/tokens/<email>.json  one refresh token per account, chmod 600
~/.spread/index.db             your content index
~/.spread/cache/               images fetched for the review page
```

So the one thing you cannot skip is **phase 0 below: make your own Google Cloud project.**
Five minutes, free, no billing account. It is not laziness that the project is not shipped
pre-configured:

- A shared OAuth client would put every user on *one* project's consent screen — which carries
  a 100-test-user cap and that project owner's verification status and quota.
- The scopes here are deliberately per-app (`drive.file`): an app only ever sees files **it**
  uploaded. Your own client means your photos are reachable by your client and nothing else.
- Sharing tokens would mean sharing access to the photos themselves.

`rclone` asks the same of you for the same reasons. If you skip it, `auth add` stops with
`missing ~/.spread/client_secret.json - see README phase 0` rather than failing obscurely.

## What it costs

Nothing. Both APIs are free, no billing account is attached, and exceeding the request quota
returns `403 rateLimitExceeded` rather than a bill. Rate limits are nowhere near reachable
here: a sweep of ~1,000 files is about 10 list calls.

## Scopes

- **backup** (default): `drive.file` + `drive.appdata`. Both non-sensitive — no verification,
  no warning screen. Sees only what this app uploaded, which is all it needs to upload, dedup
  and clean up its own work.
- **sweep** (`--profile sweep`): adds `drive.metadata.readonly` so duplicates already in your
  accounts from *other* tools are also found. Trashing those needs the restricted `drive`
  scope (a paid security assessment for production use), so `review` deep-links them for you
  to delete in Drive's own UI instead.
- `--photos` adds `photoslibrary.appendonly`.

## Known limits, stated plainly

- **Google Photos cannot be deduplicated.** Since 2025-03-31 an app may only see media it
  uploaded itself, and the Photos API has no delete endpoint at any scope. So `--also-photos`
  writes a *second* full-size copy against the same 15 GB and can never clean it up. Default
  is Drive only; use the flag on a curated subset if the Photos app's search is worth the
  bytes. Photos duplicates are reported with a link, never deleted.
- Matching is **byte-identical** (`md5` + size), which is exactly the re-backup case. A
  re-encoded or resized copy of the same shot is a different file and will not match.
- A run killed mid-upload abandons that one resumable session; Drive creates no file, so the
  next run re-uploads it. Correct, just not byte-efficient.
- Files go into one `spread-backup` folder per account, original relative path preserved in
  the file's `appProperties.relpath`.
- Spreading a library over several free accounts is not a supported Google configuration. The
  accounts stay independent, and losing one loses that shard. After every run the index is
  mirrored into each account's hidden `appDataFolder`, so the local index is not the single
  point of failure.

## Tests

`python3 test_spread.py` — 8 assert-based checks, no deps needed. They cover the two things
that actually matter: the reserve is never crossed and known content is never re-uploaded, and
nothing can delete a file except a decision posted from the review page.
