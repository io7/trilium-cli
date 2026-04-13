# trilium-cli

A small, file-based CLI for editing [TriliumNext](https://github.com/TriliumNext/Notes) notes via the ETAPI. Built for the fetch-edit-push loop that LLM-driven workflows (Claude Code, Cursor, Aider, plain shell scripts) live on, with drift detection and pre-overwrite snapshots so you don't lose notes to races or content-type quirks.

One script, stdlib only, no dependencies.

## Install

```bash
curl -L https://raw.githubusercontent.com/io7/trilium-cli/main/trilium-note \
  -o ~/.local/bin/trilium-note
chmod +x ~/.local/bin/trilium-note
```

## Configure

Both env vars are required:

```bash
export TRILIUM_API_URL=http://your-trilium-host:8080
export TRILIUM_API_TOKEN=<paste from Trilium → Options → ETAPI>
```

Optional:

```bash
export TRILIUM_TMPDIR=/tmp/trilium   # default
```

## Commands

```
trilium-note search <query> [--limit N]            # print "noteId<TAB>title" per line
trilium-note fetch  <noteId> [--file PATH]         # download to $TRILIUM_TMPDIR/<noteId>.html
trilium-note push   <noteId> [--file PATH] [--force]   # drift-checked push
trilium-note info   <noteId>                       # title, type, dates, parents
```

## The edit loop

```bash
trilium-note fetch J6JsaMQUq1bB
# …edit /tmp/trilium/J6JsaMQUq1bB.html in your editor / with Claude / sed / whatever…
trilium-note push J6JsaMQUq1bB
```

`fetch` writes a `<file>.meta.json` sidecar capturing the server's `blobId` and `utcDateModified` at fetch time. `push` uses it for drift detection; delete it if you want a blind push, or use `--force`.

## What the tool handles so you don't have to

These are all lessons learned from corrupting a note once:

1. **ETAPI PUT content-type is cursed.** `PUT /notes/<id>/content` with `Content-Type: text/html` returns `HTTP 500 "Cannot set null content"` **and silently clobbers the note to the literal string `[object Object]`** before the error is returned. `trilium-note` always sends `text/plain` — the body is still raw HTML, the server stores whatever bytes you send regardless of declared type.

2. **Drift detection on push.** If the note was modified on the server between your fetch and your push — via the Trilium UI, another tool, another session — push refuses with exit code 2 and tells you to re-fetch. Use `--force` if you know what you're doing.

3. **Pre-overwrite snapshots.** Every successful push first GETs the server's current content and saves it as `<file>.bak.<epoch>.html` next to the local file. There is always a restore point. No manual backups needed.

4. **Bad-content guard.** Push refuses to upload an empty file or a file whose only content is `[object Object]`, `null`, or `undefined`. That's almost certainly a corrupted local buffer, not a real edit.

## Exit codes

- `0` — success
- `1` — configuration error (missing env var), missing file, bad content, HTTP error
- `2` — drift detected on push (use `--force` to override)

## License

MIT. See [LICENSE](LICENSE).
