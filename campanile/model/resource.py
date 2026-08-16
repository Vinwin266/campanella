"""Resource pools.

A pool is a named integer capacity that tasks consume while they run and release
when they finish.  It is the mechanism behind "at most four things may touch the
warehouse at once" and "this job needs two of the eight licence seats".

Pools are declared on the workflow; tasks reference them by name and say how many
slots they need.  The runtime never over-commits a pool, so a task requesting
more slots than a pool has capacity for would wait forever -- that is rejected at
validation time instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..errors import ConfigurationError, ResourceCapacityError, UnknownPoolError
from .identifiers import validate_pool_name

__all__ = [
    "ResourcePool",
    "ResourceRequest",
    "normalise_request",
    "check_request",
]


@dataclass(frozen=True)
class ResourcePool:
    """A named integer capacity."""

    name: str
    capacity: int
    description: str = ""

    def __post_init__(self) -> None:
        validate_pool_name(self.name)
        if not isinstance(self.capacity, int) or isinstance(self.capacity, bool):
            raise ConfigurationError(
                f"pool {self.name!r} capacity must be an integer, "
                f"got {type(self.capacity).__name__}"
            )
        if self.capacity < 1:
            raise ConfigurationError(
                f"pool {self.name!r} capacity must be at least 1, got {self.capacity}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "capacity": self.capacity,
            "description": self.description,
        }

    def describe(self) -> str:
        suffix = f" -- {self.description}" if self.description else ""
        return f"{self.name} (capacity {self.capacity}){suffix}"


#: What a task asks for: pool name -> number of slots.  Always sorted by name so
#: that two equivalent requests compare and hash identically.
ResourceRequest = Mapping[str, int]


def normalise_request(request: Mapping[str, Any] | Iterable[str] | None) -> dict[str, int]:
    """Accept the several shapes a caller might write and return a clean map.

    A bare string or a list of strings means one slot of each named pool, which
    covers the common case::

        resources="warehouse"
        resources=["warehouse", "licence"]
        resources={"warehouse": 1, "licence": 2}
    """

    if request is None:
        return {}
    if isinstance(request, str):
        return {validate_pool_name(request): 1}
    if isinstance(request, Mapping):
        cleaned: dict[str, int] = {}
        for name, amount in request.items():
            pool = validate_pool_name(name)
            if isinstance(amount, bool) or not isinstance(amount, int):
                raise ConfigurationError(
                    f"resource amount for pool {pool!r} must be an integer, "
                    f"got {type(amount).__name__}"
                )
            if amount < 1:
                raise ConfigurationError(
                    f"resource amount for pool {pool!r} must be at least 1, got {amount}"
                )
            cleaned[pool] = amount
        return dict(sorted(cleaned.items()))
    cleaned = {}
    for name in request:
        pool = validate_pool_name(name)
        if pool in cleaned:
            raise ConfigurationError(f"pool {pool!r} listed twice in one request")
        cleaned[pool] = 1
    return dict(sorted(cleaned.items()))


def check_request(
    request: Mapping[str, int],
    pools: Mapping[str, ResourcePool],
    *,
    task: str | None = None,
) -> None:
    """Raise if ``request`` names a pool that does not exist or cannot fit it."""

    for pool_name, amount in sorted(request.items()):
        pool = pools.get(pool_name)
        if pool is None:
            raise UnknownPoolError(pool_name, task=task, known=pools)
        if amount > pool.capacity:
            raise ResourceCapacityError(pool_name, amount, pool.capacity, task=task)
