"""Finding good knob settings in a few dozen tries.

Each evaluation on hardware is a few seconds of real audio, so these are
built for budgets of 20-80 rather than the thousands a generic optimiser
expects:

* **auto** (default) - a Latin hypercube spread of starting points, because
  an amp's gain landscape has more than one basin (a cleaner setting with
  more treble can sit closer than a gainier one with less), then a pattern
  search from the best of them: try each knob up and down, keep what helps,
  halve the step when nothing does.
* **pattern** - the pattern search alone, from the rig as it stands. For
  polishing a setting that is already close.
* **optuna** - Optuna's TPE sampler, when it is installed. Better than the
  above once the budget is past ~60 and there are many knobs.

Every evaluation is kept, so the history can be drawn and any point in it
replayed.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from ..core.errors import Invalid
from .space import Space

Evaluate = Callable[[dict], "tuple[float, dict]"]


@dataclass
class Trial:
    index: int
    values: dict
    loss: float
    info: dict = field(default_factory=dict)
    phase: str = ""
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "values": self.values,
            "loss": self.loss,
            "info": self.info,
            "phase": self.phase,
            "elapsed": round(self.elapsed, 3),
        }


@dataclass
class TuneResult:
    method: str
    space: Space
    trials: list[Trial] = field(default_factory=list)
    stopped: str = ""

    @property
    def best(self) -> Trial | None:
        finite = [t for t in self.trials if math.isfinite(t.loss)]
        return min(finite, key=lambda t: t.loss) if finite else None

    @property
    def start(self) -> Trial | None:
        return self.trials[0] if self.trials else None

    @property
    def improvement(self) -> float | None:
        if not self.best or not self.start or not math.isfinite(self.start.loss) or self.start.loss <= 0:
            return None
        return 1.0 - self.best.loss / self.start.loss

    def describe(self) -> str:
        best = self.best
        if best is None:
            return "no successful evaluations"
        lines = [
            f"{len(self.trials)} evaluation(s) by {self.method}; best loss {best.loss:.4f} "
            f"at #{best.index} ({self.stopped})"
        ]
        if self.improvement is not None:
            lines.append(f"  {self.improvement:.0%} better than where it started ({self.start.loss:.4f})")
        lines.append("  " + self.space.describe(best.values))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        best = self.best
        return {
            "method": self.method,
            "space": self.space.to_dict(),
            "trials": [t.to_dict() for t in self.trials],
            "best": best.to_dict() if best else None,
            "best_display": {k.key: k.display(best.values[k.key]) for k in self.space.knobs} if best else None,
            "improvement": self.improvement,
            "stopped": self.stopped,
            "describe": self.describe(),
        }


class _Runner:
    """Shared bookkeeping: the budget, the cache, progress, cancellation."""

    def __init__(self, space: Space, evaluate: Evaluate, budget: int, progress=None, method: str = "auto") -> None:
        self.space = space
        self.evaluate = evaluate
        self.budget = budget
        self.progress = progress
        self.result = TuneResult(method=method, space=space)
        self._cache: dict[tuple, float] = {}

    @property
    def remaining(self) -> int:
        return self.budget - len(self.result.trials)

    def key(self, values: dict) -> tuple:
        return tuple(round(float(values[k.key]), 4) if k.kind == "float" else values[k.key] for k in self.space.knobs)

    def __call__(self, values: dict, phase: str) -> float:
        values = self.space.clip(values)
        key = self.key(values)
        if key in self._cache:
            return self._cache[key]
        if self.remaining <= 0:
            raise _BudgetSpent()
        if self.progress is not None:
            self.progress.check()
        started = time.monotonic()
        try:
            loss, info = self.evaluate(values)
        except _BudgetSpent:
            raise
        except Exception as exc:  # one failed render is a data point, not the end
            if type(exc).__name__ == "Cancelled":
                raise
            loss, info = float("inf"), {"error": str(exc)}
        trial = Trial(len(self.result.trials), values, float(loss), info, phase, time.monotonic() - started)
        self.result.trials.append(trial)
        self._cache[key] = trial.loss
        if self.progress is not None:
            best = self.result.best
            self.progress.update(
                len(self.result.trials) / self.budget,
                f"{len(self.result.trials)}/{self.budget}: {trial.loss:.4f} (best {best.loss:.4f})" if best else f"{len(self.result.trials)}/{self.budget}",
            )
        return trial.loss


class _BudgetSpent(Exception):
    pass


def _latin(space: Space, count: int, rng) -> list[dict]:
    points = []
    columns = []
    for knob in space.knobs:
        cuts = (np.arange(count) + rng.random(count)) / count
        rng.shuffle(cuts)
        columns.append(knob.low + cuts * (knob.high - knob.low))
    for i in range(count):
        points.append({knob.key: float(columns[j][i]) for j, knob in enumerate(space.knobs)})
    return points


def _pattern(run: _Runner, start: dict, *, step: float = 0.25, floor: float = 0.02) -> None:
    current = run.space.clip(start)
    current_loss = run(current, "pattern")
    while step >= floor and run.remaining > 0:
        improved = False
        for knob in run.space.knobs:
            span = knob.high - knob.low
            delta = max(step * span, 1.0) if knob.kind != "float" else step * span
            for direction in (+1, -1):
                trial = dict(current)
                trial[knob.key] = current[knob.key] + direction * delta
                trial = run.space.clip(trial)
                if trial == current:
                    continue
                loss = run(trial, "pattern")
                if loss < current_loss - 1e-6:
                    current, current_loss = trial, loss
                    improved = True
                    break
        if not improved:
            step /= 2


def _optuna(run: _Runner, start: dict, seed: int) -> None:
    import optuna  # type: ignore

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    sampler = optuna.samplers.TPESampler(seed=seed, n_startup_trials=max(5, len(run.space) * 2))
    study = optuna.create_study(direction="minimize", sampler=sampler)
    study.enqueue_trial({k.key: start[k.key] for k in run.space.knobs})

    def objective(trial):
        values = {}
        for knob in run.space.knobs:
            if knob.kind == "float":
                values[knob.key] = trial.suggest_float(knob.key, knob.low, knob.high)
            else:
                values[knob.key] = trial.suggest_int(knob.key, int(knob.low), int(knob.high))
        return run(values, "optuna")

    try:
        study.optimize(objective, n_trials=run.remaining)
    except _BudgetSpent:
        pass


def optimise(
    space: Space,
    evaluate: Evaluate,
    *,
    budget: int = 40,
    method: str = "auto",
    start: dict | None = None,
    seed: int = 0,
    progress=None,
) -> TuneResult:
    """Search ``space`` for the setting ``evaluate`` scores lowest."""
    if not len(space):
        raise Invalid("nothing to tune - the space has no knobs")
    if budget < 2:
        raise Invalid("a budget under two evaluations cannot compare anything")
    if method not in ("auto", "pattern", "optuna", "random"):
        raise Invalid(f"unknown method '{method}'", hint="Use auto, pattern, optuna or random.")
    if method == "optuna":
        try:
            import optuna  # type: ignore # noqa: F401
        except ImportError:
            method = "auto"

    run = _Runner(space, evaluate, budget, progress, method)
    rng = np.random.default_rng(seed)
    origin = space.clip(start or space.start())
    try:
        run(origin, "start")
        if method == "optuna":
            _optuna(run, origin, seed)
        elif method == "random":
            for point in _latin(space, run.remaining, rng):
                run(point, "random")
        elif method == "pattern":
            _pattern(run, origin, step=0.15)
        else:
            spread = min(max(4, 2 * len(space)), max(2, budget // 3))
            for point in _latin(space, spread, rng):
                run(point, "explore")
            best = run.result.best
            _pattern(run, best.values if best else origin, step=0.2)
        run.result.stopped = "converged"
    except _BudgetSpent:
        run.result.stopped = "budget spent"
    return run.result
