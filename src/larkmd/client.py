"""Client — thin wrapper around the `lark-cli` external binary.

Handles:
- `--data` payloads > 100 KB → tempfile + `@file` (lark-cli requires relative path)
- argv-too-long protection (OS argv ~128 KB ceiling)
- error normalization (returncode / empty stdout / non-zero `code` field) → typed exceptions
- KEEP_LARK_TMP_ON_ERROR=1 keeps the payload file on failure for diagnostics
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from larkmd.config import LarkConfig
from larkmd.errors import LarkCliError, SchemaMismatchError


class Client:
    def __init__(self, cfg: LarkConfig, *, argv_threshold_bytes: int = 100_000):
        self.cfg = cfg
        self.argv_threshold = argv_threshold_bytes

    def call(
        self,
        args: list[str],
        *,
        data: dict | None = None,
        params: dict | None = None,
        file: str | None = None,
        cwd: Path | None = None,
        identity: str = "user",
    ) -> dict[str, Any]:
        cmd = [self.cfg.cli_path]
        if self.cfg.profile:
            cmd += ["--profile", self.cfg.profile]
        cmd += args + ["--as", identity]
        if params is not None:
            cmd += ["--params", json.dumps(params)]

        tmp_data_file: Path | None = None
        if data is not None:
            data_json = json.dumps(data)
            if len(data_json) > self.argv_threshold:
                target_dir = cwd if cwd is not None else Path.cwd()
                fd, p = tempfile.mkstemp(suffix=".json", prefix=".lark-data-", dir=str(target_dir))
                os.close(fd)
                tmp_data_file = Path(p)
                tmp_data_file.write_text(data_json)
                cmd += ["--data", f"@{tmp_data_file.name}"]
                if cwd is None:
                    cwd = target_dir
            else:
                cmd += ["--data", data_json]
        if file is not None:
            cmd += ["--file", file]

        r = None
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
        finally:
            if tmp_data_file is not None:
                keep_on_err = os.environ.get("KEEP_LARK_TMP_ON_ERROR") == "1"
                if keep_on_err and (r is None or r.returncode != 0):
                    sys.stderr.write(f"[lark] kept tmp data: {tmp_data_file}\n")
                else:
                    tmp_data_file.unlink(missing_ok=True)

        return self._handle_response(r, cmd)

    def _handle_response(self, r: subprocess.CompletedProcess, cmd: list[str]) -> dict[str, Any]:
        if not r.stdout.strip():
            self._raise_typed(
                f"lark-cli empty stdout (rc={r.returncode}). cmd={' '.join(cmd)}",
                r.returncode, r.stderr, cmd,
            )
        try:
            out = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            raise LarkCliError(
                f"lark-cli non-JSON stdout. cmd={' '.join(cmd)}\nstdout: {r.stdout[:500]}",
                returncode=r.returncode, stderr=r.stderr, cmd=cmd,
            ) from e
        if r.returncode != 0 or out.get("ok") is False or out.get("code", 1) != 0:
            self._raise_typed(
                f"lark-cli error: {json.dumps(out, ensure_ascii=False)}",
                r.returncode, r.stderr, cmd,
            )
        return out

    def _raise_typed(self, msg: str, returncode: int, stderr: str, cmd: list[str]) -> None:
        if "1770041" in stderr or "schema mismatch" in stderr.lower():
            raise SchemaMismatchError(msg, returncode=returncode, stderr=stderr, cmd=cmd)
        raise LarkCliError(msg, returncode=returncode, stderr=stderr, cmd=cmd)
