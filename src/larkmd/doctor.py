"""Environment self-check for `larkmd doctor`."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from larkmd.config import Config

# match a markdown line that has 2+ links — lark-cli quirk: only the first survives
MULTI_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\).*\[[^\]]+\]\([^)]+\)")


def check_lark_cli(cfg: Config) -> tuple[bool, str]:
    path = shutil.which(cfg.lark.cli_path)
    if not path:
        return False, f"`{cfg.lark.cli_path}` not on PATH (npm i -g @larksuiteoapi/lark-cli)"
    try:
        r = subprocess.run([cfg.lark.cli_path, "--version"], capture_output=True, text=True, timeout=5)
        return True, f"{path} ({r.stdout.strip() or r.stderr.strip()})"
    except Exception as e:
        return False, f"{path} but --version failed: {e}"


def check_lark_login(cfg: Config) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            [cfg.lark.cli_path, "api", "GET", "/open-apis/authen/v1/user_info", "--as", "user"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return True, "logged in"
        return False, f"not logged in (run `lark-cli login --as user`); stderr: {r.stderr[:200]}"
    except Exception as e:
        return False, str(e)


def check_mmdc(cfg: Config) -> tuple[bool, str]:
    if not cfg.mermaid.enabled:
        return True, "(disabled in config)"
    p = shutil.which(cfg.mermaid.mmdc_path)
    if not p:
        return False, f"`{cfg.mermaid.mmdc_path}` not on PATH (npm i -g @mermaid-js/mermaid-cli) — mermaid blocks will fall through as plain text"
    return True, p


def check_pillow() -> tuple[bool, str]:
    try:
        from PIL import Image  # noqa: F401
        return True, "ok"
    except ImportError:
        return False, "Pillow not installed (pip install Pillow)"


def check_env_vars(cfg: Config) -> list[tuple[bool, str]]:
    """Find ${VAR} references in the raw config file and confirm they're all set in env."""
    raw = cfg.config_path.read_text()
    refs = set(re.findall(r"\$\{([A-Z0-9_]+)\}", raw))
    out = []
    for v in sorted(refs):
        if os.environ.get(v):
            out.append((True, f"${{{v}}}"))
        else:
            out.append((False, f"${{{v}}} not set"))
    return out


def scan_multi_link_lines(cfg: Config) -> list[tuple[Path, int, str]]:
    """Find md lines with 2+ links — lark-cli silently drops all but the first."""
    hits: list[tuple[Path, int, str]] = []
    root = cfg.paths.root
    for p in root.rglob("*.md"):
        # skip ignored
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        try:
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if MULTI_LINK_RE.search(line):
                    hits.append((p, n, line.strip()[:120]))
        except Exception:
            continue
    return hits


def run_doctor(cfg: Config) -> int:
    """Returns process exit code (0=all ok, 1=fatal, 2=warnings)."""
    fatal = 0
    warn = 0

    def line(ok: bool, label: str, msg: str) -> None:
        nonlocal fatal, warn
        if ok:
            print(f"  ✓ {label}: {msg}")
        else:
            print(f"  ✗ {label}: {msg}")
            fatal += 1

    print("=== larkmd doctor ===\n")
    print(f"config: {cfg.config_path}")
    print(f"tenant: {cfg.lark.tenant}")
    print(f"wiki space: {cfg.wiki.space_id}\n")

    print("Runtime:")
    ok, msg = check_lark_cli(cfg)
    line(ok, "lark-cli", msg)
    ok, msg = check_lark_login(cfg)
    line(ok, "lark-cli auth", msg)
    ok, msg = check_mmdc(cfg)
    line(ok, "mmdc", msg)
    ok, msg = check_pillow()
    line(ok, "Pillow", msg)

    print("\nEnv vars referenced in config:")
    for ok, msg in check_env_vars(cfg):
        line(ok, "env", msg)

    print("\nMarkdown lint (multi-link per line — lark-cli quirk):")
    hits = scan_multi_link_lines(cfg)
    if not hits:
        print("  ✓ no offending lines")
    else:
        warn += len(hits)
        for p, n, snippet in hits[:20]:
            rel = p.relative_to(cfg.paths.root).as_posix()
            print(f"  ⚠ {rel}:{n}: {snippet}")
        if len(hits) > 20:
            print(f"  ⚠ ... and {len(hits) - 20} more")

    print()
    if fatal:
        print(f"FAIL: {fatal} fatal issue(s)")
        return 1
    if warn:
        print(f"OK with {warn} warning(s)")
        return 2
    print("All good.")
    return 0
