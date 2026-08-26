"""Cursor tool definition — watches ~/.cursor/ for conversations, skills, config."""

from __future__ import annotations

import json
import math
import os
import platform
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import unquote

import orjson

from ..config import TOOL_PATHS
from .claude_code import _extract_cwd_from_jsonl
from .base import (
    BaseTool,
    Category,
    ContentType,
    FileClassification,
    SyncStrategy,
    WatchPath,
    path_linked_subagent_identity,
)


_CURSOR_CHATS_STORE_FULL_IDENTITY_VERSION = "cursor-chats-store-v1"


@dataclass(frozen=True)
class CursorChatsStoreMetadata:
    """Conversation facts Cursor keeps beside sparse agent transcripts."""

    started_at: str = ""
    completed_at: str = ""
    model: str = ""


@dataclass(frozen=True)
class _CursorChatsStoreCacheEntry:
    chat_path: Path
    meta_token: tuple[int, int] | None
    store_token: tuple[int, int] | None
    metadata: CursorChatsStoreMetadata


def _path_token(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _iso_cursor_timestamp(value: object) -> str:
    """Normalize Cursor epoch-ms/ISO timestamps using the state-export rules."""
    if value in (None, "") or isinstance(value, bool):
        return ""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        if not math.isfinite(seconds):
            return ""
        while abs(seconds) > 10_000_000_000:
            seconds /= 1000
        try:
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return ""
    if parsed.microsecond == 0:
        timespec = "seconds"
    elif parsed.microsecond % 1000 == 0:
        timespec = "milliseconds"
    else:
        timespec = "microseconds"
    return parsed.isoformat(timespec=timespec).replace("+00:00", "Z")


def _json_mappings(value: object) -> Iterator[dict[str, object]]:
    """Yield nested JSON objects without inspecting text content semantically."""
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            yield current
            pending.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            pending.extend(reversed(current))


def _cursor_model_from_blob(value: object) -> str:
    """Return the first explicit Cursor model name from one chats-store blob."""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    if not isinstance(value, str) or "modelName" not in value:
        return ""
    try:
        decoded = orjson.loads(value)
    except (TypeError, orjson.JSONDecodeError):
        return ""
    for item in _json_mappings(decoded):
        provider_options = item.get("providerOptions")
        if not isinstance(provider_options, dict):
            continue
        cursor_options = provider_options.get("cursor")
        if not isinstance(cursor_options, dict):
            continue
        model = cursor_options.get("modelName")
        if isinstance(model, str) and model.strip():
            return model.strip()[:256]
    return ""


def _cursor_agent_message_count(path: Path) -> int:
    """Count visible Cursor rows so only known conversation bounds get timestamps."""
    count = 0
    try:
        with path.open("rb") as stream:
            for raw_line in stream:
                try:
                    record = orjson.loads(raw_line)
                except (TypeError, orjson.JSONDecodeError):
                    continue
                if isinstance(record, dict) and record.get("role") in {
                    "user",
                    "assistant",
                }:
                    count += 1
    except OSError:
        return 0
    return count


class _CursorAgentTranscriptLineEnricher:
    """Add only conversation-level facts that sparse Cursor rows lack."""

    def __init__(
        self,
        metadata: CursorChatsStoreMetadata,
        *,
        message_count: int,
    ) -> None:
        self._metadata = metadata
        self._message_count = message_count
        self._message_index = 0

    def __call__(self, line: str) -> str:
        try:
            record = orjson.loads(line)
        except (TypeError, orjson.JSONDecodeError):
            return line
        if not isinstance(record, dict) or record.get("role") not in {
            "user",
            "assistant",
        }:
            return line

        self._message_index += 1
        changed = False
        if (
            record.get("role") == "assistant"
            and self._metadata.model
            and not record.get("model")
        ):
            record["model"] = self._metadata.model
            changed = True
        if not record.get("timestamp"):
            if self._message_index == 1 and self._metadata.started_at:
                record["timestamp"] = self._metadata.started_at
                changed = True
            elif (
                self._message_index == self._message_count
                and self._metadata.completed_at
            ):
                record["timestamp"] = self._metadata.completed_at
                changed = True
        if not changed:
            return line
        return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def _load_workspace_storage_map() -> dict[str, str]:
    """Load project_hash → real_path mapping from Cursor's workspaceStorage.

    Cursor (VS Code fork) stores workspace info in:
      ~/Library/Application Support/Cursor/User/workspaceStorage/<hash>/workspace.json
      {"folder": "file:///Users/.../project_name"}

    The <hash> is NOT the same as the project directory hash in ~/.cursor/projects/,
    but the folder URI maps to the same real path.
    """
    import platform

    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        ws_root = (
            home
            / "Library"
            / "Application Support"
            / "Cursor"
            / "User"
            / "workspaceStorage"
        )
    elif system == "Windows":
        import os

        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        ws_root = appdata / "Cursor" / "User" / "workspaceStorage"
    else:
        ws_root = home / ".config" / "Cursor" / "User" / "workspaceStorage"

    if not ws_root.exists():
        return {}

    # Build mapping: for each workspace.json, extract folder URI → real path
    # Then match against project hashes by comparing normalized path endings
    uri_to_path: dict[str, str] = {}
    for ws_dir in ws_root.iterdir():
        wj = ws_dir / "workspace.json"
        if not wj.exists():
            continue
        try:
            data = orjson.loads(wj.read_text(encoding="utf-8"))
            folder = data.get("folder", "")
            if folder.startswith("file:///"):
                decoded = unquote(folder[7:] if system != "Windows" else folder[8:])
                uri_to_path[decoded] = decoded
        except Exception:
            continue

    return uri_to_path


def _match_hash_to_workspace(
    project_hash: str, workspaces: dict[str, str]
) -> str | None:
    """Match a Cursor project hash to a real workspace path.

    Hash: 'Users-haixingdong-Desktop-dev-ft-userdata'
    Path: '/Users/haixingdong/Desktop/dev/ft_userdata'

    Strategy: normalize both to lowercase with separators removed, compare.
    """
    # Normalize hash: strip leading -, replace - with empty for comparison
    hash_norm = project_hash.strip("-").lower().replace("-", "")

    for real_path in workspaces:
        # Normalize path: strip leading /, replace / and _ and - with empty
        path_norm = (
            real_path.strip("/")
            .lower()
            .replace("/", "")
            .replace("_", "")
            .replace("-", "")
            .replace("\\", "")
        )
        if hash_norm == path_norm:
            return real_path

    return None


class CursorTool(BaseTool):
    _project_path_cache: dict[str, str | None] = {}
    _workspace_map: dict[str, str] | None = None

    def __init__(self, state_database_path: Path | None = None) -> None:
        self._state_database_path_override = state_database_path
        self._state_session_ids: frozenset[str] = frozenset()
        self._state_session_ids_checked_at = 0.0
        self._state_session_misses: dict[str, float] = {}
        self._chats_store_cache: dict[str, _CursorChatsStoreCacheEntry] = {}

    @property
    def state_database_path(self) -> Path:
        """Return Cursor's authoritative live composer database."""
        if self._state_database_path_override is not None:
            return self._state_database_path_override
        home = Path.home()
        system = platform.system()
        if system == "Darwin":
            return (
                home
                / "Library"
                / "Application Support"
                / "Cursor"
                / "User"
                / "globalStorage"
                / "state.vscdb"
            )
        if system == "Windows":
            appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
            return appdata / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        return home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"

    def authoritative_session_ids(self, *, max_age: float = 2.0) -> frozenset[str]:
        """Return composers backed by Cursor's live state database.

        The file transcript is a sparse compatibility export in recent Cursor
        releases. Once a normal or subagent composer has materialized data in
        ``state.vscdb``, the state projector owns that session and the file
        watcher must not overwrite its richer snapshot with sparse JSONL.
        """
        now = time.monotonic()
        if now - self._state_session_ids_checked_at < max_age:
            return self._state_session_ids

        database = self.state_database_path
        session_ids: frozenset[str] = frozenset()
        if database.is_file():
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(
                    f"{database.resolve().as_uri()}?mode=ro",
                    uri=True,
                    timeout=1,
                )
                connection.execute("PRAGMA query_only=ON")
                try:
                    rows = connection.execute(
                        """
                        SELECT h.composerId
                        FROM composerHeaders AS h
                        WHERE EXISTS (
                            SELECT 1
                            FROM cursorDiskKV AS kv
                            WHERE kv.key='composerData:' || h.composerId
                        )
                        """
                    )
                except sqlite3.OperationalError:
                    rows = connection.execute("SELECT composerId FROM composerHeaders")
                session_ids = frozenset(str(row[0]) for row in rows if row and row[0])
            except (OSError, sqlite3.Error):
                # Keep the last good view during Cursor's brief database swaps.
                session_ids = self._state_session_ids
            finally:
                if connection is not None:
                    connection.close()

        self._state_session_ids = session_ids
        self._state_session_ids_checked_at = now
        self._state_session_misses.clear()
        return session_ids

    def remember_authoritative_sessions(self, session_ids: set[str]) -> None:
        """Share projector discoveries with the compatibility-file watcher."""
        if not session_ids:
            return
        self._state_session_ids = self._state_session_ids.union(session_ids)
        for session_id in session_ids:
            self._state_session_misses.pop(session_id, None)

    def has_authoritative_state(self, session_id: str, *, max_age: float = 2.0) -> bool:
        """Check one unknown composer instead of rescanning every header."""
        if session_id in self._state_session_ids:
            return True
        now = time.monotonic()
        if now - self._state_session_misses.get(session_id, 0.0) < max_age:
            return False

        database = self.state_database_path
        found = False
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{database.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=1,
            )
            connection.execute("PRAGMA query_only=ON")
            try:
                row = connection.execute(
                    """
                    SELECT 1
                    FROM composerHeaders AS h
                    WHERE h.composerId=?
                      AND EXISTS (
                        SELECT 1 FROM cursorDiskKV AS kv
                        WHERE kv.key='composerData:' || h.composerId
                      )
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                row = connection.execute(
                    "SELECT 1 FROM composerHeaders WHERE composerId=? LIMIT 1",
                    (session_id,),
                ).fetchone()
            found = row is not None
        except (OSError, sqlite3.Error):
            found = False
        finally:
            if connection is not None:
                connection.close()
        if found:
            self.remember_authoritative_sessions({session_id})
        else:
            self._state_session_misses[session_id] = now
        return found

    def _get_workspace_map(self) -> dict[str, str]:
        if self._workspace_map is None:
            self._workspace_map = _load_workspace_storage_map()
        return self._workspace_map

    def _resolve_project_path(self, project_hash: str) -> str | None:
        """Resolve hash to real path via Cursor's workspaceStorage."""
        if project_hash not in self._project_path_cache:
            # Primary: match against workspaceStorage workspace.json mappings
            result = _match_hash_to_workspace(project_hash, self._get_workspace_map())
            if not result:
                # Fallback: try cwd from JSONL
                result = _extract_cwd_from_jsonl(
                    self.root_path / "projects" / project_hash
                )
            self._project_path_cache[project_hash] = result
        return self._project_path_cache[project_hash]

    @property
    def name(self) -> str:
        return "cursor"

    @property
    def display_name(self) -> str:
        return "Cursor"

    @property
    def root_path(self) -> Path:
        return TOOL_PATHS["cursor"]

    def get_watch_paths(self) -> list[WatchPath]:
        if not self.is_available():
            return []
        root = self.root_path
        return [
            # Config
            WatchPath(
                path=root,
                pattern="argv.json",
                category=Category.CONFIG,
                content_type=ContentType.JSON,
                description="Cursor argv configuration",
            ),
            # Extensions
            WatchPath(
                path=root / "extensions",
                pattern="extensions.json",
                category=Category.EXTENSION,
                content_type=ContentType.JSON,
                description="Installed extensions",
            ),
            # Agent transcripts (conversations) — same format as Claude Code
            WatchPath(
                path=root / "projects",
                pattern="**/*.jsonl",
                category=Category.CONVERSATION,
                content_type=ContentType.JSONL,
                sync_strategy=SyncStrategy.DELTA,
                recursive=True,
                description="Agent conversation transcripts",
            ),
            # Project MCP instructions
            WatchPath(
                path=root / "projects",
                pattern="**/*.md",
                category=Category.MEMORY,
                content_type=ContentType.MARKDOWN,
                recursive=True,
                description="MCP instructions and project rules",
            ),
            # Project metadata
            WatchPath(
                path=root / "projects",
                pattern="**/*.json",
                category=Category.CONFIG,
                content_type=ContentType.JSON,
                recursive=True,
                description="Project and MCP metadata",
            ),
            # skills-cursor/ = built-in skill templates (like Codex vendor_imports), skip
            # AI tracking database
            WatchPath(
                path=root / "ai-tracking",
                pattern="*.db",
                category=Category.STATE,
                content_type=ContentType.SQLITE,
                sync_strategy=SyncStrategy.POLL,
                description="AI code tracking database",
            ),
        ]

    def classify_file(self, abs_path: Path) -> FileClassification | None:
        if not self.is_available():
            return None
        try:
            rel = abs_path.relative_to(self.root_path)
        except ValueError:
            return None

        rel_str = str(rel).replace("\\", "/")
        parts = rel.parts

        # Skip cache, crashpad, etc.
        skip = {".gitignore"}
        if abs_path.name in skip:
            return None

        # argv.json
        if rel_str == "argv.json":
            return FileClassification(
                tool_name=self.name,
                category=Category.CONFIG,
                content_type=ContentType.JSON,
                sync_strategy=SyncStrategy.FULL,
                relative_path=rel_str,
            )

        # extensions
        if rel_str == "extensions/extensions.json":
            return FileClassification(
                tool_name=self.name,
                category=Category.EXTENSION,
                content_type=ContentType.JSON,
                sync_strategy=SyncStrategy.FULL,
                relative_path=rel_str,
            )

        # projects/ — agent transcripts
        if parts and parts[0] == "projects":
            if abs_path.suffix == ".jsonl":
                if self.has_authoritative_state(abs_path.stem):
                    return None
                return self.classify_transcript_source(abs_path)
            if abs_path.suffix == ".md":
                return FileClassification(
                    tool_name=self.name,
                    category=Category.MEMORY,
                    content_type=ContentType.MARKDOWN,
                    sync_strategy=SyncStrategy.FULL,
                    relative_path=rel_str,
                )
            if abs_path.suffix == ".json":
                return FileClassification(
                    tool_name=self.name,
                    category=Category.CONFIG,
                    content_type=ContentType.JSON,
                    sync_strategy=SyncStrategy.FULL,
                    relative_path=rel_str,
                )

        # skills-cursor/ = built-in templates, skip
        if parts and parts[0] == "skills-cursor":
            return None

        # ai-tracking
        if parts and parts[0] == "ai-tracking" and abs_path.suffix == ".db":
            return FileClassification(
                tool_name=self.name,
                category=Category.STATE,
                content_type=ContentType.SQLITE,
                sync_strategy=SyncStrategy.POLL,
                relative_path=rel_str,
            )

        return None

    def classify_transcript_source(
        self,
        abs_path: Path,
    ) -> FileClassification | None:
        """Classify a transcript path for the state projector.

        This intentionally bypasses ``has_authoritative_state``: the projector
        reuses the file's stable relative path and project metadata while the
        generic watcher skips its incomplete contents.
        """
        try:
            rel = abs_path.relative_to(self.root_path)
        except ValueError:
            return None
        parts = rel.parts
        if not parts or parts[0] != "projects" or abs_path.suffix != ".jsonl":
            return None
        rel_str = str(rel).replace("\\", "/")
        dir_hash = parts[1] if len(parts) >= 2 else ""
        real_path = self._resolve_project_path(dir_hash) if dir_hash else None
        project_name = (
            real_path.replace("\\", "/").rstrip("/").split("/")[-1]
            if real_path
            else dir_hash
        )
        is_subagent = "subagents" in parts
        meta: dict = {
            "project_hash": project_name,
            "session_id": abs_path.stem,
            "is_subagent": is_subagent,
        }
        if is_subagent:
            meta.update(path_linked_subagent_identity(rel_str))
        if real_path:
            meta["project_path"] = real_path
        chats_metadata = self._chats_store_metadata(abs_path.stem)
        if chats_metadata.started_at:
            meta["started_at"] = chats_metadata.started_at
        if chats_metadata.completed_at:
            meta["completed_at"] = chats_metadata.completed_at
        if chats_metadata.model:
            # This is the same native Cursor model-name convention used by
            # the state.vscdb projection. It also seeds assistant-message
            # identity for sparse transcripts that contain no model fields.
            meta["model"] = chats_metadata.model
        return FileClassification(
            tool_name=self.name,
            category=Category.CONVERSATION,
            content_type=ContentType.JSONL,
            sync_strategy=SyncStrategy.FULL,
            relative_path=rel_str,
            metadata=meta,
        )

    def full_identity_version(self, classification: FileClassification) -> str:
        """Version only enriched sparse transcripts for one automatic refresh."""
        if "/agent-transcripts/" in classification.relative_path.replace(
            "\\", "/"
        ) and any(
            classification.metadata.get(key)
            for key in ("started_at", "completed_at", "model")
        ):
            return _CURSOR_CHATS_STORE_FULL_IDENTITY_VERSION
        return ""

    def make_jsonl_line_enricher(
        self,
        classification: FileClassification,
        path: Path,
    ) -> Callable[[str], str] | None:
        """Build a streaming projection for a sparse agent transcript only."""
        if not self.full_identity_version(classification):
            return None
        metadata = CursorChatsStoreMetadata(
            started_at=str(classification.metadata.get("started_at") or ""),
            completed_at=str(classification.metadata.get("completed_at") or ""),
            model=str(classification.metadata.get("model") or ""),
        )
        return _CursorAgentTranscriptLineEnricher(
            metadata,
            message_count=_cursor_agent_message_count(path),
        )

    def _chats_store_metadata(self, session_id: str) -> CursorChatsStoreMetadata:
        """Read one Cursor chats-store entry without locking a live session."""
        if not session_id:
            return CursorChatsStoreMetadata()

        cached = self._chats_store_cache.get(session_id)
        if cached is not None:
            if (
                _path_token(cached.chat_path / "meta.json") == cached.meta_token
                and _path_token(cached.chat_path / "store.db") == cached.store_token
            ):
                return cached.metadata

        chats_root = self.root_path / "chats"
        try:
            candidates = sorted(
                path for path in chats_root.glob(f"*/{session_id}") if path.is_dir()
            )
        except OSError:
            return CursorChatsStoreMetadata()
        if not candidates:
            return CursorChatsStoreMetadata()

        # Chat IDs are globally unique in Cursor today. Sorting gives a stable
        # answer if a future release leaves an obsolete workspace copy behind.
        chat_path = candidates[0]
        meta_path = chat_path / "meta.json"
        store_path = chat_path / "store.db"
        metadata = CursorChatsStoreMetadata(
            started_at=self._chats_store_timestamp(meta_path, "createdAtMs"),
            completed_at=self._chats_store_timestamp(meta_path, "updatedAtMs"),
            model=self._chats_store_model(store_path),
        )
        self._chats_store_cache[session_id] = _CursorChatsStoreCacheEntry(
            chat_path=chat_path,
            meta_token=_path_token(meta_path),
            store_token=_path_token(store_path),
            metadata=metadata,
        )
        return metadata

    @staticmethod
    def _chats_store_timestamp(meta_path: Path, field: str) -> str:
        try:
            payload = orjson.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, UnicodeError, orjson.JSONDecodeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        return _iso_cursor_timestamp(payload.get(field))

    @staticmethod
    def _chats_store_model(store_path: Path) -> str:
        if not store_path.is_file():
            return ""
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{store_path.resolve().as_uri()}?mode=ro&immutable=1",
                uri=True,
                timeout=1,
            )
            connection.execute("PRAGMA query_only=ON")
            for row in connection.execute("SELECT data FROM blobs"):
                if not row:
                    continue
                model = _cursor_model_from_blob(row[0])
                if model:
                    return model
        except (OSError, sqlite3.Error):
            return ""
        finally:
            if connection is not None:
                connection.close()
        return ""
