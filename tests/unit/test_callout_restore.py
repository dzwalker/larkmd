"""Tests for callout reconstitution: marker extraction + post-import patch.

The post-import patch flow is exercised against a stub Client that records
every API call, so we can assert exactly which insert / delete / get_blocks
combos the restore loop emits.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from larkmd.callout_restore import (
    PLACEHOLDER_PREFIX,
    CalloutIntent,
    extract_callout_intents,
    restore_callouts,
)


# ---- extract_callout_intents ----

def test_extract_no_marker_returns_input_unchanged():
    md = "# title\n\nbody\n"
    out_md, intents = extract_callout_intents(md)
    assert out_md == md
    assert intents == []


def test_extract_single_marker_replaced_by_placeholder():
    md = "<!-- larkmd:callout emoji=fire bg=1 -->\n> hot tip\n"
    out_md, intents = extract_callout_intents(md)
    assert intents == [CalloutIntent(index=0, emoji_id="fire", background_color=1)]
    assert PLACEHOLDER_PREFIX + "0" in out_md
    assert "> hot tip" in out_md
    assert "<!-- larkmd:callout" not in out_md


def test_extract_multiple_markers_get_serial_indices():
    md = (
        "<!-- larkmd:callout emoji=warn -->\n> first\n\n"
        "<!-- larkmd:callout emoji=tada bg=6 border=3 -->\n> second\n"
    )
    out_md, intents = extract_callout_intents(md)
    assert len(intents) == 2
    assert intents[0].index == 0
    assert intents[0].emoji_id == "warn"
    assert intents[0].background_color is None
    assert intents[1].index == 1
    assert intents[1].emoji_id == "tada"
    assert intents[1].background_color == 6
    assert intents[1].border_color == 3
    assert PLACEHOLDER_PREFIX + "0" in out_md
    assert PLACEHOLDER_PREFIX + "1" in out_md


def test_extract_marker_without_attrs():
    md = "<!-- larkmd:callout -->\n> bare\n"
    _out, intents = extract_callout_intents(md)
    assert len(intents) == 1
    assert intents[0].emoji_id is None
    assert intents[0].background_color is None
    assert intents[0].callout_props() == {}


def test_extract_unknown_attrs_kept_in_extra():
    md = "<!-- larkmd:callout emoji=fire weirdkey=xyz -->\n> tip\n"
    _, intents = extract_callout_intents(md)
    assert intents[0].extra == {"weirdkey": "xyz"}


def test_extract_does_not_touch_other_larkmd_markers():
    md = "<!-- larkmd:bookmark -->\n[link](url)\n<!-- larkmd:callout -->\n> tip\n"
    out_md, intents = extract_callout_intents(md)
    assert len(intents) == 1
    # bookmark marker is left alone (sanitize handles it later)
    assert "<!-- larkmd:bookmark -->" in out_md


def test_extract_text_color_attr():
    md = "<!-- larkmd:callout color=14 -->\n> light\n"
    _, intents = extract_callout_intents(md)
    assert intents[0].text_color == 14
    assert intents[0].callout_props() == {"text_color": 14}


# ---- restore_callouts (stub Client) ----

@dataclass
class StubCallRecord:
    method: str            # GET | POST | DELETE
    path: str
    data: dict | None = None
    params: dict | None = None


@dataclass
class StubClient:
    """Records every `call(...)` and serves get_blocks responses from a queue.

    Each entry in `blocks_responses` is the full block list to return for the
    next get_blocks call (in order). insert_descendants and batch_delete_children
    are recorded but their effects on subsequent get_blocks responses are NOT
    auto-simulated — tests must script that explicitly.
    """
    blocks_responses: list[list[dict]] = field(default_factory=list)
    calls: list[StubCallRecord] = field(default_factory=list)
    insert_response: dict = field(default_factory=lambda: {"data": {}})

    def call(self, args, *, data=None, params=None, file=None, cwd=None, identity="user"):
        method = args[1]
        path = args[2]
        self.calls.append(StubCallRecord(method=method, path=path, data=data, params=params))
        if method == "GET" and path.endswith("/blocks"):
            items = self.blocks_responses.pop(0) if self.blocks_responses else []
            return {"data": {"items": items, "has_more": False}}
        if method == "POST" and "/descendant" in path:
            return self.insert_response
        if method == "DELETE":
            return {"data": {}}
        # Unmocked path
        return {"data": {}}


def _para(bid: str, text: str) -> dict:
    return {
        "block_id": bid,
        "block_type": 2,
        "text": {"elements": [{"text_run": {"content": text, "text_element_style": {}}}]},
    }


def _quote(bid: str, text: str, children: list[str] | None = None) -> dict:
    b = {
        "block_id": bid,
        "block_type": 15,
        "quote": {"elements": [{"text_run": {"content": text, "text_element_style": {}}}]},
    }
    if children:
        b["children"] = children
    return b


def _root(children: list[str]) -> dict:
    return {"block_id": "root", "block_type": 1, "children": children}


def test_restore_skips_when_no_intents():
    client = StubClient()
    n = restore_callouts(client, "DOC1", [])
    assert n == 0
    assert client.calls == []


def test_restore_skips_when_placeholder_missing():
    """Intent says callout #0 should be there but the doc has no matching para."""
    blocks = [_root(["t1"]), _para("t1", "unrelated text")]
    client = StubClient(blocks_responses=[blocks])
    intent = CalloutIntent(index=0, emoji_id="fire")
    assert restore_callouts(client, "DOC1", [intent]) == 0
    # Only the get_blocks call; no insert/delete attempted.
    assert len(client.calls) == 1
    assert client.calls[0].method == "GET"


def test_restore_skips_when_next_block_is_not_quote():
    """Placeholder found but the sibling is a paragraph, not a quote."""
    blocks = [
        _root(["ph", "p2"]),
        _para("ph", PLACEHOLDER_PREFIX + "0"),
        _para("p2", "I'm just a paragraph"),
    ]
    client = StubClient(blocks_responses=[blocks])
    intent = CalloutIntent(index=0)
    assert restore_callouts(client, "DOC1", [intent]) == 0
    # Just the GET. No insert/delete.
    assert all(c.method == "GET" for c in client.calls)


def test_restore_inserts_callout_and_deletes_pair():
    """Happy path: placeholder → quote → callout block replaces both."""
    blocks_before = [
        _root(["ph", "q1"]),
        _para("ph", PLACEHOLDER_PREFIX + "0"),
        _quote("q1", "hot tip"),
    ]
    # After insert, the callout sits at index 0 and placeholder/quote at 1,2.
    new_callout_block_id = "real_callout_id"
    blocks_after_insert = [
        _root([new_callout_block_id, "ph", "q1"]),
        {"block_id": new_callout_block_id, "block_type": 19,
         "callout": {"emoji_id": "fire"}, "children": ["co_para_id"]},
        {"block_id": "co_para_id", "block_type": 2,
         "text": {"elements": [{"text_run": {"content": "hot tip"}}]}},
        _para("ph", PLACEHOLDER_PREFIX + "0"),
        _quote("q1", "hot tip"),
    ]
    client = StubClient(blocks_responses=[blocks_before, blocks_after_insert])
    intent = CalloutIntent(index=0, emoji_id="fire", background_color=1)

    assert restore_callouts(client, "DOC1", [intent]) == 1

    # Sequence: GET → POST descendant → GET → DELETE
    methods = [c.method for c in client.calls]
    assert methods == ["GET", "POST", "GET", "DELETE"]

    # Verify the descendant POST shape
    insert_call = next(c for c in client.calls if c.method == "POST")
    assert insert_call.data["index"] == 0
    descendants = insert_call.data["descendants"]
    callout_spec = descendants[0]
    assert callout_spec["block_type"] == 19
    assert callout_spec["callout"]["emoji_id"] == "fire"
    assert callout_spec["callout"]["background_color"] == 1
    # First child must be a paragraph carrying the quote's elements
    para_spec = descendants[1]
    assert para_spec["block_type"] == 2
    assert para_spec["text"]["elements"][0]["text_run"]["content"] == "hot tip"

    # Verify the DELETE removes 2 blocks starting at the placeholder's new index
    delete_call = next(c for c in client.calls if c.method == "DELETE")
    assert delete_call.data == {"start_index": 1, "end_index": 3}


def test_restore_adopts_quote_children():
    """Quote with nested children — callout absorbs them all."""
    blocks_before = [
        _root(["ph", "q1"]),
        _para("ph", PLACEHOLDER_PREFIX + "0"),
        _quote("q1", "header", children=["sub1"]),
        _para("sub1", "nested under quote"),
    ]
    # Accept any post-insert state for delete step (we already verify content above).
    blocks_after = [
        _root(["new_co", "ph", "q1"]),
        {"block_id": "new_co", "block_type": 19, "callout": {}, "children": []},
        _para("ph", PLACEHOLDER_PREFIX + "0"),
        _quote("q1", "header", children=["sub1"]),
        _para("sub1", "nested under quote"),
    ]
    client = StubClient(blocks_responses=[blocks_before, blocks_after])
    intent = CalloutIntent(index=0)

    assert restore_callouts(client, "DOC1", [intent]) == 1

    insert_call = next(c for c in client.calls if c.method == "POST")
    descs = insert_call.data["descendants"]
    # Expect: callout + para(quote text) + cloned sub1 paragraph
    types = [d["block_type"] for d in descs]
    assert types == [19, 2, 2]
    assert descs[2]["text"]["elements"][0]["text_run"]["content"] == "nested under quote"
    # Callout's children list includes both the synthesized para AND adopted sub
    callout_children = descs[0]["children"]
    assert len(callout_children) == 2


def test_restore_one_failure_does_not_block_others():
    """Two intents — first has no placeholder, second does. Second still restores."""
    # First call: missing placeholder #0
    blocks_no_ph = [_root(["x"]), _para("x", "nothing matches")]
    # Second call: placeholder #1 + quote
    blocks_with_ph = [
        _root(["ph1", "q1"]),
        _para("ph1", PLACEHOLDER_PREFIX + "1"),
        _quote("q1", "second tip"),
    ]
    blocks_after_insert = [
        _root(["nc", "ph1", "q1"]),
        {"block_id": "nc", "block_type": 19, "callout": {}, "children": []},
        _para("ph1", PLACEHOLDER_PREFIX + "1"),
        _quote("q1", "second tip"),
    ]
    client = StubClient(blocks_responses=[blocks_no_ph, blocks_with_ph, blocks_after_insert])

    intents = [CalloutIntent(index=0), CalloutIntent(index=1, emoji_id="tada")]
    assert restore_callouts(client, "DOC1", intents) == 1
