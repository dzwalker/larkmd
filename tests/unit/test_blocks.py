"""Tests for `to_descendant_spec` — the heart of the schema-mismatch fix."""

from __future__ import annotations

from larkmd.blocks import to_descendant_spec


def test_strip_blacklist_fields():
    block = {
        "block_id": "blk1",
        "block_type": 2,
        "text": {"elements": [{"text_run": {"content": "hi"}}]},
        "parent_id": "root",
        "revision_id": 7,
        "create_time": 12345,
        "update_time": 67890,
        "creator_id": "u",
        "modifier_id": "u",
        "comment_ids": ["c1"],
    }
    spec = to_descendant_spec(block)
    assert "parent_id" not in spec
    assert "revision_id" not in spec
    assert "create_time" not in spec
    assert "update_time" not in spec
    assert "creator_id" not in spec
    assert "modifier_id" not in spec
    assert "comment_ids" not in spec
    assert spec["block_type"] == 2
    assert spec["text"] == block["text"]
    assert spec["block_id"] == "blk1"


def test_table_strips_cells_and_promotes_to_children():
    """The crucial bug: descendant API rejects table.cells. Cells must move to block.children."""
    cells = ["c1", "c2", "c3", "c4", "c5", "c6"]  # 2 rows × 3 cols
    block = {
        "block_id": "t1",
        "block_type": 31,
        "table": {
            "cells": list(cells),
            "property": {
                "column_size": 3,
                "row_size": 2,
                "column_width": [100, 200, 300],
                "merge_info": [{"row_span": 1, "col_span": 1}] * 6,
            },
        },
    }
    spec = to_descendant_spec(block)
    assert "cells" not in spec["table"]
    assert spec["children"] == cells
    assert "column_width" not in spec["table"]["property"]
    assert "merge_info" not in spec["table"]["property"]
    assert spec["table"]["property"]["row_size"] == 2  # recomputed from len(cells)/col_size
    assert spec["table"]["property"]["column_size"] == 3


def test_table_no_cells_no_children():
    block = {
        "block_id": "t2",
        "block_type": 31,
        "table": {"cells": [], "property": {"column_size": 3, "row_size": 0}},
    }
    spec = to_descendant_spec(block)
    assert "children" not in spec
    assert "cells" not in spec["table"]


def test_image_field_cleared():
    """Image block must be created with empty image; token filled by separate upload+patch."""
    block = {
        "block_id": "img1",
        "block_type": 27,
        "image": {"token": "stale-token", "width": 800, "height": 600, "scale": 1},
    }
    spec = to_descendant_spec(block)
    assert spec["image"] == {}
    assert spec["block_type"] == 27


def test_non_table_non_image_passthrough():
    block = {
        "block_id": "h1",
        "block_type": 3,
        "heading1": {"elements": [{"text_run": {"content": "title"}}]},
        "children": ["x", "y"],
    }
    spec = to_descendant_spec(block)
    assert spec["heading1"] == block["heading1"]
    assert spec["children"] == ["x", "y"]
