"""Wiki node lifecycle: delete docx-from-wiki + orphan cleanup.

Gotcha: wiki delete needs `wiki:wiki` scope (just `wiki:node:*` returns 131005).
URL path uses the docx **obj_token**, not the wiki node_token.
"""

from __future__ import annotations

import sys

from larkmd.client import Client
from larkmd.errors import LarkCliError


def delete_wiki_doc(client: Client, space_id: str, docx_token: str) -> None:
    """删除 wiki 里的 docx。注意飞书的坑：
       - URL 路径用 docx 的 obj_token，**不是** wiki node_token
       - obj_type 走 body（用 --data），用 --params 飞书会说 'obj_type required'
       - 需要 wiki:wiki scope（仅 wiki:node:* 不够）
       失败容忍（不阻塞主流程，但会留 orphan）。"""
    try:
        client.call(
            ["api", "DELETE",
             f"/open-apis/wiki/v2/spaces/{space_id}/nodes/{docx_token}"],
            data={"obj_type": "docx"},
        )
    except (SystemExit, LarkCliError) as e:
        sys.stderr.write(f"  warn: wiki delete docx={docx_token} failed: {e}\n")


def get_obj_token_from_node(client: Client, node_token: str) -> str | None:
    """get_node 反查 obj_token（wiki delete URL 要的是 obj_token，不是 node_token）。"""
    try:
        out = client.call(
            ["api", "GET", "/open-apis/wiki/v2/spaces/get_node"],
            params={"token": node_token, "obj_type": "wiki"},
        )
    except LarkCliError:
        return None
    return out.get("data", {}).get("node", {}).get("obj_token")


def cleanup_orphans(
    client: Client,
    space_id: str,
    state: dict,
    *,
    apply: bool = False,
) -> list[tuple[str, str]]:
    """删除 state 中 previous_wiki_node_tokens 列表里的所有 orphan wiki 节点。
    返回 [(rel_path, node_token), ...] of nodes attempted.

    Apply=False is a dry-run."""
    attempted: list[tuple[str, str]] = []
    files = state.get("files") or state  # tolerate both v1 and legacy schema
    for rel, info in files.items():
        if not isinstance(info, dict):
            continue
        for node_token in info.get("previous_wiki_node_tokens", []):
            attempted.append((rel, node_token))
            if not apply:
                continue
            obj_token = get_obj_token_from_node(client, node_token) or node_token
            delete_wiki_doc(client, space_id, obj_token)
    return attempted
