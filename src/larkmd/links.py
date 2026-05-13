"""Cross-document link rewriting.

Two passes:
1. **Pre-import** (`rewrite_links_to_placeholder`): rewrite `[text](rel.md)` →
   `[text](https://feishu-mirror/<absolute-rel-path>)`. The placeholder URL
   survives the markdown → docx import unchanged so we can find it again.
2. **Post-import** (`patch_all_links`): walk every block of every doc, replace
   the placeholder URL with the actual `https://<tenant>/wiki/<node>` URL,
   and also fix any `/docx/<token>` or `/wiki/<token>` references that point to
   stale tokens (using token_to_url alias map).

Also handles the lark-cli quirk: only ONE link survives per text element line
(the first one). `larkmd doctor` warns; this module just does its best.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, unquote

from larkmd.blocks import get_blocks
from larkmd.client import Client

LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
PLACEHOLDER_PREFIX = "https://feishu-mirror/"
DOCX_URL_RE = re.compile(r"feishu\.cn/docx/([A-Za-z0-9]+)")
WIKI_URL_RE = re.compile(r"feishu\.cn/wiki/([A-Za-z0-9]+)")


def rewrite_links_to_placeholder(
    md: str, mapping: dict[str, str], rel_path: str, repo_root: Path,
) -> str:
    """把 [text](relative.md) 改写为 [text](https://feishu-mirror/<absolute-path>)。
    mapping 用于第二趟改成真 URL。"""
    src_dir = Path(rel_path).parent

    def repl(m: re.Match) -> str:
        text, url = m.group(1), m.group(2)
        if url.startswith(("http://", "https://", "mailto:", "#")):
            return m.group(0)
        target = (src_dir / url.split("#")[0]).as_posix() if not url.startswith("/") else url.lstrip("/")
        try:
            resolved = (repo_root / target).resolve().relative_to(repo_root).as_posix()
        except ValueError:
            return m.group(0)  # 仓库外，保持原样
        if resolved in mapping:
            return f"[{text}]({mapping[resolved]})"
        return f"[{text}]({PLACEHOLDER_PREFIX}{quote(resolved)})"

    return LINK_RE.sub(repl, md)


def patch_link_in_block(
    client: Client,
    docx_token: str,
    block_id: str,
    block_type_key: str,
    new_elements: list[dict],
) -> None:
    """更新 block 的 text elements。Feishu PATCH 用 update_text_elements，
    适用于所有承载 text_run 的 block（text/heading/bullet/ordered/quote）。
    block_type_key 当前未直接使用，但保留以防将来分支处理。"""
    client.call(
        ["api", "PATCH", f"/open-apis/docx/v1/documents/{docx_token}/blocks/{block_id}"],
        params={"document_revision_id": -1},
        data={"update_text_elements": {"elements": new_elements}},
    )


_TEXTUAL_BLOCK_KEYS = (
    "text", "heading1", "heading2", "heading3", "heading4", "heading5",
    "heading6", "heading7", "heading8", "heading9", "bullet", "ordered", "quote",
)


def patch_all_links(
    client: Client,
    docx_token: str,
    mapping: dict[str, str],
    token_to_url: dict[str, str],
) -> int:
    """遍历 docx 所有 block 把链接更新到最新 wiki URL。三类 URL 都处理：
       1) 占位 https://feishu-mirror/<rel_path> → mapping[rel_path]
       2) /docx/<token>  → token_to_url[token]（包括历史 docx_token）
       3) /wiki/<token>  → token_to_url[token]（包括历史 wiki_node_token，已被 orphan 的）
       token_to_url 里同时包含当前与历史 token 全部映射到当前 URL。
       返回 patch 的 block 数。"""
    blocks = get_blocks(client, docx_token)
    patched = 0
    for b in blocks:
        for key in _TEXTUAL_BLOCK_KEYS:
            if key not in b or "elements" not in b[key]:
                continue
            elements = b[key]["elements"]
            changed = False
            for e in elements:
                tr = e.get("text_run", {})
                style = tr.get("text_element_style", {})
                link = style.get("link", {})
                url = link.get("url", "")
                if not url:
                    continue
                decoded = unquote(url)

                hit = False
                for prefix in (PLACEHOLDER_PREFIX, "http://feishu-mirror/"):
                    if decoded.startswith(prefix):
                        rel = decoded[len(prefix):].split("#")[0]
                        if rel in mapping:
                            link["url"] = mapping[rel]
                            changed = True
                        hit = True
                        break
                if hit:
                    continue

                m = DOCX_URL_RE.search(decoded) or WIKI_URL_RE.search(decoded)
                if m:
                    tok = m.group(1)
                    new_url = token_to_url.get(tok)
                    if new_url and new_url != link["url"]:
                        link["url"] = new_url
                        changed = True
            if changed:
                patch_link_in_block(client, docx_token, b["block_id"], key, elements)
                patched += 1
    return patched
