# larkmd

> Stateful mirror from a git markdown tree to a Feishu/Lark wiki.

`larkmd` keeps a directory of `.md` files in sync with a Feishu wiki space:
content-hash incremental updates, transactional in-place rewrites (no empty
docs on failure), cross-document link patching, table column-width memory,
and mermaid preprocessing.

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

# 4. Sync
larkmd plan                        # dry-run
larkmd apply                       # do it
```

State is kept in `.feishu-sync-state.json` (gitignored). Re-run `larkmd apply`
on every commit; only changed files are pushed.

---

## Commands

| Command | What it does |
|---|---|
| `larkmd init` | Interactive wizard → generates `larkmd.yaml` |
| `larkmd doctor` | Check `lark-cli` / `mmdc` / Pillow / env vars |
| `larkmd plan` | Dry-run: list create/update/skip per file |
| `larkmd apply` | Real sync (incremental by default; `--force` for full rebuild) |
| `larkmd cleanup` | Delete wiki nodes whose source `.md` was deleted |
| `larkmd restore-widths` | Re-apply remembered table column widths |
| `larkmd state show` | Print state file in human-readable form |

---

## Why use this over `feishu-cli` / `feishu-docx`?

| | larkmd | feishu-cli / feishu-docx |
|---|---|---|
| Stateful mirror (git tree → wiki tree) | ✅ | ❌ (one-shot import) |
| Incremental sync (content hash) | ✅ | ❌ |
| Transactional update (no empty docs) | ✅ | ❌ |
| Cross-document link patching | ✅ | ❌ |
| Table column-width memory across syncs | ✅ | ❌ |
| Mermaid preprocessing | ✅ | ❌ |
| Wiki node hierarchy from dir tree | ✅ | ❌ (CLI per node) |

`larkmd` is for **maintaining** a wiki from a markdown source of truth, not
for one-off conversions.

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
   Re-running `apply` self-heals. Never edit a synced doc by hand in Feishu —
   your edits will be wiped on next sync (unless you've rolled `last_remote_revision` — v2 future work).

4. **Table column widths require a Feishu UI nudge.** Markdown has no width.
   Once you adjust a column in Feishu's UI, `larkmd` records it in the state
   file and re-applies on every sync. Adding/removing a column resets that
   table's saved widths.

5. **`lark-cli` argv is capped at ~128 KB.** larkmd auto-switches large
   payloads to `--data @file` (relative path required by lark-cli).

6. **Wiki delete needs `wiki:wiki` scope.** If `larkmd cleanup` reports
   `131005`, re-auth: `lark-cli login --as user --scopes wiki:wiki ...`.

---

## Project status

`0.x` releases — schema may break between minor versions. State file carries
a `schema_version` and migrates automatically.

Roadmap:

- v0.1 (this release): one-way mirror, all gotchas above patched
- v0.2: bidirectional sync (Feishu UI edits → markdown), conflict detection
- v0.3: parallel sync respecting Feishu rate limits

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
