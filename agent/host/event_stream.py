"""SSE framing and cursor recovery over the Runtime EventBuffer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import json
import time
from typing import Protocol


DisconnectCheck = Callable[[], Awaitable[bool]]
Sleep = Callable[[float], Awaitable[None]]


class EventThreads(Protocol):
    def get_thread(self, thread_id: str) -> dict[str, object]: ...

    def get_events(
        self,
        thread_id: str,
        *,
        after_event_id: str | None = None,
    ): ...


class SubmissionInspector(Protocol):
    def inspect(self, thread_id: str) -> dict[str, object] | None: ...


class EventStreamAdapter:
    """Poll Runtime events and expose transport-only SSE frames."""

    def __init__(
        self,
        threads: EventThreads,
        submissions: SubmissionInspector,
        *,
        active_interval: float = 0.1,
        idle_interval: float = 1.0,
        heartbeat_interval: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._threads = threads
        self._submissions = submissions
        self._active_interval = active_interval
        self._idle_interval = idle_interval
        self._heartbeat_interval = heartbeat_interval
        self._clock = clock
        self._sleep = sleep

    async def stream(
        self,
        thread_id: str,
        *,
        after_event_id: str | None,
        disconnected: DisconnectCheck,
    ) -> AsyncIterator[str]:
        cursor = after_event_id
        last_heartbeat = self._clock()
        while not await disconnected():
            batch = self._threads.get_events(
                thread_id,
                after_event_id=cursor,
            )
            if batch.cursor_expired:
                view = self._thread_view(thread_id)
                fresh_cursor = view["event_cursor"]
                assert fresh_cursor is None or isinstance(fresh_cursor, str)
                yield sse_frame(
                    event="snapshot",
                    event_id=fresh_cursor or "",
                    data={
                        "schema_version": 1,
                        "thread": view,
                        "cursor": fresh_cursor,
                    },
                )
                cursor = fresh_cursor
                last_heartbeat = self._clock()
            else:
                for event in batch.events:
                    yield sse_frame(
                        event=event.type,
                        event_id=event.event_id,
                        data=event.to_dict(),
                    )
                    cursor = event.event_id
                    last_heartbeat = self._clock()

            now = self._clock()
            if now - last_heartbeat >= self._heartbeat_interval:
                yield ": heartbeat\n\n"
                last_heartbeat = now

            view = self._thread_view(thread_id)
            snapshot = view["snapshot"]
            assert isinstance(snapshot, dict)
            active = (
                view["submission"] is not None
                or snapshot.get("status") in {"running", "waiting_approval"}
            )
            await self._sleep(
                self._active_interval if active else self._idle_interval
            )

    def _thread_view(self, thread_id: str) -> dict[str, object]:
        view = self._threads.get_thread(thread_id)
        return {**view, "submission": self._submissions.inspect(thread_id)}


def select_event_cursor(
    query_cursor: str | None,
    last_event_id: str | None,
) -> str | None:
    """An explicit query cursor wins, including an intentionally empty value."""

    return query_cursor if query_cursor is not None else last_event_id


def sse_frame(*, event: str, event_id: str, data: object) -> str:
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return f"event: {event}\nid: {event_id}\ndata: {encoded}\n\n"
