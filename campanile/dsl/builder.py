"""A fluent front end for building workflows.

:class:`campanile.model.Workflow` is perfectly usable on its own, but declaring a
long pipeline with it means repeating the workflow object on every line.  The
builder exists to make the common shape read well::

    flow = (
        WorkflowBuilder("nightly")
        .describe("Rebuild the reporting tables")
        .pool("warehouse", 4)
        .param("day", "string", required=True)
        .build_with(lambda b: b.task("extract", extract) >> b.task("load", load))
    )

The ``>>`` operator connects tasks, and accepts lists on either side so that
fan-out and fan-in read as one line::

    extract >> [transform_eu, transform_us] >> publish
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence, Union

from ..errors import WorkflowDefinitionError
from ..model.policy import FailurePolicy, RetryPolicy, TriggerRule
from ..model.task import TaskCallable, TaskSpec
from ..model.workflow import Workflow

__all__ = ["WorkflowBuilder", "TaskHandle", "TaskGroup", "Connectable"]

Connectable = Union["TaskHandle", "TaskGroup", str, Sequence[Any]]


class TaskHandle:
    """A reference to one declared task, connectable with ``>>``."""

    __slots__ = ("_builder", "_name")

    def __init__(self, builder: "WorkflowBuilder", name: str) -> None:
        self._builder = builder
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def spec(self) -> TaskSpec:
        return self._builder.workflow.task(self._name)

    @property
    def names(self) -> tuple[str, ...]:
        return (self._name,)

    def __rshift__(self, other: Connectable) -> "TaskGroup":
        return self._builder.connect_many(self, other)

    def __rrshift__(self, other: Connectable) -> "TaskGroup":
        """Handles the ``[a, b] >> c`` form, which Python routes here.

        A plain list has no ``>>`` of its own, so it returns ``NotImplemented``
        and Python asks the right-hand side instead.
        """

        return self._builder.connect_many(other, self)

    def __lshift__(self, other: Connectable) -> "TaskGroup":
        self._builder.connect_many(other, self)
        return TaskGroup(self._builder, self.names)

    def when(self, condition: str) -> "ConditionalHandle":
        """Attach a condition to the next ``>>`` from this task."""

        return ConditionalHandle(self._builder, self._name, condition)

    def __repr__(self) -> str:
        return f"TaskHandle({self._name!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, TaskHandle):
            return self._name == other._name and self._builder is other._builder
        return NotImplemented

    def __hash__(self) -> int:
        return hash((id(self._builder), self._name))


class ConditionalHandle:
    """The result of :meth:`TaskHandle.when`; only supports ``>>``."""

    __slots__ = ("_builder", "_name", "_condition")

    def __init__(self, builder: "WorkflowBuilder", name: str, condition: str) -> None:
        self._builder = builder
        self._name = name
        self._condition = condition

    @property
    def names(self) -> tuple[str, ...]:
        return (self._name,)

    def __rshift__(self, other: Connectable) -> "TaskGroup":
        return self._builder.connect_many(self, other, condition=self._condition)

    def __repr__(self) -> str:
        return f"ConditionalHandle({self._name!r}, {self._condition!r})"


class TaskGroup:
    """Several tasks treated as one endpoint of a connection."""

    __slots__ = ("_builder", "_names")

    def __init__(self, builder: "WorkflowBuilder", names: Iterable[str]) -> None:
        self._builder = builder
        self._names = tuple(names)

    @property
    def names(self) -> tuple[str, ...]:
        return self._names

    def __iter__(self):
        return iter(TaskHandle(self._builder, name) for name in self._names)

    def __len__(self) -> int:
        return len(self._names)

    def __rshift__(self, other: Connectable) -> "TaskGroup":
        return self._builder.connect_many(self, other)

    def __rrshift__(self, other: Connectable) -> "TaskGroup":
        """Handles ``[a, b] >> group``; see :meth:`TaskHandle.__rrshift__`."""

        return self._builder.connect_many(other, self)

    def __lshift__(self, other: Connectable) -> "TaskGroup":
        self._builder.connect_many(other, self)
        return self

    def __repr__(self) -> str:
        return f"TaskGroup({list(self._names)!r})"


class WorkflowBuilder:
    """Builds a :class:`Workflow` with chainable calls."""

    def __init__(
        self,
        name: str,
        *,
        description: str = "",
        default_retry: RetryPolicy | int | None = None,
        failure_policy: FailurePolicy | str = FailurePolicy.CONTINUE,
        max_parallelism: int | None = None,
    ) -> None:
        retry = default_retry
        if isinstance(retry, int) and not isinstance(retry, bool):
            retry = RetryPolicy(max_attempts=retry)
        self.workflow = Workflow(
            name=name,
            description=description,
            default_retry=retry,
            failure_policy=(
                failure_policy
                if isinstance(failure_policy, FailurePolicy)
                else FailurePolicy(str(failure_policy))
            ),
            max_parallelism=max_parallelism,
        )

    # -- workflow-level settings --------------------------------------------

    def describe(self, description: str) -> "WorkflowBuilder":
        self.workflow.description = description
        return self

    def pool(self, name: str, capacity: int, description: str = "") -> "WorkflowBuilder":
        self.workflow.pool(name, capacity, description)
        return self

    def param(
        self,
        name: str,
        type: str = "string",
        *,
        required: bool = False,
        default: Any = None,
        choices: Iterable[Any] = (),
        description: str = "",
    ) -> "WorkflowBuilder":
        self.workflow.param(
            name,
            type,
            required=required,
            default=default,
            choices=choices,
            description=description,
        )
        return self

    def tag(self, *tags: str) -> "WorkflowBuilder":
        self.workflow.tags = tuple(dict.fromkeys(self.workflow.tags + tags))
        return self

    def failure(self, policy: FailurePolicy | str) -> "WorkflowBuilder":
        self.workflow.failure_policy = (
            policy if isinstance(policy, FailurePolicy) else FailurePolicy(str(policy))
        )
        return self

    def parallelism(self, limit: int | None) -> "WorkflowBuilder":
        self.workflow.max_parallelism = limit
        self.workflow.__post_init__()
        return self

    # -- tasks ---------------------------------------------------------------

    def task(
        self,
        name: str,
        fn: TaskCallable | None = None,
        *,
        after: Iterable[str] | str = (),
        retry: RetryPolicy | int | Mapping[str, Any] | None = None,
        timeout: float | str | None = None,
        resources: Any = None,
        priority: int = 0,
        trigger_rule: TriggerRule | str = TriggerRule.ALL_SUCCESS,
        tags: Iterable[str] = (),
        params: Mapping[str, Any] | None = None,
        description: str = "",
        condition: str | None = None,
    ) -> TaskHandle:
        """Declare a task and return a handle to it."""

        self.workflow.add(
            name,
            fn,
            after=after,
            retry=retry,
            timeout=timeout,
            resources=resources,
            priority=priority,
            trigger_rule=trigger_rule,
            tags=tags,
            params=params,
            description=description,
            condition=condition,
        )
        return TaskHandle(self, name)

    def gate(self, name: str, **kwargs: Any) -> TaskHandle:
        """Declare a body-less task used purely as a join or fan-out point."""

        return self.task(name, None, **kwargs)

    def handle(self, name: str) -> TaskHandle:
        """A handle to an already-declared task."""

        self.workflow.task(name)
        return TaskHandle(self, name)

    def group(self, *names: str) -> TaskGroup:
        for name in names:
            self.workflow.task(name)
        return TaskGroup(self, names)

    # -- edges ---------------------------------------------------------------

    def connect(
        self, upstream: str, downstream: str, *, condition: str | None = None
    ) -> "WorkflowBuilder":
        self.workflow.connect(upstream, downstream, condition=condition)
        return self

    def connect_many(
        self, upstream: Connectable, downstream: Connectable, *, condition: str | None = None
    ) -> TaskGroup:
        """Connect every task on the left to every task on the right.

        Returns a group over the *downstream* names so that ``a >> b >> c``
        chains left to right the way it reads.
        """

        sources = _names_of(upstream)
        targets = _names_of(downstream)
        if not sources or not targets:
            raise WorkflowDefinitionError(
                "both sides of a connection must name at least one task",
                workflow=self.workflow.name,
            )
        for source in sources:
            for target in targets:
                self.workflow.connect(source, target, condition=condition)
        return TaskGroup(self, targets)

    def chain(self, *names: str) -> "WorkflowBuilder":
        self.workflow.chain(*names)
        return self

    # -- finishing -----------------------------------------------------------

    def build(self) -> Workflow:
        """Return the assembled workflow after a structural check."""

        self.workflow.check_references()
        return self.workflow

    def build_with(self, define: Callable[["WorkflowBuilder"], Any]) -> Workflow:
        """Run ``define`` against this builder, then :meth:`build`."""

        define(self)
        return self.build()

    def __repr__(self) -> str:
        return f"WorkflowBuilder({self.workflow.name!r}, tasks={len(self.workflow)})"


def _names_of(value: Connectable) -> tuple[str, ...]:
    """Read task names out of whatever the ``>>`` operator was handed."""

    if isinstance(value, str):
        return (value,)
    names = getattr(value, "names", None)
    if isinstance(names, tuple):
        return names
    if isinstance(value, (list, tuple)):
        collected: list[str] = []
        for item in value:
            collected.extend(_names_of(item))
        return tuple(dict.fromkeys(collected))
    raise WorkflowDefinitionError(f"cannot connect {value!r}: not a task reference")
