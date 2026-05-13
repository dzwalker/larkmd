"""Tests for block tree → markdown rendering."""

from __future__ import annotations

from larkmd.block_to_md import RenderContext, make_link_resolver, render_document


def _page(children: list[str]) -> dict:
    return {"block_id": "root", "block_type": 1, "children": children}


def _text(bid: str, content: str, **style) -> dict:
    return {
        "block_id": bid,
        "block_type": 2,
        "text": {"elements": [{"text_run": {"content": content, "text_element_style": style}}]},
    }


def _heading(bid: str, level: int, content: str) -> dict:
    return {
        "block_id": bid,
        "block_type": 3 + (level - 1),
        f"heading{level}": {"elements": [{"text_run": {"content": content, "text_element_style": {}}}]},
    }


def _bullet(bid: str, content: str, children: list[str] | None = None) -> dict:
    b = {
        "block_id": bid,
        "block_type": 12,
        "bullet": {"elements": [{"text_run": {"content": content, "text_element_style": {}}}]},
    }
    if children:
        b["children"] = children
    return b


def _ordered(bid: str, content: str) -> dict:
    return {
        "block_id": bid,
        "block_type": 13,
        "ordered": {"elements": [{"text_run": {"content": content, "text_element_style": {}}}]},
    }


def _todo(bid: str, content: str, done: bool) -> dict:
    return {
        "block_id": bid,
        "block_type": 17,
        "todo": {
            "elements": [{"text_run": {"content": content, "text_element_style": {}}}],
            "style": {"done": done},
        },
    }


def _code(bid: str, content: str, lang: int = 49) -> dict:
    return {
        "block_id": bid,
        "block_type": 14,
        "code": {
            "elements": [{"text_run": {"content": content, "text_element_style": {}}}],
            "style": {"language": lang},
        },
    }


def _divider(bid: str) -> dict:
    return {"block_id": bid, "block_type": 22}


def _build(blocks: list[dict]) -> tuple[list[dict], RenderContext]:
    ctx = RenderContext(by_id={b["block_id"]: b for b in blocks})
    return blocks, ctx


def test_paragraph():
    blocks = [_page(["t1"]), _text("t1", "hello")]
    out = render_document(blocks, _build(blocks)[1])
    assert out == "hello\n"


def test_heading_levels():
    blocks = [_page(["h1", "h3"]), _heading("h1", 1, "Title"), _heading("h3", 3, "Sub")]
    out = render_document(blocks, _build(blocks)[1])
    assert "# Title" in out
    assert "### Sub" in out


def test_bullet_list_packs_tight():
    blocks = [_page(["a", "b"]), _bullet("a", "alpha"), _bullet("b", "beta")]
    out = render_document(blocks, _build(blocks)[1])
    # No blank line between consecutive bullets
    assert out == "- alpha\n- beta\n"


def test_ordered_list_increments():
    blocks = [_page(["a", "b", "c"]),
              _ordered("a", "first"), _ordered("b", "second"), _ordered("c", "third")]
    out = render_document(blocks, _build(blocks)[1])
    assert out == "1. first\n2. second\n3. third\n"


def test_todo_checked_unchecked():
    blocks = [_page(["a", "b"]), _todo("a", "do it", False), _todo("b", "done", True)]
    out = render_document(blocks, _build(blocks)[1])
    assert "- [ ] do it" in out
    assert "- [x] done" in out


def test_code_block_with_lang():
    blocks = [_page(["c"]), _code("c", "print(1)\n", lang=49)]  # 49 = python
    out = render_document(blocks, _build(blocks)[1])
    assert "```python" in out
    assert "print(1)" in out
    assert out.rstrip().endswith("```")


def test_divider():
    blocks = [_page(["d"]), _divider("d")]
    out = render_document(blocks, _build(blocks)[1])
    assert out.strip() == "---"


def test_nested_bullet_indent():
    blocks = [
        _page(["a"]),
        _bullet("a", "outer", children=["b"]),
        _bullet("b", "inner"),
    ]
    out = render_document(blocks, _build(blocks)[1])
    assert "- outer" in out
    assert "  - inner" in out


def test_image_token_not_in_mermaid_emits_local_path():
    blocks = [
        _page(["i1"]),
        {"block_id": "i1", "block_type": 27, "image": {"token": "TOKEN1"}},
    ]
    ctx = RenderContext(by_id={b["block_id"]: b for b in blocks})
    out = render_document(blocks, ctx)
    assert "![](.assets/TOKEN1.png)" in out
    assert ("TOKEN1", ".assets/TOKEN1.png") in ctx.downloaded_images


def test_image_token_in_mermaid_restores_source():
    blocks = [
        _page(["i1"]),
        {"block_id": "i1", "block_type": 27, "image": {"token": "MERTOK"}},
    ]
    ctx = RenderContext(
        by_id={b["block_id"]: b for b in blocks},
        mermaid_blocks={"MERTOK": "graph TD; A-->B"},
    )
    out = render_document(blocks, ctx)
    assert "```mermaid\ngraph TD; A-->B\n```" in out
    # Mermaid hits should NOT be in downloaded list.
    assert ctx.downloaded_images == []


def test_unknown_block_emits_comment():
    blocks = [
        _page(["u1"]),
        {"block_id": "u1", "block_type": 999, "weird": {}},
    ]
    out = render_document(blocks, _build(blocks)[1])
    assert "<!-- larkmd:unknown block_type=999 -->" in out


def test_callout_degraded_with_marker():
    blocks = [
        _page(["c1"]),
        {"block_id": "c1", "block_type": 19,
         "callout": {"emoji_id": "fire", "background_color": 1},
         "children": ["t1"]},
        _text("t1", "hot tip"),
    ]
    out = render_document(blocks, _build(blocks)[1])
    assert "<!-- larkmd:callout" in out
    assert "> hot tip" in out


def test_table_basic():
    cell_blocks = []
    cell_ids = []
    for i, content in enumerate(["A", "B", "C", "D"]):
        cid = f"c{i}"
        cell_ids.append(cid)
        cell_blocks.append({
            "block_id": cid,
            "block_type": 32,  # cell container
            "children": [f"p{i}"],
        })
        cell_blocks.append(_text(f"p{i}", content))
    table = {
        "block_id": "t1",
        "block_type": 31,
        "table": {"cells": cell_ids, "property": {"column_size": 2}},
    }
    blocks = [_page(["t1"]), table, *cell_blocks]
    out = render_document(blocks, _build(blocks)[1])
    assert "| A | B |" in out
    assert "| --- | --- |" in out
    assert "| C | D |" in out


def test_link_resolver_via_state():
    state_files = {
        "01-prep/checklist.md": {
            "url": "https://my.feishu.cn/wiki/ABCDEF",
            "wiki_node_token": "ABCDEF",
            "docx_token": "DOCX1",
        },
    }
    resolve = make_link_resolver(state_files)
    assert resolve("https://my.feishu.cn/wiki/ABCDEF") == "01-prep/checklist.md"
    assert resolve("https://other.feishu.cn/docx/DOCX1") == "01-prep/checklist.md"
    assert resolve("https://google.com/x") is None


def test_quote_block_with_text():
    blocks = [
        _page(["q1"]),
        {"block_id": "q1", "block_type": 15,
         "quote": {"elements": [{"text_run": {"content": "wisdom", "text_element_style": {}}}]},
         "children": []},
    ]
    out = render_document(blocks, _build(blocks)[1])
    assert "> wisdom" in out


def test_paragraph_then_heading_has_blank_line():
    blocks = [_page(["t", "h"]), _text("t", "intro"), _heading("h", 2, "Next")]
    out = render_document(blocks, _build(blocks)[1])
    assert "intro\n\n## Next" in out


# ---- Phase C: degraded-block coverage ----

def test_bookmark_degrades_to_link_with_marker():
    blocks = [
        _page(["b1"]),
        {"block_id": "b1", "block_type": 30,
         "bookmark": {"url": "https://example.com/spec"}},
    ]
    out = render_document(blocks, _build(blocks)[1])
    assert "<!-- larkmd:bookmark -->" in out
    assert "[https://example.com/spec](https://example.com/spec)" in out


def test_file_degrades_to_link_with_token_marker():
    blocks = [
        _page(["f1"]),
        {"block_id": "f1", "block_type": 23,
         "file": {"name": "design.pdf", "token": "FTOK1"}},
    ]
    out = render_document(blocks, _build(blocks)[1])
    assert "<!-- larkmd:file token=FTOK1 -->" in out
    assert "[file: design.pdf](FTOK1)" in out


def test_iframe_degrades_to_embed_link():
    blocks = [
        _page(["i1"]),
        {"block_id": "i1", "block_type": 24,
         "iframe": {"component": {"url": "https://figma.com/x"}}},
    ]
    out = render_document(blocks, _build(blocks)[1])
    assert "<!-- larkmd:iframe -->" in out
    assert "[embed](https://figma.com/x)" in out


def test_sheet_embed_marker_includes_token():
    blocks = [
        _page(["s1"]),
        {"block_id": "s1", "block_type": 28,
         "sheet": {"token": "SHTOK", "url": "https://my.feishu.cn/sheets/SHTOK"}},
    ]
    out = render_document(blocks, _build(blocks)[1])
    assert "<!-- larkmd:sheet token=SHTOK -->" in out
    assert "[sheet](https://my.feishu.cn/sheets/SHTOK)" in out


def test_bitable_embed_marker_includes_token():
    blocks = [
        _page(["b1"]),
        {"block_id": "b1", "block_type": 32,
         "bitable": {"token": "BTAB", "url": "https://my.feishu.cn/base/BTAB"}},
    ]
    out = render_document(blocks, _build(blocks)[1])
    assert "<!-- larkmd:bitable token=BTAB -->" in out


def test_sync_block_renders_children_with_marker():
    blocks = [
        _page(["s1"]),
        {"block_id": "s1", "block_type": 33, "children": ["t1"]},
        _text("t1", "shared content"),
    ]
    out = render_document(blocks, _build(blocks)[1])
    assert "<!-- larkmd:sync_block -->" in out
    assert "shared content" in out


def test_callout_attributes_in_marker():
    blocks = [
        _page(["c1"]),
        {"block_id": "c1", "block_type": 19,
         "callout": {"emoji_id": "warn", "background_color": 2, "border_color": 5},
         "children": ["t1"]},
        _text("t1", "be careful"),
    ]
    out = render_document(blocks, _build(blocks)[1])
    # Marker present with each attribute; order isn't critical for the test.
    assert "<!-- larkmd:callout " in out
    assert "emoji=warn" in out
    assert "bg=2" in out
    assert "border=5" in out
    assert "> be careful" in out


def test_callout_without_attributes_uses_bare_marker():
    blocks = [
        _page(["c1"]),
        {"block_id": "c1", "block_type": 19, "callout": {}, "children": ["t1"]},
        _text("t1", "note"),
    ]
    out = render_document(blocks, _build(blocks)[1])
    assert "<!-- larkmd:callout -->" in out
