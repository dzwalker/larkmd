"""High-level sync orchestration.

The `Syncer` class is the main entry point for both CLI and Python API.
It owns Config + Client + State and exposes:
- `discover()` → list of md files in display order
- `plan(only=...)` → list of Action(kind, rel, reason)
- `apply(plan=...)` → execute, return SyncResult per file
- `link_pass()` → cross-document link rewrite (Pass 2)
- `cleanup_orphans(apply=...)` → orphan wiki node cleanup
- `restore_widths(only=...)` → re-PATCH remembered table widths
"""

from __future__ import annotations

import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from larkmd.blocks import (
    batch_delete_children,
    get_blocks,
    get_document_revision,
    insert_descendants,
    to_descendant_spec,
)
from larkmd.callout_restore import extract_callout_intents, restore_callouts
from larkmd.client import Client
from larkmd.config import Config, SectionConfig
from larkmd.images import patch_image_block, upload_image_media
from larkmd.importer import delete_drive_file, import_md, move_docx_to_wiki
from larkmd.links import patch_all_links, rewrite_links_to_placeholder
from larkmd.mermaid import preprocess_mermaid
from larkmd.sanitize import strip_larkmd_comments
from larkmd.state import (
    load_state,
    md_hash,
    now_iso,
    save_state,
    stamp,
    verify_compatible,
)
from larkmd.tables import (
    extract_table_widths,
    restore_table_widths,
)


@dataclass
class Action:
    kind: str  # "create" | "update" | "skip"
    rel: str
    reason: str
    image_count: int = 0


@dataclass
class SyncResult:
    rel: str
    docx_token: str
    url: str
    wiki_node_token: str | None = None
    table_widths: list[list[int]] = field(default_factory=list)
    image_count: int = 0
    # image_token → mermaid source. Populated when the uploaded PNG was
    # rendered from a fenced mermaid block; used by reverse sync to restore.
    mermaid_blocks: dict[str, str] = field(default_factory=dict)


class Syncer:
    def __init__(self, cfg: Config, client: Client | None = None):
        self.cfg = cfg
        self.client = client or Client(cfg.lark, argv_threshold_bytes=cfg.importer.argv_threshold_bytes)
        self.state = load_state(cfg.paths.state_file)
        verify_compatible(self.state, tenant=cfg.lark.tenant, wiki_space_id=cfg.wiki.space_id)
        stamp(self.state, tenant=cfg.lark.tenant, wiki_space_id=cfg.wiki.space_id)

    # ----- discovery -----

    def discover(self) -> list[Path]:
        """按 root_files.order / sections[*].order 返回 md 文件，未列出的按字母序追加。"""
        root = self.cfg.paths.root
        files: list[Path] = []
        for stem in self.cfg.root_files.order:
            p = root / stem
            if p.exists():
                files.append(p)
        for s in self.cfg.sections:
            sub_dir = root / s.dir
            if not sub_dir.exists():
                continue
            seen: set[str] = set()
            for fname in s.order:
                p = sub_dir / fname
                if p.exists():
                    files.append(p)
                    seen.add(fname)
            for p in sorted(sub_dir.glob("*.md")):
                if p.name not in seen:
                    files.append(p)
        return files

    # ----- planning -----

    def plan(self, *, only: Iterable[str] | None = None, force: bool = False) -> list[Action]:
        files = self.discover()
        if only:
            keep = set(only)
            files = [f for f in files if self._rel(f) in keep]
        actions: list[Action] = []
        for f in files:
            rel = self._rel(f)
            src = f.read_text()
            h = md_hash(src)
            prev = self.state["files"].get(rel, {})
            if not force and prev.get("content_hash") == h and "docx_token" in prev:
                actions.append(Action(kind="skip", rel=rel, reason="hash unchanged"))
                continue
            kind = "update" if "docx_token" in prev else "create"
            reason = "force" if force else ("hash differs" if "docx_token" in prev else "new file")
            actions.append(Action(kind=kind, rel=rel, reason=reason))
        return actions

    # ----- apply -----

    def apply(self, plan: list[Action] | None = None, *, force: bool = False, only: Iterable[str] | None = None) -> dict[str, SyncResult | None]:
        if plan is None:
            plan = self.plan(only=only, force=force)
        results: dict[str, SyncResult | None] = {}
        for action in plan:
            if action.kind == "skip":
                print(f"  SKIP  {action.rel}  ({action.reason})")
                results[action.rel] = None
                continue
            try:
                result = self._sync_one(action)
            except Exception:
                sys.stderr.write(f"FAIL {action.rel}:\n{traceback.format_exc()}\n")
                results[action.rel] = None
                continue
            if result is None:
                results[action.rel] = None
                continue
            self._merge_into_state(action.rel, result)
            save_state(self.cfg.paths.state_file, self.state)
            results[action.rel] = result
        return results

    def _sync_one(self, action: Action) -> SyncResult | None:
        rel = action.rel
        md_file = self.cfg.paths.root / rel
        src = md_file.read_text()
        h = md_hash(src)
        prev = self.state["files"].get(rel, {})

        with tempfile.TemporaryDirectory(prefix="larkmd-") as td:
            tdpath = Path(td)
            munged = tdpath / md_file.name
            # Order matters: extract callouts first so they survive as
            # placeholder paragraphs; only THEN strip the rest.
            cleaned, callout_intents = extract_callout_intents(src)
            cleaned = strip_larkmd_comments(cleaned)
            munged.write_text(rewrite_links_to_placeholder(cleaned, {}, rel, self.cfg.paths.root))
            rendered_md, mermaid_pairs = preprocess_mermaid(munged, tdpath, self.cfg.mermaid)
            pngs = [p for _, p in mermaid_pairs]
            sources = [s for s, _ in mermaid_pairs]

            from PIL import Image
            sizes = {p.name: Image.open(p).size for p in pngs}

            folder = self.cfg.drive_folder_for(rel)

            if action.kind == "update":
                docx_token = prev["docx_token"]
                print(f"  UPDT  {rel}  → docx {docx_token} (in place)")
                new_image_block_ids, table_widths = self._update_in_place(docx_token, rendered_md, folder)
                mermaid_blocks = self._upload_and_patch_images(
                    docx_token, pngs, new_image_block_ids, sizes, rel, sources=sources,
                )
                if callout_intents:
                    n = restore_callouts(self.client, docx_token, callout_intents)
                    if n:
                        print(f"  CALL  {rel}  restored {n}/{len(callout_intents)} callout(s)")
                return SyncResult(
                    rel=rel,
                    docx_token=docx_token,
                    url=prev["url"],
                    wiki_node_token=prev.get("wiki_node_token"),
                    table_widths=table_widths,
                    image_count=len(pngs),
                    mermaid_blocks=mermaid_blocks,
                )

            # create
            name = self.cfg.display_name_for(rel)
            data = import_md(
                rendered_md, folder, name,
                cli_path=self.cfg.lark.cli_path, profile=self.cfg.lark.profile,
            )
            docx_token = data["token"]
            print(f"  IMPT  {rel}  → docx {docx_token}")

            mermaid_blocks: dict[str, str] = {}
            if pngs:
                blocks = get_blocks(self.client, docx_token)
                image_blocks = [b for b in blocks if b.get("block_type") == 27]
                if len(image_blocks) != len(pngs):
                    sys.stderr.write(
                        f"  warn: {rel} 有 {len(pngs)} PNG 但 docx 有 {len(image_blocks)} image block\n")
                for i, (png, block) in enumerate(zip(pngs, image_blocks)):
                    w, h_px = sizes[png.name]
                    file_token = upload_image_media(self.client, png, block["block_id"])
                    patch_image_block(self.client, docx_token, block["block_id"], file_token, w, h_px)
                    if i < len(sources):
                        mermaid_blocks[file_token] = sources[i]

            if callout_intents:
                n = restore_callouts(self.client, docx_token, callout_intents)
                if n:
                    print(f"  CALL  {rel}  restored {n}/{len(callout_intents)} callout(s)")

            parent_wiki = self.cfg.wiki_parent_for(rel)
            node_token = move_docx_to_wiki(
                self.client, self.cfg.wiki.space_id, docx_token, parent_wiki, cfg=self.cfg.importer,
            )
            wiki_url = f"https://{self.cfg.lark.tenant}/wiki/{node_token}"
            print(f"  PUSH  {rel}  → {wiki_url}")
            return SyncResult(
                rel=rel,
                docx_token=docx_token,
                url=wiki_url,
                wiki_node_token=node_token,
                image_count=len(pngs),
                mermaid_blocks=mermaid_blocks,
            )

    # ----- in-place update (insert-then-delete) -----

    def _update_in_place(self, old_docx_token: str, md_path: Path, scratch_folder: str) -> tuple[list[str], list[list[int]]]:
        """事务安全更新：insert-then-delete。失败时老 docx 不会留空。"""
        old_blocks_pre = get_blocks(self.client, old_docx_token)
        old_root = next(b for b in old_blocks_pre if b.get("block_type") == 1)
        old_children_count = len(old_root.get("children", []))
        saved_widths = extract_table_widths(old_blocks_pre)

        temp_data = import_md(
            md_path, scratch_folder, md_path.stem + "-tmp",
            cli_path=self.cfg.lark.cli_path, profile=self.cfg.lark.profile,
        )
        temp_token = temp_data["token"]

        try:
            temp_blocks = get_blocks(self.client, temp_token)
            temp_root = next(b for b in temp_blocks if b.get("block_type") == 1)
            children_id = list(temp_root.get("children", []))
            descendants = [
                to_descendant_spec(b) for b in temp_blocks
                if b["block_id"] != temp_root["block_id"]
            ]
            new_count = len(children_id)

            id_map = insert_descendants(
                self.client, old_docx_token, old_root["block_id"], children_id, descendants, index=0,
            )

            refreshed = get_blocks(self.client, old_docx_token)
            new_root = next(b for b in refreshed if b.get("block_type") == 1)
            actual_count = len(new_root.get("children", []))
            if actual_count < old_children_count + new_count:
                raise RuntimeError(
                    f"insert verification failed: expected ≥ {old_children_count + new_count} "
                    f"children after insert, got {actual_count}. 老内容仍在后段，未删旧 children。"
                )

            if old_children_count > 0:
                batch_delete_children(
                    self.client, old_docx_token, old_root["block_id"],
                    old_children_count, start_index=new_count,
                )

            applied_widths: list[list[int]] = []
            if saved_widths:
                after_delete = get_blocks(self.client, old_docx_token)
                try:
                    restore_table_widths(self.client, old_docx_token, after_delete, saved_widths)
                    applied_widths = saved_widths
                except Exception as e:
                    sys.stderr.write(f"  warn: restore table widths failed: {e}\n")

            new_image_ids: list[str] = []
            if isinstance(id_map, dict) and id_map:
                for b in temp_blocks:
                    if b.get("block_type") == 27:
                        real = id_map.get(b["block_id"])
                        if real:
                            new_image_ids.append(real)
            if not new_image_ids:
                new_image_ids = [b["block_id"] for b in refreshed if b.get("block_type") == 27]

            return new_image_ids, applied_widths
        finally:
            delete_drive_file(self.client, temp_token)

    def _upload_and_patch_images(
        self, docx_token, pngs, new_image_block_ids, sizes, rel,
        *, sources: list[str] | None = None,
    ) -> dict[str, str]:
        """Upload PNGs into image blocks; return mapping image_token → mermaid source
        (only for entries where a source is provided, in the same order as pngs)."""
        if not pngs:
            return {}
        if len(new_image_block_ids) != len(pngs):
            sys.stderr.write(
                f"  warn: {rel} 有 {len(pngs)} PNG 但找到 {len(new_image_block_ids)} 个 image block\n")
        token_to_source: dict[str, str] = {}
        for i, (png, block_id) in enumerate(zip(pngs, new_image_block_ids)):
            w, h_px = sizes[png.name]
            file_token = upload_image_media(self.client, png, block_id)
            patch_image_block(self.client, docx_token, block_id, file_token, w, h_px)
            if sources and i < len(sources):
                token_to_source[file_token] = sources[i]
        return token_to_source

    # ----- link pass -----

    def link_pass(self) -> dict[str, int]:
        """Pass 2: rewrite cross-doc links in every synced doc."""
        files = self.state["files"]
        mapping = {p: v["url"] for p, v in files.items() if "url" in v}
        token_to_url: dict[str, str] = {}
        for v in files.values():
            url = v.get("url")
            if not url:
                continue
            for tk in ("docx_token", "wiki_node_token"):
                if tk in v:
                    token_to_url[v[tk]] = url
            for t in v.get("previous_docx_tokens", []):
                token_to_url[t] = url
            for t in v.get("previous_wiki_node_tokens", []):
                token_to_url[t] = url

        print(f"\n=== Pass 2: patch cross-doc links ({len(mapping)} docs / {len(token_to_url)} aliases) ===")
        result: dict[str, int] = {}
        for rel, info in files.items():
            token = info.get("docx_token")
            if not token:
                continue
            try:
                n = patch_all_links(self.client, token, mapping, token_to_url)
                print(f"  LINK  {rel}  patched {n} blocks")
                result[rel] = n
            except Exception as e:
                sys.stderr.write(f"link-patch fail {rel}: {e}\n")
        return result

    # ----- restore widths -----

    def restore_widths(self, *, only: Iterable[str] | None = None) -> dict[str, int]:
        """Re-PATCH remembered table widths (from state.table_widths)."""
        files = self.state["files"]
        keep = set(only) if only else None
        result: dict[str, int] = {}
        for rel, info in files.items():
            if keep and rel not in keep:
                continue
            saved = info.get("table_widths") or []
            if not saved:
                continue
            token = info["docx_token"]
            blocks = get_blocks(self.client, token)
            n = restore_table_widths(self.client, token, blocks, saved)
            print(f"  WIDE  {rel}  patched {n} columns")
            result[rel] = n
        return result

    # ----- helpers -----

    def _rel(self, f: Path) -> str:
        return f.relative_to(self.cfg.paths.root).as_posix()

    def _merge_into_state(self, rel: str, result: SyncResult) -> None:
        prev = self.state["files"].get(rel, {})
        # Refresh remote-revision baseline so future `pull-plan` correctly
        # treats this push as the new agreed-upon state. Best-effort: skip on
        # API hiccup so a successful push isn't undone by a stat-only failure.
        try:
            new_revision = get_document_revision(self.client, result.docx_token)
        except Exception as e:
            sys.stderr.write(f"  warn: revision fetch failed for {rel}: {e}\n")
            new_revision = prev.get("last_remote_revision")
        new = {
            "docx_token": result.docx_token,
            "url": result.url,
            "content_hash": md_hash((self.cfg.paths.root / rel).read_text()),
            "last_synced": now_iso(),
            "image_count": result.image_count,
            "table_widths": result.table_widths,
            "last_remote_revision": new_revision,
        }
        if result.wiki_node_token:
            new["wiki_node_token"] = result.wiki_node_token
        for k in ("previous_wiki_node_tokens", "previous_docx_tokens"):
            if k in prev:
                new[k] = prev[k]
        # Mermaid mapping is rebuilt every push (re-import generates fresh
        # image_tokens). If this push had no mermaid blocks, drop any stale
        # mapping from the prior push.
        if result.mermaid_blocks:
            new["mermaid_blocks"] = result.mermaid_blocks
        self.state["files"][rel] = new
