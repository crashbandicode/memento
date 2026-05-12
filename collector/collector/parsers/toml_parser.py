"""TOML parser — reads TOML config files."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Prefer stdlib tomllib (Python 3.11+) — it's a no-op for PyInstaller to
# bundle (no extension modules) and avoids tomli 2.x's mypyc compiled .pyd
# that PyInstaller's static analyzer misses, blowing up the frozen binary
# with `ModuleNotFoundError: No module named '<hash>_mypyc'`.
if sys.version_info >= (3, 11):
    import tomllib as tomli  # type: ignore[import-not-found]
else:
    import tomli  # type: ignore[import-not-found]

from .base import BaseParser, ParseResult


class TomlParser(BaseParser):

    def can_parse(self, path: Path) -> bool:
        return path.suffix.lower() == ".toml"

    def parse(self, path: Path, offset: int = 0) -> ParseResult:
        raw = path.read_bytes()
        raw_text = raw.decode("utf-8", errors="replace")

        try:
            data = tomli.loads(raw_text)
        except tomli.TOMLDecodeError:
            return ParseResult(
                content=raw_text,
                title=path.stem,
                metadata={"parse_error": True},
                line_count=raw_text.count("\n") + 1,
            )

        metadata: dict = {"top_level_keys": list(data.keys())[:20]}

        # Store both original TOML and parsed JSON representation
        content = raw_text

        return ParseResult(
            content=content,
            title=path.stem,
            metadata={**metadata, "parsed": data},
            line_count=content.count("\n") + 1,
        )
