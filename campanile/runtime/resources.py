"""Accounting for resource pools.

A ledger tracks, for each declared pool, how many slots are in use and by whom.
It never over-commits and never under-releases: acquiring more than is available
raises rather than going negative, and releasing something that was not held is
an error too, because both are bookkeeping bugs that would otherwise show up
much later as an unexplained stall.

The ledger is pure accounting.  It does not know about tasks running, only about
names holding slots.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from ..errors import ResourceCapacityError, StateTransitionError, UnknownPoolError
from ..model.resource import ResourcePool
from ..util.ordered import OrderedSet

__all__ = ["ResourceLedger", "Reservation"]


class Reservation:
    """What one holder took from which pools."""

    __slots__ = ("holder", "amounts")

    def __init__(self, holder: str, amounts: Mapping[str, int]) -> None:
        self.holder = holder
        self.amounts = dict(sorted(amounts.items()))

    @property
    def total(self) -> int:
        return sum(self.amounts.values())

    @property
    def pools(self) -> tuple[str, ...]:
        return tuple(self.amounts)

    def describe(self) -> str:
        rendered = ", ".join(f"{pool}:{amount}" for pool, amount in self.amounts.items())
        return f"{self.holder} holds {rendered}"

    def __repr__(self) -> str:
        return f"Reservation({self.holder!r}, {self.amounts!r})"


class ResourceLedger:
    """Tracks slot usage across a set of pools."""

    __slots__ = ("_pools", "_used", "_reservations", "_peak")

    def __init__(self, pools: Mapping[str, ResourcePool] | Iterable[ResourcePool] = ()) -> None:
        if isinstance(pools, Mapping):
            self._pools = dict(pools)
        else:
            self._pools = {pool.name: pool for pool in pools}
        self._used: dict[str, int] = {name: 0 for name in self._pools}
        self._reservations: dict[str, Reservation] = {}
        self._peak: dict[str, int] = {name: 0 for name in self._pools}

    # -- queries -------------------------------------------------------------

    @property
    def pools(self) -> tuple[str, ...]:
        return tuple(self._pools)

    def capacity(self, pool: str) -> int:
        return self._pool(pool).capacity

    def used(self, pool: str) -> int:
        self._pool(pool)
        return self._used[pool]

    def available(self, pool: str) -> int:
        return self.capacity(pool) - self.used(pool)

    def peak(self, pool: str) -> int:
        """The highest simultaneous usage seen so far."""

        self._pool(pool)
        return self._peak[pool]

    def holders(self, pool: str | None = None) -> tuple[str, ...]:
        """Who currently holds slots, in acquisition order."""

        if pool is None:
            return tuple(self._reservations)
        self._pool(pool)
        return tuple(
            holder
            for holder, reservation in self._reservations.items()
            if pool in reservation.amounts
        )

    def is_held_by(self, holder: str) -> bool:
        return holder in self._reservations

    def reservation(self, holder: str) -> Reservation | None:
        return self._reservations.get(holder)

    def can_admit(self, request: Mapping[str, int]) -> bool:
        """Whether ``request`` fits in what is currently free."""

        for pool, amount in request.items():
            if amount > self.available(pool):
                return False
        return True

    def blocking_pools(self, request: Mapping[str, int]) -> tuple[str, ...]:
        """Which pools stop ``request`` from being admitted, sorted by name."""

        return tuple(
            sorted(
                pool
                for pool, amount in request.items()
                if amount > self.available(pool)
            )
        )

    # -- mutation ------------------------------------------------------------

    def acquire(self, holder: str, request: Mapping[str, int]) -> Reservation:
        """Take slots for ``holder``.

        The whole request succeeds or none of it does, so a task never sits
        holding half of what it needs while waiting for the rest.
        """

        if holder in self._reservations:
            raise StateTransitionError(f"holder {holder!r}", "holding", "acquiring")

        for pool, amount in sorted(request.items()):
            pool_spec = self._pool(pool)
            if amount > pool_spec.capacity:
                raise ResourceCapacityError(pool, amount, pool_spec.capacity, task=holder)
            if amount > self.available(pool):
                raise ResourceCapacityError(
                    pool, amount, self.available(pool), task=holder
                )

        for pool, amount in sorted(request.items()):
            self._used[pool] += amount
            self._peak[pool] = max(self._peak[pool], self._used[pool])

        reservation = Reservation(holder, request)
        self._reservations[holder] = reservation
        return reservation

    def release(self, holder: str) -> Reservation:
        """Give back everything ``holder`` took."""

        reservation = self._reservations.pop(holder, None)
        if reservation is None:
            raise StateTransitionError(f"holder {holder!r}", "idle", "releasing")
        for pool, amount in reservation.amounts.items():
            self._used[pool] -= amount
            if self._used[pool] < 0:  # pragma: no cover - defensive
                raise StateTransitionError(f"pool {pool!r}", "0", "negative")
        return reservation

    def release_all(self) -> tuple[Reservation, ...]:
        """Release every outstanding reservation, in acquisition order."""

        released = []
        for holder in list(self._reservations):
            released.append(self.release(holder))
        return tuple(released)

    def reset(self) -> None:
        self._used = {name: 0 for name in self._pools}
        self._reservations.clear()
        self._peak = {name: 0 for name in self._pools}

    # -- rendering -----------------------------------------------------------

    def usage(self) -> dict[str, tuple[int, int]]:
        """``{pool: (used, capacity)}`` for every pool, in declaration order."""

        return {
            name: (self._used[name], pool.capacity) for name, pool in self._pools.items()
        }

    def describe(self) -> str:
        if not self._pools:
            return "no pools"
        parts = [
            f"{name}: {used}/{capacity}"
            for name, (used, capacity) in self.usage().items()
        ]
        return ", ".join(parts)

    def outstanding(self) -> OrderedSet[str]:
        return OrderedSet(self._reservations)

    def _pool(self, name: str) -> ResourcePool:
        try:
            return self._pools[name]
        except KeyError:
            raise UnknownPoolError(name, known=self._pools) from None

    def __repr__(self) -> str:
        return f"ResourceLedger({self.describe()})"
