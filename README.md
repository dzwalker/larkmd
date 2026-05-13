# larkmd

> Bidirectional mirror between a git markdown tree and a Feishu/Lark wiki.

`larkmd` keeps a directory of `.md` files in sync with a Feishu wiki space:
content-hash incremental updates, transactional in-place rewrites (no empty
docs on failure), cross-document link patching, table column-width memory,
mermaid round-trip, and a reverse `pull` that turns Feishu edits back into
markdown with explicit conflict detection.

Built on top of the official [`lark-cli`](https://github.com/larksuite/cli) so
you don't have to manage OAuth tokens yourself.

---

## Quick start

```bash
# 1. Install
pip install larkmd
npm i -g @larksuiteoapi/lark-cli   # external runtime dependency
lark-cli login --as user           # one-time auth

# 2. Initialize config in your repo
cd path/to/your-md-repo
larkmd init                        # generates larkmd.yaml

# 3. Sanity check
larkmd doctor

# 4. Push: md → Feishu
larkmd plan                        # dry-run
larkmd apply                       # do it

# 5. Pull: Feishu → md (anything you edited in the UI)
larkmd pull-plan                   # dry-run, shows clean / remote-only / local-only / conflict
larkmd pull                        # write changed Feishu docs back to local md
```

State is kept in `.feishu-sync-state.json` (gitignored). Re-run `larkmd apply`
on every commit; only changed files are pushed. Run `larkmd pull` whenever
your team has been editing in Feishu.

---

## Commands

| Command | What it does |
|---|---|
| `larkmd init` | Interactive wizard → generates `larkmd.yaml` |
| `larkmd doctor` | Check `lark-cli` / `mmdc` / Pillow / env vars |
| `larkmd plan` | Dry-run push: list create/update/skip per file |
| `larkmd apply` | Real push (incremental by default; `--force` for full rebuild) |
| `larkmd pull-plan` | Dry-run pull: list clean / remote-only / local-only / conflict |
| `larkmd pull` | Reverse sync: rewrite local md from Feishu (`--force-remote` / `--force-local` for conflicts) |
| `larkmd cleanup` | Delete wiki nodes whose source `.md` was deleted |
| `larkmd restore-widths` | Re-apply remembered table column widths |
| `larkmd state show` | Print state file in human-readable form |

---

## Bidirectional sync model

```
       ┌────────── apply ──────────►┐
git md │                            │ Feishu wiki
       └◄───────── pull  ───────────┘
           (revision-aware)
```

**Push** (`apply`): walk md tree, hash-diff against state, import changed files
via `lark-cli drive +import`, in-place insert/delete blocks, upload mermaid
PNGs, restore callout block types, patch cross-doc links, move into wiki.

**Pull** (`pull`): for every file recorded in state, fetch the docx
`revision_id` and the block tree. Compare hashes both sides:

| local hash | remote revision | result |
|---|---|---|
| unchanged | unchanged | **clean** — skip |
| unchanged | changed | **remote-only** — overwrite local md from Feishu |
| changed | unchanged | **local-only** — skip; you should `apply` instead |
| changed | changed | **conflict** — abort (use `--force-remote` or `--force-local`) |

Conflict aborts default to safe — no local changes are touched until you
choose a side.

---

## Reverse-sync coverage

Round-trip fidelity per Feishu block type:

### Lossless

| Feishu block | Markdown | Notes |
|---|---|---|
| Text paragraph | plain text | bold / italic / code / strike / link preserved |
| Heading 1–9 | `#` … `#########` | |
| Bullet / ordered list | `-` / `1.` | nested via 2-space indent |
| Todo | `- [ ]` / `- [x]` | |
| Code | ` ```lang ` | language id mapped per Feishu reference |
| Quote | `> ` | |
| Equation | `$$…$$` (block) / `$…$` (inline) | |
| Divider | `---` | |
| Table | GFM | column widths preserved via state |
| Image (mermaid) | ` ```mermaid ` | source preserved in state, restored on pull |
| Image (other) | `![](.assets/<token>.png)` | downloaded to `<md_dir>/.assets/` |

### Lossy with restoration

| Feishu block | Pull writes | Push restores? |
|---|---|---|
| **Callout** | `<!-- larkmd:callout emoji=fire bg=1 -->\n> body` | ✅ marker tracked, callout block reconstituted post-import |

### Lossy degradation (no restoration)

| Feishu block | Pull writes | After push |
|---|---|---|
| Bookmark | `<!-- larkmd:bookmark -->\n[url](url)` | becomes plain link |
| File attachment | `<!-- larkmd:file token=… -->\n[file: name](token)` | becomes plain link |
| Iframe | `<!-- larkmd:iframe -->\n[embed](url)` | becomes plain link |
| Sheet / bitable embed | `<!-- larkmd:sheet token=… -->\n[sheet](url)` | becomes plain link |
| Sync block | `<!-- larkmd:sync_block -->\n<children>` | sync wrapper lost; content stays |
| MindNote / Whiteboard / Diagram | `<!-- larkmd:mindnote token=… -->` placeholder | content unrecoverable from md |

The `<!-- larkmd:* -->` markers are stripped before push so they don't appear
in the rendered Feishu doc. They exist as breadcrumbs for the human reader of
the markdown — "this used to be a callout in Feishu" — and to drive the
callout restore step. Markers you write yourself (`<!-- TODO -->`, etc.) are
left alone.

### Out of scope (v0)

- New Feishu docs not in state — `pull` won't auto-create local md (path
  inference is ambiguous). Run `apply` from the local side first.
- Comments / reactions / version history.
- Parallel pull (Feishu has aggressive per-doc rate limits).

---

## Mermaid round-trip

```
md:    ```mermaid                    Feishu doc:
       graph TD; A-->B               [image: PNG]
       ```                                   ↑
                ↓ apply                      │
       mmdc renders → PNG ───────────────────┘
       upload returns image_token
       → state.mermaid_blocks[image_token] = "graph TD; A-->B"

       ↓ pull (later)
       image block has token "T"
       state has T → write ```mermaid graph TD; A-->B``` (no PNG download)
```

If you want a fully byte-equal `apply → pull` round-trip on a mermaid-only
file: state must already know the source. The very first pull after a push
performed by an older larkmd (no `mermaid_blocks` entry) will fall back to
the plain `![](.assets/<token>.png)` form.

---

## Callout restoration (deep dive)

Callouts are popular and have no native markdown form, so we go to extra
lengths to keep them as callouts after a pull → push cycle:

1. **Pull** writes the marker + a quote:
   ```
   <!-- larkmd:callout emoji=fire bg=1 -->
   > hot tip
   ```

2. **Push pre-process** (in `_sync_one`): scan for callout markers, replace
   each with a unique placeholder paragraph that survives Feishu's markdown
   importer as plain text:
   ```
   LARKMD_CALLOUT_PLACEHOLDER_0
   > hot tip
   ```

3. **`drive +import`** ingests as normal — placeholder becomes a paragraph
   block, quote becomes a quote block.

4. **Post-import patch** (`callout_restore.restore_callouts`): walk the docx,
   find each placeholder paragraph, claim the next sibling (must be a quote),
   build a real callout block (`block_type=19`) carrying the quote's
   elements as a child paragraph + adopting any nested children, insert via
   the descendant API, then delete the placeholder + quote pair.

5. If the placeholder can't be found or the next block isn't a quote, the
   marker is silently skipped — the placeholder paragraph is left in the doc
   as a visible breadcrumb. We never delete content we can't replace.

Other lossy block types (bookmark / iframe / sheet / etc.) are not restored
this way; their markers exist purely as documentation.

---

## Why use this over `feishu-cli` / `feishu-docx`?

| | larkmd | feishu-cli / feishu-docx |
|---|---|---|
| Stateful mirror (git tree → wiki tree) | ✅ | ❌ (one-shot import) |
| Bidirectional with conflict detection | ✅ | ❌ |
| Incremental sync (content hash + revision) | ✅ | ❌ |
| Transactional update (no empty docs) | ✅ | ❌ |
| Cross-document link patching | ✅ | ❌ |
| Table column-width memory across syncs | ✅ | ❌ |
| Mermaid round-trip (md ↔ docx PNG ↔ md) | ✅ | ❌ |
| Callout type restoration on push | ✅ | ❌ |
| Wiki node hierarchy from dir tree | ✅ | ❌ (CLI per node) |

`larkmd` is for **maintaining** a wiki from a markdown source of truth (with
occasional UI edits flowing back), not for one-off conversions.

---

## Gotchas (you'll hit these on day one)

1. **lark-cli silently keeps only the first link in a multi-link line.**
   Put each `[a](x)` `[b](y)` on its own line, or use one as inline and the
   other in a footnote. `larkmd doctor` warns on offending lines.

2. **Mermaid emoji / Chinese render as boxes** unless you install Noto fonts:
   ```bash
   sudo apt install fonts-noto-cjk fonts-noto-color-emoji
   ```

3. **`apply` is insert-then-delete.** If anything between `import_md` and
   `descendant create` fails, the doc may briefly contain old + new content.
   Re-running `apply` self-heals.

4. **Edit in Feishu freely — but pull first.** Reverse sync is revision-aware,
   so the cycle is: `pull` to grab UI edits → review/commit → `apply` to push
   your local changes back. `pull-plan` tells you who's ahead.

5. **Table column widths require a Feishu UI nudge.** Markdown has no width.
   Once you adjust a column in Feishu's UI, `larkmd` records it in the state
   file and re-applies on every sync. Adding/removing a column resets that
   table's saved widths.

6. **`lark-cli` argv is capped at ~128 KB.** larkmd auto-switches large
   payloads to `--data @file` (relative path required by lark-cli).

7. **Wiki delete needs `wiki:wiki` scope.** If `larkmd cleanup` reports
   `131005`, re-auth: `lark-cli login --as user --scopes wiki:wiki ...`.

8. **First pull after upgrading from a no-revision state file** treats every
   file as `remote-only`. Run `larkmd apply --force` once to record the
   current `last_remote_revision` baseline, then subsequent pulls behave
   normally.

---

## Project status

`0.x` releases — schema may break between minor versions. State file carries
a `schema_version` and migrates automatically.

- v0.1: one-way push mirror, all gotchas above patched
- **v0.2 (current): bidirectional sync, mermaid round-trip, callout restore,
  conflict detection**
- v0.3: parallel sync respecting Feishu rate limits, new-doc discovery on pull

---

## Development

```bash
git clone https://github.com/dzwalker/larkmd
cd larkmd
pip install -e ".[dev]"
pytest
```

E2E tests are opt-in (need a real Feishu test workspace) — see
[tests/e2e/README.md](tests/e2e/README.md).

---

## License

MIT — see [LICENSE](LICENSE).
