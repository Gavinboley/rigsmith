"""Background jobs with progress, shared by every surface.

Some of what rigsmith does is slow. Demucs source separation runs for minutes
on CPU; reamping runs in real time by definition; a subnet sweep takes
seconds. In the first version all of that happened inside a blocking call
that returned only when finished, which left an agent — and a person — with
no way to tell "working" from "hung".

One job runner fixes that for all three surfaces at once:

* the **CLI** renders a progress bar from the event stream,
* the **MCP server** hands back a job id to poll,
* the **web UI** streams events over a WebSocket.

Deliberately dependency-free and thread-based rather than asyncio-based. The
slow work here is blocking (subprocess, PortAudio, file I/O), threads suit it,
and core must not require a running event loop — the CLI does not have one.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}


@dataclass
class JobEvent:
    """One thing that happened to a job."""

    job_id: str
    at: float
    kind: str
    """``state``, ``progress``, or ``log``."""
    state: JobState | None = None
    progress: float | None = None
    """0.0-1.0, or ``None`` when the work cannot report a fraction."""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "at": self.at,
            "kind": self.kind,
            "state": self.state.value if self.state else None,
            "progress": self.progress,
            "message": self.message,
        }


class Cancelled(Exception):
    """Raised inside a job when cancellation has been requested."""


@dataclass
class Progress:
    """The handle a job's own code uses to report on itself.

    Passed as the first argument to every job function. Cancellation is
    cooperative: long work should call :meth:`check` (or read
    :attr:`cancelled`) somewhere in its loop, because a thread cannot be
    safely killed from outside.
    """

    _job: "Job"

    def update(self, fraction: float | None = None, message: str = "") -> None:
        """Report progress. ``fraction`` is 0.0-1.0, or None if unknown."""
        self._job._record(
            JobEvent(
                job_id=self._job.id,
                at=time.time(),
                kind="progress",
                progress=None if fraction is None else max(0.0, min(1.0, fraction)),
                message=message,
            )
        )

    def log(self, message: str) -> None:
        """Report a line of detail without changing the progress fraction."""
        self._job._record(
            JobEvent(job_id=self._job.id, at=time.time(), kind="log", message=message)
        )

    @property
    def cancelled(self) -> bool:
        return self._job._cancel.is_set()

    def check(self) -> None:
        """Raise :class:`Cancelled` if cancellation has been requested."""
        if self.cancelled:
            raise Cancelled()


@dataclass
class Job:
    """A unit of background work and everything known about it."""

    id: str
    name: str
    state: JobState = JobState.PENDING
    progress: float | None = None
    message: str = ""
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    events: list[JobEvent] = field(default_factory=list)

    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _done: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _listeners: list[Callable[[JobEvent], None]] = field(
        default_factory=list, repr=False
    )

    #: Keep the tail of the log rather than growing without bound on a job
    #: that logs per-frame.
    max_events: int = 500

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return end - (self.started_at or self.created_at)

    @property
    def finished(self) -> bool:
        return self.state.finished

    def _record(self, event: JobEvent) -> None:
        with self._lock:
            if event.kind == "progress" and event.progress is not None:
                self.progress = event.progress
            if event.message:
                self.message = event.message
            if event.state is not None:
                self.state = event.state
            self.events.append(event)
            if len(self.events) > self.max_events:
                del self.events[: len(self.events) - self.max_events]
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                # A broken subscriber must not take the job down with it.
                pass

    def listen(self, callback: Callable[[JobEvent], None]) -> Callable[[], None]:
        """Subscribe to this job's events. Returns an unsubscribe callable."""
        with self._lock:
            self._listeners.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return unsubscribe

    def cancel(self) -> None:
        """Request cancellation. The job stops at its next checkpoint."""
        self._cancel.set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the job finishes. Returns False on timeout."""
        return self._done.wait(timeout)

    def to_dict(self, *, include_events: bool = False) -> dict[str, Any]:
        data = {
            "id": self.id,
            "name": self.name,
            "state": self.state.value,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "elapsed": round(self.elapsed, 2),
            "created_at": self.created_at,
            "finished": self.finished,
        }
        if include_events:
            data["events"] = [e.to_dict() for e in self.events]
        return data

    def render(self) -> str:
        bits = [f"{self.name} [{self.state.value}]"]
        if self.progress is not None and not self.finished:
            bits.append(f"{self.progress:.0%}")
        if self.message:
            bits.append(self.message)
        bits.append(f"{self.elapsed:.1f}s")
        line = "  ".join(bits)
        if self.error:
            line += f"\n  error: {self.error}"
        return line


class JobManager:
    """Runs jobs on daemon threads and keeps their history."""

    def __init__(self, *, keep: int = 100) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._keep = keep

    def submit(
        self,
        name: str,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Job:
        """Run ``func(progress, *args, **kwargs)`` on a background thread.

        The function's first argument is a :class:`Progress` handle. Its
        return value becomes ``job.result``.
        """
        job = Job(id=uuid.uuid4().hex[:12], name=name)

        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune_locked()

        def run() -> None:
            job.started_at = time.time()
            job._record(
                JobEvent(
                    job_id=job.id,
                    at=time.time(),
                    kind="state",
                    state=JobState.RUNNING,
                    message="started",
                )
            )
            progress = Progress(job)
            try:
                if progress.cancelled:
                    raise Cancelled()
                job.result = func(progress, *args, **kwargs)
            except Cancelled:
                job.finished_at = time.time()
                job._record(
                    JobEvent(
                        job_id=job.id,
                        at=time.time(),
                        kind="state",
                        state=JobState.CANCELLED,
                        message="cancelled",
                    )
                )
            except Exception as exc:
                job.finished_at = time.time()
                job.error = f"{type(exc).__name__}: {exc}"
                # Keep the traceback for the log without putting it in the
                # user-facing message.
                job._record(
                    JobEvent(
                        job_id=job.id,
                        at=time.time(),
                        kind="log",
                        message=traceback.format_exc(limit=5),
                    )
                )
                job._record(
                    JobEvent(
                        job_id=job.id,
                        at=time.time(),
                        kind="state",
                        state=JobState.FAILED,
                        message=job.error,
                    )
                )
            else:
                job.finished_at = time.time()
                job._record(
                    JobEvent(
                        job_id=job.id,
                        at=time.time(),
                        kind="state",
                        state=JobState.SUCCEEDED,
                        progress=1.0,
                        message="done",
                    )
                )
            finally:
                job._done.set()

        thread = threading.Thread(target=run, name=f"rigsmith-job-{job.id}", daemon=True)
        thread.start()
        return job

    def _prune_locked(self) -> None:
        """Drop the oldest finished jobs once history exceeds ``keep``."""
        while len(self._order) > self._keep:
            for index, job_id in enumerate(self._order):
                if self._jobs[job_id].finished:
                    del self._order[index]
                    self._jobs.pop(job_id, None)
                    break
            else:
                break  # nothing finished yet; let it grow

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            from .errors import NotFound

            known = ", ".join(reversed(self._order[-5:])) or "none"
            raise NotFound(
                f"no job '{job_id}'",
                hint=f"Recent job ids: {known}",
            )
        return job

    def list_jobs(self, *, active_only: bool = False) -> list[Job]:
        with self._lock:
            jobs = [self._jobs[i] for i in self._order if i in self._jobs]
        if active_only:
            jobs = [j for j in jobs if not j.finished]
        return list(reversed(jobs))

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        job.cancel()
        return job

    def stream(self, job_id: str, *, timeout: float | None = None) -> Iterator[JobEvent]:
        """Yield events for a job until it finishes.

        Replays anything already recorded first, so a late subscriber does not
        miss the start.
        """
        import queue

        job = self.get(job_id)
        pending: queue.Queue[JobEvent | None] = queue.Queue()

        with job._lock:
            backlog = list(job.events)
        unsubscribe = job.listen(pending.put)

        try:
            for event in backlog:
                yield event
            if job.finished and not backlog:
                return
            while True:
                try:
                    event = pending.get(timeout=timeout if timeout else 1.0)
                except Exception:
                    if job.finished:
                        return
                    continue
                if event is None:
                    return
                yield event
                if event.kind == "state" and event.state and event.state.finished:
                    return
        finally:
            unsubscribe()


_manager: JobManager | None = None


def get_manager() -> JobManager:
    """The process-wide job manager."""
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager


def set_manager(manager: JobManager | None) -> None:
    """Replace the process-wide manager. Used by tests."""
    global _manager
    _manager = manager
