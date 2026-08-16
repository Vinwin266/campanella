"""Cancellation.

Two things can stop a run early: a caller cancelling it from outside, and a
``fail_fast`` workflow whose first failure should stop everything else.  Both go
through a :class:`CancellationToken`, so the scheduler has exactly one place to
check.

Cancellation is cooperative and coarse.  A task already running is allowed to
finish -- there is no safe way to abort arbitrary Python -- but nothing new
starts, and every task that had not started is marked cancelled.
"""

from __future__ import annotations

from typing import Callable, Iterable, Mapping, Sequence

from ..model.state import TaskState
from ..util.ordered import OrderedSet

__all__ = ["CancellationToken", "cancellable_tasks", "downstream_of_failure"]


class CancellationToken:
    """A one-way flag with a reason attached."""

    __slots__ = ("_cancelled", "_reason", "_at", "_listeners")

    def __init__(self) -> None:
        self._cancelled = False
        self._reason = ""
        self._at: float | None = None
        self._listeners: list[Callable[[str], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def at(self) -> float | None:
        """When the token was first cancelled, on the run's clock."""

        return self._at

    def cancel(self, reason: str = "cancelled", *, at: float | None = None) -> bool:
        """Cancel, returning ``True`` only for the first call.

        Later calls are no-ops that keep the original reason, so the *first*
        cause of a cascade is the one reported rather than the last.
        """

        if self._cancelled:
            return False
        self._cancelled = True
        self._reason = reason
        self._at = at
        for listener in list(self._listeners):
            listener(reason)
        return True

    def subscribe(self, listener: Callable[[str], None]) -> Callable[[], None]:
        """Register a callback fired once, when the token is cancelled."""

        if self._cancelled:
            listener(self._reason)
        else:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def reset(self) -> None:
        """Clear the flag.  Only meaningful when reusing a token across runs."""

        self._cancelled = False
        self._reason = ""
        self._at = None

    def __bool__(self) -> bool:
        return self._cancelled

    def __repr__(self) -> str:
        if not self._cancelled:
            return "CancellationToken(active)"
        return f"CancellationToken(cancelled={self._reason!r})"


def cancellable_tasks(
    states: Mapping[str, TaskState], order: Sequence[str]
) -> tuple[str, ...]:
    """Tasks that can still be cancelled, in declaration order.

    A task already in a terminal state is left alone -- a run that failed and
    was then cancelled still reports the failure, because that is what actually
    happened.
    """

    return tuple(
        name
        for name in order
        if name in states and not states[name].is_terminal
    )


def downstream_of_failure(
    failed: Iterable[str],
    downstream_map: Mapping[str, Sequence[str]],
    states: Mapping[str, TaskState],
) -> OrderedSet[str]:
    """Every not-yet-terminal task reachable from a failed one.

    This is the set a ``continue`` failure policy marks ``UPSTREAM_FAILED``.
    Tasks that already finished are excluded, and so are tasks reachable only
    through an already-terminal path, because their own trigger rule has already
    had its say.
    """

    affected: OrderedSet[str] = OrderedSet()
    frontier = list(failed)
    while frontier:
        current = frontier.pop(0)
        for child in downstream_map.get(current, ()):
            if child in affected:
                continue
            state = states.get(child)
            if state is None or state.is_terminal:
                continue
            affected.add(child)
            frontier.append(child)
    return affected
