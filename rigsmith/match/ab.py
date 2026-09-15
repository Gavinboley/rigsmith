"""Layer 3: blind A/B listening, which is the actual arbiter.

Metrics rank candidates. They do not decide. A capture can win on spectral
distance and still be the wrong feel, and the person who will play through
the rig is the only one who can judge that.

So this runs proper blind trials: candidates are relabelled with opaque ids,
the mapping is kept out of what the listener sees, and the verdicts are
recorded against those ids. Only when the session is scored does the mapping
come back. That matters because knowing which one is "the A2 capture the
agent recommended" is enough to hear it as better.

Sessions persist to JSON so listening can be spread over days.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Trial:
    """One comparison put to the listener."""

    trial_id: int
    left: str
    """Blinded id, not the candidate's real label."""
    right: str
    verdict: str | None = None
    """``left``, ``right``, ``tie``, or ``None`` while unanswered."""
    confidence: int | None = None
    """1-5, optional."""
    comment: str | None = None
    answered_at: str | None = None

    @property
    def answered(self) -> bool:
        return self.verdict is not None


@dataclass
class ABSession:
    """A blind listening test over a set of candidates."""

    session_id: str
    target_label: str
    blinded: dict[str, str] = field(default_factory=dict)
    """blinded id -> real candidate label. Not shown during the test."""
    audio_paths: dict[str, str] = field(default_factory=dict)
    """blinded id -> audio file path."""
    trials: list[Trial] = field(default_factory=list)
    created_at: str = ""
    notes: list[str] = field(default_factory=list)

    # -- progress ---------------------------------------------------------

    @property
    def answered_count(self) -> int:
        return sum(1 for t in self.trials if t.answered)

    @property
    def complete(self) -> bool:
        return bool(self.trials) and all(t.answered for t in self.trials)

    def next_trial(self) -> Trial | None:
        return next((t for t in self.trials if not t.answered), None)

    def trial(self, trial_id: int) -> Trial:
        found = next((t for t in self.trials if t.trial_id == trial_id), None)
        if found is None:
            raise KeyError(f"no trial {trial_id} in session '{self.session_id}'")
        return found

    def answer(
        self,
        trial_id: int,
        verdict: str,
        *,
        confidence: int | None = None,
        comment: str | None = None,
    ) -> Trial:
        """Record a verdict.

        Args:
            verdict: ``left``, ``right``, or ``tie``.
        """
        if verdict not in ("left", "right", "tie"):
            raise ValueError("verdict must be 'left', 'right', or 'tie'")
        if confidence is not None and not 1 <= confidence <= 5:
            raise ValueError("confidence must be between 1 and 5")

        trial = self.trial(trial_id)
        trial.verdict = verdict
        trial.confidence = confidence
        trial.comment = comment
        trial.answered_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return trial

    # -- scoring ----------------------------------------------------------

    def standings(self) -> list[tuple[str, float, int]]:
        """Real labels ranked by wins, best first.

        Returns ``(label, points, comparisons)``. A win scores 1, a tie 0.5.
        """
        points: dict[str, float] = {b: 0.0 for b in self.blinded}
        appearances: dict[str, int] = {b: 0 for b in self.blinded}

        for trial in self.trials:
            if not trial.answered:
                continue
            appearances[trial.left] = appearances.get(trial.left, 0) + 1
            appearances[trial.right] = appearances.get(trial.right, 0) + 1
            if trial.verdict == "left":
                points[trial.left] = points.get(trial.left, 0) + 1
            elif trial.verdict == "right":
                points[trial.right] = points.get(trial.right, 0) + 1
            else:
                points[trial.left] = points.get(trial.left, 0) + 0.5
                points[trial.right] = points.get(trial.right, 0) + 0.5

        rows = [
            (self.blinded.get(b, b), points.get(b, 0.0), appearances.get(b, 0))
            for b in self.blinded
        ]
        rows.sort(key=lambda r: (-r[1], r[0]))
        return rows

    def render_standings(self) -> str:
        if not self.answered_count:
            return "No trials answered yet."
        lines = [
            f"Blind A/B results for '{self.target_label}' "
            f"({self.answered_count}/{len(self.trials)} trials answered):",
            "",
        ]
        for label, points, seen in self.standings():
            share = f"{points}/{seen}" if seen else "-"
            lines.append(f"  {label:32} {share}")
        if not self.complete:
            lines.append("\nIncomplete - remaining trials may change the order.")
        return "\n".join(lines)

    def render_next(self) -> str:
        """What to tell the listener for the next trial, revealing nothing."""
        trial = self.next_trial()
        if trial is None:
            return "All trials answered. Call standings() to unblind."
        return (
            f"Trial {trial.trial_id} of {len(self.trials)}\n"
            f"  Target:  {self.target_label}\n"
            f"  A:  {self.audio_paths.get(trial.left, trial.left)}\n"
            f"  B:  {self.audio_paths.get(trial.right, trial.right)}\n\n"
            "Which is closer to the target? Answer 'left' (A), 'right' (B), or 'tie'."
        )

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "target_label": self.target_label,
            "blinded": self.blinded,
            "audio_paths": self.audio_paths,
            "trials": [asdict(t) for t in self.trials],
            "created_at": self.created_at,
            "notes": self.notes,
        }

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "ABSession":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        session = cls(
            session_id=data["session_id"],
            target_label=data["target_label"],
            blinded=data.get("blinded", {}),
            audio_paths=data.get("audio_paths", {}),
            created_at=data.get("created_at", ""),
            notes=data.get("notes", []),
        )
        session.trials = [Trial(**t) for t in data.get("trials", [])]
        return session


def create_session(
    session_id: str,
    target_label: str,
    candidates: dict[str, str],
    *,
    pairs: int | None = None,
    seed: int | None = None,
) -> ABSession:
    """Build a blind A/B session over ``candidates``.

    Args:
        candidates: real label -> audio file path.
        pairs: How many trials. Defaults to every unique pairing, which is
            ``n*(n-1)/2`` and gets long fast - cap it for more than four
            candidates.
        seed: Fixes the blinding and trial order, for reproducibility.

    The blinded ids are assigned from a shuffled order, so they carry no
    information about rank, and left/right placement is randomised per trial
    to avoid a side bias.
    """
    if len(candidates) < 2:
        raise ValueError("a blind test needs at least two candidates")

    rng = random.Random(seed)
    labels = list(candidates)
    rng.shuffle(labels)

    session = ABSession(
        session_id=session_id,
        target_label=target_label,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    for index, label in enumerate(labels):
        blind_id = f"X{index + 1}"
        session.blinded[blind_id] = label
        session.audio_paths[blind_id] = candidates[label]

    blind_ids = list(session.blinded)
    combinations = [
        (a, b)
        for i, a in enumerate(blind_ids)
        for b in blind_ids[i + 1 :]
    ]
    rng.shuffle(combinations)
    if pairs is not None:
        combinations = combinations[:pairs]

    for trial_id, (a, b) in enumerate(combinations, start=1):
        left, right = (a, b) if rng.random() < 0.5 else (b, a)
        session.trials.append(Trial(trial_id=trial_id, left=left, right=right))

    session.notes.append(
        "Candidate identities are hidden until standings() is called. Listen "
        "on the monitoring you actually play through, and trust the trials "
        "over any metric ranking."
    )
    return session
