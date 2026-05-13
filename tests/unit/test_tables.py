"""Tests for table column-width memory."""

from __future__ import annotations

from larkmd.tables import extract_table_widths


def test_extract_widths_in_order():
    blocks = [
        {"block_id": "h", "block_type": 3, "heading1": {}},
        {"block_id": "t1", "block_type": 31, "table": {
            "property": {"column_size": 2, "column_width": [100, 200]}}},
        {"block_id": "p", "block_type": 2, "text": {}},
        {"block_id": "t2", "block_type": 31, "table": {
            "property": {"column_size": 3, "column_width": [50, 60, 70]}}},
    ]
    assert extract_table_widths(blocks) == [[100, 200], [50, 60, 70]]


def test_extract_widths_missing_field():
    """Tables with no column_width yet (importer default) get empty list."""
    blocks = [
        {"block_id": "t1", "block_type": 31, "table": {"property": {"column_size": 2}}},
        {"block_id": "t2", "block_type": 31, "table": {}},
    ]
    assert extract_table_widths(blocks) == [[], []]


def test_extract_widths_no_tables():
    blocks = [{"block_id": "h", "block_type": 3, "heading1": {}}]
    assert extract_table_widths(blocks) == []
