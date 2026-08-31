"""Provider-independent durable storage for Runtime Thread state.

The store owns persistence and JSON mapping only.  Runtime lifecycle objects
(tasks, locks, approval futures, tool registries, subprocesses and providers)
never cross this boundary.  SQLite is deliberately local and single-process;
the in-memory implementation keeps tests and ephemeral embeddings lightweight.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Protocol, Sequence

from agent.core.messages import (
    Message,
    ReasoningBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)

from .events import AgentEvent, json_safe
from .settings import (
    AgentLimits,
    ApprovalMode,
    ModelSettings,
    ThinkingKeep,
    ThinkingSettings,
    ThreadSettings,
    TurnSettingsOverride,
)
from .types import ThreadStatus, TurnStatus, TurnSummary


STORE_SCHEMA_VERSION = 1
DEFAULT_DATABASE_FILENAME = "threads.sqlite3"


class ThreadStoreError(RuntimeError):
    """A durable Thread store could not be opened or decoded safely."""


@dataclass(frozen=True, slots=True)
class StoredActiveTurn:
    """Minimal active Turn marker used for truthful restart recovery."""

    turn_id: str
    started_at: str
    idempotency_key: str | None = None
    iterations: int = 0
    tool_calls: int = 0
    usage: dict[str, int | None] = field(default_factory=dict)
    last_assistant_text: str = ""


@dataclass(frozen=True, slots=True)
class StoredIdempotency:
    """Detached request identity and optional completed/recovered summary."""

    user_text: str
    settings_override: TurnSettingsOverride | None
    summary: TurnSummary | None = None
    interrupted: bool = False


@dataclass(slots=True)
class ThreadState:
    """Complete provider-independent state owned by one persisted Thread."""

    thread_id: str
    workspace: str
    status: ThreadStatus
    settings: ThreadSettings
    messages: list[Message]
    completed_turns: int
    created_at: str
    updated_at: str
    latest_turn: TurnSummary | None = None
    turns: list[TurnSummary] = field(default_factory=list)
    events: list[AgentEvent] = field(default_factory=list)
    event_sequence: int = 0
    active_turn: StoredActiveTurn | None = None
    idempotency: dict[str, StoredIdempotency] = field(default_factory=dict)


class ThreadStore(Protocol):
    """Small persistence seam consumed by :class:`ThreadRuntime`."""

    def list_threads(self) -> list[ThreadState]:
        """Return detached Thread state in stable creation order."""

    def get_thread(self, thread_id: str) -> ThreadState | None:
        """Return detached state, or ``None`` for an unknown ID."""

    def save_thread(self, state: ThreadState) -> None:
        """Atomically persist one complete semantic state transition."""

    def save_thread_transition(
        self,
        state: ThreadState,
        *,
        new_events: Sequence[AgentEvent] = (),
    ) -> None:
        """Persist state plus only newly appended durable events."""

    def close(self) -> None:
        """Release store resources; repeated calls are safe."""


def default_state_directory() -> Path:
    """Return the per-user Linux/WSL state directory without touching disk."""

    configured = os.environ.get("XDG_STATE_HOME")
    root = Path(configured) if configured else Path.home() / ".local" / "state"
    return root / "my-coding-agent"


def default_database_path() -> Path:
    """Return the fixed SQLite filename used by the production Host."""

    return default_state_directory() / DEFAULT_DATABASE_FILENAME


class InMemoryThreadStore:
    """Detached, process-local implementation of the ThreadStore contract."""

    def __init__(self, states: list[ThreadState] | None = None) -> None:
        self._states: dict[str, ThreadState] = {}
        self._order: list[str] = []
        for state in states or []:
            self.save_thread(state)

    def list_threads(self) -> list[ThreadState]:
        return [deepcopy(self._states[thread_id]) for thread_id in self._order]

    def get_thread(self, thread_id: str) -> ThreadState | None:
        state = self._states.get(thread_id)
        return None if state is None else deepcopy(state)

    def save_thread(self, state: ThreadState) -> None:
        _validate_state(state)
        if state.thread_id not in self._states:
            self._order.append(state.thread_id)
        self._states[state.thread_id] = deepcopy(state)

    def save_thread_transition(
        self,
        state: ThreadState,
        *,
        new_events: Sequence[AgentEvent] = (),
    ) -> None:
        # In-memory state is detached in one assignment; accepting the
        # transition seam keeps Runtime provider-independent and mirrors the
        # SQLite implementation without adding persistence-specific logic.
        del new_events
        self.save_thread(state)

    # Friendly aliases for embedders that call the seam ``save``/``get``.
    save = save_thread
    get = get_thread
    load = get_thread
    load_all = list_threads

    def close(self) -> None:
        return None


class LocalThreadStore:
    """SQLite-backed ThreadStore with an explicit versioned schema."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        database_path: Path | str | None = None,
    ) -> None:
        if path is not None and database_path is not None:
            raise ValueError("provide either path or database_path, not both")
        if database_path is not None:
            self.path = Path(database_path)
        elif path is not None:
            self.path = self._database_path(Path(path))
        else:
            self.path = default_database_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        try:
            self._connection = sqlite3.connect(
                self.path,
                timeout=30,
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 30000")
            self._lock = RLock()
            self._closed = False
            self._initialize_schema()
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except ThreadStoreError:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error) as error:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise ThreadStoreError("Thread database could not be opened") from error

    @staticmethod
    def _database_path(selected: Path) -> Path:
        # A suffix makes direct test/deployment paths convenient; a suffixless
        # path is a state directory and receives one fixed database filename.
        if selected.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            return selected
        return selected / DEFAULT_DATABASE_FILENAME

    def _initialize_schema(self) -> None:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version > STORE_SCHEMA_VERSION:
            raise ThreadStoreError(
                f"unsupported Thread database schema version: {version}"
            )
        if version == 0:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS thread_events (
                    thread_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (thread_id, event_id),
                    FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS thread_events_sequence
                    ON thread_events(thread_id, sequence);
                CREATE TABLE IF NOT EXISTS thread_idempotency (
                    thread_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    PRIMARY KEY (thread_id, idempotency_key),
                    FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
                        ON DELETE CASCADE
                );
                PRAGMA user_version = 1;
                """
            )
            self._connection.commit()

    def list_threads(self) -> list[ThreadState]:
        with self._locked_connection():
            rows = self._connection.execute(
                "SELECT state_json FROM threads ORDER BY created_at, thread_id"
            ).fetchall()
            return [self._load_row(row[0]) for row in rows]

    def get_thread(self, thread_id: str) -> ThreadState | None:
        with self._locked_connection():
            row = self._connection.execute(
                "SELECT state_json FROM threads WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            return None if row is None else self._load_row(row[0])

    def save_thread(self, state: ThreadState) -> None:
        self._save_state(state, new_events=state.events, replace_events=True)

    def save_thread_transition(
        self,
        state: ThreadState,
        *,
        new_events: Sequence[AgentEvent] = (),
    ) -> None:
        self._save_state(state, new_events=new_events, replace_events=False)

    def _save_state(
        self,
        state: ThreadState,
        *,
        new_events: Sequence[AgentEvent],
        replace_events: bool,
    ) -> None:
        _validate_state(state)
        state_json = _encode(_state_to_dict(state, include_events=False, include_idempotency=False))
        event_rows = [
            (
                state.thread_id,
                event.event_id,
                event.sequence,
                _encode(event.to_dict()),
            )
            for event in new_events
        ]
        idempotency_rows = [
            (
                state.thread_id,
                key,
                _encode(_idempotency_to_dict(value)),
            )
            for key, value in state.idempotency.items()
        ]
        with self._locked_connection():
            try:
                self._connection.execute(
                    """
                    INSERT INTO threads(thread_id, state_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        state_json = excluded.state_json,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at
                    """,
                    (state.thread_id, state_json, state.created_at, state.updated_at),
                )
                if replace_events:
                    self._connection.execute(
                        "DELETE FROM thread_events WHERE thread_id = ?",
                        (state.thread_id,),
                    )
                self._connection.executemany(
                    """
                    INSERT OR IGNORE INTO thread_events(thread_id, event_id, sequence, event_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    event_rows,
                )
                if idempotency_rows:
                    placeholders = ",".join("?" for _ in idempotency_rows)
                    self._connection.execute(
                        "DELETE FROM thread_idempotency "
                        f"WHERE thread_id = ? AND idempotency_key NOT IN ({placeholders})",
                        (state.thread_id, *(row[1] for row in idempotency_rows)),
                    )
                else:
                    self._connection.execute(
                        "DELETE FROM thread_idempotency WHERE thread_id = ?",
                        (state.thread_id,),
                    )
                self._connection.executemany(
                    """
                    INSERT INTO thread_idempotency(thread_id, idempotency_key, request_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT(thread_id, idempotency_key) DO UPDATE SET
                        request_json = excluded.request_json
                    """,
                    idempotency_rows,
                )
                self._connection.commit()
            except sqlite3.Error as error:
                self._connection.rollback()
                raise ThreadStoreError("Thread state could not be saved") from error

    save = save_thread
    get = get_thread
    load = get_thread
    load_all = list_threads

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def _load_row(self, state_json: str) -> ThreadState:
        try:
            raw = json.loads(state_json)
            if not isinstance(raw, dict):
                raise ValueError("state must be an object")
            with self._connection:
                # Events and idempotency are normalized in their own tables;
                # the row snapshot remains the single state transition record.
                thread_id = raw["thread_id"]
                event_rows = self._connection.execute(
                    """
                    SELECT event_json FROM thread_events
                    WHERE thread_id = ? ORDER BY sequence, event_id
                    """,
                    (thread_id,),
                ).fetchall()
                idempotency_rows = self._connection.execute(
                    """
                    SELECT idempotency_key, request_json
                    FROM thread_idempotency WHERE thread_id = ?
                    """,
                    (thread_id,),
                ).fetchall()
            raw["events"] = [json.loads(row[0]) for row in event_rows]
            raw["idempotency"] = {
                key: json.loads(request_json)
                for key, request_json in idempotency_rows
            }
            return _state_from_dict(raw)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
            raise ThreadStoreError("Thread database contains invalid state") from error

    class _ConnectionLock:
        def __init__(self, store: LocalThreadStore):
            self.store = store

        def __enter__(self):
            self.store._lock.acquire()
            if self.store._closed:
                self.store._lock.release()
                raise ThreadStoreError("Thread store is closed")
            return self.store

        def __exit__(self, *_: object):
            self.store._lock.release()

    def _locked_connection(self) -> LocalThreadStore._ConnectionLock:
        return self._ConnectionLock(self)


def _encode(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ThreadStoreError("Thread state is not JSON serializable") from error


def _validate_state(state: ThreadState) -> None:
    if not isinstance(state, ThreadState):
        raise TypeError("state must be ThreadState")
    if not state.thread_id or not isinstance(state.workspace, str):
        raise ValueError("Thread identity and workspace must be strings")
    if not isinstance(state.settings, ThreadSettings):
        raise ValueError("Thread settings must be ThreadSettings")
    if not isinstance(state.status, ThreadStatus):
        raise ValueError("Thread status must be ThreadStatus")


def _settings_to_dict(settings: ModelSettings | ThreadSettings) -> dict[str, object]:
    thinking = settings.thinking
    return {
        "provider_config_id": settings.provider_config_id,
        "model": settings.model,
        "temperature": settings.temperature,
        "max_tokens": settings.max_tokens,
        "thinking": None
        if thinking is None
        else {
            "enabled": thinking.enabled,
            "budget_tokens": thinking.budget_tokens,
            "keep": None if thinking.keep is None else thinking.keep.value,
        },
        "limits": {
            "max_iterations": settings.limits.max_iterations,
            "max_tool_calls": settings.limits.max_tool_calls,
            "max_execution_seconds": settings.limits.max_execution_seconds,
        },
        "approval_mode": settings.approval_mode.value,
        **({"version": settings.version} if isinstance(settings, ThreadSettings) else {}),
    }


def _settings_from_dict(raw: object, *, versioned: bool) -> ThreadSettings | ModelSettings:
    if not isinstance(raw, dict):
        raise ValueError("settings must be an object")
    thinking_raw = raw.get("thinking")
    thinking = None
    if thinking_raw is not None:
        if not isinstance(thinking_raw, dict):
            raise ValueError("thinking must be an object")
        keep = thinking_raw.get("keep")
        thinking = ThinkingSettings(
            enabled=thinking_raw["enabled"],
            budget_tokens=thinking_raw.get("budget_tokens"),
            keep=None if keep is None else ThinkingKeep(keep),
        )
    limits_raw = raw.get("limits")
    if not isinstance(limits_raw, dict):
        raise ValueError("limits must be an object")
    base = dict(
        provider_config_id=raw["provider_config_id"],
        model=raw["model"],
        temperature=raw.get("temperature"),
        max_tokens=raw.get("max_tokens"),
        thinking=thinking,
        limits=AgentLimits(**limits_raw),
        approval_mode=ApprovalMode(raw["approval_mode"]),
    )
    if versioned:
        return ThreadSettings(**base, version=raw["version"])
    return ModelSettings(**base)


def _block_to_dict(block: object) -> dict[str, object]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ReasoningBlock):
        return {"type": "reasoning", "text": block.text}
    if isinstance(block, ToolCallBlock):
        return {
            "type": "tool_call",
            "id": block.id,
            "name": block.name,
            "arguments": json_safe(block.arguments),
            "arguments_error": block.arguments_error,
            "raw_arguments": block.raw_arguments,
        }
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_call_id": block.tool_call_id,
            "content": block.content,
            "metadata": json_safe(block.metadata),
            "error_code": block.error_code,
        }
    raise ValueError(f"unsupported conversation block: {type(block).__name__}")


def _block_from_dict(raw: object) -> TextBlock | ReasoningBlock | ToolCallBlock | ToolResultBlock:
    if not isinstance(raw, dict):
        raise ValueError("conversation block must be an object")
    block_type = raw.get("type")
    if block_type == "text":
        return TextBlock(text=raw["text"])
    if block_type == "reasoning":
        return ReasoningBlock(text=raw["text"])
    if block_type == "tool_call":
        return ToolCallBlock(
            id=raw["id"],
            name=raw["name"],
            arguments=raw.get("arguments"),
            arguments_error=raw.get("arguments_error"),
            raw_arguments=raw.get("raw_arguments"),
        )
    if block_type == "tool_result":
        return ToolResultBlock(
            tool_call_id=raw["tool_call_id"],
            content=raw["content"],
            metadata=raw.get("metadata", {}),
            error_code=raw.get("error_code"),
        )
    raise ValueError(f"unsupported conversation block type: {block_type!r}")


def _message_to_dict(message: Message) -> dict[str, object]:
    return {"role": message.role, "content": [_block_to_dict(block) for block in message.content]}


def _message_from_dict(raw: object) -> Message:
    if not isinstance(raw, dict):
        raise ValueError("conversation message must be an object")
    return Message(
        role=raw["role"],
        content=[_block_from_dict(block) for block in raw["content"]],
    )


def _summary_to_dict(summary: TurnSummary | None) -> dict[str, object] | None:
    return None if summary is None else summary.to_dict()


def _summary_from_dict(raw: object) -> TurnSummary | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("turn summary must be an object")
    return TurnSummary(
        schema_version=raw["schema_version"],
        turn_id=raw["turn_id"],
        thread_id=raw["thread_id"],
        status=TurnStatus(raw["status"]),
        stop_reason=raw["stop_reason"],
        final_text=raw["final_text"],
        iterations=raw["iterations"],
        tool_calls=raw["tool_calls"],
        usage=raw.get("usage", {}),
        modified_files=raw.get("modified_files", []),
        file_diffs=raw.get("file_diffs", []),
        diff_complete=raw.get("diff_complete", False),
        started_at=raw.get("started_at", ""),
        ended_at=raw.get("ended_at", ""),
        error=raw.get("error"),
    )


def _event_from_dict(raw: object) -> AgentEvent:
    if not isinstance(raw, dict):
        raise ValueError("event must be an object")
    return AgentEvent(
        schema_version=raw["schema_version"],
        event_id=raw["event_id"],
        thread_id=raw["thread_id"],
        turn_id=raw.get("turn_id"),
        sequence=raw["sequence"],
        type=raw["type"],
        timestamp=raw["timestamp"],
        payload=raw.get("payload", {}),
    )


def _override_to_dict(value: TurnSettingsOverride | None) -> dict[str, object] | None:
    if value is None:
        return None
    from .settings import _UNSET

    result: dict[str, object] = {}
    for name in (
        "provider_config_id", "model", "temperature", "max_tokens", "thinking",
        "limits", "approval_mode",
    ):
        field_value = getattr(value, name)
        if field_value is _UNSET:
            continue
        if isinstance(field_value, ThinkingSettings):
            result[name] = {
                "enabled": field_value.enabled,
                "budget_tokens": field_value.budget_tokens,
                "keep": None if field_value.keep is None else field_value.keep.value,
            }
        elif isinstance(field_value, AgentLimits):
            result[name] = {
                "max_iterations": field_value.max_iterations,
                "max_tool_calls": field_value.max_tool_calls,
                "max_execution_seconds": field_value.max_execution_seconds,
            }
        elif isinstance(field_value, ApprovalMode):
            result[name] = field_value.value
        else:
            result[name] = field_value
    return result


def _override_from_dict(raw: object) -> TurnSettingsOverride | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("settings override must be an object")
    values = dict(raw)
    thinking = values.get("thinking")
    if isinstance(thinking, dict):
        keep = thinking.get("keep")
        values["thinking"] = ThinkingSettings(
            enabled=thinking["enabled"],
            budget_tokens=thinking.get("budget_tokens"),
            keep=None if keep is None else ThinkingKeep(keep),
        )
    limits = values.get("limits")
    if isinstance(limits, dict):
        values["limits"] = AgentLimits(**limits)
    if "approval_mode" in values:
        values["approval_mode"] = ApprovalMode(values["approval_mode"])
    return TurnSettingsOverride(**values)


def _active_to_dict(value: StoredActiveTurn | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "turn_id": value.turn_id,
        "started_at": value.started_at,
        "idempotency_key": value.idempotency_key,
        "iterations": value.iterations,
        "tool_calls": value.tool_calls,
        "usage": value.usage,
        "last_assistant_text": value.last_assistant_text,
    }


def _active_from_dict(raw: object) -> StoredActiveTurn | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("active turn must be an object")
    return StoredActiveTurn(
        turn_id=raw["turn_id"],
        started_at=raw.get("started_at", ""),
        idempotency_key=raw.get("idempotency_key"),
        iterations=raw.get("iterations", 0),
        tool_calls=raw.get("tool_calls", 0),
        usage=raw.get("usage", {}),
        last_assistant_text=raw.get("last_assistant_text", ""),
    )


def _idempotency_to_dict(value: StoredIdempotency) -> dict[str, object]:
    return {
        "user_text": value.user_text,
        "settings_override": _override_to_dict(value.settings_override),
        "summary": _summary_to_dict(value.summary),
        "interrupted": value.interrupted,
    }


def _idempotency_from_dict(raw: object) -> StoredIdempotency:
    if not isinstance(raw, dict):
        raise ValueError("idempotency record must be an object")
    return StoredIdempotency(
        user_text=raw["user_text"],
        settings_override=_override_from_dict(raw.get("settings_override")),
        summary=_summary_from_dict(raw.get("summary")),
        interrupted=raw.get("interrupted", False),
    )


def _state_to_dict(
    state: ThreadState,
    *,
    include_events: bool = True,
    include_idempotency: bool = True,
) -> dict[str, object]:
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "thread_id": state.thread_id,
        "workspace": state.workspace,
        "status": state.status.value,
        "settings": _settings_to_dict(state.settings),
        "messages": [_message_to_dict(message) for message in state.messages],
        "completed_turns": state.completed_turns,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "latest_turn": _summary_to_dict(state.latest_turn),
        "turns": [_summary_to_dict(summary) for summary in state.turns],
        "event_sequence": state.event_sequence,
        "active_turn": _active_to_dict(state.active_turn),
        "events": [json_safe(event.to_dict()) for event in state.events]
        if include_events
        else [],
        "idempotency": {
            key: _idempotency_to_dict(value)
            for key, value in state.idempotency.items()
        }
        if include_idempotency
        else {},
    }


def _state_from_dict(raw: object) -> ThreadState:
    if not isinstance(raw, dict):
        raise ValueError("Thread state must be an object")
    if raw.get("schema_version") != STORE_SCHEMA_VERSION:
        raise ValueError("unsupported Thread state schema version")
    latest = _summary_from_dict(raw.get("latest_turn"))
    turns = [
        summary
        for item in raw.get("turns", [])
        if (summary := _summary_from_dict(item)) is not None
    ]
    events = [_event_from_dict(item) for item in raw.get("events", [])]
    idempotency_raw = raw.get("idempotency", {})
    if not isinstance(idempotency_raw, dict):
        raise ValueError("idempotency must be an object")
    return ThreadState(
        thread_id=raw["thread_id"],
        workspace=raw["workspace"],
        status=ThreadStatus(raw["status"]),
        settings=_settings_from_dict(raw["settings"], versioned=True),  # type: ignore[arg-type]
        messages=[_message_from_dict(item) for item in raw.get("messages", [])],
        completed_turns=raw["completed_turns"],
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        latest_turn=latest,
        turns=turns,
        events=events,
        event_sequence=raw.get("event_sequence", max((event.sequence for event in events), default=0)),
        active_turn=_active_from_dict(raw.get("active_turn")),
        idempotency={
            key: _idempotency_from_dict(value)
            for key, value in idempotency_raw.items()
        },
    )


def serialize_thread_state(state: ThreadState) -> dict[str, object]:
    """Return explicit JSON-compatible state for diagnostics and migrations."""

    _validate_state(state)
    return deepcopy(_state_to_dict(state))


def deserialize_thread_state(raw: object) -> ThreadState:
    """Decode one explicit state document without pickle or provider objects."""

    return _state_from_dict(raw)
