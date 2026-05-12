"""Obsidian vault tool definition — watches the user's vault for markdown notes.

Auto-discovery: if no vault_path is passed (the desktop app doesn't set one,
the install script only sets one if user opted in), we read Obsidian's own
config file (`obsidian.json`) to find their most recently opened vault. This
removes the "ask user for vault path" step from the install flow — if you
have Obsidian, we find your vault; if not, the tool reports unavailable and
skips itself silently.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ..config import CollectorConfig
from .base import (
    BaseTool, Category, ContentType, FileClassification, SyncStrategy, WatchPath,
)


def _obsidian_config_candidates() -> list[Path]:
    """Possible locations of obsidian.json across platforms."""
    home = Path.home()
    paths: list[Path] = []
    if sys.platform == "darwin":
        paths.append(home / "Library" / "Application Support" / "obsidian" / "obsidian.json")
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(Path(appdata) / "obsidian" / "obsidian.json")
    else:
        paths.append(home / ".config" / "obsidian" / "obsidian.json")
    return paths


def _autodiscover_vault() -> Path | None:
    """Return the most-recently-opened vault from obsidian.json, or None.

    obsidian.json shape:
      {"vaults": {"<hash>": {"path": "/abs/path", "ts": <unix-ms>, "open": true}}}

    We pick the vault with the highest `ts` (most recent open) whose path
    still exists on disk — a deleted-but-not-cleaned vault entry would
    otherwise make us watch a non-existent dir and never sync anything.
    """
    for cfg in _obsidian_config_candidates():
        if not cfg.exists():
            continue
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            continue
        vaults = data.get("vaults") or {}
        candidates = []
        for entry in vaults.values():
            path_str = entry.get("path")
            if not path_str:
                continue
            p = Path(path_str)
            if p.exists() and p.is_dir():
                candidates.append((entry.get("ts", 0), p))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
    return None


class ObsidianTool(BaseTool):

    def __init__(self, vault_path: Path | None = None) -> None:
        # Precedence: explicit constructor arg > MEMENTO_OBSIDIAN_VAULT_PATH
        # env (via CollectorConfig) > auto-discover from obsidian.json > None.
        if vault_path:
            self._vault_path = vault_path
        else:
            cfg_path = CollectorConfig().obsidian_vault_path
            if cfg_path and Path(cfg_path).exists():
                self._vault_path = Path(cfg_path)
            else:
                self._vault_path = _autodiscover_vault()

    def is_available(self) -> bool:
        return self._vault_path is not None and self._vault_path.exists()

    @property
    def name(self) -> str:
        return "obsidian"

    @property
    def display_name(self) -> str:
        return "Obsidian"

    @property
    def root_path(self) -> Path:
        return self._vault_path

    def get_watch_paths(self) -> list[WatchPath]:
        if not self._vault_path:
            return []
        return [
            WatchPath(
                path=self._vault_path,
                pattern="**/*.md",
                category=Category.NOTE,
                content_type=ContentType.MARKDOWN,
                recursive=True,
                description="All markdown notes in the vault",
            ),
        ]

    def classify_file(self, abs_path: Path) -> FileClassification | None:
        if not self._vault_path:
            return None
        try:
            rel = abs_path.relative_to(self._vault_path)
        except ValueError:
            return None

        # Skip .obsidian config directory and .trash
        parts = rel.parts
        if parts and parts[0] in (".obsidian", ".trash"):
            return None

        # Only markdown files
        if abs_path.suffix != ".md":
            return None

        rel_str = str(rel).replace("\\", "/")

        # Infer category from folder structure
        metadata: dict = {"vault_name": self._vault_path.name}
        if len(parts) >= 2:
            metadata["folder"] = parts[0]

        return FileClassification(
            tool_name=self.name,
            category=Category.NOTE,
            content_type=ContentType.MARKDOWN,
            sync_strategy=SyncStrategy.FULL,
            relative_path=rel_str,
            metadata=metadata,
        )

    @property
    def excluded_paths(self) -> list[str]:
        if not self._vault_path:
            return []
        root = str(self._vault_path)
        return [
            f"{root}/.obsidian/**",
            f"{root}/.trash/**",
        ]
