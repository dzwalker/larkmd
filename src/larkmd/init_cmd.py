"""`larkmd init` — generate a starter larkmd.yaml."""

from __future__ import annotations

from pathlib import Path

TEMPLATE = """\
# larkmd.yaml — see https://github.com/dzwalker/larkmd
version: 1

lark:
  tenant: {tenant}                    # your Feishu host, e.g. r3c0qt6yjw.feishu.cn
  profile: feishu                     # lark-cli profile (run `lark-cli profile list`)
  cli_path: lark-cli                  # override if not on PATH

wiki:
  space_id: ${{FEISHU_WIKI_SPACE_ID}}   # ${{VAR}} pulled from environment

paths:
  root: .
  state_file: .feishu-sync-state.json
  ignore: ["**/node_modules/**", "**/.venv/**"]

# Files at the repo root, no numeric prefix
root_files:
  order: [README.md]
  drive_folder: ${{FEISHU_DRIVE_FOLDER_TOKEN}}
  wiki_parent: ""                       # "" = wiki space root

# Subdirectories → wiki sections
sections:
  - dir: docs
    title_prefix: "01-Docs"
    drive_folder: ${{FEISHU_DRIVE_FOLDER_TOKEN}}
    wiki_parent: ${{FEISHU_WIKI_NODE_DOCS}}
    order: []                           # explicit order; rest appended alphabetically

naming:
  numeric_prefix: true                  # title becomes "01-Docs-1-overview"
  strip_md_extension: true

mermaid:
  enabled: true
  mmdc_path: mmdc
  puppeteer_config: null                # path to puppeteer-config.json if needed
  cache_dir: .larkmd-cache/mermaid

importer:
  move_max_retries: 30
  move_retry_interval_sec: 1.5
  argv_threshold_bytes: 100000
"""


def write_template(path: Path, *, tenant: str = "your-host.feishu.cn") -> None:
    if path.exists():
        raise FileExistsError(f"{path} already exists; refusing to overwrite")
    path.write_text(TEMPLATE.format(tenant=tenant))


def run_init(path: Path, *, tenant: str | None = None, force: bool = False) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists (use --force to overwrite)")
    path.write_text(TEMPLATE.format(tenant=tenant or "your-host.feishu.cn"))
    print(f"wrote {path}")
    print("\nNext steps:")
    print(f"  1. edit {path} to point at your wiki space + drive folders")
    print("  2. populate env vars referenced as ${VAR}")
    print("  3. run `larkmd doctor` to verify setup")
    print("  4. run `larkmd plan` to preview, then `larkmd apply`")
