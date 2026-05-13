"""Mermaid preprocessing — invoke `mmdc` (mermaid-cli) to render fenced
``` ```mermaid blocks ``` into PNGs the importer can pick up.

Optional: if mmdc isn't on PATH, callers should fall through to plain markdown.

For reverse-sync round-trip we also extract each block's ORIGINAL source so
the syncer can persist (image_token → source) into state.mermaid_blocks; the
puller then restores the fenced block from that mapping instead of writing a
plain `![](.assets/...)` reference.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from larkmd.config import MermaidConfig


def has_mermaid(md_path: Path) -> bool:
    return "```mermaid" in md_path.read_text()


def mmdc_available(cfg: MermaidConfig) -> bool:
    return shutil.which(cfg.mmdc_path) is not None


def extract_mermaid_sources(md: str) -> list[str]:
    """Extract every fenced ```mermaid block in document order.

    Returns the inner source text per block (without the ``` ```mermaid``` /
    ``` ``` ``` fences, no trailing newline). Used to align with mmdc's PNG
    output naming (`<stem>-1.png, -2.png, ...`) so we can map upload tokens
    back to their original mermaid source for round-trip restore.

    Only top-level fences count; we never expect mermaid inside another fence
    so a tiny line scanner is enough.
    """
    sources: list[str] = []
    in_block = False
    cur: list[str] = []
    for line in md.split("\n"):
        stripped = line.strip()
        if not in_block:
            if stripped == "```mermaid":
                in_block = True
                cur = []
        else:
            if stripped == "```":
                sources.append("\n".join(cur))
                in_block = False
            else:
                cur.append(line)
    return sources


def preprocess_mermaid(
    md_path: Path, out_dir: Path, cfg: MermaidConfig,
) -> tuple[Path, list[tuple[str, Path]]]:
    """跑 mmdc，返回 (rendered_md_path, [(source, png_path), ...])。
    无 mermaid 块或 mmdc 不可用时直接返回原文件 + 空列表。

    `source` 是 mermaid 块的原始文本（用于 reverse-sync 还原）；列表顺序与
    PNG 对应（mmdc 输出 `<stem>-1.png, -2.png, ...` 按出现顺序，与源码出现
    顺序一致）。
    """
    out_md = out_dir / md_path.name
    raw = md_path.read_text()
    if not cfg.enabled or not has_mermaid(md_path):
        out_md.write_text(raw)
        return out_md, []
    if not mmdc_available(cfg):
        # silently fall through; doctor will warn
        out_md.write_text(raw)
        return out_md, []

    sources = extract_mermaid_sources(raw)

    cmd = [
        cfg.mmdc_path, "-i", str(md_path), "-o", str(out_md),
        "-e", "png", "-b", "transparent", "--scale", "2",
    ]
    if cfg.puppeteer_config:
        cmd += ["-p", cfg.puppeteer_config]
    subprocess.run(cmd, check=True, capture_output=True)
    pngs = sorted(out_dir.glob(f"{md_path.stem}-*.png"))
    if len(pngs) != len(sources):
        # fence/PNG mismatch — emit pairs only for the overlap; the rest fall
        # back to plain image refs on next pull.
        n = min(len(pngs), len(sources))
        return out_md, list(zip(sources[:n], pngs[:n]))
    return out_md, list(zip(sources, pngs))
