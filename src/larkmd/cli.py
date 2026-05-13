"""larkmd CLI (click)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from larkmd import __version__
from larkmd.config import Config
from larkmd.errors import ConfigError, LarkmdError


def _load_cfg(config_path: str) -> Config:
    try:
        return Config.load(config_path)
    except ConfigError as e:
        click.echo(f"config error: {e}", err=True)
        sys.exit(1)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version", prog_name="larkmd")
@click.option("-c", "--config", default="larkmd.yaml", show_default=True,
              help="Path to larkmd.yaml")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.pass_context
def main(ctx: click.Context, config: str, verbose: bool) -> None:
    """larkmd — stateful mirror from a git markdown tree to a Feishu/Lark wiki."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    ctx.obj["verbose"] = verbose


@main.command()
@click.option("--tenant", default=None, help="Your Feishu tenant host (e.g. r3c0qt6yjw.feishu.cn)")
@click.option("--force", is_flag=True, help="Overwrite existing larkmd.yaml")
@click.pass_context
def init(ctx: click.Context, tenant: str | None, force: bool) -> None:
    """Generate a starter larkmd.yaml in the current directory."""
    from larkmd.init_cmd import run_init
    path = Path(ctx.obj["config_path"])
    try:
        run_init(path, tenant=tenant, force=force)
    except FileExistsError as e:
        click.echo(str(e), err=True)
        sys.exit(1)


@main.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Self-check: lark-cli / mmdc / Pillow / env vars / multi-link warnings."""
    from larkmd.doctor import run_doctor
    cfg = _load_cfg(ctx.obj["config_path"])
    sys.exit(run_doctor(cfg))


@main.command()
@click.option("--only", multiple=True, help="Limit to these md paths (repo-relative)")
@click.option("--force", is_flag=True, help="Ignore content hash; replan everything")
@click.pass_context
def plan(ctx: click.Context, only: tuple[str, ...], force: bool) -> None:
    """Dry-run: list create/update/skip per file."""
    from larkmd.syncer import Syncer
    cfg = _load_cfg(ctx.obj["config_path"])
    syncer = Syncer(cfg)
    actions = syncer.plan(only=list(only) or None, force=force)
    creates = sum(1 for a in actions if a.kind == "create")
    updates = sum(1 for a in actions if a.kind == "update")
    skips = sum(1 for a in actions if a.kind == "skip")
    print(f"=== plan: {creates} create / {updates} update / {skips} skip ===")
    for a in actions:
        marker = {"create": "C", "update": "U", "skip": "."}[a.kind]
        print(f"  {marker} {a.rel}  ({a.reason})")


@main.command()
@click.option("--only", multiple=True, help="Limit to these md paths (repo-relative)")
@click.option("--force", is_flag=True, help="Ignore content hash; rebuild everything")
@click.option("--skip-link-pass", is_flag=True, help="Skip Pass 2 (cross-doc link patching)")
@click.pass_context
def apply(ctx: click.Context, only: tuple[str, ...], force: bool, skip_link_pass: bool) -> None:
    """Real sync: import/update each md, then patch cross-doc links."""
    from larkmd.syncer import Syncer
    cfg = _load_cfg(ctx.obj["config_path"])
    syncer = Syncer(cfg)
    plan_actions = syncer.plan(only=list(only) or None, force=force)
    print(f"=== Pass 1: apply {len(plan_actions)} files ===")
    syncer.apply(plan_actions)
    if not skip_link_pass:
        syncer.link_pass()


@main.command()
@click.option("--only", multiple=True, help="Limit to these md paths")
@click.pass_context
def restore_widths(ctx: click.Context, only: tuple[str, ...]) -> None:
    """Re-apply remembered table column widths to existing docs."""
    from larkmd.syncer import Syncer
    cfg = _load_cfg(ctx.obj["config_path"])
    syncer = Syncer(cfg)
    syncer.restore_widths(only=list(only) or None)


@main.command()
@click.option("--apply", "do_apply", is_flag=True, help="Actually delete (default: dry-run)")
@click.pass_context
def cleanup(ctx: click.Context, do_apply: bool) -> None:
    """Delete wiki nodes recorded in previous_wiki_node_tokens (orphans).
    Requires `wiki:wiki` scope on the lark-cli profile."""
    from larkmd.client import Client
    from larkmd.state import load_state
    from larkmd.wiki import cleanup_orphans
    cfg = _load_cfg(ctx.obj["config_path"])
    client = Client(cfg.lark, argv_threshold_bytes=cfg.importer.argv_threshold_bytes)
    state = load_state(cfg.paths.state_file)
    attempted = cleanup_orphans(client, cfg.wiki.space_id, state, apply=do_apply)
    verb = "deleting" if do_apply else "would delete"
    print(f"{verb} {len(attempted)} orphan node(s):")
    for rel, tok in attempted:
        print(f"  {tok}  (from {rel})")


@main.group()
def state() -> None:
    """Inspect or maintain the sync state file."""


@state.command("show")
@click.pass_context
def state_show(ctx: click.Context) -> None:
    """Print state file in pretty form."""
    from larkmd.state import load_state
    cfg = _load_cfg(ctx.obj["config_path"])
    s = load_state(cfg.paths.state_file)
    files = s.get("files", {})
    print(f"schema_version: {s.get('schema_version')}")
    print(f"tenant: {s.get('tenant')}")
    print(f"wiki_space_id: {s.get('wiki_space_id')}")
    print(f"files: {len(files)}")
    for rel in sorted(files):
        info = files[rel]
        print(f"  {rel}")
        print(f"    url: {info.get('url')}")
        print(f"    docx_token: {info.get('docx_token')}")
        print(f"    last_synced: {info.get('last_synced')}")


@state.command("prune")
@click.option("--apply", "do_apply", is_flag=True, help="Actually remove (default: dry-run)")
@click.pass_context
def state_prune(ctx: click.Context, do_apply: bool) -> None:
    """Drop entries from state whose source md no longer exists."""
    from larkmd.state import load_state, save_state
    cfg = _load_cfg(ctx.obj["config_path"])
    s = load_state(cfg.paths.state_file)
    files = s.get("files", {})
    gone = [rel for rel in files if not (cfg.paths.root / rel).exists()]
    verb = "removing" if do_apply else "would remove"
    print(f"{verb} {len(gone)} entries with no source md:")
    for rel in gone:
        print(f"  {rel}")
    if do_apply and gone:
        for rel in gone:
            del files[rel]
        save_state(cfg.paths.state_file, s)


if __name__ == "__main__":
    try:
        main()
    except LarkmdError as e:
        click.echo(f"larkmd: {e}", err=True)
        sys.exit(2)
