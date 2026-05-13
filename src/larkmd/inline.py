"""Feishu text-element array → markdown inline text.

A Feishu block's textual content is `{key: {"elements": [...]}}` where each
element is one of:

  {"text_run": {"content": "...", "text_element_style": {bold, italic,
                inline_code, strikethrough, link: {url}}}}
  {"mention_user": {...}}
  {"mention_doc": {"token", "title", "url"}}
  {"equation": {"content": "..."}}
  {"reminder": {...}}, {"file": {...}}, ...

We support the round-trippable subset (text_run + equation + mention_doc as
plain link). Other element types are rendered as their text content if
available, else dropped silently.

Style flags are emitted by stacking markers in a stable order:
    code → strike → bold → italic
so a fully-styled run becomes `***~~` ``code`` `~~***`. We do NOT collapse
adjacent runs with the same style; one element = one styled span.
"""

from __future__ import annotations

from urllib.parse import unquote


def render_elements(elements: list[dict], *, link_resolver=None) -> str:
    """Render a Feishu element array as a markdown inline string.

    `link_resolver` is an optional callable `(url) -> Optional[str]` that may
    rewrite a URL — used to turn placeholder/wiki URLs back into relative
    `path/to.md` links.
    """
    out: list[str] = []
    for e in elements or []:
        out.append(_render_one(e, link_resolver))
    return "".join(out)


def _render_one(e: dict, link_resolver) -> str:
    if "text_run" in e:
        return _render_text_run(e["text_run"], link_resolver)
    if "equation" in e:
        # inline equation: $...$
        content = (e["equation"].get("content") or "").rstrip("\n")
        return f"${content}$" if content else ""
    if "mention_doc" in e:
        m = e["mention_doc"]
        title = m.get("title") or m.get("token") or "doc"
        url = m.get("url") or ""
        if link_resolver:
            url = link_resolver(url) or url
        return f"[{_escape_link_text(title)}]({url})"
    if "mention_user" in e:
        m = e["mention_user"]
        return f"@{m.get('name') or m.get('user_id') or 'user'}"
    # Unknown element types: try a best-effort content extraction.
    for v in e.values():
        if isinstance(v, dict) and "content" in v:
            return str(v["content"])
    return ""


def _render_text_run(tr: dict, link_resolver) -> str:
    content = tr.get("content", "")
    if not content:
        return ""
    style = tr.get("text_element_style") or {}

    # Newlines inside a text_run are line breaks within the same paragraph;
    # markdown-wise the surrounding block renderer handles paragraph breaks.
    # We keep `\n` literal so block renderer can split if needed.

    link = (style.get("link") or {}).get("url")
    if link:
        link = unquote(link)
        if link_resolver:
            link = link_resolver(link) or link

    text = _apply_style(content, style)
    if link:
        return f"[{_escape_link_text(text)}]({link})"
    return text


def _apply_style(content: str, style: dict) -> str:
    """Wrap content in markdown style markers. Inline code wins over other
    styles for its inner content (we render as code first then wrap)."""
    s = content
    if style.get("inline_code"):
        # Escape backticks by choosing a longer fence if needed.
        fence = "`"
        while fence in s:
            fence += "`"
        s = f"{fence}{s}{fence}"
    if style.get("strikethrough"):
        s = f"~~{s}~~"
    if style.get("bold") and style.get("italic"):
        s = f"***{s}***"
    elif style.get("bold"):
        s = f"**{s}**"
    elif style.get("italic"):
        s = f"*{s}*"
    return s


def _escape_link_text(text: str) -> str:
    """Escape `]` inside link text so the markdown link parser doesn't truncate."""
    return text.replace("]", "\\]")
