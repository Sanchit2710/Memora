"""Localhost review page. The only path in this program by which anything is deleted.

Images are proxied through this server using the account's own credentials, so they never
depend on short-lived thumbnail URLs, and they are cached on disk so flipping between two
copies is instant.
"""
import html
import json
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import accounts, dedup, store

CACHE = store.HOME / "cache"


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fetch_bytes(email, remote_id):
    p = CACHE / f"{email}-{remote_id}"
    if p.exists():
        return p.read_bytes()
    svc = accounts.drive(email)
    data = accounts.retry(lambda: svc.files().get_media(fileId=remote_id).execute())
    CACHE.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return data


PAGE = r"""<!doctype html><meta charset=utf-8>
<title>memora review</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f6f4;--card:#fff;--ink:#1a1a18;--dim:#6b6b66;--line:#e2e2dd;
      --warn:#b4451f;--ok:#2f6f4f;--accent:#2b5fd9}
@media(prefers-color-scheme:dark){:root{--bg:#141413;--card:#1d1d1b;--ink:#eceae4;
      --dim:#9a988f;--line:#30302c;--warn:#e8835c;--ok:#7bc39a;--accent:#7aa2f7}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 ui-sans-serif,-apple-system,Segoe UI,sans-serif}
header{padding:20px 24px;border-bottom:1px solid var(--line);position:sticky;top:0;
  background:var(--bg);z-index:5}
h1{margin:0 0 4px;font-size:18px;letter-spacing:-.01em}
.sub{color:var(--dim);font-size:13px}
main{padding:24px;max-width:1200px;margin:0 auto}
.group{background:var(--card);border:1px solid var(--line);border-radius:10px;
  padding:18px;margin-bottom:22px}
.why{font-size:13px;color:var(--dim);margin-bottom:14px}
.why b{color:var(--warn);font-weight:600}
.pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.copy{border:1px solid var(--line);border-radius:8px;overflow:hidden;display:flex;
  flex-direction:column}
.copy img{width:100%;aspect-ratio:4/3;object-fit:contain;background:#000;cursor:zoom-in;
  display:block}
.noimg{aspect-ratio:4/3;display:grid;place-items:center;background:var(--bg);
  color:var(--dim);font-size:13px;text-align:center;padding:12px}
.meta{padding:10px 12px;font-size:13px}
.meta .em{font-weight:600}
.meta div{color:var(--dim)}
.meta a{color:var(--accent)}
.sugg{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--ok)}
.acts{display:flex;flex-wrap:wrap;gap:8px;padding:10px 12px;border-top:1px solid var(--line);
  margin-top:auto}
button{font:inherit;font-size:13px;padding:7px 11px;border-radius:6px;cursor:pointer;
  border:1px solid var(--line);background:var(--bg);color:var(--ink)}
button.danger{border-color:var(--warn);color:var(--warn)}
button:hover{filter:brightness(1.08)}
.groupacts{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.done{opacity:.45}
.tag{font-size:12px;color:var(--ok)}
#flip{position:fixed;inset:0;background:#000;display:none;z-index:50}
#flip img{width:100%;height:100%;object-fit:contain}
#flip .bar{position:absolute;top:0;left:0;right:0;padding:10px 14px;color:#fff;
  background:rgba(0,0,0,.6);font-size:13px;display:flex;justify-content:space-between}
.empty{text-align:center;color:var(--dim);padding:60px 20px}
kbd{border:1px solid var(--line);border-radius:4px;padding:1px 5px;font-size:12px}
</style>
<header><h1>Duplicate review</h1>
<div class=sub>__COUNT__ group(s) · __BYTES__ reclaimable · nothing is deleted until you
choose · deletions go to that account's Drive trash (30-day recovery) ·
click an image to flip full-screen, <kbd>&larr;</kbd><kbd>&rarr;</kbd> to compare,
<kbd>esc</kbd> to close</div></header>
<main>__BODY__</main>
<div id=flip><div class=bar><span id=flipname></span><span>esc to close</span></div>
<img id=flipimg></div>
<script>
const G = __DATA__;
let fl=null, fi=0;
function flip(g,i){const c=G[g].copies;
  fl={src:c.map(x=>x.src),names:c.map(x=>x.account+' \u00b7 '+x.name)};fi=i;show();
  document.getElementById('flip').style.display='block'}
function show(){document.getElementById('flipimg').src=fl.src[fi];
  document.getElementById('flipname').textContent=fl.names[fi]+'  ('+(fi+1)+'/'+fl.src.length+')'}
document.onkeydown=e=>{if(!fl)return;
  if(e.key==='Escape'){fl=null;document.getElementById('flip').style.display='none'}
  if(e.key==='ArrowRight'){fi=(fi+1)%fl.src.length;show()}
  if(e.key==='ArrowLeft'){fi=(fi-1+fl.src.length)%fl.src.length;show()}}
function keepOne(g,i){
  const grp=G[g], keep=grp.copies[i], drop=grp.copies.filter((_,j)=>j!==i);
  const msg='Trash every OTHER copy in this group and keep:\n\n'
    +keep.account+'\n'+keep.name+'\n\nTo be trashed:\n'
    +drop.map(d=>'  '+d.account+'  '+d.name+'  ('+d.human+')').join('\n')
    +'\n\nThey go to that account\u2019s Drive trash, recoverable for 30 days.';
  if(confirm(msg)) post(g,'keep_one',keep.id);
}
function decide(g,action){post(g,action,null)}
async function post(g,action,keepId){
  const el=document.getElementById('g-'+g);
  const r=await fetch('/decide',{method:'POST',
    body:JSON.stringify({key:G[g].key,action:action,keep_id:keepId})});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){alert('failed: '+(j.error||r.status));return}
  el.classList.add('done');
  el.querySelector('.groupacts').innerHTML='<span class=tag>'+j.message+'</span>';
  el.querySelectorAll('.acts').forEach(a=>a.remove());
}
</script>
"""


def _src(c):
    return (f"/img?e={urllib.parse.quote(c['account'])}"
            f"&id={urllib.parse.quote(c['remote_id'])}")


def render(groups):
    """All per-file data goes into one JSON blob, never into an HTML attribute - a
    filename with an apostrophe in it used to break every handler on the page."""
    if not groups:
        page = PAGE.replace("__BODY__",
                            "<div class=empty>No duplicate groups pending. Run "
                            "<code>memora sweep</code> to look again.</div>")
        return (page.replace("__COUNT__", "0").replace("__BYTES__", "0 B")
                .replace("__DATA__", "[]"))

    total = sum(g["size"] * (len(g["copies"]) - 1) for g in groups)
    data = [{"key": g["key"],
             "copies": [{"id": c["remote_id"], "account": c["account"],
                         "name": c["name"] or c["remote_id"], "src": _src(c),
                         "human": human(c["size"])} for c in g["copies"]]}
            for g in groups]

    body = []
    for gi, g in enumerate(groups):
        panes = []
        for i, c in enumerate(g["copies"]):
            keep = (" <span class=sugg>suggested keep</span>"
                    if c["remote_id"] == g["suggest_keep"] else "")
            link = (f'<a href="{html.escape(c["product_url"], quote=True)}" target=_blank>'
                    "open in Drive</a>") if c["product_url"] else \
                   '<span style="color:var(--dim)">not linkable</span>'
            panes.append(
                f'<div class=copy>'
                f'<img src="{_src(c)}" loading=lazy onclick="flip({gi},{i})" '
                f'onerror="this.replaceWith(Object.assign(document.createElement(\'div\'),'
                f'{{className:\'noimg\',textContent:\'preview unavailable - decide from '
                f'the metadata, or open it in Drive\'}}))">'
                f'<div class=meta><div class=em>{html.escape(c["account"])}{keep}</div>'
                f'<div>{html.escape(c["name"] or "?")}</div>'
                f'<div>{human(c["size"])} &middot; '
                f'{html.escape(c["created_time"][:10] if c["created_time"] else "date unknown")}'
                f'</div>'
                f'<div>{link}</div></div>'
                f'<div class=acts><button class=danger onclick="keepOne({gi},{i})">'
                f'Keep this one, trash the other(s)</button></div></div>')

        photos = ""
        if g["photos"]:
            links = " ".join(
                f'<a href="{html.escape(p["product_url"] or "#", quote=True)}" '
                f'target=_blank>{html.escape(p["account"])}</a>' for p in g["photos"])
            photos = (f'<div class=why>Also in Google Photos ({links}) &mdash; the Photos API '
                      "has no delete endpoint at any scope, so that copy has to be removed in "
                      "the Photos app by hand.</div>")

        body.append(
            f'<div class=group id="g-{gi}">'
            f'<div class=why>Matched because the content is <b>byte-for-byte identical</b>: '
            f'md5 <code>{html.escape(g["md5"])}</code>, every copy {g["size"]:,} bytes. '
            "There is no visual difference to hunt for &mdash; the only question is whether "
            "you meant to keep it in two places.</div>"
            f'{photos}<div class=pair>{"".join(panes)}</div>'
            f'<div class=groupacts>'
            f'<button onclick="decide({gi},\'keep_all\')">Keep both</button>'
            f'<button onclick="decide({gi},\'not_duplicates\')">Not duplicates &mdash; '
            "never ask again</button>"
            f'<button onclick="decide({gi},\'skip\')">Skip for now</button>'
            "</div></div>")

    blob = json.dumps(data).replace("</", "<\\/")
    return (PAGE.replace("__BODY__", "".join(body))
            .replace("__COUNT__", str(len(groups)))
            .replace("__BYTES__", human(total))
            .replace("__DATA__", blob))


def serve(db, port=8731, open_browser=True):
    state = {"groups": {}}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/":
                gs = dedup.groups(db)
                state["groups"] = {g["key"]: g for g in gs}
                return self._send(200, render(gs).encode())
            if u.path == "/img":
                q = urllib.parse.parse_qs(u.query)
                try:
                    data = fetch_bytes(q["e"][0], q["id"][0])
                except Exception as e:
                    return self._send(404, str(e).encode(), "text/plain")
                return self._send(200, data, "application/octet-stream")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/decide":
                return self._send(404, b"{}", "application/json")
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            g = state["groups"].get(req.get("key"))
            if not g:
                return self._send(400, json.dumps(
                    {"error": "unknown group, reload the page"}).encode(),
                    "application/json")
            try:
                trashed = dedup.apply_decision(db, g, req["action"], req.get("keep_id"))
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)}).encode(),
                                  "application/json")
            msg = {"keep_all": "kept both", "not_duplicates": "marked not duplicates",
                   "skip": "skipped"}.get(req["action"])
            if msg is None:
                msg = (f"trashed {len(trashed)} copy(ies), "
                       f"{human(sum(c['size'] for c in trashed))} reclaimed")
            return self._send(200, json.dumps({"message": msg}).encode(),
                              "application/json")

    url = f"http://127.0.0.1:{port}/"
    print(f"review at {url}  (ctrl-c to stop)")
    if open_browser:
        webbrowser.open(url)
    HTTPServer(("127.0.0.1", port), H).serve_forever()
