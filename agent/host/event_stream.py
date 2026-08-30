"""SSE framing and cursor recovery over the Runtime EventBuffer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import inspect
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

    def inspect_failure(self, thread_id: str) -> dict[str, object] | None: ...


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
        subscribe = getattr(self._threads, "subscribe_events", None)
        if callable(subscribe):
            subscription = subscribe(
                thread_id,
                after_event_id=after_event_id,
            )
            if subscription is not None:
                async for frame in self._stream_realtime(
                    thread_id,
                    subscription=subscription,
                    after_event_id=after_event_id,
                    disconnected=disconnected,
                ):
                    yield frame
                return

        cursor = after_event_id
        last_heartbeat = self._clock()
        reported_host_failure: tuple[str, str] | None = None
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
                reported_host_failure = _host_failure_key(view)
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

            view = self._thread_view(thread_id)
            host_failure = _host_failure_key(view)
            if host_failure is not None and host_failure != reported_host_failure:
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
                reported_host_failure = host_failure
                last_heartbeat = self._clock()
            elif host_failure is None:
                reported_host_failure = None

            now = self._clock()
            if now - last_heartbeat >= self._heartbeat_interval:
                yield ": heartbeat\n\n"
                last_heartbeat = now

            snapshot = view["snapshot"]
            assert isinstance(snapshot, dict)
            active = (
                view["submission"] is not None
                or snapshot.get("status") in {"running", "waiting_approval"}
            )
            await self._sleep(
                self._active_interval if active else self._idle_interval
            )

    async def _stream_realtime(
        self,
        thread_id: str,
        *,
        subscription,
        after_event_id: str | None,
        disconnected: DisconnectCheck,
    ) -> AsyncIterator[str]:
        """Wait on EventBuffer wakeups while preserving replay/recovery rules."""

        cursor = after_event_id
        last_heartbeat = self._clock()
        reported_host_failure: tuple[str, str] | None = None
        batch_task: asyncio.Task | None = None
        disconnected_task: asyncio.Task | None = None
        try:
            if await disconnected():
                return
            batch_task = asyncio.create_task(subscription.read(cursor))
            while True:
                disconnected_task = asyncio.create_task(
                    self._watch_disconnected(disconnected)
                )
                timeout = max(
                    0.0,
                    self._heartbeat_interval - (self._clock() - last_heartbeat),
                )
                done, _ = await asyncio.wait(
                    {batch_task, disconnected_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnected_task in done:
                    await asyncio.gather(disconnected_task, return_exceptions=True)
                    batch_task.cancel()
                    await asyncio.gather(batch_task, return_exceptions=True)
                    disconnected_task = None
                    break
                disconnected_task.cancel()
                await asyncio.gather(disconnected_task, return_exceptions=True)
                disconnected_task = None
                if batch_task not in done:
                    yield ": heartbeat\n\n"
                    last_heartbeat = self._clock()
                    continue
                batch = await batch_task
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
                    reported_host_failure = _host_failure_key(view)
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

                batch_task = asyncio.create_task(subscription.read(cursor))

                view = self._thread_view(thread_id)
                host_failure = _host_failure_key(view)
                if host_failure is not None and host_failure != reported_host_failure:
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
                    reported_host_failure = host_failure
                    last_heartbeat = self._clock()
                elif host_failure is None:
                    reported_host_failure = None
        finally:
            if batch_task is not None and not batch_task.done():
                batch_task.cancel()
                await asyncio.gather(batch_task, return_exceptions=True)
            if disconnected_task is not None and not disconnected_task.done():
                disconnected_task.cancel()
                await asyncio.gather(disconnected_task, return_exceptions=True)
            close = getattr(subscription, "aclose", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result

    @staticmethod
    async def _watch_disconnected(disconnected: DisconnectCheck) -> bool:
        """Poll disconnect state with a bounded delay to avoid a busy loop."""

        while True:
            await asyncio.sleep(0.1)
            if await disconnected():
                return True

    def _thread_view(self, thread_id: str) -> dict[str, object]:
        view = self._threads.get_thread(thread_id)
        inspect_failure = getattr(self._submissions, "inspect_failure", None)
        host_error = inspect_failure(thread_id) if callable(inspect_failure) else None
        return {
            **view,
            "submission": self._submissions.inspect(thread_id),
            "host_error": host_error,
        }


def _host_failure_key(view: dict[str, object]) -> tuple[str, str] | None:
    error = view.get("host_error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    message = error.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        return None
    return code, message


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
