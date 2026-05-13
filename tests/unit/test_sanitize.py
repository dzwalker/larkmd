"""Tests for `strip_larkmd_comments` — pre-import HTML comment removal."""

from __future__ import annotations

from larkmd.sanitize import strip_larkmd_comments


def test_strip_inline_comment():
    md = "before <!-- larkmd:bookmark --> after"
    assert strip_larkmd_comments(md) == "before  after"


def test_strip_own_line_collapses_blanks():
    md = "para1\n\n<!-- larkmd:callout emoji=fire -->\n> quoted\n\npara2\n"
    out = strip_larkmd_comments(md)
    # The marker line is gone; the paragraph + quote stay adjacent without
    # a stale double-blank.
    assert out == "para1\n\n> quoted\n\npara2\n"


def test_strip_multiple_markers_in_one_doc():
    md = (
        "<!-- larkmd:callout emoji=warn -->\n"
        "> heads up\n\n"
        "<!-- larkmd:bookmark -->\n"
        "[link](https://x)\n\n"
        "<!-- larkmd:sheet token=ABC -->\n"
        "[sheet](ABC)\n"
    )
    out = strip_larkmd_comments(md)
    assert "larkmd:" not in out
    assert "> heads up" in out
    assert "[link](https://x)" in out
    assert "[sheet](ABC)" in out


def test_user_html_comments_kept():
    md = "<!-- TODO: review --> body\n<!-- planning note -->\nmore"
    out = strip_larkmd_comments(md)
    assert "<!-- TODO: review -->" in out
    assert "<!-- planning note -->" in out


def test_strip_with_attributes_and_unicode():
    md = "<!-- larkmd:callout emoji=🔥 bg=2 border=3 -->\n> hot\n"
    out = strip_larkmd_comments(md)
    assert out == "> hot\n"


def test_no_marker_returns_input():
    md = "# title\n\nbody\n"
    assert strip_larkmd_comments(md) == md


def test_idempotent():
    """Stripping twice equals stripping once — push pipeline can re-run safely."""
    md = "<!-- larkmd:iframe -->\n[embed](https://x)\n"
    once = strip_larkmd_comments(md)
    twice = strip_larkmd_comments(once)
    assert once == twice


def test_preserves_mermaid_fences():
    """Mermaid blocks have no larkmd comment; they must pass through untouched."""
    md = "```mermaid\ngraph TD; A-->B\n```\n"
    assert strip_larkmd_comments(md) == md
