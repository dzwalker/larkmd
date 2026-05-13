"""Reverse sync orchestration (Feishu → md).

Mirrors `Syncer` for the pull direction:

- `plan(only=...)` → list of PullAction(kind, rel, reason)
    kinds:
      "clean"        — both sides match recorded state; nothing to do
      "remote-only"  — Feishu changed since last sync, local matches → safe pull
      "local-only"   — local edited since last sync, Feishu unchanged → user
                       should `apply` instead; pull skips
      "conflict"     — both sides changed → abort unless --force-remote/--force-local
      "missing"      — file not in state file (no docx_token recorded) → cannot pull

- `apply(plan)` → for each remote-only (or forced) action, fetch blocks, render
    markdown, write the file, refresh state.
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from larkmd.block_to_md import RenderContext, make_link_resolver, render_document
from larkmd.blocks import get_blocks, get_document_revision
from larkmd.client import Client
from larkmd.config import Config
from larkmd.media import download_media
from larkmd.state import (
    load_state,
    md_hash,
    now_iso,
    save_state,
    stamp,
    verify_compatible,
)


@dataclass
class PullAction:
    kind: str  # clean | remote-only | local-only | conflict | missing
    rel: str
    reason: str
    docx_token: str | None = None
    current_revision: int | None = None


@dataclass
class PullResult:
    rel: str
    docx_token: str
    md_written: bool
    bytes_written: int
    images_downloaded: int
    warnings: list[str]


class Puller:
    def __init__(self, cfg: Config, client: Client | None = None):
        self.cfg = cfg
        self.client = client or Client(cfg.lark, argv_threshold_bytes=cfg.importer.argv_threshold_bytes)
        self.state = load_state(cfg.paths.state_file)
        verify_compatible(self.state, tenant=cfg.lark.tenant, wiki_space_id=cfg.wiki.space_id)
        stamp(self.state, tenant=cfg.lark.tenant, wiki_space_id=cfg.wiki.space_id)

    # ----- planning -----

    def plan(self, *, only: Iterable[str] | None = None) -> list[PullAction]:
        files = self.state.get("files") or {}
        if only:
            wanted = list(only)
            unknown = [r for r in wanted if r not in files]
            if unknown:
                # Synthesize "missing" actions so callers see the gap.
                actions = [PullAction(kind="missing", rel=r, reason="not in state") for r in unknown]
            else:
                actions = []
            rels = [r for r in wanted if r in files]
        else:
            actions = []
            rels = sorted(files.keys())

        for rel in rels:
            actions.append(self._diff_one(rel, files[rel]))
        return actions

    def _diff_one(self, rel: str, info: dict) -> PullAction:
        docx_token = info.get("docx_token")
        if not docx_token:
            return PullAction(kind="missing", rel=rel, reason="no docx_token in state")

        local_path = self.cfg.paths.root / rel
        local_changed = False
        if local_path.exists():
            cur_hash = md_hash(local_path.read_text())
            local_changed = (cur_hash != info.get("content_hash"))
        else:
            # Local file gone but state remembers it → treat as local-changed (user
            # may have intentionally deleted; abort to remote-only safe path).
            local_changed = True

        try:
            remote_rev = get_document_revision(self.client, docx_token)
        except Exception as e:
            return PullAction(
                kind="missing", rel=rel,
                reason=f"failed to fetch revision: {e}",
                docx_token=docx_token,
            )

        last_rev = info.get("last_remote_revision")
        # When state never recorded a revision (legacy), treat remote as
        # changed only if the doc has had any block edits — but we can't tell
        # cheaply, so we conservatively assume remote-changed and let user
        # decide via flags.
        if last_rev is None:
            remote_changed = True
            reason_remote = "no baseline revision recorded"
        else:
            remote_changed = (remote_rev != last_rev)
            reason_remote = f"rev {last_rev}→{remote_rev}" if remote_changed else f"rev {remote_rev}"

        if not local_changed and not remote_changed:
            return PullAction(kind="clean", rel=rel, reason=reason_remote,
                              docx_token=docx_token, current_revision=remote_rev)
        if not local_changed and remote_changed:
            return PullAction(kind="remote-only", rel=rel, reason=reason_remote,
                              docx_token=docx_token, current_revision=remote_rev)
        if local_changed and not remote_changed:
            return PullAction(kind="local-only", rel=rel,
                              reason="local hash differs; use `apply` to push",
                              docx_token=docx_token, current_revision=remote_rev)
        return PullAction(
            kind="conflict", rel=rel,
            reason=f"local edited AND {reason_remote}",
            docx_token=docx_token, current_revision=remote_rev,
        )

    # ----- apply -----

    def apply(
        self,
        plan: list[PullAction],
        *,
        force_remote: bool = False,
        force_local: bool = False,
    ) -> dict[str, PullResult | None]:
        results: dict[str, PullResult | None] = {}
        for action in plan:
            should_pull = False
            note = ""
            if action.kind == "clean":
                note = "skip (clean)"
            elif action.kind == "missing":
                note = f"skip ({action.reason})"
            elif action.kind == "remote-only":
                should_pull = True
                note = "pull"
            elif action.kind == "local-only":
                if force_local:
                    note = "skip (local-only); refreshing baseline revision"
                    self._stamp_revision_only(action)
                else:
                    note = "skip (local-only); run `apply` to push"
            elif action.kind == "conflict":
                if force_remote:
                    should_pull = True
                    note = "force-remote: overwriting local"
                elif force_local:
                    note = "force-local: keeping local, refreshing baseline revision"
                    self._stamp_revision_only(action)
                else:
                    note = "ABORT (conflict). Re-run with --force-remote or --force-local"

            print(f"  {action.kind.upper():12s} {action.rel}  ({note})")
            if not should_pull:
                results[action.rel] = None
                continue
            try:
                results[action.rel] = self._pull_one(action)
                save_state(self.cfg.paths.state_file, self.state)
            except Exception:
                sys.stderr.write(f"FAIL {action.rel}:\n{traceback.format_exc()}\n")
                results[action.rel] = None
        return results

    def _pull_one(self, action: PullAction) -> PullResult:
        rel = action.rel
        info = self.state["files"].get(rel) or {}
        docx_token = action.docx_token or info["docx_token"]

        blocks = get_blocks(self.client, docx_token)
        ctx = RenderContext(
            by_id={b["block_id"]: b for b in blocks},
            mermaid_blocks=info.get("mermaid_blocks") or {},
            link_resolver=make_link_resolver(self.state.get("files") or {}),
        )
        md = render_document(blocks, ctx)

        target_path = self.cfg.paths.root / rel
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Download images referenced in md (skip mermaid hits — those don't appear
        # in `downloaded_images`).
        n_imgs = 0
        if ctx.downloaded_images:
            assets_dir = target_path.parent / ctx.image_assets_dir
            for token, rel_in_md in ctx.downloaded_images:
                dest = target_path.parent / rel_in_md
                if dest.exists():
                    continue
                try:
                    download_media(self.client, token, dest)
                    n_imgs += 1
                except Exception as e:
                    ctx.warnings.append(f"image download failed token={token}: {e}")

        target_path.write_text(md)
        info_new = dict(info)
        info_new["docx_token"] = docx_token
        info_new["content_hash"] = md_hash(md)
        info_new["last_synced"] = now_iso()
        info_new["last_remote_revision"] = action.current_revision
        # Preserve url / wiki_node_token / table_widths / mermaid_blocks from prior info.
        self.state["files"][rel] = info_new

        return PullResult(
            rel=rel,
            docx_token=docx_token,
            md_written=True,
            bytes_written=len(md.encode()),
            images_downloaded=n_imgs,
            warnings=list(ctx.warnings),
        )

    def _stamp_revision_only(self, action: PullAction) -> None:
        """For --force-local: keep local md, just refresh `last_remote_revision`
        so future pull-plan stops reporting the same conflict."""
        info = self.state["files"].get(action.rel)
        if not info or action.current_revision is None:
            return
        info["last_remote_revision"] = action.current_revision
        save_state(self.cfg.paths.state_file, self.state)
