"""
trilium-note — file-based CLI for TriliumNext notes via the ETAPI.

Commands:
  search <query> [--limit N]             Search notes, print noteId + title (one per line)
  fetch  <noteId> [--file PATH]          Download note to file (default: /tmp/trilium/<noteId>.html)
  push   <noteId> [--file PATH] [--force]  Upload file back, with drift detection + auto backup
  info   <noteId>                        Print note metadata (title, type, dates)

Env vars (both required):
  TRILIUM_API_URL    e.g. http://your-trilium-host:8080
  TRILIUM_API_TOKEN  ETAPI token from Trilium Options → ETAPI
  TRILIUM_TMPDIR     optional, default: /tmp/trilium

Edge cases this tool handles so callers don't have to:

1. Content-Type on PUT. The ETAPI PUT /notes/<id>/content endpoint rejects
   Content-Type: text/html with HTTP 500 "Cannot set null content" AND clobbers
   the note to the literal string "[object Object]". We always send text/plain;
   the body is still raw HTML, the server stores whatever bytes we send.

2. Silent overwrites from UI edits. Between fetch and push, the note may have
   been edited through the Trilium UI (or by another Claude session). We record
   the server's dateModified + blobId at fetch time in a sidecar <file>.meta.json,
   and on push we re-check the server. If it changed, push refuses unless --force
   is passed.

3. Accidental clobbers from bad local state. On push we refuse to upload an
   empty file or a file containing only "[object Object]" — that's almost
   certainly a corrupted local buffer, not a real edit.

4. Pre-overwrite snapshots. On every push we first fetch the server's current
   content and save it as <file>.bak.<epoch>.html next to the local file, so
   there's always a timestamped restore point. No manual backup needed.
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

API_URL = os.environ.get("TRILIUM_API_URL")
API_TOKEN = os.environ.get("TRILIUM_API_TOKEN")
if not API_URL or not API_TOKEN:
    sys.exit(
        "trilium-note: TRILIUM_API_URL and TRILIUM_API_TOKEN must both be set. "
        "Get an ETAPI token from Trilium Options → ETAPI."
    )
if not API_URL.rstrip("/").endswith("/etapi"):
    API_URL = API_URL.rstrip("/") + "/etapi"
TMPDIR = os.environ.get("TRILIUM_TMPDIR", "/tmp/trilium")

BAD_BODIES = {"", "[object Object]", "null", "undefined"}


def api(method, path, data=None, content_type="application/json"):
    url = f"{API_URL}{path}"
    headers = {"Authorization": API_TOKEN}
    body = None
    if data is not None:
        if content_type == "application/json":
            body = json.dumps(data).encode()
        else:
            body = data.encode() if isinstance(data, str) else data
        headers["Content-Type"] = content_type
    if body is not None:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
    else:
        req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return json.loads(raw) if raw else {}
            return raw.decode()
    except urllib.error.HTTPError as e:
        print(f"Error: {e.code} {e.reason}", file=sys.stderr)
        body = e.read().decode()
        if body:
            print(body, file=sys.stderr)
        sys.exit(1)


def meta_path(filepath: str) -> str:
    return filepath + ".meta.json"


def write_meta(filepath: str, note: dict, content: str) -> None:
    meta = {
        "noteId": note.get("noteId"),
        "dateModified": note.get("dateModified"),
        "utcDateModified": note.get("utcDateModified"),
        "blobId": note.get("blobId"),
        "fetchedAt": time.time(),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }
    with open(meta_path(filepath), "w") as f:
        json.dump(meta, f, indent=2)


def read_meta(filepath: str) -> dict | None:
    mp = meta_path(filepath)
    if not os.path.exists(mp):
        return None
    try:
        with open(mp) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def cmd_search(args):
    params = urllib.parse.urlencode({"search": args.query, "limit": args.limit})
    result = api("GET", f"/notes?{params}")
    notes = result.get("results", [])
    if not notes:
        print("No results.")
        return
    for n in notes:
        print(f"{n['noteId']}\t{n['title']}")


def cmd_fetch(args):
    os.makedirs(TMPDIR, exist_ok=True)
    note_id = args.noteId
    filepath = args.file or os.path.join(TMPDIR, f"{note_id}.html")
    note = api("GET", f"/notes/{note_id}")
    content = api("GET", f"/notes/{note_id}/content")
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w") as f:
        f.write(content)
    write_meta(filepath, note, content)
    print(filepath)


def cmd_push(args):
    note_id = args.noteId
    filepath = args.file or os.path.join(TMPDIR, f"{note_id}.html")

    if not os.path.exists(filepath):
        print(f"Error: {filepath} not found — fetch first.", file=sys.stderr)
        sys.exit(1)

    with open(filepath, "r") as f:
        content = f.read()

    # Guard 1: refuse obviously-broken content
    stripped = content.strip()
    if stripped in BAD_BODIES:
        print(
            f"Error: refusing to push — local file looks corrupted "
            f"(content is {stripped!r}). Delete {filepath} and re-fetch if intentional.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Guard 2: drift detection — compare server state to the fetch-time sidecar
    meta = read_meta(filepath)
    current = api("GET", f"/notes/{note_id}")
    if not args.force:
        if meta is None:
            print(
                f"Error: no .meta.json sidecar for {filepath}. "
                f"Run `trilium-note fetch {note_id}` first (or pass --force to push blind).",
                file=sys.stderr,
            )
            sys.exit(1)
        drift_fields = []
        if meta.get("blobId") and current.get("blobId") and meta["blobId"] != current["blobId"]:
            drift_fields.append(f"blobId {meta['blobId']} -> {current['blobId']}")
        if meta.get("utcDateModified") != current.get("utcDateModified"):
            drift_fields.append(
                f"utcDateModified {meta.get('utcDateModified')} -> {current.get('utcDateModified')}"
            )
        if drift_fields:
            print(
                "Error: note was modified on the server since last fetch. Refusing to overwrite.\n  "
                + "\n  ".join(drift_fields)
                + f"\nRe-fetch with `trilium-note fetch {note_id}`, reapply your edits, and push again.\n"
                + "Use --force to override.",
                file=sys.stderr,
            )
            sys.exit(2)

    # Guard 3: snapshot the server's current content before overwriting it
    server_content = api("GET", f"/notes/{note_id}/content")
    snap = f"{filepath}.bak.{int(time.time())}.html"
    with open(snap, "w") as f:
        f.write(server_content)

    # Push — always text/plain, never text/html (ETAPI quirk; see module docstring)
    api("PUT", f"/notes/{note_id}/content", data=content, content_type="text/plain")

    # Refresh the sidecar so subsequent pushes drift-check from the new state
    fresh = api("GET", f"/notes/{note_id}")
    write_meta(filepath, fresh, content)

    print(f"OK — pushed {len(content)} bytes to {note_id} (snapshot: {snap})")


def cmd_info(args):
    note = api("GET", f"/notes/{args.noteId}")
    print(f"noteId:   {note['noteId']}")
    print(f"title:    {note['title']}")
    print(f"type:     {note['type']}")
    print(f"mime:     {note['mime']}")
    print(f"modified: {note.get('dateModified', '?')}")
    parents = note.get("parentNoteIds", [])
    if parents:
        print(f"parents:  {', '.join(parents)}")


def main():
    p = argparse.ArgumentParser(prog="trilium-note", description="File-based Trilium note tool")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("search", help="Search notes")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)

    f = sub.add_parser("fetch", help="Download note to file")
    f.add_argument("noteId")
    f.add_argument("--file", default=None)

    pu = sub.add_parser("push", help="Upload file to note (drift-checked, snapshots old content)")
    pu.add_argument("noteId")
    pu.add_argument("--file", default=None)
    pu.add_argument("--force", action="store_true", help="Skip drift detection")

    i = sub.add_parser("info", help="Note metadata")
    i.add_argument("noteId")

    args = p.parse_args()
    if args.cmd == "search":
        cmd_search(args)
    elif args.cmd == "fetch":
        cmd_fetch(args)
    elif args.cmd == "push":
        cmd_push(args)
    elif args.cmd == "info":
        cmd_info(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
