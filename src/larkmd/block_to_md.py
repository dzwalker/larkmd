"""Block tree → markdown renderer (reverse of import_md).

Walks a Feishu docx block list (as returned by `blocks.get_blocks`) and
produces a markdown string that round-trips reasonably with the forward
sync path.

Block-type coverage (Phase A):
  1   Page (root, never rendered as a block — only its children)
  2   Text paragraph
  3-11 Heading 1-9
  12  Bullet list item
  13  Ordered list item
  14  Code
  15  Quote container
  16  Equation (block-level)
  17  Todo
  19  Callout                (degraded to blockquote + HTML comment)
  22  Divider
  23  File                   (degraded to link + HTML comment)
  24  Iframe                 (degraded to link + HTML comment)
  27  Image                  (mermaid restore via state, else `![](.assets/...)`)
  28  Sheet embed            (degraded)
  30  Bookmark               (degraded to link)
  31  Table
  32  Bitable embed          (degraded)
  33  Sync block             (children rendered transparently)

Anything else: HTML comment placeholder so the user sees the gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import unquote, urlparse

from larkmd.inline import render_elements


@dataclass
class RenderContext:
    """Shared state passed down the tree walker."""
    by_id: dict[str, dict]                       # block_id -> block
    mermaid_blocks: dict[str, str] = field(default_factory=dict)  # image_token -> source
    image_assets_dir: str = ".assets"            # relative dir to write `![](...)` paths against
    downloaded_images: list[tuple[str, str]] = field(default_factory=list)  # (image_token, local_rel)
    link_resolver: Callable[[str], str | None] | None = None
    warnings: list[str] = field(default_factory=list)


# Heading 1..9 → block_type 3..11
_HEADING_TYPES = {3 + i: i + 1 for i in range(9)}


def render_document(blocks: list[dict], ctx: RenderContext) -> str:
    """Render the page block's children as markdown. Returns a single string,
    no trailing newline normalization (caller decides)."""
    root = next((b for b in blocks if b.get("block_type") == 1), None)
    if root is None:
        # No Page block? Render in document order anyway.
        root_children_ids = [b["block_id"] for b in blocks]
    else:
        root_children_ids = list(root.get("children") or [])

    parts = _render_children(root_children_ids, ctx, indent="", ordered_index=None)
    return "\n".join(parts).rstrip() + "\n" if parts else ""


def _render_children(
    children_ids: list[str],
    ctx: RenderContext,
    *,
    indent: str,
    ordered_index: list[int] | None = None,
) -> list[str]:
    """Render an ordered list of child block_ids into a list of markdown
    "paragraphs" (each entry typically becomes its own line group). Blank lines
    between block-level constructs are inserted by the caller via `\n` join.

    Maintains a local counter for consecutive ordered-list items so they emit
    1., 2., 3. ... A non-ordered block resets it.
    """
    out: list[str] = []
    prev_kind: str | None = None
    counter: list[int] = [0]  # mutable so _render_block can bump it
    for cid in children_ids:
        block = ctx.by_id.get(cid)
        if block is None:
            continue
        bt = block.get("block_type")
        if bt != 13:
            counter[0] = 0  # break the run
        kind, text = _render_block(block, ctx, indent=indent, ordered_index=counter)
        if not text and kind != "blank":
            continue
        if out:
            tight = (prev_kind in ("bullet", "ordered", "todo") and kind in ("bullet", "ordered", "todo"))
            if not tight:
                out.append("")
        out.append(text)
        prev_kind = kind
    return out


def _render_block(
    block: dict,
    ctx: RenderContext,
    *,
    indent: str,
    ordered_index: list[int] | None,
) -> tuple[str, str]:
    """Render a single block. Returns (kind, text). `kind` is a coarse tag
    used by the caller for spacing decisions. `text` may contain newlines.
    Returns ("blank", "") for blocks that emit nothing."""
    bt = block.get("block_type")

    if bt == 2:
        return "paragraph", indent + _inline(block.get("text"), ctx)

    if bt in _HEADING_TYPES:
        level = _HEADING_TYPES[bt]
        text = _inline(block.get(f"heading{level}"), ctx)
        return "heading", f"{'#' * level} {text}"

    if bt == 12:
        return _render_list_item(block, ctx, indent=indent, marker="- ")

    if bt == 13:
        if ordered_index is None:
            ordered_index = [0]
        ordered_index[0] += 1
        n = ordered_index[0]
        return _render_list_item(block, ctx, indent=indent, marker=f"{n}. ")

    if bt == 17:
        done = (block.get("todo") or {}).get("style", {}).get("done")
        marker = "- [x] " if done else "- [ ] "
        return _render_list_item(block, ctx, indent=indent, marker=marker)

    if bt == 14:
        return "code", _render_code(block, ctx, indent=indent)

    if bt == 15:
        return "quote", _render_quote_container(block, ctx, indent=indent)

    if bt == 16:
        eq = (block.get("equation") or {}).get("elements") or []
        content = render_elements(eq).rstrip("\n")
        return "equation", f"$$\n{content}\n$$"

    if bt == 19:
        return "callout", _render_callout(block, ctx, indent=indent)

    if bt == 22:
        return "divider", "---"

    if bt == 23:
        return "paragraph", _render_file(block)

    if bt == 24:
        return "paragraph", _render_iframe(block)

    if bt == 27:
        return "paragraph", _render_image(block, ctx)

    if bt == 28:
        return "paragraph", _render_embed(block, "sheet", "sheet")

    if bt == 30:
        return "paragraph", _render_bookmark(block, ctx)

    if bt == 31:
        return "table", _render_table(block, ctx)

    if bt == 32:
        return "paragraph", _render_embed(block, "bitable", "bitable")

    if bt == 33:
        # Sync block — render children transparently with a marker so push side
        # can keep it as sync_block if user re-pushes.
        children = list(block.get("children") or [])
        body = "\n\n".join(_render_children(children, ctx, indent=indent, ordered_index=None))
        return "paragraph", f"<!-- larkmd:sync_block -->\n{body}"

    # Page block as nested? Skip silently.
    if bt == 1:
        return "blank", ""

    # Unknown — leave a tracer.
    ctx.warnings.append(f"unknown block_type={bt} block_id={block.get('block_id')}")
    return "paragraph", f"<!-- larkmd:unknown block_type={bt} -->"


# ----- list item helpers -----

def _render_list_item(
    block: dict,
    ctx: RenderContext,
    *,
    indent: str,
    marker: str,
) -> tuple[str, str]:
    bt = block.get("block_type")
    key = {12: "bullet", 13: "ordered", 17: "todo"}[bt]
    text = _inline(block.get(key), ctx)
    head = f"{indent}{marker}{text}"
    children = list(block.get("children") or [])
    if not children:
        return key, head
    sub_indent = indent + "  "
    sub_lines = _render_children(children, ctx, indent=sub_indent, ordered_index=None)
    body = "\n".join(line for line in sub_lines if line)
    return key, head + ("\n" + body if body else "")


# ----- code -----

def _render_code(block: dict, ctx: RenderContext, *, indent: str) -> str:
    code = block.get("code") or {}
    elements = code.get("elements") or []
    content = render_elements(elements)
    lang = _CODE_LANG.get(code.get("style", {}).get("language"), "")
    fence = "```"
    # ensure fence doesn't collide with content
    while fence in content:
        fence += "`"
    return f"{indent}{fence}{lang}\n{content.rstrip()}\n{indent}{fence}"


# Feishu code language id → markdown fence label.
# Source: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/docx-v1/data-structure/block#62a7c20a
_CODE_LANG = {
    1: "plain", 2: "abap", 3: "ada", 4: "apache", 5: "apex", 6: "assembly",
    7: "bash", 8: "csharp", 9: "cpp", 10: "c", 11: "cobol", 12: "css",
    13: "coffeescript", 14: "d", 15: "dart", 16: "delphi", 17: "django",
    18: "dockerfile", 19: "erlang", 20: "fortran", 22: "go", 23: "groovy",
    24: "html", 25: "htmlbars", 26: "http", 27: "haskell", 28: "json",
    29: "java", 30: "javascript", 31: "julia", 32: "kotlin", 33: "latex",
    34: "lisp", 36: "lua", 37: "matlab", 38: "makefile", 39: "markdown",
    40: "nginx", 41: "objective-c", 43: "php", 44: "perl", 46: "powershell",
    47: "prolog", 48: "protobuf", 49: "python", 50: "r", 52: "ruby",
    53: "rust", 54: "sas", 55: "scss", 56: "sql", 57: "scala", 58: "scheme",
    60: "swift", 61: "thrift", 62: "typescript", 63: "vbscript", 64: "vb",
    65: "xml", 66: "yaml", 67: "cmake", 68: "diff", 69: "gherkin", 70: "graphql",
}


# ----- quote container -----

def _render_quote_container(block: dict, ctx: RenderContext, *, indent: str) -> str:
    """Quote block can have its own elements AND child blocks (rare). We emit
    the head text first (if any), then prefix every child line with `> `."""
    head = _inline(block.get("quote"), ctx)
    children = list(block.get("children") or [])
    body_parts = _render_children(children, ctx, indent="", ordered_index=None)
    body = "\n".join(body_parts)
    combined = (head + ("\n" + body if body else "")).strip("\n") if head else body
    if not combined:
        return ""
    return "\n".join(f"{indent}> {line}" if line else f"{indent}>" for line in combined.split("\n"))


# ----- callout (degraded) -----

def _render_callout(block: dict, ctx: RenderContext, *, indent: str) -> str:
    callout = block.get("callout") or {}
    emoji_id = callout.get("emoji_id") or ""
    bg = callout.get("background_color")
    border = callout.get("border_color")
    children = list(block.get("children") or [])
    body_parts = _render_children(children, ctx, indent="", ordered_index=None)
    body = "\n".join(body_parts)
    attrs: list[str] = []
    if emoji_id:
        attrs.append(f"emoji={emoji_id}")
    if bg is not None:
        attrs.append(f"bg={bg}")
    if border is not None:
        attrs.append(f"border={border}")
    marker = f"<!-- larkmd:callout {' '.join(attrs)} -->" if attrs else "<!-- larkmd:callout -->"
    quoted = "\n".join(f"{indent}> {line}" if line else f"{indent}>" for line in body.split("\n")) if body else f"{indent}>"
    return f"{marker}\n{quoted}"


# ----- file / iframe / sheet / bitable / bookmark (degraded) -----

def _render_file(block: dict) -> str:
    f = block.get("file") or {}
    name = f.get("name") or "file"
    token = f.get("token") or ""
    return f"<!-- larkmd:file token={token} -->\n[file: {name}]({token})"


def _render_iframe(block: dict) -> str:
    iframe = block.get("iframe") or {}
    comp = iframe.get("component") or {}
    url = comp.get("url") or ""
    return f"<!-- larkmd:iframe -->\n[embed]({url})"


def _render_embed(block: dict, kind: str, key: str) -> str:
    emb = block.get(key) or {}
    token = emb.get("token") or ""
    url = emb.get("url") or token
    return f"<!-- larkmd:{kind} token={token} -->\n[{kind}]({url})"


def _render_bookmark(block: dict, ctx: RenderContext) -> str:
    bk = block.get("bookmark") or {}
    url = bk.get("url") or ""
    if ctx.link_resolver:
        url = ctx.link_resolver(url) or url
    return f"<!-- larkmd:bookmark -->\n[{url}]({url})"


# ----- image -----

def _render_image(block: dict, ctx: RenderContext) -> str:
    img = block.get("image") or {}
    token = img.get("token") or ""
    if token and token in ctx.mermaid_blocks:
        source = ctx.mermaid_blocks[token].rstrip("\n")
        return f"```mermaid\n{source}\n```"
    if not token:
        return "<!-- larkmd:image (no token) -->"
    # Phase A: defer download to a media pass — for now emit predictable path.
    rel_path = f"{ctx.image_assets_dir}/{token}.png"
    ctx.downloaded_images.append((token, rel_path))
    return f"![]({rel_path})"


# ----- table -----

def _render_table(block: dict, ctx: RenderContext) -> str:
    """Render block_type 31 as GFM. Table cell ids live in `table.cells` as a
    flat list in row-major order, length = column_size * row_size. Each cell
    block_id resolves to a block_type 32-or-similar cell that has its own
    children (paragraphs)."""
    table = block.get("table") or {}
    prop = table.get("property") or {}
    col_size = prop.get("column_size") or 1
    cells = list(table.get("cells") or [])
    if not cells:
        return ""
    row_size = len(cells) // col_size if col_size else 0
    if row_size == 0:
        return ""

    rows: list[list[str]] = []
    for r in range(row_size):
        row: list[str] = []
        for c in range(col_size):
            cell_id = cells[r * col_size + c]
            cell_block = ctx.by_id.get(cell_id, {})
            cell_md = _render_table_cell(cell_block, ctx)
            row.append(cell_md)
        rows.append(row)

    # GFM requires a header row. If the original doesn't have one, synthesize blanks.
    header = rows[0] if rows else [""] * col_size
    body = rows[1:] if len(rows) > 1 else []

    def fmt_row(cells: list[str]) -> str:
        return "| " + " | ".join(_table_cell_escape(c) for c in cells) + " |"

    sep = "| " + " | ".join("---" for _ in range(col_size)) + " |"
    out = [fmt_row(header), sep]
    for r in body:
        out.append(fmt_row(r))
    return "\n".join(out)


def _render_table_cell(cell_block: dict, ctx: RenderContext) -> str:
    """A cell block contains children (usually paragraphs). Flatten to inline-ish
    text with `<br>` between paragraphs since GFM cells are single-line."""
    children = list(cell_block.get("children") or [])
    parts: list[str] = []
    for cid in children:
        b = ctx.by_id.get(cid)
        if b is None:
            continue
        bt = b.get("block_type")
        if bt == 2:
            parts.append(_inline(b.get("text"), ctx))
        elif bt in _HEADING_TYPES:
            level = _HEADING_TYPES[bt]
            parts.append(_inline(b.get(f"heading{level}"), ctx))
        elif bt == 12:
            parts.append("• " + _inline(b.get("bullet"), ctx))
        elif bt == 13:
            parts.append("1. " + _inline(b.get("ordered"), ctx))
        elif bt == 17:
            done = (b.get("todo") or {}).get("style", {}).get("done")
            parts.append(("[x] " if done else "[ ] ") + _inline(b.get("todo"), ctx))
        elif bt == 27:
            parts.append(_render_image(b, ctx))
        else:
            # unsupported in cell — best effort
            kind, txt = _render_block(b, ctx, indent="", ordered_index=None)
            parts.append(txt.replace("\n", " "))
    return "<br>".join(p for p in parts if p)


def _table_cell_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", "<br>")


# ----- inline shortcut -----

def _inline(node: dict | None, ctx: RenderContext) -> str:
    if not node:
        return ""
    return render_elements(node.get("elements") or [], link_resolver=ctx.link_resolver)


# ----- url → relative md path resolver factory -----

def make_link_resolver(state_files: dict) -> Callable[[str], str | None]:
    """Build a URL → relative-md-path resolver from the current sync state.

    Matches against:
      - the recorded `url` exactly
      - any `/wiki/<token>` or `/docx/<token>` substring whose token equals
        recorded `wiki_node_token`, `docx_token`, or any of the `previous_*`.

    Returns None if no match (caller keeps original URL).
    """
    url_to_rel: dict[str, str] = {}
    token_to_rel: dict[str, str] = {}
    for rel, info in state_files.items():
        url = info.get("url")
        if url:
            url_to_rel[url] = rel
        for tk in ("docx_token", "wiki_node_token"):
            tok = info.get(tk)
            if tok:
                token_to_rel[tok] = rel
        for tk in ("previous_docx_tokens", "previous_wiki_node_tokens"):
            for tok in info.get(tk) or []:
                token_to_rel[tok] = rel

    def resolve(url: str) -> str | None:
        if not url:
            return None
        if url in url_to_rel:
            return url_to_rel[url]
        try:
            parsed = urlparse(unquote(url))
        except Exception:
            return None
        for prefix in ("/wiki/", "/docx/"):
            idx = parsed.path.find(prefix)
            if idx >= 0:
                tok = parsed.path[idx + len(prefix):].split("/")[0]
                if tok in token_to_rel:
                    return token_to_rel[tok]
        return None

    return resolve
