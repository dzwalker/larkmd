"""Tests for config loading + ${VAR} interpolation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from larkmd.config import Config
from larkmd.errors import ConfigError


def _write(p: Path, text: str) -> Path:
    p.write_text(text)
    return p


BASIC_YAML = """\
version: 1
lark:
  tenant: test.feishu.cn
wiki:
  space_id: ${TEST_SPACE_ID}
paths:
  root: .
  state_file: .feishu-sync-state.json
root_files:
  order: [README.md]
  drive_folder: ${TEST_DRIVE_FOLDER}
  wiki_parent: ""
sections:
  - dir: docs
    title_prefix: "01-Docs"
    drive_folder: ${TEST_DRIVE_FOLDER}
    wiki_parent: ${TEST_WIKI_NODE}
    order: []
"""


def test_load_basic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_SPACE_ID", "spc_123")
    monkeypatch.setenv("TEST_DRIVE_FOLDER", "drv_456")
    monkeypatch.setenv("TEST_WIKI_NODE", "node_789")
    cfg_path = _write(tmp_path / "larkmd.yaml", BASIC_YAML)
    cfg = Config.load(cfg_path)
    assert cfg.lark.tenant == "test.feishu.cn"
    assert cfg.wiki.space_id == "spc_123"
    assert cfg.root_files.drive_folder == "drv_456"
    assert cfg.sections[0].dir == "docs"
    assert cfg.sections[0].drive_folder == "drv_456"
    assert cfg.sections[0].wiki_parent == "node_789"


def test_missing_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TEST_SPACE_ID", raising=False)
    monkeypatch.setenv("TEST_DRIVE_FOLDER", "drv_456")
    monkeypatch.setenv("TEST_WIKI_NODE", "node_789")
    cfg_path = _write(tmp_path / "larkmd.yaml", BASIC_YAML)
    with pytest.raises(ConfigError, match="TEST_SPACE_ID"):
        Config.load(cfg_path)


def test_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        Config.load("/nonexistent/larkmd.yaml")


def test_unsupported_version(tmp_path: Path):
    cfg_path = _write(tmp_path / "larkmd.yaml", "version: 99\nlark: {tenant: x}\nwiki: {space_id: x}\nroot_files: {order: [], drive_folder: x}\n")
    with pytest.raises(ConfigError, match="version"):
        Config.load(cfg_path)


def test_display_name_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_SPACE_ID", "x")
    monkeypatch.setenv("TEST_DRIVE_FOLDER", "x")
    monkeypatch.setenv("TEST_WIKI_NODE", "x")
    cfg_path = _write(tmp_path / "larkmd.yaml", BASIC_YAML)
    cfg = Config.load(cfg_path)
    assert cfg.display_name_for("README.md") == "README"


def test_display_name_section_with_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_SPACE_ID", "x")
    monkeypatch.setenv("TEST_DRIVE_FOLDER", "x")
    monkeypatch.setenv("TEST_WIKI_NODE", "x")
    yaml_text = BASIC_YAML.replace("order: []", 'order: ["alpha.md", "beta.md"]')
    cfg_path = _write(tmp_path / "larkmd.yaml", yaml_text)
    cfg = Config.load(cfg_path)
    assert cfg.display_name_for("docs/alpha.md") == "01-Docs-1-alpha"
    assert cfg.display_name_for("docs/beta.md") == "01-Docs-2-beta"
    # not in order → tail
    assert cfg.display_name_for("docs/gamma.md") == "01-Docs-3-gamma"


def test_drive_and_wiki_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_SPACE_ID", "x")
    monkeypatch.setenv("TEST_DRIVE_FOLDER", "drv_456")
    monkeypatch.setenv("TEST_WIKI_NODE", "node_789")
    cfg_path = _write(tmp_path / "larkmd.yaml", BASIC_YAML)
    cfg = Config.load(cfg_path)
    # root file
    assert cfg.drive_folder_for("README.md") == "drv_456"
    assert cfg.wiki_parent_for("README.md") == ""
    # section file
    assert cfg.drive_folder_for("docs/x.md") == "drv_456"
    assert cfg.wiki_parent_for("docs/x.md") == "node_789"
