"""Task specifications.

A :class:`TaskSpec` is everything the engine needs to know about one node in a
workflow: what to call, how long to let it run, how often to retry it, what it
consumes while running, and what it requires of its upstreams.

Specs are frozen.  Editing one produces a new spec via :meth:`TaskSpec.evolve`,
which keeps a workflow definition safe to share between runs -- a run may not
mutate the definition it was started from.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping

from ..errors import ConfigurationError
from ..util.duration import coerce_duration, format_duration
from .identifiers import validate_tag, validate_task_name
from .policy import NO_RETRY, RetryPolicy, TriggerRule
from .resource import normalise_request

__all__ = ["TaskSpec", "TaskCallable", "DEFAULT_PRIORITY"]

#: A task body.  It may take a run context or take nothing at all; the runtime
#: inspects the signature and calls it the right way.
TaskCallable = Callable[..., Any]

#: Priorities are "lower runs first", like ``nice``.  Zero is the middle.
DEFAULT_PRIORITY = 0


@dataclass(frozen=True)
class TaskSpec:
    """One node of a workflow."""

    name: str
    fn: TaskCallable | None = None
    retry: RetryPolicy = NO_RETRY
    timeout: float | None = None
    resources: Mapping[str, int] = field(default_factory=dict)
    priority: int = DEFAULT_PRIORITY
    trigger_rule: TriggerRule = TriggerRule.ALL_SUCCESS
    tags: tuple[str, ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)
    description: str = ""

    def __post_init__(self) -> None:
        validate_task_name(self.name)
        if self.fn is not None and not callable(self.fn):
            raise ConfigurationError(
                f"task {self.name!r} body must be callable, got {type(self.fn).__name__}"
            )
        if not isinstance(self.retry, RetryPolicy):
            raise ConfigurationError(
                f"task {self.name!r} retry must be a RetryPolicy, "
                f"got {type(self.retry).__name__}"
            )
        if not isinstance(self.trigger_rule, TriggerRule):
            raise ConfigurationError(
                f"task {self.name!r} trigger_rule must be a TriggerRule, "
                f"got {type(self.trigger_rule).__name__}"
            )
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ConfigurationError(
                f"task {self.name!r} priority must be an integer, "
                f"got {type(self.priority).__name__}"
            )

        timeout = coerce_duration(self.timeout, None)
        if timeout is not None and timeout <= 0:
            raise ConfigurationError(
                f"task {self.name!r} timeout must be positive, got {timeout}"
            )
        object.__setattr__(self, "timeout", timeout)
        object.__setattr__(self, "resources", normalise_request(self.resources))
        object.__setattr__(self, "tags", tuple(validate_tag(tag) for tag in self.tags))
        object.__setattr__(self, "params", dict(self.params))

    # -- construction --------------------------------------------------------

    @classmethod
    def build(
        cls,
        name: str,
        fn: TaskCallable | None = None,
        *,
        retry: RetryPolicy | int | Mapping[str, Any] | None = None,
        timeout: float | str | None = None,
        resources: Any = None,
        priority: int = DEFAULT_PRIORITY,
        trigger_rule: TriggerRule | str = TriggerRule.ALL_SUCCESS,
        tags: Iterable[str] = (),
        params: Mapping[str, Any] | None = None,
        description: str = "",
    ) -> "TaskSpec":
        """Build a spec from the loose forms a workflow author writes.

        ``retry`` accepts a policy, a bare attempt count, or a mapping of policy
        fields; ``trigger_rule`` accepts the enum or its name; ``timeout``
        accepts a duration string.
        """

        return cls(
            name=name,
            fn=fn,
            retry=coerce_retry(retry),
            timeout=timeout,
            resources=resources,
            priority=priority,
            trigger_rule=(
                trigger_rule
                if isinstance(trigger_rule, TriggerRule)
                else _coerce_trigger_rule(trigger_rule, name)
            ),
            tags=tuple(tags),
            params=dict(params or {}),
            description=description,
        )

    def evolve(self, **changes: Any) -> "TaskSpec":
        """Return a copy with ``changes`` applied."""

        unknown = sorted(set(changes) - {f.name for f in self.__dataclass_fields__.values()})
        if unknown:
            raise ConfigurationError(f"unknown task field(s): {', '.join(unknown)}")
        return replace(self, **changes)

    # -- queries -------------------------------------------------------------

    @property
    def is_noop(self) -> bool:
        """True for a structural task with no body, used as a join or a gate."""

        return self.fn is None

    @property
    def pools(self) -> tuple[str, ...]:
        return tuple(self.resources)

    def requires(self, pool: str) -> int:
        """Slots of ``pool`` this task needs, zero if it does not use it."""

        return int(self.resources.get(pool, 0))

    def has_tag(self, tag: str) -> bool:
        return tag in self.tags

    def matches(self, *, tag: str | None = None, pool: str | None = None) -> bool:
        """Filter predicate used by the CLI's ``--tag`` and ``--pool`` options."""

        if tag is not None and not self.has_tag(tag):
            return False
        if pool is not None and self.requires(pool) == 0:
            return False
        return True

    def sort_key(self, position: int) -> tuple[int, int, str]:
        """The dispatcher's ordering key: priority, declaration order, name."""

        return (self.priority, position, self.name)

    # -- rendering -----------------------------------------------------------

    def describe(self) -> str:
        bits = [self.name]
        if self.is_noop:
            bits.append("(no body)")
        if self.priority != DEFAULT_PRIORITY:
            bits.append(f"priority={self.priority}")
        if self.trigger_rule is not TriggerRule.ALL_SUCCESS:
            bits.append(f"trigger={self.trigger_rule}")
        if self.retry.enabled:
            bits.append(f"retry={self.retry.max_attempts}x")
        if self.timeout is not None:
            bits.append(f"timeout={format_duration(self.timeout)}")
        if self.resources:
            rendered = ",".join(f"{pool}:{amount}" for pool, amount in self.resources.items())
            bits.append(f"resources={rendered}")
        if self.tags:
            bits.append("tags=" + ",".join(self.tags))
        return " ".join(bits)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "has_body": not self.is_noop,
            "retry": self.retry.as_dict(),
            "timeout": self.timeout,
            "resources": dict(self.resources),
            "priority": self.priority,
            "trigger_rule": self.trigger_rule.value,
            "tags": list(self.tags),
            "params": dict(self.params),
            "description": self.description,
        }


def coerce_retry(value: RetryPolicy | int | Mapping[str, Any] | None) -> RetryPolicy:
    """Accept the several ways a caller may express a retry policy."""

    if value is None:
        return NO_RETRY
    if isinstance(value, RetryPolicy):
        return value
    if isinstance(value, bool):
        raise ConfigurationError("retry must be a policy, an attempt count or a mapping")
    if isinstance(value, int):
        return RetryPolicy(max_attempts=value)
    if isinstance(value, Mapping):
        return RetryPolicy.from_dict(value)
    raise ConfigurationError(
        f"cannot read a retry policy from {type(value).__name__}"
    )


def _coerce_trigger_rule(value: Any, task: str) -> TriggerRule:
    try:
        return TriggerRule(str(value))
    except ValueError as exc:
        known = ", ".join(rule.value for rule in TriggerRule)
        raise ConfigurationError(
            f"task {task!r} has unknown trigger rule {value!r}; expected one of {known}"
        ) from exc
