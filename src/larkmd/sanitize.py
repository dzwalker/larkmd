"""Pre-import sanitizer — strip larkmd-managed HTML comments before push.

Reverse sync (`pull`) writes traceability markers like
``<!-- larkmd:callout emoji=fire bg=1 -->`` next to degraded blocks so a human
reader can see "this used to be a callout in Feishu". Those markers are
intended to live in the .md file only; if we let them through to
`drive +import`, Feishu's markdown importer treats unrecognised HTML as a
literal `text_run` and the comment text shows up verbatim in the doc.

This module strips ONLY larkmd-prefixed comments (`<!-- larkmd:... -->`).
User-authored comments (`<!-- TODO -->`, etc.) are left alone — they're the
user's call.
"""

from __future__ import annotations

import re

# Match <!-- larkmd:anything --> on a line of its own OR inline.
# Non-greedy body so adjacent comments on the same line each get matched.
_LARKMD_COMMENT_RE = re.compile(r"<!--\s*larkmd:[^>]*?-->", re.DOTALL)


def strip_larkmd_comments(md: str) -> str:
    """Remove every ``<!-- larkmd:... -->`` marker.

    When a marker is the only thing on its line, the now-blank line is
    collapsed against any adjacent blank line so we don't leave double-blank
    gaps. A leading blank line at the very top is also dropped.
    """
    cleaned = _LARKMD_COMMENT_RE.sub("", md)
    out_lines: list[str] = []
    for line in cleaned.split("\n"):
        is_blank = line.strip() == ""
        # Drop leading blank lines.
        if is_blank and not out_lines:
            continue
        # Collapse adjacent blanks.
        if is_blank and out_lines and out_lines[-1].strip() == "":
            continue
        out_lines.append(line)
    return "\n".join(out_lines)
