"""Sync state file (.feishu-sync-state.json) read/write + schema versioning.

Schema v1::

    {
      "schema_version": 1,
      "tenant": "<host>",
      "wiki_space_id": "...",
      "files": {
        "<rel_path>": {
          "docx_token": "...",
          "wiki_node_token": "...",
          "url": "...",
          "content_hash": "<sha256-16>",
          "last_synced": "<iso8601>",
          "table_widths": [[..],..],
          "image_count": int,
          "previous_wiki_node_tokens": [...],
          "previous_docx_tokens": [...],
          "last_remote_revision": null
        }
      }
    }

Legacy schema (no top-level keys, just `{rel_path: {...}}`) is auto-migrated
on first read.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from larkmd.errors import StateIncompatibleError

CURRENT_SCHEMA_VERSION = 1


def md_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": CURRENT_SCHEMA_VERSION, "tenant": None, "wiki_space_id": None, "files": {}}
    raw = json.loads(path.read_text())
    return _migrate(raw)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _migrate(raw: dict) -> dict:
    """Promote legacy `{rel: {...}}` shape to v1 with metadata."""
    if "schema_version" in raw and "files" in raw:
        return raw
    # legacy: top-level keys are rel_paths
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "tenant": None,
        "wiki_space_id": None,
        "files": {k: v for k, v in raw.items() if isinstance(v, dict)},
    }


def verify_compatible(state: dict, *, tenant: str, wiki_space_id: str) -> None:
    """Abort if state was written for a different tenant/space — protects against
    accidentally wiping the wrong workspace when switching configs."""
    state_tenant = state.get("tenant")
    state_space = state.get("wiki_space_id")
    if state_tenant and state_tenant != tenant:
        raise StateIncompatibleError(
            f"state file tenant={state_tenant!r} but config tenant={tenant!r}"
        )
    if state_space and state_space != wiki_space_id:
        raise StateIncompatibleError(
            f"state file wiki_space_id={state_space!r} but config wiki_space_id={wiki_space_id!r}"
        )


def stamp(state: dict, *, tenant: str, wiki_space_id: str) -> None:
    """Set top-level metadata so subsequent runs can verify compatibility."""
    state["schema_version"] = CURRENT_SCHEMA_VERSION
    state["tenant"] = tenant
    state["wiki_space_id"] = wiki_space_id
    state.setdefault("files", {})
