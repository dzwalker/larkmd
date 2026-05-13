"""Tests for state load/save + legacy migration + tenant guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from larkmd.errors import StateIncompatibleError
from larkmd.state import (
    CURRENT_SCHEMA_VERSION,
    load_state,
    md_hash,
    save_state,
    stamp,
    verify_compatible,
)


def test_md_hash_stable():
    assert md_hash("hello") == md_hash("hello")
    assert md_hash("hello") != md_hash("world")
    assert len(md_hash("any")) == 16


def test_load_missing(tmp_path: Path):
    s = load_state(tmp_path / "nope.json")
    assert s["schema_version"] == CURRENT_SCHEMA_VERSION
    assert s["files"] == {}
    assert s["tenant"] is None


def test_load_legacy_migrates(tmp_path: Path):
    # legacy: top-level keys are rel_paths (no schema_version, tenant, files)
    legacy = {
        "README.md": {"docx_token": "tok1", "url": "https://x/y", "content_hash": "h"},
        "01-prep/checklist.md": {"docx_token": "tok2", "url": "https://x/z", "content_hash": "h2"},
    }
    p = tmp_path / "state.json"
    p.write_text(json.dumps(legacy))
    s = load_state(p)
    assert s["schema_version"] == CURRENT_SCHEMA_VERSION
    assert "README.md" in s["files"]
    assert s["files"]["README.md"]["docx_token"] == "tok1"


def test_save_and_reload(tmp_path: Path):
    p = tmp_path / "state.json"
    s = {"schema_version": 1, "tenant": "t", "wiki_space_id": "w", "files": {"a.md": {"docx_token": "x"}}}
    save_state(p, s)
    loaded = load_state(p)
    assert loaded == s


def test_tenant_mismatch_raises():
    s = {"schema_version": 1, "tenant": "old.feishu.cn", "wiki_space_id": "spc", "files": {}}
    with pytest.raises(StateIncompatibleError, match="tenant"):
        verify_compatible(s, tenant="new.feishu.cn", wiki_space_id="spc")


def test_space_mismatch_raises():
    s = {"schema_version": 1, "tenant": "t", "wiki_space_id": "spc_old", "files": {}}
    with pytest.raises(StateIncompatibleError, match="wiki_space_id"):
        verify_compatible(s, tenant="t", wiki_space_id="spc_new")


def test_first_run_no_tenant_ok():
    """Fresh state (tenant=None) shouldn't trigger guard."""
    s = {"schema_version": 1, "tenant": None, "wiki_space_id": None, "files": {}}
    verify_compatible(s, tenant="t", wiki_space_id="spc")  # no exception


def test_stamp_sets_metadata():
    s = {"files": {}}
    stamp(s, tenant="t", wiki_space_id="spc")
    assert s["tenant"] == "t"
    assert s["wiki_space_id"] == "spc"
    assert s["schema_version"] == CURRENT_SCHEMA_VERSION
