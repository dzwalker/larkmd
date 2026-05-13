#!/usr/bin/env python3
"""
sync_to_feishu.py — git markdown 单源 → 飞书云盘 docx 镜像

工作流：
  1. mmdc 把 md 中的 mermaid 块渲染成 PNG（缓存：未变内容跳过）
  2. 把每个 md 用 lark-cli `drive +import` 创建/重建 docx
  3. 找到自动生成的 image block，上传 PNG 并 patch token + 真实尺寸
  4. 第二趟：用收集到的 {md path → docx URL} 映射，patch 所有跨文档链接

依赖：
  - lark-cli（profile 应已登录用户身份；默认 feishu）
  - mmdc（@mermaid-js/mermaid-cli）+ /tmp/feishu-poc/puppeteer-config.json
  - python3 + Pillow（uv add Pillow 或 pip install Pillow）

环境变量（从 .env 读）：
  FEISHU_WIKI_SPACE_ID           — 知识库 space_id（最终归属）
  FEISHU_WIKI_NODE_01_PREP       — 知识库分区节点 01-准备
  FEISHU_WIKI_NODE_02_ONSITE     — 知识库分区节点 02-现场
  FEISHU_WIKI_NODE_03_OUTPUT     — 知识库分区节点 03-输出
  FEISHU_WIKI_NODE_04_TEAMKIT    — 知识库分区节点 04-团队套件
  FEISHU_HACKATHON_FOLDER_TOKEN  — 中转云盘根文件夹（import 临时落地用）
  FEISHU_FOLDER_01_PREP_TOKEN    — 中转云盘子目录（同上，按分区）
  FEISHU_FOLDER_02_ONSITE_TOKEN
  FEISHU_FOLDER_03_OUTPUT_TOKEN
  FEISHU_FOLDER_04_TEAMKIT_TOKEN

状态：
  .feishu-sync-state.json（gitignored）记录 {rel_path → {docx_token, url, content_hash}}
  避免每次重建 docx，只在 md 内容变化时才重建。

用法：
  python3 scripts/sync_to_feishu.py            # dry-run，列出要做的事
  python3 scripts/sync_to_feishu.py --apply    # 真同步
  python3 scripts/sync_to_feishu.py --apply --force  # 全部重建（忽略 hash 缓存）
  python3 scripts/sync_to_feishu.py --apply --only README.md  # 只同步指定文件

已知限制：
  - 同一行多个 markdown 链接 Feishu 只保留第一个（importer quirk）
  - mermaid emoji 在无中文 emoji 字体时会显示为方框（系统装 Noto Color Emoji 即可）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote

REPO = Path(__file__).resolve().parent.parent
STATE_FILE = REPO / ".feishu-sync-state.json"
ENV_FILE = REPO / ".env"
PUPPETEER_CFG = Path("/tmp/feishu-poc/puppeteer-config.json")
TENANT = "r3c0qt6yjw.feishu.cn"

# 中转用的云盘子文件夹（import_md 临时落地，import 后立刻 move 到 wiki）
DIR_TO_DRIVE_FOLDER_ENV = {
    "01-prep": "FEISHU_FOLDER_01_PREP_TOKEN",
    "02-execution": "FEISHU_FOLDER_02_ONSITE_TOKEN",
    "03-team-kit": "FEISHU_FOLDER_04_TEAMKIT_TOKEN",
    "04-post": "FEISHU_FOLDER_03_OUTPUT_TOKEN",
}
# 知识库分区节点（move_docs_to_wiki 的 parent_wiki_token）
DIR_TO_WIKI_NODE_ENV = {
    "01-prep": "FEISHU_WIKI_NODE_01_PREP",
    "02-execution": "FEISHU_WIKI_NODE_02_ONSITE",
    "03-team-kit": "FEISHU_WIKI_NODE_04_TEAMKIT",
    "04-post": "FEISHU_WIKI_NODE_03_OUTPUT",
}
ROOT_FILES = {"README.md", "overview.md", "timeline.md"}  # 放 wiki 根

# 每个分区里文件的展示顺序 + 编号前缀。
# wiki 侧栏按节点创建时间排序，所以脚本会按这个顺序逐个 import，
# 文件名（== wiki 节点标题）就用 "NN-stem" 形式。
# 源 .md 文件名不变，只影响 wiki 显示。
SECTION_ORDER: dict[str, list[str]] = {
    "01-prep": [
        # 主线
        "checklist", "procurement-list",
        # 人 / 题
        "team-formation", "pain-point-collection", "problem-pool",
        # 激励
        "incentive-plan",
        # 工具 / 技术
        "tech-setup", "lark-bots", "recording-plan",
    ],
    "02-execution": [
        # 时间线
        "kickoff-script", "day1-afternoon-agenda", "day1-evening-agenda", "day2-morning-agenda",
        # 角色 SOP
        "facilitator-sop", "mentor-playbook", "checkpoint-questions",
        # 评审 / 演示
        "demo-format", "judging-rubric", "voting-design", "awards",
    ],
    "03-team-kit": [
        # 立项
        "team-charter-template", "mvp-scope-template",
        # 协作
        "team-roles-card", "team-collab-kit", "parallel-edit-merge-playbook",
        # 工具
        "claude-code-quickstart", "ai-agent-patterns", "integration-recipes", "lark-integration-cookbook",
        # 演示
        "demo-deck-template", "faq",
    ],
    "04-post": [
        "recording-pipeline", "retro-template", "adoption-plan",
    ],
}
ROOT_ORDER = ["README", "overview", "current-priorities", "timeline"]  # 根目录文件顺序（不加编号前缀）


def load_env() -> dict[str, str]:
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"')
    return env


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def list_md_files() -> list[Path]:
    """按 SECTION_ORDER / ROOT_ORDER 返回；不在配置里的文件附加在分区末尾（按字母）。"""
    files: list[Path] = []
    # 根
    for stem in ROOT_ORDER:
        p = REPO / f"{stem}.md"
        if p.exists():
            files.append(p)
    # 各分区
    for sub, ordered_stems in SECTION_ORDER.items():
        sub_dir = REPO / sub
        if not sub_dir.exists():
            continue
        seen: set[str] = set()
        for stem in ordered_stems:
            p = sub_dir / f"{stem}.md"
            if p.exists():
                files.append(p)
                seen.add(stem)
        # 配置漏掉的文件兜底
        for p in sorted(sub_dir.glob("*.md")):
            if p.stem not in seen:
                files.append(p)
    return files


def display_name_for(rel_path: str) -> str:
    """wiki 节点标题：分区里加 NN- 前缀，根文件保持原名。"""
    parts = rel_path.split("/")
    if len(parts) == 1:  # root file
        return Path(rel_path).stem
    sub, fname = parts[0], parts[1]
    stem = Path(fname).stem
    ordered = SECTION_ORDER.get(sub, [])
    if stem in ordered:
        idx = ordered.index(stem) + 1
        return f"{idx:02d}-{stem}"
    return stem  # 未配置的，原名


def drive_folder_for(env: dict, rel_path: str) -> str:
    """import 时用的中转云盘文件夹 token。"""
    parts = rel_path.split("/")
    if len(parts) == 1:
        return env["FEISHU_HACKATHON_FOLDER_TOKEN"]
    return env[DIR_TO_DRIVE_FOLDER_ENV[parts[0]]]


def wiki_parent_for(env: dict, rel_path: str) -> str:
    """move_docs_to_wiki 时的 parent_wiki_token。根文件返回空（top-level）。"""
    parts = rel_path.split("/")
    if len(parts) == 1:
        return ""
    return env[DIR_TO_WIKI_NODE_ENV[parts[0]]]


def md_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# Markdown link rewrite ----------------------------------------------------

LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def rewrite_links_to_placeholder(md: str, mapping: dict[str, str], rel_path: str) -> str:
    """把 [text](relative.md) 改写为 [text](https://feishu-mirror/<absolute-path>)。
    mapping 用于第二趟改成真 URL。"""
    src_dir = Path(rel_path).parent

    def repl(m: re.Match) -> str:
        text, url = m.group(1), m.group(2)
        if url.startswith(("http://", "https://", "mailto:", "#")):
            return m.group(0)
        # 解析为相对仓库根的路径
        target = (src_dir / url.split("#")[0]).as_posix() if not url.startswith("/") else url.lstrip("/")
        # 规范化
        try:
            resolved = (REPO / target).resolve().relative_to(REPO).as_posix()
        except ValueError:
            return m.group(0)  # 仓库外，保持原样
        # 第二趟用真 URL；这一趟用占位
        if resolved in mapping:
            return f"[{text}]({mapping[resolved]})"
        return f"[{text}](https://feishu-mirror/{quote(resolved)})"

    return LINK_RE.sub(repl, md)


# mmdc preprocess ---------------------------------------------------------

def preprocess_mermaid(md_path: Path, out_dir: Path) -> tuple[Path, list[Path]]:
    """跑 mmdc，返回 (rendered_md_path, [png_paths])。"""
    out_md = out_dir / md_path.name
    if not has_mermaid(md_path):
        out_md.write_text(md_path.read_text())
        return out_md, []

    cmd = [
        "mmdc", "-i", str(md_path), "-o", str(out_md),
        "-p", str(PUPPETEER_CFG),
        "-e", "png", "-b", "transparent", "--scale", "2",
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    pngs = sorted(out_dir.glob(f"{md_path.stem}-*.png"))
    return out_md, pngs


def has_mermaid(md_path: Path) -> bool:
    return "```mermaid" in md_path.read_text()


# lark-cli wrappers -------------------------------------------------------

def lark(args: list[str], data: dict | None = None, params: dict | None = None,
         file: str | None = None, cwd: Path | None = None) -> dict:
    cmd = ["lark-cli"] + args + ["--as", "user"]
    if params is not None:
        cmd += ["--params", json.dumps(params)]
    tmp_data_file: Path | None = None
    if data is not None:
        data_json = json.dumps(data)
        # OS argv 限制 ~128KB；大 payload 改走 @file 避免 OSError 7
        if len(data_json) > 100_000:
            import tempfile
            # lark-cli 要求 @file 是 cwd 下的相对路径，文件创建在 caller 指定的 cwd 里
            target_dir = cwd if cwd is not None else Path.cwd()
            fd, p = tempfile.mkstemp(suffix=".json", prefix=".lark-data-", dir=str(target_dir))
            os.close(fd)
            tmp_data_file = Path(p)
            tmp_data_file.write_text(data_json)
            cmd += ["--data", f"@{tmp_data_file.name}"]
            if cwd is None:
                cwd = target_dir
        else:
            cmd += ["--data", data_json]
    if file is not None:
        cmd += ["--file", file]
    r = None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    finally:
        # KEEP_LARK_TMP_ON_ERROR=1 时，调用失败保留 tmp 文件以便诊断
        if tmp_data_file is not None:
            keep_on_err = os.environ.get("KEEP_LARK_TMP_ON_ERROR") == "1"
            if keep_on_err and (r is None or r.returncode != 0):
                sys.stderr.write(f"[lark] kept tmp data: {tmp_data_file}\n")
            else:
                tmp_data_file.unlink(missing_ok=True)
    if not r.stdout.strip():
        raise RuntimeError(
            f"lark-cli empty stdout (rc={r.returncode}). cmd={' '.join(cmd)}\n"
            f"stderr: {r.stderr}")
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"lark-cli non-JSON stdout. cmd={' '.join(cmd)}\n"
            f"stdout: {r.stdout[:500]}\nstderr: {r.stderr[:500]}") from e
    # 飞书 API 成功 = code: 0；lark-cli 失败时 ok: false
    if r.returncode != 0 or out.get("ok") is False or out.get("code", 1) != 0:
        raise RuntimeError(f"lark-cli error: {json.dumps(out, ensure_ascii=False)}")
    return out


def import_md(md_path: Path, folder_token: str, name: str) -> dict:
    """drive +import 会先打几行进度日志，再吐 JSON。
    lark-cli 要求 --file 是 cwd 下的相对路径，因此 cd 到 md 的目录。"""
    cmd = [
        "lark-cli", "drive", "+import",
        "--file", md_path.name,
        "--folder-token", folder_token,
        "--type", "docx",
        "--name", name,
        "--as", "user",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=md_path.parent)
    idx = r.stdout.find("{")
    if idx < 0:
        raise RuntimeError(f"drive +import no JSON. stdout: {r.stdout}\nstderr: {r.stderr}")
    out = json.loads(r.stdout[idx:])
    if r.returncode != 0 or out.get("ok") is False:
        raise RuntimeError(f"drive +import failed: {json.dumps(out, ensure_ascii=False)}")
    return out["data"]  # token, url, type


def delete_drive_file(token: str, file_type: str = "docx") -> None:
    """删除云盘文件（未进 wiki 的 docx 用）。失败容忍。"""
    try:
        lark(["api", "DELETE", f"/open-apis/drive/v1/files/{token}"],
             params={"type": file_type})
    except (SystemExit, RuntimeError) as e:
        sys.stderr.write(f"  warn: drive delete {token} failed: {e}\n")


def delete_wiki_doc(env: dict, docx_token: str) -> None:
    """删除 wiki 里的 docx。注意飞书的坑：
       - URL 路径用 docx 的 obj_token，**不是** wiki node_token
       - obj_type 走 body（用 --data），用 --params 飞书会说 'obj_type required'
       - 需要 wiki:wiki scope（仅 wiki:node:* 不够）
       失败容忍（不阻塞主流程，但会留 orphan）。"""
    space_id = env["FEISHU_WIKI_SPACE_ID"]
    try:
        lark(["api", "DELETE",
              f"/open-apis/wiki/v2/spaces/{space_id}/nodes/{docx_token}"],
             data={"obj_type": "docx"})
    except (SystemExit, RuntimeError) as e:
        sys.stderr.write(f"  warn: wiki delete docx={docx_token} failed: {e}\n")


def get_blocks(docx_token: str) -> list[dict]:
    """拉 docx 全部 blocks，自动翻页。
    page_size=500 是飞书上限；大文档（如 checklist 含 4 个表 → 500+ blocks）必须翻页，
    否则 root.children 中的部分 id 不在返回 items 里，descendant API 会报 1770041。"""
    items: list[dict] = []
    page_token = ""
    while True:
        params = {"page_size": 500, "document_revision_id": -1}
        if page_token:
            params["page_token"] = page_token
        out = lark(["api", "GET", f"/open-apis/docx/v1/documents/{docx_token}/blocks"],
                   params=params)
        data = out["data"]
        items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
        if not page_token:
            break
    return items


def move_docx_to_wiki(env: dict, docx_token: str, parent_wiki_token: str) -> str:
    """把 cloud drive 里的 docx 移进 wiki。返回 wiki node_token。
    parent_wiki_token=空 → 移到 wiki 根。"""
    space_id = env["FEISHU_WIKI_SPACE_ID"]
    body = {"obj_type": "docx", "obj_token": docx_token}
    if parent_wiki_token:
        body["parent_wiki_token"] = parent_wiki_token
    r = lark(["api", "POST", f"/open-apis/wiki/v2/spaces/{space_id}/nodes/move_docs_to_wiki"],
             data=body)
    data = r["data"]
    if "wiki_token" in data:  # 已在 wiki，同步返回
        return data["wiki_token"]
    task_id = data["task_id"]
    import time
    for _ in range(30):
        time.sleep(1.5)
        tr = lark(["api", "GET", f"/open-apis/wiki/v2/tasks/{task_id}"],
                  params={"task_type": "move"})
        results = tr["data"]["task"].get("move_result", [])
        if results and results[0].get("status_msg") == "success":
            return results[0]["node"]["node_token"]
    raise RuntimeError(f"move_docs_to_wiki poll timeout for {docx_token}")


def batch_delete_children(docx_token: str, parent_block_id: str, count: int,
                          start_index: int = 0) -> None:
    """删 parent 下 [start_index, start_index+count) 范围的子 block。Feishu 接 DELETE 而非 POST。"""
    if count <= 0:
        return
    lark(
        ["api", "DELETE",
         f"/open-apis/docx/v1/documents/{docx_token}/blocks/{parent_block_id}/children/batch_delete"],
        data={"start_index": start_index, "end_index": start_index + count},
    )


# Descendant API：把整棵子树一次性 insert 到指定 parent 下 -------------------

# GET /blocks 返回里这些字段不应回写 descendant API（系统生成的元信息）
_DESCENDANT_BLACKLIST = {
    "parent_id", "revision_id", "comment_ids",
    "creator_id", "create_time", "update_time", "modifier_id",
}


def _to_descendant_spec(block: dict) -> dict:
    """把 GET /blocks 返回的 block 转成 descendant API 接受的 spec。
    保留 block_id（作为 placeholder id）+ block_type + children + 各 block_type 的内容字段。
    特殊处理 table（block_type 31）：剥 GET 才有的 column_width/merge_info，补 create 必需的 row_size。
    特殊处理 image（block_type 27）：清空 image 字段（descendant create 只接受空 image，
    token/尺寸由后续 upload + patch_image_block 填）。"""
    spec = {k: v for k, v in block.items() if k not in _DESCENDANT_BLACKLIST}
    if spec.get("block_type") == 31 and "table" in spec:
        t = spec["table"]
        prop = dict(t.get("property", {}))
        prop.pop("merge_info", None)
        # column_width 在 descendant create 上触发 1770041 schema mismatch；
        # 暂剥掉，create 后由 _patch_table_widths 单独 PATCH 回写。
        prop.pop("column_width", None)
        cells = t.get("cells", [])
        col = prop.get("column_size", 1)
        row = len(cells) // col if col else 0
        prop["row_size"] = row
        # descendant API 期望 table.children = cell ids（不是 table.cells）：
        #   - GET /blocks 同时返回 table.cells 和 block.children（同 id 列表，冗余）
        #   - descendant create 必须删 table.cells，cell 引用通过 spec['children'] 表达
        #   - 参考 https://github.com/leemysw/feishu-docx feishu_docx/core/sdk/docx.py
        t.pop("cells", None)
        t["property"] = prop
        if cells:
            spec["children"] = cells
    if spec.get("block_type") == 27:
        spec["image"] = {}
    return spec


def insert_descendants(
    docx_token: str, parent_block_id: str,
    children_id: list[str], descendants: list[dict], index: int = 0,
) -> dict[str, str]:
    """通过 descendant API 把整棵子树一次性插入到 parent 下。
    children_id 是直接 children 的 placeholder ids；
    descendants 含全部后代（含 children_id 自己 + 它们的子孙）。
    返回 placeholder_id → real_block_id 映射。"""
    if not children_id:
        return {}
    out = lark(
        ["api", "POST",
         f"/open-apis/docx/v1/documents/{docx_token}/blocks/{parent_block_id}/descendant"],
        data={"children_id": children_id, "descendants": descendants, "index": index},
        params={"document_revision_id": -1},
    )
    data = out.get("data", {})
    # Feishu 返回的字段名在不同版本/文档里有出入，尽量兼容
    rel = data.get("block_id_relations") or data.get("block_id_relation") or {}
    if not rel:
        # 兜底：用 first_level_id + descendants 顺序自己拼
        first = data.get("first_level_block_ids") or data.get("first_level_id") or []
        if first and len(first) == len(children_id):
            rel = dict(zip(children_id, first))
        # 子孙的 id 暂时取不到，这种情况下图片处理走 children 块再 GET 的兜底
    return rel


def _extract_table_widths(blocks: list[dict]) -> list[list[int]]:
    """按 blocks 中出现顺序提取所有 table 的 column_width 列表。
    返回 [[w1, w2, ...], ...]，未设宽度的表用空列表占位（保持顺序对齐）。"""
    widths = []
    for b in blocks:
        if b.get("block_type") == 31:
            prop = b.get("table", {}).get("property", {}) or {}
            widths.append(list(prop.get("column_width") or []))
    return widths


def patch_table_column_width(docx_token: str, table_block_id: str,
                             column_index: int, width: int) -> None:
    """PATCH 单列宽度（飞书 API 一次只能改一列，最小 50px）。"""
    if width < 50:
        width = 50
    lark(
        ["api", "PATCH",
         f"/open-apis/docx/v1/documents/{docx_token}/blocks/{table_block_id}"],
        data={"update_table_property": {"column_index": column_index, "column_width": width}},
        params={"document_revision_id": -1},
    )


def restore_table_widths(docx_token: str, blocks: list[dict],
                         saved_widths: list[list[int]]) -> int:
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
            patch_table_column_width(docx_token, t["block_id"], ci, target)
            patches += 1
    return patches


def update_docx_in_place(
    env: dict, old_docx_token: str, md_path: Path, scratch_folder: str,
) -> list[str]:
    """原地更新 old_docx_token 的内容（token 不变，URL 不变）。
    事务安全（insert-then-delete）：先把新内容插到老 docx 最前面，验证插入成功后
    再删旧 children；任何前置步骤失败都不会让老 docx 留空（最坏只是老内容前面
    多出一段半成品，可手动清理或下次 sync 自动重写）。

    流程：
      0. GET 老 docx，记录 children 数量 + 各表列宽
      1. import 临时 docx 拿正确 block 树（失败 → 老 docx 完好无损）
      2. descendant API 把整棵新树插到老 docx root 的 index=0
      3. 验证插入数 == 新 children 数，否则抛错（老内容仍在后面）
      4. 删除老 children（位移到 [M, M+N)）
      5. 把记忆的列宽 PATCH 回新表（按文档顺序对齐）
      6. 删除临时 docx
    返回：老 docx 中新插入的 image block id 列表（按 md 中出现顺序）。"""
    # 0. 记录老 children 数量 + 各表列宽（用户在飞书 UI 调过的宽度跨 sync 持久化）
    old_blocks_pre = get_blocks(old_docx_token)
    old_root = next(b for b in old_blocks_pre if b.get("block_type") == 1)
    old_children_count = len(old_root.get("children", []))
    saved_widths = _extract_table_widths(old_blocks_pre)

    # 1. import 临时 docx 拿到 block 树
    temp_data = import_md(md_path, scratch_folder, md_path.stem + "-tmp")
    temp_token = temp_data["token"]

    try:
        temp_blocks = get_blocks(temp_token)
        temp_root = next(b for b in temp_blocks if b.get("block_type") == 1)
        children_id = list(temp_root.get("children", []))
        descendants = [
            _to_descendant_spec(b) for b in temp_blocks
            if b["block_id"] != temp_root["block_id"]
        ]
        new_count = len(children_id)

        # 2. 把新树插到 root 的最前面（index=0）
        id_map = insert_descendants(
            old_docx_token, old_root["block_id"], children_id, descendants, index=0,
        )

        # 3. 验证插入成功：refetch 老 docx，root.children 应至少多了 new_count 个
        refreshed = get_blocks(old_docx_token)
        new_root = next(b for b in refreshed if b.get("block_type") == 1)
        actual_count = len(new_root.get("children", []))
        if actual_count < old_children_count + new_count:
            raise RuntimeError(
                f"insert verification failed: expected ≥ {old_children_count + new_count} "
                f"children after insert, got {actual_count}. 老内容仍在后段，未删旧 children。")

        # 4. 删老 children（被新内容挤后退到 [new_count, new_count + old_children_count)）
        if old_children_count > 0:
            batch_delete_children(
                old_docx_token, old_root["block_id"],
                old_children_count, start_index=new_count,
            )

        # 5. 把列宽写回（仅新表，refetch 一次最新顺序）
        if saved_widths:
            after_delete = get_blocks(old_docx_token)
            try:
                restore_table_widths(old_docx_token, after_delete, saved_widths)
            except Exception as e:
                sys.stderr.write(f"  warn: restore table widths failed: {e}\n")

        # 6. 收集老 docx 中新 image block 的 real id（按 md 顺序）
        new_image_ids: list[str] = []
        if isinstance(id_map, dict) and id_map:
            for b in temp_blocks:
                if b.get("block_type") == 27:
                    real = id_map.get(b["block_id"])
                    if real:
                        new_image_ids.append(real)
        if not new_image_ids:
            # 兜底：refreshed 已 GET 过，按出现顺序找 image
            new_image_ids = [b["block_id"] for b in refreshed if b.get("block_type") == 27]

        return new_image_ids
    finally:
        # 7. 删临时 docx（drive 文件，未进 wiki）
        delete_drive_file(temp_token)


def upload_image_media(png: Path, parent_node_block_id: str) -> str:
    size = png.stat().st_size
    out = lark(
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


def patch_image_block(docx_token: str, block_id: str, file_token: str,
                      width: int, height: int) -> None:
    lark(
        ["api", "PATCH", f"/open-apis/docx/v1/documents/{docx_token}/blocks/{block_id}"],
        params={"document_revision_id": -1},
        data={"replace_image": {"token": file_token, "width": width, "height": height}},
    )


def patch_link_in_block(docx_token: str, block_id: str, block_type_key: str,
                        new_elements: list[dict]) -> None:
    """更新 block 的 text elements。Feishu PATCH 用 update_text_elements，
    适用于所有承载 text_run 的 block（text/heading/bullet/ordered/quote）。"""
    lark(
        ["api", "PATCH", f"/open-apis/docx/v1/documents/{docx_token}/blocks/{block_id}"],
        params={"document_revision_id": -1},
        data={"update_text_elements": {"elements": new_elements}},
    )


# Pass 2: link rewriting --------------------------------------------------

PLACEHOLDER_PREFIX = "https://feishu-mirror/"
DOCX_URL_RE = re.compile(r"feishu\.cn/docx/([A-Za-z0-9]+)")
WIKI_URL_RE = re.compile(r"feishu\.cn/wiki/([A-Za-z0-9]+)")


def patch_all_links(docx_token: str, mapping: dict[str, str],
                    token_to_url: dict[str, str]) -> int:
    """遍历 docx 所有 block 把链接更新到最新 wiki URL。三类 URL 都处理：
       1) 占位 https://feishu-mirror/<rel_path> → mapping[rel_path]
       2) /docx/<token>  → token_to_url[token]（包括历史 docx_token）
       3) /wiki/<token>  → token_to_url[token]（包括历史 wiki_node_token，已被 orphan 的）
       token_to_url 里同时包含当前与历史 token 全部映射到当前 URL。
       返回 patch 的 block 数。"""
    blocks = get_blocks(docx_token)
    patched = 0
    for b in blocks:
        for key in ("text", "heading1", "heading2", "heading3", "heading4", "heading5",
                    "heading6", "heading7", "heading8", "heading9", "bullet", "ordered", "quote"):
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

                # 类型 1：占位 → wiki URL
                hit = False
                for prefix in ("https://feishu-mirror/", "http://feishu-mirror/"):
                    if decoded.startswith(prefix):
                        rel = decoded[len(prefix):].split("#")[0]
                        if rel in mapping:
                            link["url"] = mapping[rel]
                            changed = True
                        hit = True
                        break
                if hit:
                    continue

                # 类型 2 & 3：/docx/ 或 /wiki/ → 当前 URL
                m = DOCX_URL_RE.search(decoded) or WIKI_URL_RE.search(decoded)
                if m:
                    tok = m.group(1)
                    new_url = token_to_url.get(tok)
                    if new_url and new_url != link["url"]:
                        link["url"] = new_url
                        changed = True
            if changed:
                patch_link_in_block(docx_token, b["block_id"], key, elements)
                patched += 1
    return patched


# Main pipeline -----------------------------------------------------------

def sync_one(md_file: Path, env: dict, state: dict, force: bool, apply: bool) -> dict | None:
    rel = md_file.relative_to(REPO).as_posix()
    src = md_file.read_text()
    h = md_hash(src)
    prev = state.get(rel, {})

    if not force and prev.get("content_hash") == h and "docx_token" in prev:
        print(f"  SKIP  {rel}  (unchanged)")
        return prev

    # 1. preprocess md（mermaid）
    with tempfile.TemporaryDirectory(prefix="feishu-sync-") as td:
        tdpath = Path(td)
        # 先用占位写入，第二趟会 patch link
        munged = (tdpath / md_file.name)
        munged.write_text(rewrite_links_to_placeholder(src, {}, rel))

        rendered_md, pngs = preprocess_mermaid(munged, tdpath)
        # PNG sizes
        from PIL import Image
        sizes = {p.name: Image.open(p).size for p in pngs}

        if not apply:
            mode = "UPDATE-IN-PLACE" if "docx_token" in prev else "CREATE"
            print(f"  PLAN  {rel}  → {mode} + {len(pngs)} image(s)")
            return None

        folder = drive_folder_for(env, rel)

        # 分支 A：已有 docx_token → 原地更新（保留 URL，外发链接不失效）
        if "docx_token" in prev:
            docx_token = prev["docx_token"]
            print(f"  UPDT  {rel}  → docx {docx_token} (in place)")
            new_image_block_ids = update_docx_in_place(env, docx_token, rendered_md, folder)
            if pngs:
                if len(new_image_block_ids) != len(pngs):
                    sys.stderr.write(
                        f"  warn: {rel} 有 {len(pngs)} PNG 但找到 {len(new_image_block_ids)} 个 image block\n")
                for png, block_id in zip(pngs, new_image_block_ids):
                    w, h_px = sizes[png.name]
                    file_token = upload_image_media(png, block_id)
                    patch_image_block(docx_token, block_id, file_token, w, h_px)

            result = {
                "docx_token": docx_token,
                "url": prev["url"],
                "content_hash": h,
            }
            # 保留 wiki_node_token（如果之前 move 过）和历史 orphan 列表
            if "wiki_node_token" in prev:
                result["wiki_node_token"] = prev["wiki_node_token"]
            for k in ("previous_wiki_node_tokens", "previous_docx_tokens"):
                if k in prev:
                    result[k] = prev[k]
            return result

        # 分支 B：新文件 → import + move 进 wiki
        name = display_name_for(rel)
        data = import_md(rendered_md, folder, name)
        docx_token = data["token"]
        print(f"  IMPT  {rel}  → docx {docx_token}")

        # 找 image block + 上传 PNG + patch token/尺寸
        if pngs:
            blocks = get_blocks(docx_token)
            image_blocks = [b for b in blocks if b.get("block_type") == 27]
            if len(image_blocks) != len(pngs):
                sys.stderr.write(
                    f"  warn: {rel} 有 {len(pngs)} PNG 但 docx 有 {len(image_blocks)} image block\n")
            for png, block in zip(pngs, image_blocks):
                w, h_px = sizes[png.name]
                file_token = upload_image_media(png, block["block_id"])
                patch_image_block(docx_token, block["block_id"], file_token, w, h_px)

        # 把 docx 移进 wiki 对应分区
        parent_wiki = wiki_parent_for(env, rel)
        node_token = move_docx_to_wiki(env, docx_token, parent_wiki)
        wiki_url = f"https://{TENANT}/wiki/{node_token}"
        print(f"  PUSH  {rel}  → {wiki_url}")

        return {
            "docx_token": docx_token,
            "wiki_node_token": node_token,
            "url": wiki_url,
            "content_hash": h,
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真同步（默认 dry-run）")
    ap.add_argument("--force", action="store_true", help="忽略 hash 缓存，全部重建")
    ap.add_argument("--only", action="append", help="只同步指定文件（仓库相对路径），可多次")
    ap.add_argument("--skip-link-pass", action="store_true", help="跳过第二趟链接 patch")
    args = ap.parse_args()

    env = load_env()
    state = load_state()
    files = list_md_files()
    if args.only:
        only = set(args.only)
        files = [f for f in files if f.relative_to(REPO).as_posix() in only]

    print(f"=== Pass 1: import {len(files)} files ===")
    import traceback
    for f in files:
        try:
            new = sync_one(f, env, state, args.force, args.apply)
        except Exception as e:
            sys.stderr.write(f"FAIL {f}:\n{traceback.format_exc()}\n")
            continue
        if new and args.apply:
            state[f.relative_to(REPO).as_posix()] = new
            save_state(state)

    if not args.apply or args.skip_link_pass:
        return

    # Pass 2: rewrite links using token map
    mapping = {p: v["url"] for p, v in state.items()}
    # token_to_url 同时收当前 + 历史 docx_token / wiki_node_token，全部指向当前 URL
    token_to_url: dict[str, str] = {}
    for v in state.values():
        if "url" not in v:
            continue
        url = v["url"]
        if "docx_token" in v:
            token_to_url[v["docx_token"]] = url
        if "wiki_node_token" in v:
            token_to_url[v["wiki_node_token"]] = url
        for t in v.get("previous_docx_tokens", []):
            token_to_url[t] = url
        for t in v.get("previous_wiki_node_tokens", []):
            token_to_url[t] = url
    print(f"\n=== Pass 2: patch cross-doc links ({len(mapping)} docs / {len(token_to_url)} aliases) ===")
    for f in files:
        rel = f.relative_to(REPO).as_posix()
        if rel not in state:
            continue
        token = state[rel]["docx_token"]
        try:
            n = patch_all_links(token, mapping, token_to_url)
            print(f"  LINK  {rel}  patched {n} blocks")
        except Exception as e:
            sys.stderr.write(f"link-patch fail {rel}: {e}\n")


if __name__ == "__main__":
    main()
