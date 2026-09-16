"""Blind A/B listening, background jobs, and the workspace."""

from __future__ import annotations

from typing import Literal

from ...core import matching, workspace
from ...core.jobs import get_manager
from .._shared import tool


# --------------------------------------------------------------------------
# blind A/B
# --------------------------------------------------------------------------


@tool
def start_ab_test(
    session_id: str,
    target_label: str,
    candidates: dict[str, str],
    pairs: int | None = None,
) -> str:
    """Start a blind A/B listening test over candidate renders.

    Metrics rank; ears decide. Candidate identities are hidden until results
    are requested, because knowing which one the agent recommended is enough
    to hear it as better.

    Args:
        candidates: {label: audio path}, at least two.
        pairs: Cap the number of trials. Defaults to every unique pairing,
            which is n*(n-1)/2 - worth capping above four candidates.
    """
    session = matching.create_ab(session_id, target_label, candidates, pairs=pairs)
    return (
        f"Created blind A/B session '{session_id}' with {len(session.trials)} "
        f"trial(s) over {len(candidates)} candidates.\n\n{session.render_next()}"
    )


@tool
def list_ab_sessions() -> str:
    """List blind listening sessions in the workspace.

    Sessions persist, so one started days ago is still here - this is how you
    find it again without remembering the id.
    """
    sessions = matching.list_ab_sessions()
    if not sessions:
        return "No A/B sessions. Start one with start_ab_test."
    lines = [f"{len(sessions)} session(s):"]
    for session_id in sessions:
        session = matching.load_ab(session_id)
        state = "complete" if session.complete else "in progress"
        lines.append(
            f"  {session_id:24} {session.answered_count}/{len(session.trials)} "
            f"trials, {state}  (target: {session.target_label})"
        )
    return "\n".join(lines)


@tool
def submit_ab_verdict(
    session_id: str,
    trial_id: int,
    verdict: Literal["left", "right", "tie"],
    confidence: int | None = None,
    comment: str | None = None,
) -> str:
    """Record a verdict for one blind A/B trial and present the next one."""
    session = matching.answer_ab(
        session_id, trial_id, verdict, confidence=confidence, comment=comment
    )
    if session.complete:
        return (
            f"Trial {trial_id} recorded. All trials done.\n\n"
            f"{session.render_standings()}"
        )
    return f"Trial {trial_id} recorded.\n\n{session.render_next()}"


@tool
def ab_results(session_id: str) -> str:
    """Unblind a listening session and show the standings.

    Reveals which candidate was which for the first time, ranked by how
    often each won its trials. Safe to call before every trial is
    answered, but partial results can still change.
    """
    return matching.load_ab(session_id).render_standings()


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------


@tool
def job_status(job_id: str, include_log: bool = False) -> str:
    """Check a background job - progress, state, and result.

    Slow work (source separation, offline candidate sweeps) runs in the
    background and hands back a job id. This is how you tell working from
    hung, and how you collect the result when it finishes.
    """
    job = get_manager().get(job_id)
    lines = [job.render()]

    if include_log:
        lines.append("\nlog:")
        lines.extend(
            f"  {e.message}" for e in job.events if e.kind == "log" and e.message
        )

    if job.state.value == "succeeded" and job.result is not None:
        render = getattr(job.result, "render", None)
        lines.append("\nresult:")
        lines.append(render() if callable(render) else str(job.result))
    return "\n".join(lines)


@tool
def list_jobs(active_only: bool = False) -> str:
    """List background jobs, most recent first.

    Shows state, progress and elapsed time for separation and offline
    search runs, so a job started earlier in the session is findable
    without having kept its id.
    """
    jobs = get_manager().list_jobs(active_only=active_only)
    if not jobs:
        return "No jobs." if not active_only else "No jobs running."
    lines = [f"{len(jobs)} job(s):"]
    lines.extend(f"  {j.id}  {j.render()}" for j in jobs)
    return "\n".join(lines)


@tool
def cancel_job(job_id: str) -> str:
    """Ask a running job to stop at its next checkpoint.

    Cancellation is cooperative - the job stops when it next checks, so a
    long step may run to completion first. Partial output already written
    to the workspace is left in place.
    """
    job = get_manager().cancel(job_id)
    return f"Cancellation requested for {job.id} ({job.name})."


# --------------------------------------------------------------------------
# workspace
# --------------------------------------------------------------------------


@tool
def workspace_status() -> str:
    """Show what rigsmith has accumulated in its workspace, and how big it is.

    Covers device backups, protocol captures, unpacked firmware, rendered
    audio, drafts, and A/B sessions.
    """
    return workspace.inspect().render()


@tool
def workspace_prune(
    areas: list[str] | None = None,
    older_than_days: float | None = None,
    apply: bool = False,
) -> str:
    """Clean up workspace content. Reports without deleting unless apply=True.

    Backups and drafts are protected by default - name them explicitly to
    prune them anyway. Backups are the safety net for device writes, so
    think before clearing them.
    """
    return workspace.prune(
        areas, older_than_days=older_than_days, dry_run=not apply
    ).render()
