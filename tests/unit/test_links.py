"""Tests for markdown cross-doc link rewriting."""

from __future__ import annotations

from pathlib import Path

import pytest

from larkmd.links import PLACEHOLDER_PREFIX, rewrite_links_to_placeholder


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "01-prep").mkdir()
    (tmp_path / "01-prep" / "checklist.md").write_text("x")
    (tmp_path / "README.md").write_text("x")
    return tmp_path


def test_relative_link_to_placeholder(repo: Path):
    md = "see [checklist](01-prep/checklist.md) for details"
    out = rewrite_links_to_placeholder(md, {}, "README.md", repo)
    assert f"({PLACEHOLDER_PREFIX}01-prep/checklist.md)" in out


def test_relative_link_with_anchor(repo: Path):
    md = "see [section](01-prep/checklist.md#h-8) for details"
    out = rewrite_links_to_placeholder(md, {}, "README.md", repo)
    # anchor is stripped before path resolution; placeholder uses just the path
    assert f"({PLACEHOLDER_PREFIX}01-prep/checklist.md)" in out


def test_sibling_link(repo: Path):
    (repo / "01-prep" / "team.md").write_text("x")
    md = "→ [team](team.md)"
    out = rewrite_links_to_placeholder(md, {}, "01-prep/checklist.md", repo)
    assert f"({PLACEHOLDER_PREFIX}01-prep/team.md)" in out


def test_external_link_unchanged(repo: Path):
    md = "[google](https://google.com)"
    out = rewrite_links_to_placeholder(md, {}, "README.md", repo)
    assert out == md


def test_anchor_only_unchanged(repo: Path):
    md = "[top](#header)"
    out = rewrite_links_to_placeholder(md, {}, "README.md", repo)
    assert out == md


def test_mailto_unchanged(repo: Path):
    md = "[mail](mailto:x@y.com)"
    out = rewrite_links_to_placeholder(md, {}, "README.md", repo)
    assert out == md


def test_link_to_nonexistent_outside_repo(repo: Path):
    """Links resolving outside the repo root should be left as-is."""
    md = "[external](../outside.md)"
    out = rewrite_links_to_placeholder(md, {}, "README.md", repo)
    assert out == md  # bail out, keep original


def test_mapping_uses_real_url(repo: Path):
    md = "[checklist](01-prep/checklist.md)"
    mapping = {"01-prep/checklist.md": "https://my.feishu.cn/wiki/abc"}
    out = rewrite_links_to_placeholder(md, mapping, "README.md", repo)
    assert "https://my.feishu.cn/wiki/abc" in out
    assert PLACEHOLDER_PREFIX not in out
