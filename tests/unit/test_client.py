"""Tests for Client argv-threshold @file fallback (without invoking real lark-cli)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from larkmd.client import Client
from larkmd.config import LarkConfig
from larkmd.errors import LarkCliError, SchemaMismatchError


def _make_completed(stdout: str = '{"ok": true, "code": 0, "data": {}}', stderr: str = "", rc: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def test_small_payload_inline_arg():
    """Small data goes via --data <inline JSON>."""
    cfg = LarkConfig(tenant="x.feishu.cn")
    client = Client(cfg, argv_threshold_bytes=1000)
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["cwd"] = kw.get("cwd")
        return _make_completed()

    with patch("subprocess.run", side_effect=fake_run):
        client.call(["api", "POST", "/x"], data={"key": "value"})
    assert "--data" in captured["cmd"]
    idx = captured["cmd"].index("--data")
    val = captured["cmd"][idx + 1]
    assert not val.startswith("@")
    assert json.loads(val) == {"key": "value"}


def test_large_payload_uses_at_file(tmp_path: Path):
    """Payload over threshold spills to a tempfile in cwd, passed as @relative-name."""
    cfg = LarkConfig(tenant="x.feishu.cn")
    client = Client(cfg, argv_threshold_bytes=100)
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["cwd"] = kw.get("cwd")
        # while still running, the tempfile must exist in cwd
        for arg in cmd:
            if arg.startswith("@"):
                tmp_name = arg[1:]
                assert "/" not in tmp_name, "tmp file must be a relative name (lark-cli quirk)"
                assert (Path(kw["cwd"]) / tmp_name).exists()
        return _make_completed()

    big_data = {"k": "x" * 500}
    with patch("subprocess.run", side_effect=fake_run):
        client.call(["api", "POST", "/x"], data=big_data, cwd=tmp_path)
    assert any(a.startswith("@") for a in captured["cmd"])
    assert captured["cwd"] == tmp_path
    # tempfile cleaned up after success
    assert not list(tmp_path.glob(".lark-data-*.json"))


def test_schema_mismatch_typed_exception():
    cfg = LarkConfig(tenant="x.feishu.cn")
    client = Client(cfg)
    stderr = '{"error": {"code": 1770041, "message": "open schema mismatch"}}'

    with patch("subprocess.run", return_value=_make_completed(stdout="", stderr=stderr, rc=1)):
        with pytest.raises(SchemaMismatchError):
            client.call(["api", "POST", "/x"], data={})


def test_generic_lark_error():
    cfg = LarkConfig(tenant="x.feishu.cn")
    client = Client(cfg)
    with patch("subprocess.run", return_value=_make_completed(stdout="", stderr="boom", rc=1)):
        with pytest.raises(LarkCliError) as ei:
            client.call(["api", "POST", "/x"], data={})
        assert not isinstance(ei.value, SchemaMismatchError)


def test_non_zero_code_in_response_raises():
    cfg = LarkConfig(tenant="x.feishu.cn")
    client = Client(cfg)
    body = '{"ok": false, "code": 99, "msg": "denied"}'
    with patch("subprocess.run", return_value=_make_completed(stdout=body, stderr="", rc=0)):
        with pytest.raises(LarkCliError):
            client.call(["api", "GET", "/x"])
