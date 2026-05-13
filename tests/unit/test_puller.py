"""Tests for Puller — diff/conflict logic against a stub Client.

The Client is mocked at the Python level (no subprocess), so these are pure
unit tests of the planning + apply state-machine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from larkmd.config import (
    Config,
    ImporterConfig,
    LarkConfig,
    MermaidConfig,
    NamingConfig,
    PathsConfig,
    RootFilesConfig,
    WikiConfig,
)
from larkmd.puller import Puller
from larkmd.state import save_state


@dataclass
class StubClient:
    """Mimics Client.call enough for Puller's calls.

    Routes:
      GET /open-apis/docx/v1/documents/{token}            → revision
      GET /open-apis/docx/v1/documents/{token}/blocks     → blocks
    """
    revisions: dict[str, int] = field(default_factory=dict)
    blocks_by_token: dict[str, list[dict]] = field(default_factory=dict)
    cfg: LarkConfig = field(default_factory=lambda: LarkConfig(tenant="t.feishu.cn"))

    def call(self, args, *, data=None, params=None, file=None, cwd=None, identity="user"):
        # args = ["api", "GET", "/open-apis/docx/v1/documents/<tok>[/blocks]"]
        path = args[2]
        if path.endswith("/blocks"):
            tok = path.split("/documents/")[1].split("/")[0]
            return {"data": {"items": list(self.blocks_by_token.get(tok, [])), "has_more": False}}
        # plain doc fetch
        tok = path.split("/documents/")[1]
        return {"data": {"document": {"document_id": tok, "revision_id": self.revisions.get(tok, 1)}}}


def _make_cfg(tmp_path: Path) -> Config:
    return Config(
        version=1,
        lark=LarkConfig(tenant="t.feishu.cn"),
        wiki=WikiConfig(space_id="spc"),
        paths=PathsConfig(root=tmp_path, state_file=tmp_path / ".feishu-sync-state.json"),
        root_files=RootFilesConfig(order=[], drive_folder="folder"),
        sections=[],
        naming=NamingConfig(),
        mermaid=MermaidConfig(),
        importer=ImporterConfig(),
        config_path=tmp_path / "larkmd.yaml",
    )


def _seed_state(cfg: Config, files: dict) -> None:
    state = {
        "schema_version": 1,
        "tenant": cfg.lark.tenant,
        "wiki_space_id": cfg.wiki.space_id,
        "files": files,
    }
    save_state(cfg.paths.state_file, state)


def _block_set(text_content: str = "hi") -> list[dict]:
    return [
        {"block_id": "root", "block_type": 1, "children": ["t1"]},
        {"block_id": "t1", "block_type": 2,
         "text": {"elements": [{"text_run": {"content": text_content, "text_element_style": {}}}]}},
    ]


# ---- planning ----

def test_plan_clean_when_both_unchanged(tmp_path):
    cfg = _make_cfg(tmp_path)
    (tmp_path / "a.md").write_text("local\n")
    from larkmd.state import md_hash
    _seed_state(cfg, {
        "a.md": {
            "docx_token": "DOC1",
            "url": "u",
            "content_hash": md_hash("local\n"),
            "last_remote_revision": 5,
        },
    })
    client = StubClient(revisions={"DOC1": 5})
    puller = Puller(cfg, client=client)
    [a] = puller.plan()
    assert a.kind == "clean"
    assert a.current_revision == 5


def test_plan_remote_only(tmp_path):
    cfg = _make_cfg(tmp_path)
    (tmp_path / "a.md").write_text("local\n")
    from larkmd.state import md_hash
    _seed_state(cfg, {
        "a.md": {
            "docx_token": "DOC1",
            "url": "u",
            "content_hash": md_hash("local\n"),
            "last_remote_revision": 5,
        },
    })
    client = StubClient(revisions={"DOC1": 7})
    puller = Puller(cfg, client=client)
    [a] = puller.plan()
    assert a.kind == "remote-only"


def test_plan_local_only(tmp_path):
    cfg = _make_cfg(tmp_path)
    (tmp_path / "a.md").write_text("EDITED\n")
    from larkmd.state import md_hash
    _seed_state(cfg, {
        "a.md": {
            "docx_token": "DOC1",
            "url": "u",
            "content_hash": md_hash("original\n"),
            "last_remote_revision": 5,
        },
    })
    client = StubClient(revisions={"DOC1": 5})
    puller = Puller(cfg, client=client)
    [a] = puller.plan()
    assert a.kind == "local-only"


def test_plan_conflict(tmp_path):
    cfg = _make_cfg(tmp_path)
    (tmp_path / "a.md").write_text("EDITED\n")
    from larkmd.state import md_hash
    _seed_state(cfg, {
        "a.md": {
            "docx_token": "DOC1",
            "url": "u",
            "content_hash": md_hash("original\n"),
            "last_remote_revision": 5,
        },
    })
    client = StubClient(revisions={"DOC1": 7})
    puller = Puller(cfg, client=client)
    [a] = puller.plan()
    assert a.kind == "conflict"


def test_plan_only_filters(tmp_path):
    cfg = _make_cfg(tmp_path)
    (tmp_path / "a.md").write_text("x\n")
    (tmp_path / "b.md").write_text("y\n")
    from larkmd.state import md_hash
    _seed_state(cfg, {
        "a.md": {"docx_token": "DOC1", "content_hash": md_hash("x\n"), "last_remote_revision": 1},
        "b.md": {"docx_token": "DOC2", "content_hash": md_hash("y\n"), "last_remote_revision": 1},
    })
    client = StubClient(revisions={"DOC1": 1, "DOC2": 1})
    puller = Puller(cfg, client=client)
    actions = puller.plan(only=["a.md"])
    assert [a.rel for a in actions] == ["a.md"]


def test_plan_unknown_path_emits_missing(tmp_path):
    cfg = _make_cfg(tmp_path)
    _seed_state(cfg, {})
    client = StubClient()
    puller = Puller(cfg, client=client)
    actions = puller.plan(only=["nope.md"])
    assert len(actions) == 1
    assert actions[0].kind == "missing"


def test_plan_no_baseline_revision_treated_as_remote_changed(tmp_path):
    cfg = _make_cfg(tmp_path)
    (tmp_path / "a.md").write_text("x\n")
    from larkmd.state import md_hash
    _seed_state(cfg, {
        "a.md": {"docx_token": "DOC1", "content_hash": md_hash("x\n")},  # no last_remote_revision
    })
    client = StubClient(revisions={"DOC1": 1})
    puller = Puller(cfg, client=client)
    [a] = puller.plan()
    assert a.kind == "remote-only"


# ---- apply ----

def test_apply_remote_only_writes_file(tmp_path):
    cfg = _make_cfg(tmp_path)
    (tmp_path / "a.md").write_text("local\n")
    from larkmd.state import md_hash
    _seed_state(cfg, {
        "a.md": {
            "docx_token": "DOC1", "content_hash": md_hash("local\n"),
            "last_remote_revision": 5,
        },
    })
    client = StubClient(revisions={"DOC1": 7}, blocks_by_token={"DOC1": _block_set("from feishu")})
    puller = Puller(cfg, client=client)
    actions = puller.plan()
    results = puller.apply(actions)
    assert results["a.md"] is not None
    assert (tmp_path / "a.md").read_text() == "from feishu\n"
    new_state = json.loads(cfg.paths.state_file.read_text())
    assert new_state["files"]["a.md"]["last_remote_revision"] == 7
    # content_hash should now match the new file
    assert new_state["files"]["a.md"]["content_hash"] == md_hash("from feishu\n")


def test_apply_conflict_aborts_without_force(tmp_path):
    cfg = _make_cfg(tmp_path)
    (tmp_path / "a.md").write_text("EDITED locally\n")
    from larkmd.state import md_hash
    _seed_state(cfg, {
        "a.md": {
            "docx_token": "DOC1", "content_hash": md_hash("original\n"),
            "last_remote_revision": 5,
        },
    })
    client = StubClient(revisions={"DOC1": 7}, blocks_by_token={"DOC1": _block_set("remote")})
    puller = Puller(cfg, client=client)
    actions = puller.plan()
    results = puller.apply(actions)
    assert results["a.md"] is None
    # Local file unchanged
    assert (tmp_path / "a.md").read_text() == "EDITED locally\n"


def test_apply_conflict_force_remote_overwrites(tmp_path):
    cfg = _make_cfg(tmp_path)
    (tmp_path / "a.md").write_text("EDITED locally\n")
    from larkmd.state import md_hash
    _seed_state(cfg, {
        "a.md": {
            "docx_token": "DOC1", "content_hash": md_hash("original\n"),
            "last_remote_revision": 5,
        },
    })
    client = StubClient(revisions={"DOC1": 7}, blocks_by_token={"DOC1": _block_set("remote wins")})
    puller = Puller(cfg, client=client)
    actions = puller.plan()
    results = puller.apply(actions, force_remote=True)
    assert results["a.md"] is not None
    assert (tmp_path / "a.md").read_text() == "remote wins\n"


def test_apply_conflict_force_local_keeps_local_refreshes_revision(tmp_path):
    cfg = _make_cfg(tmp_path)
    (tmp_path / "a.md").write_text("EDITED locally\n")
    from larkmd.state import md_hash
    _seed_state(cfg, {
        "a.md": {
            "docx_token": "DOC1", "content_hash": md_hash("original\n"),
            "last_remote_revision": 5,
        },
    })
    client = StubClient(revisions={"DOC1": 7}, blocks_by_token={"DOC1": _block_set("remote")})
    puller = Puller(cfg, client=client)
    actions = puller.plan()
    puller.apply(actions, force_local=True)
    # Local file still local
    assert (tmp_path / "a.md").read_text() == "EDITED locally\n"
    # Revision baseline refreshed
    new_state = json.loads(cfg.paths.state_file.read_text())
    assert new_state["files"]["a.md"]["last_remote_revision"] == 7
