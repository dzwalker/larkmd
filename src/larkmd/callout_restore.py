"""Restore Feishu callout (block_type 19) blocks during push.

Reverse sync writes callouts as a marker comment + a markdown quote:

    <!-- larkmd:callout emoji=fire bg=1 -->
    > hot tip

If we let that round-trip through `drive +import` as-is, Feishu's importer
keeps the quote (block_type 15) and either drops or literalises the HTML
comment. Either way the original callout type is lost.

This module restores it via a two-phase trick:

1. **Pre-import** (`extract_callout_intents`): replace each marker with a
   unique placeholder paragraph (`LARKMD_CALLOUT_PLACEHOLDER_<N>`). The
   placeholder is plain ASCII text so it survives import as a normal
   paragraph block we can locate by content. The quote that follows is left
   untouched so its content imports normally.

2. **Post-import** (`restore_callouts`): walk the docx, find each placeholder
   paragraph, claim the next sibling (must be a quote), and replace the pair
   with a real callout block whose `children` carry the quote's content
   (the quote's own elements become the callout's first child paragraph;
   any nested children are adopted).

If a placeholder can't be found or the next sibling isn't a quote (e.g. the
user reordered things between pull and push), the marker is silently
skipped — the placeholder paragraph is left in the doc as a visible breadcrumb
so the user can see what happened. We never delete content we can't replace.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

from larkmd.blocks import (
    batch_delete_children,
    get_blocks,
    insert_descendants,
    to_descendant_spec,
)
from larkmd.client import Client

PLACEHOLDER_PREFIX = "LARKMD_CALLOUT_PLACEHOLDER_"

# Match `<!-- larkmd:callout [attrs] -->` plus its trailing newline so the
# placeholder lands cleanly on its own line and the quote stays where it was.
_CALLOUT_MARKER_RE = re.compile(
    r"<!--\s*larkmd:callout(?:\s+([^>]*?))?\s*-->\s*\n?",
)


@dataclass
class CalloutIntent:
    """A callout we'll try to reconstitute after import.

    `index` is the source-order serial used in the placeholder text so each
    intent maps to exactly one paragraph block in the imported docx.
    """
    index: int
    emoji_id: str | None = None
    background_color: int | None = None
    border_color: int | None = None
    text_color: int | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def placeholder_text(self) -> str:
        return f"{PLACEHOLDER_PREFIX}{self.index}"

    def callout_props(self) -> dict:
        out: dict = {}
        if self.emoji_id:
            out["emoji_id"] = self.emoji_id
        if self.background_color is not None:
            out["background_color"] = self.background_color
        if self.border_color is not None:
            out["border_color"] = self.border_color
        if self.text_color is not None:
            out["text_color"] = self.text_color
        return out


def _parse_attrs(s: str) -> dict[str, str]:
    """Parse `emoji=fire bg=1 border=2` → dict (whitespace-separated)."""
    out: dict[str, str] = {}
    for tok in s.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _maybe_int(s: str | None) -> int | None:
    if s is None:
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def extract_callout_intents(md: str) -> tuple[str, list[CalloutIntent]]:
    """Find every `<!-- larkmd:callout ... -->` marker in `md` and replace it
    with a placeholder paragraph. Returns (modified_md, intents in order)."""
    intents: list[CalloutIntent] = []

    def repl(m: re.Match) -> str:
        idx = len(intents)
        attrs = _parse_attrs(m.group(1) or "")
        intent = CalloutIntent(
            index=idx,
            emoji_id=attrs.pop("emoji", None) or None,
            background_color=_maybe_int(attrs.pop("bg", None)),
            border_color=_maybe_int(attrs.pop("border", None)),
            text_color=_maybe_int(attrs.pop("color", None)),
            extra=attrs,
        )
        intents.append(intent)
        return f"{intent.placeholder_text}\n"

    new_md = _CALLOUT_MARKER_RE.sub(repl, md)
    return new_md, intents


# ----- post-import restoration -----

def restore_callouts(
    client: Client,
    docx_token: str,
    intents: list[CalloutIntent],
) -> int:
    """For each intent, locate placeholder + quote and replace with a callout.
    Returns the number successfully restored. Failures are logged to stderr but
    never raise — partial restoration is preferred over an aborted push."""
    restored = 0
    for intent in intents:
        try:
            if _restore_one(client, docx_token, intent):
                restored += 1
        except Exception as e:
            sys.stderr.write(f"  warn: restore callout #{intent.index} failed: {e}\n")
    return restored


def _restore_one(client: Client, docx_token: str, intent: CalloutIntent) -> bool:
    """Single intent. We re-fetch blocks per call to dodge index-shift bugs;
    docs with many callouts pay an O(N) cost but stay correct."""
    blocks = get_blocks(client, docx_token)
    by_id = {b["block_id"]: b for b in blocks}
    root = next((b for b in blocks if b.get("block_type") == 1), None)
    if root is None:
        return False
    children_ids = list(root.get("children") or [])

    ph_idx = _find_placeholder_index(children_ids, by_id, intent.placeholder_text)
    if ph_idx is None:
        return False

    if ph_idx + 1 >= len(children_ids):
        return False
    quote_id = children_ids[ph_idx + 1]
    quote = by_id.get(quote_id)
    if not quote or quote.get("block_type") != 15:
        return False

    callout_ph = f"_co_{intent.index}"
    children_phs, descendants = _build_callout_children(intent, quote, by_id)
    descendants.insert(0, {
        "block_id": callout_ph,
        "block_type": 19,
        "callout": intent.callout_props(),
        "children": children_phs,
    })

    # Insert callout at the placeholder's index — pushes placeholder + quote
    # down by one. We then delete those two, leaving net +1 callout, -2 old.
    insert_descendants(
        client, docx_token, root["block_id"],
        children_id=[callout_ph],
        descendants=descendants,
        index=ph_idx,
    )

    # The placeholder + quote are now at ph_idx+1 and ph_idx+2 (callout pushed
    # them down by one). Verify with a refetch in case Feishu reordered, then
    # delete the contiguous pair.
    refreshed = get_blocks(client, docx_token)
    refresh_root = next((b for b in refreshed if b.get("block_type") == 1), None)
    if refresh_root is None:
        return False
    new_children = list(refresh_root.get("children") or [])
    refresh_by_id = {b["block_id"]: b for b in refreshed}
    new_ph_idx = _find_placeholder_index(new_children, refresh_by_id, intent.placeholder_text)
    if new_ph_idx is None:
        # Already gone — nothing to clean. Treat as success.
        return True
    if new_ph_idx + 1 < len(new_children) and new_children[new_ph_idx + 1] == quote_id:
        batch_delete_children(
            client, docx_token, root["block_id"],
            count=2, start_index=new_ph_idx,
        )
    else:
        # Fallback: delete just the placeholder; leaving the quote is harmless
        # (worst case the user sees a duplicate quote next to the new callout).
        batch_delete_children(
            client, docx_token, root["block_id"],
            count=1, start_index=new_ph_idx,
        )
    return True


def _find_placeholder_index(
    children_ids: list[str], by_id: dict[str, dict], target_text: str,
) -> int | None:
    for i, cid in enumerate(children_ids):
        b = by_id.get(cid)
        if not b or b.get("block_type") != 2:
            continue
        if _paragraph_text(b) == target_text:
            return i
    return None


def _paragraph_text(block: dict) -> str:
    return "".join(
        e.get("text_run", {}).get("content", "")
        for e in (block.get("text", {}).get("elements") or [])
    )


def _build_callout_children(
    intent: CalloutIntent,
    quote: dict,
    by_id: dict[str, dict],
) -> tuple[list[str], list[dict]]:
    """Construct callout children + descendant specs from a quote block.

    The quote's own `quote.elements` text becomes the callout's first child
    paragraph. The quote's existing nested `children` (lists, sub-paragraphs)
    are deep-cloned as additional callout children.
    """
    children_phs: list[str] = []
    descendants: list[dict] = []

    quote_elements = (quote.get("quote") or {}).get("elements") or []
    if quote_elements:
        para_ph = f"_co_p_{intent.index}"
        children_phs.append(para_ph)
        descendants.append({
            "block_id": para_ph,
            "block_type": 2,
            "text": {"elements": quote_elements},
        })

    for sub_id in quote.get("children") or []:
        sub = by_id.get(sub_id)
        if sub is None:
            continue
        children_phs.append(sub_id)
        descendants.extend(_clone_subtree(sub_id, by_id))

    return children_phs, descendants


def _clone_subtree(root_id: str, by_id: dict[str, dict]) -> list[dict]:
    """Pre-order deep clone — every block becomes a descendant spec, in the
    order Feishu's descendant API expects (parent before child)."""
    out: list[dict] = []
    block = by_id.get(root_id)
    if block is None:
        return out
    out.append(to_descendant_spec(block))
    for cid in block.get("children") or []:
        out.extend(_clone_subtree(cid, by_id))
    return out
