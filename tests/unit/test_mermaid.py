"""Tests for mermaid source extraction and round-trip via state.

The mmdc subprocess + image upload aren't tested here — those live behind
external binaries. We pin down the pure-Python pieces:
  - extract_mermaid_sources order matches PNG numbering
  - block_to_md restores the source when state has the (token → source) map
"""

from __future__ import annotations

from larkmd.block_to_md import RenderContext, render_document
from larkmd.mermaid import extract_mermaid_sources


def test_extract_single_block():
    md = "intro\n\n```mermaid\ngraph TD; A-->B\n```\n\nafter\n"
    assert extract_mermaid_sources(md) == ["graph TD; A-->B"]


def test_extract_multiple_blocks_in_order():
    md = (
        "```mermaid\nflowchart\n  A-->B\n```\n"
        "\nbetween\n\n"
        "```mermaid\nsequenceDiagram\n  A->>B: hi\n```\n"
    )
    sources = extract_mermaid_sources(md)
    assert len(sources) == 2
    assert sources[0] == "flowchart\n  A-->B"
    assert sources[1] == "sequenceDiagram\n  A->>B: hi"


def test_extract_ignores_other_fences():
    md = (
        "```python\nprint(1)\n```\n"
        "```mermaid\ngraph LR; X-->Y\n```\n"
        "```\nplain\n```\n"
    )
    sources = extract_mermaid_sources(md)
    assert sources == ["graph LR; X-->Y"]


def test_extract_empty_when_no_mermaid():
    assert extract_mermaid_sources("just text\n```python\nx\n```\n") == []


def test_extract_handles_trailing_blank_lines_in_block():
    md = "```mermaid\ngraph TD; A-->B\n\n```\n"
    # blank line inside the block stays preserved
    assert extract_mermaid_sources(md) == ["graph TD; A-->B\n"]


def test_round_trip_via_state_files():
    """Simulate: push extracted source X under image_token T → state writes
    {T: X} → pull renders image block with token T → expects ```mermaid X```."""
    source = "graph LR\n  A-->B\n  B-->C"
    file_token = "FILETOK_FROM_UPLOAD"
    state_mermaid_blocks = {file_token: source}

    blocks = [
        {"block_id": "root", "block_type": 1, "children": ["i1"]},
        {"block_id": "i1", "block_type": 27, "image": {"token": file_token}},
    ]
    ctx = RenderContext(
        by_id={b["block_id"]: b for b in blocks},
        mermaid_blocks=state_mermaid_blocks,
    )
    out = render_document(blocks, ctx)
    assert f"```mermaid\n{source}\n```" in out
    assert ctx.downloaded_images == []  # no image download for restored mermaid
