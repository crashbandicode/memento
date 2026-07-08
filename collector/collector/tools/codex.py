"""Codex tool definition — watches ~/.codex/ for sessions, history, config."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

from ..config import TOOL_PATHS
from .base import (
    BaseTool, Category, ContentType, FileClassification, SyncStrategy, WatchPath,
)

_SKIP_DIRS = {"users", "user", "home", "desktop", "dev", "documents",
              "python", "projects", "src", "code"}

# Cache: thread_id → {title, first_user_message}
_thread_info_cache: dict[str, dict] | None = None
_thread_info_cache_signature: tuple[object, ...] | None = None
_thread_info_lock = threading.RLock()


_history_cache: dict[str, list[dict]] | None = None


def _load_history(codex_home: Path) -> dict[str, list[dict]]:
    """Read history.jsonl — maps session_id → list of {ts, text} user inputs."""
    global _history_cache
    if _history_cache is not None:
        return _history_cache

    history_file = codex_home / "history.jsonl"
    result: dict[str, list[dict]] = {}
    if not history_file.exists():
        _history_cache = result
        return result

    try:
        with open(history_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                sid = obj.get("session_id", "")
                text = obj.get("text", "")
                ts = obj.get("ts", 0)
                if sid and text:
                    result.setdefault(sid, []).append({"ts": ts, "text": text})
    except Exception:
        pass
    _history_cache = result
    return result


def _state_db_signature(state_db: Path) -> tuple[object, ...]:
    """Include SQLite's WAL because title writes may not touch the main file."""
    parts: list[object] = [str(state_db.resolve())]
    for path in (state_db, Path(f"{state_db}-wal")):
        try:
            stat = path.stat()
            parts.extend((stat.st_size, stat.st_mtime_ns))
        except OSError:
            parts.extend((None, None))
    return tuple(parts)


def _load_threads_from_sqlite(
    codex_home: Path,
    *,
    force_refresh: bool = False,
) -> dict[str, dict]:
    """Read thread titles and first_user_message from state_5.sqlite."""
    global _thread_info_cache, _thread_info_cache_signature

    state_db = codex_home / "state_5.sqlite"
    if not state_db.exists():
        return {}

    signature = _state_db_signature(state_db)
    with _thread_info_lock:
        if (
            not force_refresh
            and _thread_info_cache is not None
            and _thread_info_cache_signature == signature
        ):
            return _thread_info_cache

    result: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
        try:
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(threads)")
            }
            first_message = (
                "first_user_message" if "first_user_message" in columns else "''"
            )
            if "updated_at_ms" in columns:
                revision = (
                    "COALESCE(NULLIF(updated_at_ms, 0), updated_at * 1000, 0)"
                )
            elif "updated_at" in columns:
                revision = "COALESCE(updated_at * 1000, 0)"
            else:
                revision = "0"
            rollout_path = "rollout_path" if "rollout_path" in columns else "''"
            thread_source = "thread_source" if "thread_source" in columns else "''"
            agent_path = "agent_path" if "agent_path" in columns else "''"
            cursor = conn.execute(
                f"SELECT id, title, {first_message}, {revision}, {rollout_path}, "
                f"{thread_source}, {agent_path} "
                "FROM threads"
            )
            for row in cursor.fetchall():
                (
                    tid,
                    title,
                    first_msg,
                    source_revision,
                    source_path,
                    source_kind,
                    source_agent_path,
                ) = row
                if tid:
                    result[str(tid)] = {
                        "title": title or "",
                        "first_user_message": first_msg or "",
                        "revision": max(0, int(source_revision or 0)),
                        "rollout_path": source_path or "",
                        "thread_source": source_kind or "",
                        "agent_path": source_agent_path or "",
                    }
        finally:
            conn.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        # A concurrent Codex checkpoint can briefly make the read fail. Keep
        # the previous complete snapshot instead of treating every row as gone.
        with _thread_info_lock:
            return _thread_info_cache or {}
    with _thread_info_lock:
        _thread_info_cache = result
        _thread_info_cache_signature = _state_db_signature(state_db)
        return _thread_info_cache


class CodexTool(BaseTool):

    @property
    def name(self) -> str:
        return "codex"

    @property
    def display_name(self) -> str:
        return "Codex"

    @property
    def root_path(self) -> Path:
        return TOOL_PATHS["codex"]

    def get_watch_paths(self) -> list[WatchPath]:
        root = self.root_path
        return [
            # Config
            WatchPath(
                path=root,
                pattern="config.toml",
                category=Category.CONFIG,
                content_type=ContentType.TOML,
                description="Main config: model, reasoning level, personality",
            ),
            # AGENTS.md
            WatchPath(
                path=root,
                pattern="AGENTS.md",
                category=Category.IDENTITY,
                content_type=ContentType.MARKDOWN,
                description="Agent instructions",
            ),
            # History
            WatchPath(
                path=root,
                pattern="history.jsonl",
                category=Category.HISTORY,
                content_type=ContentType.JSONL,
                sync_strategy=SyncStrategy.DELTA,
                description="Session command history",
            ),
            # Active sessions — FULL sync to avoid DELTA truncation of user_message
            WatchPath(
                path=root / "sessions",
                pattern="**/*.jsonl",
                category=Category.CONVERSATION,
                content_type=ContentType.JSONL,
                sync_strategy=SyncStrategy.FULL,
                recursive=True,
                description="Conversation session transcripts",
            ),
            # Archived sessions
            WatchPath(
                path=root / "archived_sessions",
                pattern="*.jsonl",
                category=Category.CONVERSATION,
                content_type=ContentType.JSONL,
                sync_strategy=SyncStrategy.FULL,
                description="Archived conversation sessions",
            ),
            # SQLite logs (polled)
            WatchPath(
                path=root,
                pattern="logs_1.sqlite",
                category=Category.STATE,
                content_type=ContentType.SQLITE,
                sync_strategy=SyncStrategy.POLL,
                description="Structured log database",
            ),
            # SQLite state (polled)
            WatchPath(
                path=root,
                pattern="state_5.sqlite",
                category=Category.STATE,
                content_type=ContentType.SQLITE,
                sync_strategy=SyncStrategy.POLL,
                description="Threads and jobs state database",
            ),
        ]

    def classify_file(self, abs_path: Path) -> FileClassification | None:
        try:
            rel = abs_path.relative_to(self.root_path)
        except ValueError:
            return None

        rel_str = str(rel).replace("\\", "/")
        parts = rel.parts

        # Exclude auth, cache, tmp, log, shell snapshots
        skip_dirs = {"cache", "tmp", ".tmp", "log", "shell_snapshots"}
        if parts and parts[0] in skip_dirs:
            return None

        # Exclude auth.json entirely
        if rel_str == "auth.json":
            return None

        # config.toml
        if rel_str == "config.toml":
            return FileClassification(
                tool_name=self.name,
                category=Category.CONFIG,
                content_type=ContentType.TOML,
                sync_strategy=SyncStrategy.FULL,
                relative_path=rel_str,
            )

        # AGENTS.md
        if rel_str == "AGENTS.md":
            return FileClassification(
                tool_name=self.name,
                category=Category.IDENTITY,
                content_type=ContentType.MARKDOWN,
                sync_strategy=SyncStrategy.FULL,
                relative_path=rel_str,
            )

        # history.jsonl
        if rel_str == "history.jsonl":
            return FileClassification(
                tool_name=self.name,
                category=Category.HISTORY,
                content_type=ContentType.JSONL,
                sync_strategy=SyncStrategy.DELTA,
                relative_path=rel_str,
            )

        # Active sessions
        if parts[0] == "sessions" and abs_path.suffix == ".jsonl":
            session_meta = self._read_initial_session_meta(abs_path)
            project_name, project_path = self._extract_cwd_from_meta(session_meta)
            meta: dict = {"session_name": abs_path.stem}
            if project_name:
                meta["project_hash"] = project_name
            if project_path:
                meta["project_path"] = project_path
            identity = self._extract_session_identity(abs_path, session_meta)
            meta.update(identity)
            self._enrich_with_thread_info(
                abs_path,
                meta,
                thread_id=identity.get("thread_id"),
            )
            return FileClassification(
                tool_name=self.name,
                category=Category.CONVERSATION,
                content_type=ContentType.JSONL,
                sync_strategy=SyncStrategy.FULL,
                relative_path=rel_str,
                metadata=meta,
            )

        # Archived sessions
        if parts[0] == "archived_sessions" and abs_path.suffix == ".jsonl":
            session_meta = self._read_initial_session_meta(abs_path)
            project_name, project_path = self._extract_cwd_from_meta(session_meta)
            meta = {"session_name": abs_path.stem, "archived": True}
            if project_name:
                meta["project_hash"] = project_name
            if project_path:
                meta["project_path"] = project_path
            identity = self._extract_session_identity(abs_path, session_meta)
            meta.update(identity)
            self._enrich_with_thread_info(
                abs_path,
                meta,
                thread_id=identity.get("thread_id"),
            )
            return FileClassification(
                tool_name=self.name,
                category=Category.CONVERSATION,
                content_type=ContentType.JSONL,
                sync_strategy=SyncStrategy.FULL,
                relative_path=rel_str,
                metadata=meta,
            )

        # version.json
        if rel_str == "version.json":
            return FileClassification(
                tool_name=self.name,
                category=Category.CONFIG,
                content_type=ContentType.JSON,
                sync_strategy=SyncStrategy.FULL,
                relative_path=rel_str,
            )

        # SQLite databases
        if abs_path.name in ("logs_1.sqlite", "state_5.sqlite"):
            return FileClassification(
                tool_name=self.name,
                category=Category.STATE,
                content_type=ContentType.SQLITE,
                sync_strategy=SyncStrategy.POLL,
                relative_path=rel_str,
            )

        # Skip vendor_imports (built-in skill templates, not user data)
        if parts[0] == "vendor_imports":
            return None

        # models_cache.json — useful for tracking model availability
        if rel_str == "models_cache.json":
            return FileClassification(
                tool_name=self.name,
                category=Category.CONFIG,
                content_type=ContentType.JSON,
                sync_strategy=SyncStrategy.FULL,
                relative_path=rel_str,
            )

        return None

    def _enrich_with_thread_info(
        self,
        abs_path: Path,
        meta: dict,
        *,
        thread_id: str | None = None,
    ) -> None:
        """Add title, first_user_message, and history from sqlite + history.jsonl."""
        thread_id = thread_id or self._extract_thread_id(abs_path)
        if not thread_id:
            return
        # Thread info from sqlite (title + first prompt)
        threads = _load_threads_from_sqlite(self.root_path)
        info = threads.get(thread_id)
        if info:
            if info.get("title"):
                meta["title"] = info["title"]
            if info.get("first_user_message"):
                meta["first_user_message"] = info["first_user_message"]
        # User input history from history.jsonl (all user messages for this session)
        history = _load_history(self.root_path)
        user_inputs = history.get(thread_id, [])
        if user_inputs:
            meta["user_history"] = user_inputs

    def thread_title_records(self) -> dict[str, dict]:
        """Return a fresh, compact state snapshot for explicit-rename polling."""
        threads = _load_threads_from_sqlite(self.root_path, force_refresh=True)
        records: dict[str, dict] = {}
        for thread_id, info in threads.items():
            thread_source = str(info.get("thread_source") or "").strip().lower()
            if thread_source not in {"", "root", "user"}:
                continue
            if not thread_source and str(info.get("agent_path") or "").strip():
                continue
            title = str(info.get("title") or "").strip()[:500]
            if not title:
                continue
            record = {
                "metadata_type": "codex_thread_title",
                "tool": self.name,
                "thread_id": thread_id,
                "title": title,
                "revision": max(0, int(info.get("revision") or 0)),
            }
            relative_path = self._state_rollout_relative_path(
                str(info.get("rollout_path") or "")
            )
            if relative_path and len(relative_path) <= 2000:
                record["relative_path"] = relative_path
            records[thread_id] = record
        return records

    def _state_rollout_relative_path(self, rollout_path: str) -> str:
        """Normalize the state DB rollout path to the collector document key."""
        normalized = rollout_path.replace("\\", "/").strip()
        if not normalized:
            return ""
        root = str(self.root_path).replace("\\", "/").rstrip("/")
        prefix = f"{root}/"
        if normalized.casefold().startswith(prefix.casefold()):
            return normalized[len(prefix):]
        for marker in ("/archived_sessions/", "/sessions/"):
            index = normalized.casefold().find(marker)
            if index >= 0:
                return normalized[index + 1:]
        return ""

    @staticmethod
    def _read_initial_session_meta(abs_path: Path) -> dict:
        """Read payload from the first nonblank record when it is session_meta.

        Codex subagent rollouts include inherited session_meta records later in
        the file.  Those describe ancestors and must never replace the rollout's
        own identity from the initial record.
        """
        try:
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if obj.get("type") != "session_meta":
                        return {}
                    payload = obj.get("payload")
                    return payload if isinstance(payload, dict) else {}
        except Exception:
            pass
        return {}

    @staticmethod
    def _filename_thread_id(abs_path: Path) -> str:
        """Extract a thread UUID from a rollout filename."""
        name = abs_path.stem
        match = re.search(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12})",
            name,
            re.I,
        )
        return match.group(1) if match else ""

    @staticmethod
    def _metadata_string(metadata: dict, key: str) -> str:
        value = metadata.get(key)
        return value.strip() if isinstance(value, str) else ""

    @classmethod
    def _extract_session_identity(
        cls,
        abs_path: Path,
        session_meta: dict,
    ) -> dict:
        """Normalize initial Codex lineage fields into collector metadata."""
        thread_id = (
            cls._metadata_string(session_meta, "id")
            or cls._filename_thread_id(abs_path)
        )
        if not thread_id:
            return {}

        # Collector session_id remains the current rollout/thread identity.
        # Raw Codex payload.session_id is the root session for subagent forks.
        root_session_id = (
            cls._metadata_string(session_meta, "session_id") or thread_id
        )
        result: dict = {
            "session_id": thread_id,
            "thread_id": thread_id,
            "root_session_id": root_session_id,
        }

        source = session_meta.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawn = (
            subagent.get("thread_spawn")
            if isinstance(subagent, dict)
            else None
        )
        spawn = spawn if isinstance(spawn, dict) else {}

        string_fields = {
            "thread_source": cls._metadata_string(session_meta, "thread_source"),
            "parent_thread_id": (
                cls._metadata_string(session_meta, "parent_thread_id")
                or cls._metadata_string(spawn, "parent_thread_id")
            ),
            "forked_from_id": cls._metadata_string(
                session_meta,
                "forked_from_id",
            ),
            "agent_path": (
                cls._metadata_string(session_meta, "agent_path")
                or cls._metadata_string(spawn, "agent_path")
            ),
            "agent_nickname": (
                cls._metadata_string(session_meta, "agent_nickname")
                or cls._metadata_string(spawn, "agent_nickname")
            ),
        }
        result.update({key: value for key, value in string_fields.items() if value})

        depth = spawn.get("depth")
        if isinstance(depth, int) and not isinstance(depth, bool):
            result["agent_depth"] = depth
        elif isinstance(depth, str) and depth.isdigit():
            result["agent_depth"] = int(depth)
        return result

    @classmethod
    def _extract_thread_id(cls, abs_path: Path) -> str:
        """Extract current thread UUID from initial session_meta or filename."""
        session_meta = cls._read_initial_session_meta(abs_path)
        return (
            cls._metadata_string(session_meta, "id")
            or cls._filename_thread_id(abs_path)
        )

    @classmethod
    def _extract_cwd_from_meta(
        cls,
        session_meta: dict,
    ) -> tuple[str | None, str | None]:
        """Extract (project_name, full_cwd) from an initial session_meta payload."""
        cwd = cls._metadata_string(session_meta, "cwd")
        if cwd:
            parts = cwd.replace("\\", "/").rstrip("/").split("/")
            meaningful = [
                part
                for part in parts
                if part.lower() not in _SKIP_DIRS and len(part) > 1
            ]
            name = meaningful[-1] if meaningful else None
            return name, cwd
        return None, None

    @classmethod
    def _extract_cwd_from_session(
        cls,
        abs_path: Path,
    ) -> tuple[str | None, str | None]:
        """Back-compatible path-based cwd extraction helper."""
        return cls._extract_cwd_from_meta(cls._read_initial_session_meta(abs_path))

    @property
    def excluded_paths(self) -> list[str]:
        root = str(self.root_path)
        return [
            f"{root}/auth.json",
            f"{root}/cache/**",
            f"{root}/tmp/**",
            f"{root}/.tmp/**",
            f"{root}/log/**",
            f"{root}/shell_snapshots/**",
            f"{root}/vendor_imports/**",
        ]
