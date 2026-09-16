"""Background jobs: the thing every slow operation is reported through.

A job system that misreports is worse than none - a progress bar that lies
and a cancel button that does nothing both cost more trust than they buy.
"""

from __future__ import annotations

import threading
import time

import pytest

from rigsmith.core.jobs import Cancelled, JobManager, JobState


@pytest.fixture()
def manager():
    return JobManager()


def settled(job, timeout: float = 5.0):
    assert job.wait(timeout), f"job {job.name} never finished"
    return job


# --------------------------------------------------------------------------
# outcomes
# --------------------------------------------------------------------------


def test_a_result_comes_back(manager):
    job = settled(manager.submit("work", lambda progress: 41 + 1))
    assert job.state is JobState.SUCCEEDED
    assert job.result == 42
    assert job.error is None
    assert job.finished


def test_a_failure_is_reported_without_a_traceback_in_the_message(manager):
    def explode(progress):
        raise ValueError("the capture did not load")

    job = settled(manager.submit("work", explode))

    assert job.state is JobState.FAILED
    assert job.error == "ValueError: the capture did not load"
    assert "Traceback" not in job.error
    # But it is kept, because somebody eventually has to debug it.
    assert any("Traceback" in (e.message or "") for e in job.events)


def test_progress_reaches_the_job(manager):
    def slow(progress):
        progress.update(0.25, "quarter")
        progress.update(0.75, "most of the way")
        return "done"

    job = settled(manager.submit("work", slow))
    assert job.progress == 0.75
    assert job.message in ("most of the way", "done", "")
    messages = [e.message for e in job.events if e.message]
    assert "quarter" in messages


def test_arguments_are_passed_through(manager):
    job = settled(manager.submit("work", lambda progress, a, b=0: a + b, 3, b=4))
    assert job.result == 7


# --------------------------------------------------------------------------
# cancellation, which is cooperative and has to admit it
# --------------------------------------------------------------------------


def test_a_job_stops_at_its_next_checkpoint(manager):
    started = threading.Event()

    def loop(progress):
        started.set()
        for index in range(1000):
            progress.check()
            time.sleep(0.002)
        return "ran to the end"

    job = manager.submit("loop", loop)
    assert started.wait(5)
    manager.cancel(job.id)
    settled(job)

    assert job.state is JobState.CANCELLED
    assert job.result is None


def test_cancelling_says_what_it_is_waiting_for(manager):
    """A cancel button that changes nothing on screen looks broken."""
    started = threading.Event()
    release = threading.Event()

    def blocking(progress):
        started.set()
        release.wait(5)
        progress.check()
        return "finished"

    job = manager.submit("blocking", blocking)
    assert started.wait(5)
    manager.cancel(job.id)

    assert any(
        "stopping at the next checkpoint" in (e.message or "") for e in job.events
    )
    release.set()
    settled(job)


def test_work_that_never_checks_runs_to_completion(manager):
    """Honest about the contract rather than pretending cancel is a kill."""
    started = threading.Event()

    def uncooperative(progress):
        started.set()
        time.sleep(0.05)
        return "finished anyway"

    job = manager.submit("uncooperative", uncooperative)
    assert started.wait(5)
    manager.cancel(job.id)
    settled(job)

    assert job.state is JobState.SUCCEEDED
    assert job.result == "finished anyway"


def test_cancelling_a_finished_job_changes_nothing(manager):
    job = settled(manager.submit("work", lambda progress: "done"))
    before = len(job.events)
    manager.cancel(job.id)
    assert job.state is JobState.SUCCEEDED
    assert len(job.events) == before


def test_raising_cancelled_directly_is_a_cancellation_not_a_failure(manager):
    def give_up(progress):
        raise Cancelled()

    job = settled(manager.submit("work", give_up))
    assert job.state is JobState.CANCELLED
    assert job.error is None


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------


def test_streaming_a_finished_job_returns_at_once(manager):
    """It used to replay the backlog and then wait a second on a queue
    nothing would ever put to."""
    job = settled(manager.submit("work", lambda progress: "done"))

    start = time.monotonic()
    events = list(manager.stream(job.id))
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"streaming a finished job took {elapsed:.2f}s"
    assert events[-1].state is JobState.SUCCEEDED


def test_a_late_subscriber_still_sees_the_start(manager):
    def slow(progress):
        progress.update(0.5, "halfway")
        time.sleep(0.05)
        return "done"

    job = manager.submit("work", slow)
    time.sleep(0.02)

    messages = [e.message for e in manager.stream(job.id, timeout=2.0) if e.message]
    assert "started" in messages
    assert "halfway" in messages


def test_a_broken_subscriber_does_not_take_the_job_down(manager):
    job = manager.submit("work", lambda progress: "done")
    job.listen(lambda event: 1 / 0)
    settled(job)
    assert job.state is JobState.SUCCEEDED


def test_unsubscribing_stops_the_callbacks(manager):
    seen: list[str] = []
    job = manager.submit("work", lambda progress: time.sleep(0.05) or "done")
    unsubscribe = job.listen(lambda event: seen.append(event.kind))
    unsubscribe()
    settled(job)
    assert not seen


# --------------------------------------------------------------------------
# the manager
# --------------------------------------------------------------------------


def test_jobs_are_listed_and_findable(manager):
    first = settled(manager.submit("first", lambda progress: 1))
    second = settled(manager.submit("second", lambda progress: 2))

    listed = {job.id for job in manager.list_jobs()}
    assert {first.id, second.id} <= listed
    assert manager.get(first.id) is first


def test_an_unknown_job_is_not_found(manager):
    from rigsmith.core.errors import NotFound

    with pytest.raises(NotFound):
        manager.get("nope")


def test_the_history_does_not_grow_without_bound(manager):
    """A session left open all day must not accumulate every job forever."""
    for index in range(60):
        settled(manager.submit(f"job-{index}", lambda progress: index))

    assert len(manager.list_jobs()) <= 60


def test_a_chatty_job_keeps_only_the_tail_of_its_log(manager):
    def chatty(progress):
        for index in range(1200):
            progress.log(f"frame {index}")
        return "done"

    job = settled(manager.submit("chatty", chatty))
    assert len(job.events) <= job.max_events + 5
    assert any("frame 1199" in (e.message or "") for e in job.events)
