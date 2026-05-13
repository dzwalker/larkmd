"""Block-level Feishu docx operations.

- get_blocks (paginated, page_size=500)
- _to_descendant_spec (sanitize GET-shape → create-shape; cells/image/column_width)
- insert_descendants
- batch_delete_children
"""

from __future__ import annotations

from larkmd.client import Client

# GET /blocks 返回里这些字段不应回写 descendant API（系统生成的元信息）
_DESCENDANT_BLACKLIST = {
    "parent_id", "revision_id", "comment_ids",
    "creator_id", "create_time", "update_time", "modifier_id",
}


def get_blocks(client: Client, docx_token: str) -> list[dict]:
    """拉 docx 全部 blocks，自动翻页。
    page_size=500 是飞书上限；大文档（如 checklist 含 4 个表 → 500+ blocks）必须翻页，
    否则 root.children 中的部分 id 不在返回 items 里，descendant API 会报 1770041。"""
    items: list[dict] = []
    page_token = ""
    while True:
        params = {"page_size": 500, "document_revision_id": -1}
        if page_token:
            params["page_token"] = page_token
        out = client.call(
            ["api", "GET", f"/open-apis/docx/v1/documents/{docx_token}/blocks"],
            params=params,
        )
        data = out["data"]
        items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
        if not page_token:
            break
    return items


def to_descendant_spec(block: dict) -> dict:
    """把 GET /blocks 返回的 block 转成 descendant API 接受的 spec。
    保留 block_id（作为 placeholder id）+ block_type + children + 各 block_type 的内容字段。

    Critical sanitization (every one of these is a real bug we hit):
    - block_type 31 (table): drop table.cells (redundant with block.children, descendant
      create rejects with 1770041); drop column_width (separate PATCH after create);
      drop merge_info (read-only, set by backend); compute row_size from cell count.
    - block_type 27 (image): clear image field (descendant create only accepts empty image,
      token+size filled by subsequent upload + patch_image_block).

    See: https://github.com/leemysw/feishu-docx feishu_docx/core/sdk/docx.py for the
    table.cells discovery.
    """
    spec = {k: v for k, v in block.items() if k not in _DESCENDANT_BLACKLIST}
    if spec.get("block_type") == 31 and "table" in spec:
        t = spec["table"]
        prop = dict(t.get("property", {}))
        prop.pop("merge_info", None)
        prop.pop("column_width", None)
        cells = t.get("cells", [])
        col = prop.get("column_size", 1)
        row = len(cells) // col if col else 0
        prop["row_size"] = row
        t.pop("cells", None)
        t["property"] = prop
        if cells:
            spec["children"] = cells
    if spec.get("block_type") == 27:
        spec["image"] = {}
    return spec


def insert_descendants(
    client: Client,
    docx_token: str,
    parent_block_id: str,
    children_id: list[str],
    descendants: list[dict],
    *,
    index: int = 0,
) -> dict[str, str]:
    """通过 descendant API 把整棵子树一次性插入到 parent 下。
    children_id 是直接 children 的 placeholder ids；
    descendants 含全部后代（含 children_id 自己 + 它们的子孙）。
    返回 placeholder_id → real_block_id 映射。"""
    if not children_id:
        return {}
    out = client.call(
        ["api", "POST",
         f"/open-apis/docx/v1/documents/{docx_token}/blocks/{parent_block_id}/descendant"],
        data={"children_id": children_id, "descendants": descendants, "index": index},
        params={"document_revision_id": -1},
    )
    data = out.get("data", {})
    rel = data.get("block_id_relations") or data.get("block_id_relation") or {}
    if not rel:
        first = data.get("first_level_block_ids") or data.get("first_level_id") or []
        if first and len(first) == len(children_id):
            rel = dict(zip(children_id, first))
    return rel


def batch_delete_children(
    client: Client,
    docx_token: str,
    parent_block_id: str,
    count: int,
    *,
    start_index: int = 0,
) -> None:
    """删 parent 下 [start_index, start_index+count) 范围的子 block。Feishu 接 DELETE 而非 POST。"""
    if count <= 0:
        return
    client.call(
        ["api", "DELETE",
         f"/open-apis/docx/v1/documents/{docx_token}/blocks/{parent_block_id}/children/batch_delete"],
        data={"start_index": start_index, "end_index": start_index + count},
    )
