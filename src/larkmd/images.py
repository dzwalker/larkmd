"""Image upload + patch into image blocks."""

from __future__ import annotations

from pathlib import Path

from larkmd.client import Client


def upload_image_media(client: Client, png: Path, parent_node_block_id: str) -> str:
    """Upload PNG as docx_image media; return file_token."""
    size = png.stat().st_size
    out = client.call(
        ["api", "POST", "/open-apis/drive/v1/medias/upload_all"],
        data={
            "file_name": png.name,
            "parent_type": "docx_image",
            "parent_node": parent_node_block_id,
            "size": size,
        },
        file=f"file={png.name}",
        cwd=png.parent,
    )
    return out["data"]["file_token"]


def patch_image_block(
    client: Client,
    docx_token: str,
    block_id: str,
    file_token: str,
    width: int,
    height: int,
) -> None:
    client.call(
        ["api", "PATCH", f"/open-apis/docx/v1/documents/{docx_token}/blocks/{block_id}"],
        params={"document_revision_id": -1},
        data={"replace_image": {"token": file_token, "width": width, "height": height}},
    )
