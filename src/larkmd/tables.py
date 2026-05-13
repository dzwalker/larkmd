"""Table column-width memory.

Feishu's PATCH API only allows updating one column at a time, with min 50 px.
markdown has no width concept, so we GET widths from the old docx before each
sync, then PATCH them back on the new doc to make manual UI adjustments survive.
"""

from __future__ import annotations

from larkmd.client import Client


def extract_table_widths(blocks: list[dict]) -> list[list[int]]:
    """按 blocks 中出现顺序提取所有 table 的 column_width 列表。
    返回 [[w1, w2, ...], ...]，未设宽度的表用空列表占位（保持顺序对齐）。"""
    widths = []
    for b in blocks:
        if b.get("block_type") == 31:
            prop = b.get("table", {}).get("property", {}) or {}
            widths.append(list(prop.get("column_width") or []))
    return widths


def patch_table_column_width(
    client: Client,
    docx_token: str,
    table_block_id: str,
    column_index: int,
    width: int,
) -> None:
    """PATCH 单列宽度（飞书 API 一次只能改一列，最小 50px）。"""
    if width < 50:
        width = 50
    client.call(
        ["api", "PATCH",
         f"/open-apis/docx/v1/documents/{docx_token}/blocks/{table_block_id}"],
        data={"update_table_property": {"column_index": column_index, "column_width": width}},
        params={"document_revision_id": -1},
    )


def restore_table_widths(
    client: Client,
    docx_token: str,
    blocks: list[dict],
    saved_widths: list[list[int]],
) -> int:
    """按文档顺序对齐 saved_widths，把每张表的列宽 PATCH 回去。
    返回 PATCH 调用次数。表数量或列数与 saved 不匹配时按 min 对齐，多/少的列跳过。"""
    new_tables = [b for b in blocks if b.get("block_type") == 31]
    patches = 0
    for i, t in enumerate(new_tables):
        if i >= len(saved_widths):
            break
        widths = saved_widths[i]
        if not widths:
            continue
        col_size = t.get("table", {}).get("property", {}).get("column_size", 0)
        cur_widths = list(t.get("table", {}).get("property", {}).get("column_width") or [])
        for ci in range(min(col_size, len(widths))):
            target = widths[ci]
            if ci < len(cur_widths) and cur_widths[ci] == target:
                continue  # 已是目标值，跳过
            patch_table_column_width(client, docx_token, t["block_id"], ci, target)
            patches += 1
    return patches
