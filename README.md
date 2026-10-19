# Memora

**Back one photo library up across several Google accounts — each photo uploaded exactly once.**

Every free Google account gets 15 GB. If you have several, you have plenty of space in total,
but using it is miserable: sign into account B to continue a backup, and the backup tool
re-uploads everything that is already sitting in account A, because it has no idea account A
exists. You burn the same gigabytes twice, accounts fill to 15 GB, and Gmail starts bouncing
mail because there is no room left.

Memora fixes that with three things:

1. **One index of what is already backed up, keyed by file content.** A file already held by
   any connected account is never uploaded again — not even under a different name, a
   different folder, or a different account.
2. **A quota reserve.** Each account is filled only to a limit you set (default: stop 2 GB
   short, so a 15 GB account fills to 13 GB), leaving headroom for incoming mail. When an
   account hits its reserve, the next one takes over automatically.
3. **A duplicate reviewer.** For photos already duplicated across accounts, Memora finds them
   and shows you both copies full size. **You** decide which to keep. It never deletes on its
   own, and what it does delete goes to that account's Drive trash, recoverable for 30 days.

---

## Table of contents

- [How it works](#how-it-works)
- [What you need](#what-you-need)
- [Setup, step by step](#setup-step-by-step)
- [Using it](#using-it)
- [Command reference](#command-reference)
- [Troubleshooting](#troubleshooting)
- [Known limits](#known-limits)
- [Safety](#safety-what-can-and-cannot-delete-your-photos)
- [Running the tests](#running-the-tests)
- [Roadmap](#roadmap)

---

## How it works

### Content addressing is the whole trick

Memora identifies a photo by the **md5 hash of its bytes plus its size**, never by its filename
or path. Before uploading anything it hashes the file and asks its local index: *does any
connected account already hold this content?* If yes, it skips.

Google Drive returns an `md5Checksum` on every file it stores, free of charge, so the same key
works for local files and for files already in your accounts — nothing has to be downloaded to
compare them.

This is why renaming a photo, moving it into a different folder, or re-running against a
reorganised library all produce zero uploads.

### Where the data lives

```
~/.memora/client_secret.json   your Google OAuth client (you create this; see setup)
~/.memora/tokens/<email>.json  one refresh token per account, chmod 600
~/.memora/index.db             the SQLite index: accounts, content, copies, your decisions
~/.memora/cache/               images fetched for the review page
```

Nothing sensitive is ever stored inside the repository. After each backup run, a copy of the
index is mirrored into every account's hidden `appDataFolder`, so the index is not a single
point of failure if your laptop dies.

### Placement

For each file, in order:

1. Hash it. If any enabled account already holds that content → **skip**.
2. Otherwise choose a target account: of the accounts where
   `used + in-flight + file size ≤ limit − reserve`, pick the one with the most headroom.
3. If no account qualifies → **blocked**, reported with the free bytes of each account. Nothing
   is silently dropped.
4. Upload, verify Drive's hash matches, and only then record the copy. A copy is never recorded
   before the upload confirms — a premature record is a file silently lost.

Quota is re-read from Google at the start of each run and tracked locally during it, because
Google's reported usage lags behind an upload by a few seconds.

### Duplicates

`memora sweep` lists what is actually in each account and folds it into the index, then groups
entries that share content and appear in more than one place. `memora review` serves those
groups on a local page for you to decide. That page is the only code path in the program that
can delete one of your files.

---

## What you need

- **Python 3.9+** (developed on 3.13)
- **Two or more Google accounts** — one works, but the account-switching is the point
- **A Google Cloud project of your own** — free, no billing account, ~4 minutes to set up
- Roughly 1 MB of disk per 10,000 photos for the index

Memora uses no paid service. Both Google APIs it touches are free, and exceeding their request
quotas returns an error rather than a bill. See [What it costs](#what-it-costs).

---

## Setup, step by step

### 1. Get the code and install the dependencies

```bash
git clone https://github.com/Sanchit2710/Memora.git
cd Memora
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Check it works before touching Google — the tests need no credentials and no network:

```bash
.venv/bin/python test_memora.py      # expect: 10 checks passed
```

### 2. Create your own Google Cloud project

You must do this part yourself in a browser. Memora deliberately does **not** ship a
pre-configured client — see [why](#why-you-need-your-own-google-project).

1. Go to <https://console.cloud.google.com> and sign in with any of your Google accounts.
2. Click the project dropdown (top left) → **New Project**. Name it `memora` → **Create**.
   Make sure the new project is selected afterwards.
3. In the search bar, find **Google Drive API** → **Enable**.
   *(Only enable the Photos Library API if you intend to use `--also-photos`, which is off by
   default and has significant downsides — see [Known limits](#known-limits).)*

### 3. Configure the consent screen and **publish it**

In the left menu, go to **APIs & Services → OAuth consent screen**. Newer consoles label this
section **Google Auth Platform**.

1. Choose **External** → **Create**.
2. Fill in the app name (`memora`), your support email, and your developer contact email.
   Save through the remaining screens. You do not need to add any scopes here — Memora requests
   its scopes at runtime, and they are all non-sensitive.
3. Open the **Audience** page in that same left menu.
4. Under *Publishing status: Testing*, click **PUBLISH APP** and confirm. It must read
   **In production**.

> **Do not skip step 4.** While the app sits in *Testing*, Google issues refresh tokens that
> **expire after 7 days**, so your backups break weekly, and only accounts you explicitly added
> as "test users" can sign in at all. Publishing is instant here and requires no verification
> review, because `drive.file` and `drive.appdata` are non-sensitive scopes.

### 4. Create the OAuth client and download it

1. Left menu → **Credentials** → **+ Create credentials** → **OAuth client ID**.
2. Application type: **Desktop app** → **Create**.
3. **Download JSON**.
4. Move it into place:

```bash
mkdir -p ~/.memora
mv ~/Downloads/client_secret_*.json ~/.memora/client_secret.json
```

### 5. Connect your accounts

Run this once per account:

```bash
.venv/bin/python -m memora auth add
```

A browser window opens. Pick the account, allow access. You will see a screen saying
*"Google hasn't verified this app"* — that is expected for a published-but-unverified personal
app. Click **Advanced → Go to memora (unsafe)**. It is your own app, in your own project,
and its scope only lets it see files it uploaded itself.

The terminal confirms:

```
added you@gmail.com  11.3 GB of 15.0 GB used
```

Repeat for every account you want to use. Then check the picture:

```bash
.venv/bin/python -m memora status
```

```
account                                  used      limit   reserve   placeable  on
you@gmail.com                          11.3 GB    15.0 GB    2.0 GB      1.7 GB  yes
you2@gmail.com                          2.1 GB    15.0 GB    2.0 GB     10.9 GB  yes

total placeable across 2 enabled account(s): 12.6 GB
```

`placeable` is what Memora will actually use: `limit − reserve − used`.

---

## Using it

### Always dry-run first

```bash
.venv/bin/python -m memora backup ~/Pictures --dry-run
```

This hashes everything and prints the plan without uploading a byte: which account each file
would go to, which files are already backed up, and which are blocked.

```
would upload DSC02072.JPG  1.7 MB  -> you2@gmail.com [drive]
would upload DSC02258.JPG  1.6 MB  -> you2@gmail.com [drive]
...
12 placed (14.5 MB), 0 already backed up (0 B not re-uploaded), 0 blocked
```

### Back up for real

```bash
.venv/bin/python -m memora backup ~/Pictures
```

Run it a second time and it should upload **nothing**:

```
0 placed (0 B), 12 already backed up (14.5 MB not re-uploaded), 0 blocked
```

That line is the entire reason this tool exists. If it ever reports bytes placed for files you
have already backed up, something is wrong.

When an account reaches its reserve, the next one takes over mid-run, and files too big for
any remaining headroom are reported rather than skipped silently:

```
up     DSC02884.JPG  1.6 MB  -> you@gmail.com
up     DSC02890.JPG  1.5 MB  -> you2@gmail.com
BLOCKED DSC03224.JPG (1.1 MB) - every account is at its reserve: you@gmail.com 58144B free; you2@gmail.com 591061B free
```

### Find and resolve duplicates

```bash
.venv/bin/python -m memora sweep
```

```
you@gmail.com: indexed 1041 file(s)
you2@gmail.com: indexed 388 file(s)

4 duplicate group(s), 6.5 MB reclaimable
  1.8 MB  you@gmail.com/DSC02264.JPG | you2@gmail.com/DSC02264.JPG
  ...

nothing deleted. decide each group: memora review
```

Then:

```bash
.venv/bin/python -m memora review
```

This opens <http://127.0.0.1:8731/>. For each group you get both copies **side by side at full
size**, with the account, filename, exact byte count and date under each, and a note explaining
why Memora thinks they match. Click an image to compare full-screen; `←`/`→` flip between the
copies in place; `esc` closes.

Four choices per group:

| Choice | What happens |
|---|---|
| **Keep this one, trash the other(s)** | Confirms by name and account first, then moves the other copies to that account's Drive trash (30-day recovery) |
| **Keep both** | Nothing is deleted; this group stops being offered |
| **Not duplicates — never ask again** | Recorded permanently, so the group never reappears in a future sweep |
| **Skip for now** | Left in the queue for next time |

A counter shows how many groups remain, and deciding one scrolls you to the next.

### Tuning the reserve

The default reserve is 2 GB per account. To leave more or less headroom:

```bash
.venv/bin/python -m memora auth reserve you@gmail.com 3      # keep 3 GB free for mail
```

To take an account out of rotation without removing it:

```bash
.venv/bin/python -m memora auth disable you@gmail.com
.venv/bin/python -m memora auth enable  you@gmail.com
```

A disabled account is no longer an upload target **and** its files no longer count as "already
backed up", so files only held there will be re-uploaded elsewhere. That is deliberate: a
disabled account is treated as unavailable.

---

## Command reference

```
memora auth add [--profile sweep] [--photos]   authorise an account
memora auth list                               show accounts, scopes, enabled state
memora auth enable|disable EMAIL               include/exclude an account
memora auth reserve EMAIL GB                   set that account's headroom
memora status [--cached]                       quota and placeable bytes (--cached: no API call)
memora backup DIR [--dry-run] [-v]             place files, switching accounts at the reserve
               [--also-photos] [--all-files]
memora sweep [--all-files] [--show N]          index remote files, report duplicate groups
memora review [--port N] [--no-open]           the review page; the only way to delete
```

`--all-files` includes non-media files; by default only images and videos are considered.
`-v` lists skipped files instead of only counting them.

There is deliberately **no delete flag.** Deletion happens only through `review`.

### Scopes, and why there are two profiles

| Profile | Scopes | Sees | Needs Google verification |
|---|---|---|---|
| `backup` (default) | `drive.file`, `drive.appdata` | only files Memora itself uploaded | no |
| `sweep` | the above + `drive.metadata.readonly` | everything in the account | no, but it is a *sensitive* scope |

Use the default unless you need to find duplicates created by *other* tools (Google's own
backup, another phone app). For those, `memora auth add --profile sweep` lets Memora see them;
because deleting a file it did not create would require Google's restricted `drive` scope (which
needs a paid security assessment for production use), the review page gives you a direct link
to delete those in Drive's own interface instead.

### What it costs

Nothing. Both APIs are free, no billing account is attached to the project, and exceeding the
request quota returns `403 rateLimitExceeded` — it does not bill. For scale: newer projects get
1,000,000 quota units per minute, a file-list call costs 100 units, so sweeping 1,000 files uses
about 0.1% of one minute's allowance. Uploads are capped at 750 GB/day per account, which is
irrelevant at 15 GB per account.

### Why you need your own Google project

- A shared OAuth client would put every user on **one** project's consent screen, which carries
  a 100-test-user cap plus that owner's quota and verification status.
- With your own client and the `drive.file` scope, Memora can only ever see files **it**
  uploaded. Your photos are not reachable by anyone else's client.
- Sharing tokens would mean sharing access to the photos themselves.

`rclone` asks the same of you, for the same reasons.

---

## Troubleshooting

**`Access blocked: memora has not completed the Google verification process` / `Error 403:
access_denied`**
Your app is still in *Testing*. Go to **Google Auth Platform → Audience → PUBLISH APP**. Or, as
a quick fix, add the account under **Audience → Test users** — but that keeps the 7-day token
expiry, so publishing is the real answer.

**`Google hasn't verified this app`**
Expected, and not the same as the error above. Click **Advanced → Go to memora (unsafe)**.

**`missing ~/.memora/client_secret.json - see README phase 0`**
Step 4 of setup did not land. Check `ls ~/.memora/` — the downloaded file must be renamed to
exactly `client_secret.json`.

**`missing dependency googleapiclient - run: pip install -r requirements.txt`**
You are running your system Python instead of the venv. Use `.venv/bin/python`, not `python3`.

**You have to sign in again every week**
Your app is in *Testing*. Publish it (step 3.4).

**`no token for you@gmail.com - run: memora auth add`**
The account is in the index but its token file is gone, or was revoked at
<https://myaccount.google.com/permissions>. Re-run `auth add` for it.

**A backup run says `FAILED <file>`**
That one file failed (unreadable, or the API refused it) and the rest of the run continued.
Nothing was recorded for it, so re-running retries it.

**`CORRUPT` / `Drive stored different bytes`**
The bytes Drive stored did not hash to what Memora read locally. The upload was discarded
rather than left untracked, and it is in your Drive trash. Re-run to retry.

**The review page shows `preview unavailable`**
Drive could not return a thumbnail or the bytes for that file (unsupported format, or a
transient failure). Decide from the metadata, or use the "open in Drive" link. Such a group
always defaults to *Skip*, never to a delete.

---

## Known limits

- **Matching is byte-identical only.** `md5` + size catches the re-backup case exactly, with no
  false positives. It will *not* match a re-encoded, resized, or WhatsApp-compressed version of
  the same photo — those are different files.
- **Google Photos cannot be deduplicated, at all.** Since 2025-03-31 an app may only see media
  it uploaded itself, and the Photos API has no delete endpoint at any scope. So `--also-photos`
  writes a *second* full-size copy against the same 15 GB that can never be cleaned up
  automatically. It is off by default. Photos duplicates are reported with a link for you to
  remove by hand.
- **Files land in one `memora-backup` folder per account**, with the original relative path kept
  in each file's `appProperties.relpath` so the tree can be reconstructed.
- **A run killed mid-upload** abandons that one resumable session. Drive creates no file, so the
  next run re-uploads it — correct, just not byte-efficient.
- **No restore command yet.** Your files are ordinary Drive files you can download normally; a
  `memora restore` that rebuilds the original tree from `relpath` is on the roadmap.
- **Spreading a library across several free accounts is not a configuration Google supports.**
  The accounts stay independent, and losing access to one loses that shard. The index is
  mirrored into every account to limit the damage.

---

## Safety: what can and cannot delete your photos

This matters enough to be explicit.

- The CLI has **no delete flag**. A test (`test_no_delete_path_outside_review`) fails the build
  if a delete call appears anywhere outside the two places allowed below.
- `dedup.py` deletes a file only when the review page posts a decision a human made.
- `sync.py` may delete exactly one thing: an upload it made seconds earlier whose stored bytes
  came back with the wrong hash. It never touches anything pre-existing.
- Deletion means **trash, not purge** — 30 days of recovery in that account's own Drive trash.
- Every delete is confirmed first, naming the account and filename being removed.
- The review server binds to `127.0.0.1` only.
- No credential, token, or account name is in this repository, and
  `test_repo_carries_no_credentials` fails the build if one ever is.

---

## Running the tests

```bash
.venv/bin/python test_memora.py
```

Ten assert-based checks, no framework, no network, no credentials required. They cover the
things that would really hurt if they broke:

- the reserve is never crossed, in-flight bytes included
- accounts switch when one is full, and everything blocks when all are
- known content is skipped rather than re-uploaded, including under a new name
- duplicate grouping needs two copies *and* matching size
- a decision sticks; only "keep one" deletes; a keeper from outside the group is rejected
- no delete path exists outside the review gate
- no credential-shaped string is in any tracked file
- the review page survives filenames containing quotes, backticks and `</script>`

---

## Roadmap

- `memora restore DIR` — rebuild the original folder tree from `appProperties.relpath`
- Perceptual hashing behind a confidence threshold, to catch re-encoded copies (strictly
  opt-in: this is where false positives start deleting real photos)
- Cross-run upload resume
- **Android app** — the index and placement logic are plain Python with no desktop
  dependencies, so the engine ports cleanly; the Google OAuth and Drive client libraries are
  the part that needs replacing with their Android equivalents.

---

## License

MIT — see [LICENSE](LICENSE).
