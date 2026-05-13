"""Mermaid preprocessing — invoke `mmdc` (mermaid-cli) to render fenced
``` ```mermaid blocks ``` into PNGs the importer can pick up.

Optional: if mmdc isn't on PATH, callers should fall through to plain markdown.
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


def preprocess_mermaid(
    md_path: Path, out_dir: Path, cfg: MermaidConfig,
) -> tuple[Path, list[Path]]:
    """跑 mmdc，返回 (rendered_md_path, [png_paths])。
    无 mermaid 块或 mmdc 不可用时直接返回原文件 + 空列表。"""
    out_md = out_dir / md_path.name
    if not cfg.enabled or not has_mermaid(md_path):
        out_md.write_text(md_path.read_text())
        return out_md, []
    if not mmdc_available(cfg):
        # silently fall through; doctor will warn
        out_md.write_text(md_path.read_text())
        return out_md, []

    cmd = [
        cfg.mmdc_path, "-i", str(md_path), "-o", str(out_md),
        "-e", "png", "-b", "transparent", "--scale", "2",
    ]
    if cfg.puppeteer_config:
        cmd += ["-p", cfg.puppeteer_config]
    subprocess.run(cmd, check=True, capture_output=True)
    pngs = sorted(out_dir.glob(f"{md_path.stem}-*.png"))
    return out_md, pngs
