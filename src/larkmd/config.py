"""Config — load and validate larkmd.yaml.

Schema documented in README.md; data model below.

To be implemented in Phase B:
- ${VAR} env interpolation
- dataclass validation
- helpers: drive_folder_for(rel_path), wiki_parent_for(rel_path), display_name_for(rel_path)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from larkmd.errors import ConfigError

_ENV_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


@dataclass
class LarkConfig:
    tenant: str
    profile: str = "feishu"
    cli_path: str = "lark-cli"


@dataclass
class WikiConfig:
    space_id: str


@dataclass
class PathsConfig:
    root: Path
    state_file: Path
    ignore: list[str] = field(default_factory=list)


@dataclass
class RootFilesConfig:
    order: list[str]
    drive_folder: str
    wiki_parent: str = ""


@dataclass
class SectionConfig:
    dir: str
    title_prefix: str
    drive_folder: str
    wiki_parent: str
    order: list[str] = field(default_factory=list)


@dataclass
class NamingConfig:
    numeric_prefix: bool = True
    strip_md_extension: bool = True


@dataclass
class MermaidConfig:
    enabled: bool = True
    mmdc_path: str = "mmdc"
    puppeteer_config: str | None = None
    cache_dir: str = ".larkmd-cache/mermaid"


@dataclass
class ImporterConfig:
    move_max_retries: int = 30
    move_retry_interval_sec: float = 1.5
    argv_threshold_bytes: int = 100_000


@dataclass
class Config:
    version: int
    lark: LarkConfig
    wiki: WikiConfig
    paths: PathsConfig
    root_files: RootFilesConfig
    sections: list[SectionConfig]
    naming: NamingConfig
    mermaid: MermaidConfig
    importer: ImporterConfig
    config_path: Path

    @classmethod
    def load(cls, path: str | Path) -> Config:
        p = Path(path).resolve()
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        raw = yaml.safe_load(p.read_text()) or {}
        return cls._from_dict(raw, config_path=p)

    @classmethod
    def _from_dict(cls, raw: dict[str, Any], *, config_path: Path) -> Config:
        version = raw.get("version", 1)
        if version != 1:
            raise ConfigError(f"unsupported config version: {version} (only v1 supported)")

        repo_root = config_path.parent

        def interp(s: str) -> str:
            def sub(m):
                var = m.group(1)
                val = os.environ.get(var)
                if val is None:
                    raise ConfigError(f"env var ${{{var}}} referenced in config but not set")
                return val
            return _ENV_VAR_RE.sub(sub, s) if isinstance(s, str) else s

        def deep_interp(obj):
            if isinstance(obj, str):
                return interp(obj)
            if isinstance(obj, dict):
                return {k: deep_interp(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [deep_interp(v) for v in obj]
            return obj

        raw = deep_interp(raw)

        lark_raw = raw.get("lark") or {}
        if not lark_raw.get("tenant"):
            raise ConfigError("lark.tenant is required")
        lark = LarkConfig(**lark_raw)

        wiki_raw = raw.get("wiki") or {}
        if not wiki_raw.get("space_id"):
            raise ConfigError("wiki.space_id is required")
        wiki = WikiConfig(**wiki_raw)

        paths_raw = raw.get("paths") or {}
        paths = PathsConfig(
            root=Path(paths_raw.get("root", ".")).resolve() if Path(paths_raw.get("root", ".")).is_absolute() else (repo_root / paths_raw.get("root", ".")).resolve(),
            state_file=Path(paths_raw.get("state_file", ".feishu-sync-state.json")),
            ignore=paths_raw.get("ignore", []),
        )
        if not paths.state_file.is_absolute():
            paths.state_file = paths.root / paths.state_file

        rf_raw = raw.get("root_files") or {}
        if "drive_folder" not in rf_raw:
            raise ConfigError("root_files.drive_folder is required")
        root_files = RootFilesConfig(
            order=rf_raw.get("order", []),
            drive_folder=rf_raw["drive_folder"],
            wiki_parent=rf_raw.get("wiki_parent", ""),
        )

        sections = []
        for s in raw.get("sections", []) or []:
            for k in ("dir", "title_prefix", "drive_folder", "wiki_parent"):
                if k not in s:
                    raise ConfigError(f"section missing required field: {k}")
            sections.append(SectionConfig(
                dir=s["dir"],
                title_prefix=s["title_prefix"],
                drive_folder=s["drive_folder"],
                wiki_parent=s["wiki_parent"],
                order=s.get("order", []),
            ))

        naming = NamingConfig(**(raw.get("naming") or {}))
        mermaid = MermaidConfig(**(raw.get("mermaid") or {}))
        importer = ImporterConfig(**(raw.get("importer") or {}))

        return cls(
            version=version,
            lark=lark,
            wiki=wiki,
            paths=paths,
            root_files=root_files,
            sections=sections,
            naming=naming,
            mermaid=mermaid,
            importer=importer,
            config_path=config_path,
        )

    # ----- helpers used by syncer/importer -----

    def section_for(self, rel_path: str) -> SectionConfig | None:
        """Return the section a given relative path belongs to, or None for root files."""
        parts = rel_path.split("/")
        if len(parts) == 1:
            return None
        for s in self.sections:
            if s.dir == parts[0]:
                return s
        raise ConfigError(f"no section configured for directory: {parts[0]}")

    def drive_folder_for(self, rel_path: str) -> str:
        s = self.section_for(rel_path)
        return s.drive_folder if s else self.root_files.drive_folder

    def wiki_parent_for(self, rel_path: str) -> str:
        s = self.section_for(rel_path)
        return s.wiki_parent if s else self.root_files.wiki_parent

    def display_name_for(self, rel_path: str) -> str:
        """Generate the wiki node title for a given markdown file.

        Naming rules (controlled by NamingConfig):
        - root files: stem (or full name)
        - section files: "<title_prefix>-<index>-<stem>" e.g. "01-准备-1.1-checklist"
          when numeric_prefix=True, otherwise just "<title_prefix>-<stem>"
        """
        from pathlib import PurePosixPath

        p = PurePosixPath(rel_path)
        stem = p.stem if self.naming.strip_md_extension else p.name
        s = self.section_for(rel_path)
        if s is None:
            return stem
        if not self.naming.numeric_prefix:
            return f"{s.title_prefix}-{stem}"
        order = s.order or []
        try:
            idx = order.index(p.name) + 1
        except ValueError:
            idx = len(order) + 1  # not in explicit order → tail
        return f"{s.title_prefix}-{idx}-{stem}"
