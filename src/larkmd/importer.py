"""Markdown → docx import + move-to-wiki orchestration.

Wraps two lark-cli operations:
- `drive +import`: upload md, create a docx in a drive folder
- `wiki/v2/.../move_docs_to_wiki`: move the docx into a wiki space (async task,
  poll up to N times — Feishu importer is occasionally flaky with status 3)
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from larkmd.client import Client
from larkmd.config import ImporterConfig
from larkmd.errors import ImporterStuckError, LarkCliError


def import_md(md_path: Path, folder_token: str, name: str, *, cli_path: str = "lark-cli") -> dict:
    """drive +import 会先打几行进度日志，再吐 JSON。
    lark-cli 要求 --file 是 cwd 下的相对路径，因此 cd 到 md 的目录。
    返回 {"token": ..., "url": ..., "type": ...}.

    Note: 这里不走 Client.call，因为 +import 是子命令而非 raw API；
    输出格式也不是纯 JSON（前面有进度日志），需要单独 parse。"""
    cmd = [
        cli_path, "drive", "+import",
        "--file", md_path.name,
        "--folder-token", folder_token,
        "--type", "docx",
        "--name", name,
        "--as", "user",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=md_path.parent)
    idx = r.stdout.find("{")
    if idx < 0:
        raise LarkCliError(
            f"drive +import no JSON. stdout: {r.stdout}",
            returncode=r.returncode, stderr=r.stderr, cmd=cmd,
        )
    out = json.loads(r.stdout[idx:])
    if r.returncode != 0 or out.get("ok") is False:
        raise LarkCliError(
            f"drive +import failed: {json.dumps(out, ensure_ascii=False)}",
            returncode=r.returncode, stderr=r.stderr, cmd=cmd,
        )
    return out["data"]


def delete_drive_file(client: Client, token: str, file_type: str = "docx") -> None:
    """删除云盘文件（未进 wiki 的 docx 用）。失败容忍。"""
    try:
        client.call(
            ["api", "DELETE", f"/open-apis/drive/v1/files/{token}"],
            params={"type": file_type},
        )
    except (SystemExit, LarkCliError) as e:
        sys.stderr.write(f"  warn: drive delete {token} failed: {e}\n")


def move_docx_to_wiki(
    client: Client,
    space_id: str,
    docx_token: str,
    parent_wiki_token: str,
    *,
    cfg: ImporterConfig,
) -> str:
    """把 cloud drive 里的 docx 移进 wiki。返回 wiki node_token。
    parent_wiki_token=空 → 移到 wiki 根。
    importer 偶发 status 3 → 用轮询规避。"""
    body = {"obj_type": "docx", "obj_token": docx_token}
    if parent_wiki_token:
        body["parent_wiki_token"] = parent_wiki_token
    r = client.call(
        ["api", "POST", f"/open-apis/wiki/v2/spaces/{space_id}/nodes/move_docs_to_wiki"],
        data=body,
    )
    data = r["data"]
    if "wiki_token" in data:  # 已在 wiki，同步返回
        return data["wiki_token"]
    task_id = data["task_id"]
    for _ in range(cfg.move_max_retries):
        time.sleep(cfg.move_retry_interval_sec)
        tr = client.call(
            ["api", "GET", f"/open-apis/wiki/v2/tasks/{task_id}"],
            params={"task_type": "move"},
        )
        results = tr["data"]["task"].get("move_result", [])
        if results and results[0].get("status_msg") == "success":
            return results[0]["node"]["node_token"]
    raise ImporterStuckError(
        f"move_docs_to_wiki poll timeout for {docx_token} after "
        f"{cfg.move_max_retries} × {cfg.move_retry_interval_sec}s"
    )
