"""Background Turn lifecycle without duplicating Runtime Agent state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from agent.runtime import ThreadClosedError, TurnSettingsOverride


class DuplicateTurnSubmissionError(RuntimeError):
    code = "TURN_ALREADY_RUNNING"


class NoActiveTurnError(RuntimeError):
    code = "NO_ACTIVE_TURN"


class RuntimeCommands(Protocol):
    async def run_turn(
        self,
        thread_id: str,
        user_text: str,
        *,
        idempotency_key: str | None = None,
        settings_override: TurnSettingsOverride | None = None,
    ): ...

    def cancel_turn(self, thread_id: str) -> bool: ...


class ThreadCatalog(Protocol):
    @property
    def runtime(self) -> RuntimeCommands | None: ...

    def get_thread(self, thread_id: str) -> dict[str, object]: ...


@dataclass(slots=True)
class _Submission:
    thread_id: str
    accepted_at: str
    task: asyncio.Task[None] | None = None
    transport_status: str | None = None

    def public(self, status: str) -> dict[str, object]:
        return {
            "thread_id": self.thread_id,
            "status": status,
            "accepted_at": self.accepted_at,
        }


class TurnTaskManager:
    """Register, run, cancel and clean one background Turn per Thread."""

    def __init__(self, threads: ThreadCatalog) -> None:
        self._threads = threads
        self._submissions: dict[str, _Submission] = {}
        self._failures: dict[str, dict[str, str]] = {}
        self._accepting = True

    async def start(
        self,
        thread_id: str,
        user_text: str,
        *,
        idempotency_key: str | None = None,
        settings_override: TurnSettingsOverride | None = None,
    ) -> dict[str, object]:
        if not self._accepting:
            raise RuntimeError("TurnTaskManager is shutting down")
        if not isinstance(user_text, str) or not user_text.strip():
            raise ValueError("message must be a non-empty string")
        view = self._threads.get_thread(thread_id)
        snapshot = view["snapshot"]
        assert isinstance(snapshot, dict)
        if snapshot.get("status") == "closed":
            raise ThreadClosedError(f"thread is closed: {thread_id}")
        if (
            thread_id in self._submissions
            or snapshot.get("active_turn_id") is not None
        ):
            raise DuplicateTurnSubmissionError(
                f"Thread already has a Turn: {thread_id}"
            )
        runtime = self._threads.runtime
        if runtime is None:
            raise RuntimeError("Thread Runtime is unavailable")

        self._failures.pop(thread_id, None)
        submission = _Submission(
            thread_id=thread_id,
            accepted_at=_utc_now(),
        )
        self._submissions[thread_id] = submission
        submission.task = asyncio.create_task(
            self._run(
                submission,
                runtime,
                user_text,
                idempotency_key=idempotency_key,
                settings_override=settings_override,
            )
        )
        # Let run_turn reach its first asynchronous preflight boundary so an
        # immediately following Stop remains observable to Runtime.
        await asyncio.sleep(0)
        return submission.public("starting")

    def inspect(self, thread_id: str) -> dict[str, object] | None:
        submission = self._submissions.get(thread_id)
        if submission is None:
            return None
        view = self._threads.get_thread(thread_id)
        snapshot = view["snapshot"]
        assert isinstance(snapshot, dict)
        status = submission.transport_status or (
            "running" if snapshot.get("active_turn_id") else "starting"
        )
        return submission.public(status)

    def inspect_failure(self, thread_id: str) -> dict[str, object] | None:
        failure = self._failures.get(thread_id)
        return None if failure is None else dict(failure)

    def cancel(self, thread_id: str) -> dict[str, object]:
        submission = self._submissions.get(thread_id)
        if submission is None:
            raise NoActiveTurnError(f"Thread has no active Turn: {thread_id}")
        view = self._threads.get_thread(thread_id)
        snapshot = view["snapshot"]
        assert isinstance(snapshot, dict)
        if snapshot.get("active_turn_id") is None:
            assert submission.task is not None
            submission.task.cancel()
        else:
            runtime = self._threads.runtime
            if runtime is None or not runtime.cancel_turn(thread_id):
                raise NoActiveTurnError(
                    f"Thread has no cancellable Turn: {thread_id}"
                )
        submission.transport_status = "cancelling"
        return submission.public("cancelling")

    async def shutdown(self) -> None:
        self._accepting = False
        tasks = [
            submission.task
            for submission in self._submissions.values()
            if submission.task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._submissions.clear()

    async def _run(
        self,
        submission: _Submission,
        runtime: RuntimeCommands,
        user_text: str,
        *,
        idempotency_key: str | None,
        settings_override: TurnSettingsOverride | None,
    ) -> None:
        try:
            if settings_override is None:
                await runtime.run_turn(
                    submission.thread_id,
                    user_text,
                    idempotency_key=idempotency_key,
                )
            else:
                await runtime.run_turn(
                    submission.thread_id,
                    user_text,
                    idempotency_key=idempotency_key,
                    settings_override=settings_override,
                )
        except asyncio.CancelledError:
            self._record_failure(
                submission.thread_id,
                fallback_code="TURN_CANCELLED_BEFORE_START",
                fallback_message="Turn was cancelled before it started",
            )
        except Exception:
            self._record_failure(
                submission.thread_id,
                fallback_code="TURN_TASK_FAILED",
                fallback_message="Agent Turn task failed",
            )
        finally:
            if self._submissions.get(submission.thread_id) is submission:
                del self._submissions[submission.thread_id]

    def _record_failure(
        self,
        thread_id: str,
        *,
        fallback_code: str,
        fallback_message: str,
    ) -> None:
        failure = self._latest_rejection(thread_id)
        self._failures[thread_id] = failure or {
            "code": fallback_code,
            "message": fallback_message,
        }

    def _latest_rejection(self, thread_id: str) -> dict[str, str] | None:
        read_events = getattr(self._threads, "get_events", None)
        if not callable(read_events):
            return None
        try:
            batch = read_events(thread_id)
            event = batch.events[-1] if batch.events else None
        except Exception:
            return None
        if event is None or event.type != "turn_rejected":
            return None
        error = event.payload.get("error")
        if not isinstance(error, dict):
            return None
        code = error.get("code")
        message = error.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            return None
        return {"code": code, "message": message}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
