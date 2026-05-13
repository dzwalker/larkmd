"""Download Feishu media (images) to local files.

The forward sync uploads PNGs via `drive/v1/medias/upload_all`. The reverse
path is `drive/v1/medias/{file_token}/download`, which streams binary back.

`lark-cli api GET` doesn't naturally handle binary, so we shell out to the
`download` subcommand it exposes; if that's unavailable we fall back to a
direct HTTP GET using the tenant access token (out of scope for Phase A).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from larkmd.client import Client
from larkmd.errors import LarkCliError


def download_media(client: Client, file_token: str, dest: Path) -> Path:
    """Download `file_token` to `dest` (creating parent dirs). Returns dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # lark-cli `drive download` accepts --file-token + --output.
    # If the binary doesn't have it (older versions), error surfaces with stderr.
    cmd = [
        client.cfg.cli_path, "drive", "download",
        "--file-token", file_token,
        "--output", str(dest),
        "--as", "user",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not dest.exists():
        raise LarkCliError(
            f"download_media failed for token={file_token}: {r.stderr.strip() or 'no output'}",
            returncode=r.returncode, stderr=r.stderr, cmd=cmd,
        )
    return dest
