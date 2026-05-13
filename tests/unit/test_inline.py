"""Tests for Feishu text-element → markdown inline rendering."""

from __future__ import annotations

from larkmd.inline import render_elements


def _tr(content: str, **style) -> dict:
    return {"text_run": {"content": content, "text_element_style": style}}


def test_plain_text():
    assert render_elements([_tr("hello")]) == "hello"


def test_bold():
    assert render_elements([_tr("hi", bold=True)]) == "**hi**"


def test_italic():
    assert render_elements([_tr("hi", italic=True)]) == "*hi*"


def test_bold_italic_combined():
    assert render_elements([_tr("hi", bold=True, italic=True)]) == "***hi***"


def test_strikethrough():
    assert render_elements([_tr("gone", strikethrough=True)]) == "~~gone~~"


def test_inline_code():
    assert render_elements([_tr("x()", inline_code=True)]) == "`x()`"


def test_inline_code_with_backticks_uses_longer_fence():
    assert render_elements([_tr("a`b", inline_code=True)]) == "``a`b``"


def test_link():
    el = _tr("here", **{"link": {"url": "https%3A%2F%2Fex.com%2Fa"}})
    assert render_elements([el]) == "[here](https://ex.com/a)"


def test_link_with_link_resolver():
    el = _tr("doc", **{"link": {"url": "https://my.feishu.cn/wiki/abc"}})
    out = render_elements([el], link_resolver=lambda u: "01-prep/checklist.md")
    assert out == "[doc](01-prep/checklist.md)"


def test_link_text_escaping():
    el = _tr("a]b", **{"link": {"url": "https://x"}})
    assert render_elements([el]) == "[a\\]b](https://x)"


def test_concatenation_preserves_styles():
    out = render_elements([
        _tr("plain "),
        _tr("bold", bold=True),
        _tr(" tail"),
    ])
    assert out == "plain **bold** tail"


def test_inline_equation():
    out = render_elements([{"equation": {"content": "x^2"}}])
    assert out == "$x^2$"


def test_mention_doc_with_resolver():
    el = {"mention_doc": {"title": "Spec", "url": "https://my.feishu.cn/wiki/zzz"}}
    out = render_elements([el], link_resolver=lambda u: "spec.md")
    assert out == "[Spec](spec.md)"


def test_unknown_element_silent_drop():
    # No `content` field anywhere; renderer drops gracefully.
    assert render_elements([{"weird_element": {"foo": "bar"}}]) == ""


def test_link_overrides_style_wrapping():
    """Bold/italic INSIDE the link text — markdown is `[**hi**](url)`."""
    el = _tr("hi", bold=True, **{"link": {"url": "https://x"}})
    assert render_elements([el]) == "[**hi**](https://x)"
