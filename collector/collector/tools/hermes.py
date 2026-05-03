"""Hermes Agent tool definition — watches ~/.hermes/ for sessions, persona, skills.

Hermes Agent (NousResearch, https://github.com/NousResearch/hermes-agent) is a
self-improving autonomous agent. Local layout we care about:

  ~/.hermes/sessions/session_*.json     conversation transcripts
  ~/.hermes/SOUL.md                     agent persona / instructions
  ~/.hermes/skills/<name>/DESCRIPTION.md
                                        skill description files
  ~/.hermes/state.db                    SQLite source of truth
  ~/.hermes/.hermes_history             CLI input history (plain text)

Excluded for privacy / noise:
  .env, auth.json, auth.lock            credentials
  config.yaml                           may include API keys / base URLs
  audio_cache/, image_cache/, logs/     multimedia + logs
  hermes-agent/                         git checkout of source code
  sandboxes/, hooks/, pairing/, cron/   runtime infra
  whatsapp/                             gateway integration data
  models_dev_cache.json                 internal cache
  .skills_prompt_snapshot.json          internal cache
  state.db-wal, state.db-shm            SQLite WAL/SHM
  sessions/request_dump_*.json          raw API request dumps
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import TOOL_PATHS
from .base import (
    BaseTool, Category, ContentType, FileClassification, SyncStrategy, WatchPath,
)


_SKIP_TOP_DIRS = frozenset({
    "audio_cache", "image_cache", "logs", "hermes-agent",
    "sandboxes", "hooks", "pairing", "whatsapp", "cron", "bin",
    "memories",
})

_SKIP_NAMES = frozenset({
    ".env", "auth.json", "auth.lock",
    ".update_check", ".skills_prompt_snapshot.json",
    "models_dev_cache.json", "config.yaml", "context_length_cache.yaml",
    "state.db-wal", "state.db-shm",
})


class HermesTool(BaseTool):

    @property
    def name(self) -> str:
        return "hermes"

    @property
    def display_name(self) -> str:
        return "Hermes"

    @property
    def root_path(self) -> Path:
        return TOOL_PATHS["hermes"]

    def get_watch_paths(self) -> list[WatchPath]:
        root = self.root_path
        return [
            WatchPath(
                path=root,
                pattern="SOUL.md",
                category=Category.IDENTITY,
                content_type=ContentType.MARKDOWN,
                description="Agent persona and tone",
            ),
            WatchPath(
                path=root,
                pattern=".hermes_history",
                category=Category.HISTORY,
                content_type=ContentType.TEXT,
                sync_strategy=SyncStrategy.DELTA,
                description="CLI input history",
            ),
            WatchPath(
                path=root / "sessions",
                pattern="session_*.json",
                category=Category.CONVERSATION,
                content_type=ContentType.JSON,
                sync_strategy=SyncStrategy.FULL,
                description="Conversation session transcripts",
            ),
            WatchPath(
                path=root / "skills",
                pattern="**/DESCRIPTION.md",
                category=Category.SKILL,
                content_type=ContentType.MARKDOWN,
                recursive=True,
                description="Skill description files",
            ),
            WatchPath(
                path=root,
                pattern="state.db",
                category=Category.STATE,
                content_type=ContentType.SQLITE,
                sync_strategy=SyncStrategy.POLL,
                description="Sessions + messages + cost SQLite database",
            ),
        ]

    def classify_file(self, abs_path: Path) -> FileClassification | None:
        try:
            rel = abs_path.relative_to(self.root_path)
        except ValueError:
            return None

        rel_str = str(rel).replace("\\", "/")
        parts = rel.parts
        name = abs_path.name

        if parts and parts[0] in _SKIP_TOP_DIRS:
            return None
        if name in _SKIP_NAMES:
            return None

        if rel_str == "SOUL.md":
            return FileClassification(
                tool_name=self.name,
                category=Category.IDENTITY,
                content_type=ContentType.MARKDOWN,
                sync_strategy=SyncStrategy.FULL,
                relative_path=rel_str,
            )

        if rel_str == ".hermes_history":
            return FileClassification(
                tool_name=self.name,
                category=Category.HISTORY,
                content_type=ContentType.TEXT,
                sync_strategy=SyncStrategy.DELTA,
                relative_path=rel_str,
            )

        # Sessions: only session_*.json (skip request_dump_*.json)
        if parts and parts[0] == "sessions":
            if name.startswith("session_") and name.endswith(".json"):
                meta = self._enrich_session_meta(abs_path)
                meta["session_name"] = abs_path.stem
                # Hermes has no per-directory project concept (it's a general
                # cross-task agent). Group all sessions under a single virtual
                # 'hermes' project so the Web Projects page surfaces them
                # rather than leaving them orphaned.
                meta["project_hash"] = "hermes"
                meta["project_path"] = str(self.root_path)
                return FileClassification(
                    tool_name=self.name,
                    category=Category.CONVERSATION,
                    content_type=ContentType.JSON,
                    sync_strategy=SyncStrategy.FULL,
                    relative_path=rel_str,
                    metadata=meta,
                )
            return None

        if parts and parts[0] == "skills":
            if name == "DESCRIPTION.md":
                meta: dict = {}
                if len(parts) >= 2:
                    meta["skill_name"] = parts[1]
                return FileClassification(
                    tool_name=self.name,
                    category=Category.SKILL,
                    content_type=ContentType.MARKDOWN,
                    sync_strategy=SyncStrategy.FULL,
                    relative_path=rel_str,
                    metadata=meta,
                )
            return None

        if name == "state.db":
            return FileClassification(
                tool_name=self.name,
                category=Category.STATE,
                content_type=ContentType.SQLITE,
                sync_strategy=SyncStrategy.POLL,
                relative_path=rel_str,
            )

        return None

    @staticmethod
    def _enrich_session_meta(abs_path: Path) -> dict:
        """Pull title/model/platform/first_user_message from session JSON."""
        meta: dict = {}
        try:
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                d = json.load(f)
        except Exception:
            return meta

        if not isinstance(d, dict):
            return meta

        for k in ("model", "platform", "session_id", "session_start", "last_updated"):
            v = d.get(k)
            if v:
                meta[k] = v

        messages = d.get("messages") or []
        for m in messages:
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                meta["first_user_message"] = content.strip()[:200]
                break
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "").strip()
                        if text:
                            meta["first_user_message"] = text[:200]
                            break
                if "first_user_message" in meta:
                    break
        return meta

    @property
    def excluded_paths(self) -> list[str]:
        root = str(self.root_path)
        return [
            f"{root}/.env",
            f"{root}/auth.json",
            f"{root}/auth.lock",
            f"{root}/.update_check",
            f"{root}/.skills_prompt_snapshot.json",
            f"{root}/models_dev_cache.json",
            f"{root}/config.yaml",
            f"{root}/context_length_cache.yaml",
            f"{root}/state.db-wal",
            f"{root}/state.db-shm",
            f"{root}/audio_cache/**",
            f"{root}/image_cache/**",
            f"{root}/logs/**",
            f"{root}/hermes-agent/**",
            f"{root}/sandboxes/**",
            f"{root}/hooks/**",
            f"{root}/pairing/**",
            f"{root}/whatsapp/**",
            f"{root}/cron/**",
            f"{root}/bin/**",
            f"{root}/memories/**",
            f"{root}/sessions/request_dump_*.json",
        ]
