"""Workflow definitions.

A :class:`Workflow` is a named, ordered collection of :class:`TaskSpec` plus the
edges between them, the resource pools they draw on, and the run-level policy
that governs what happens when one of them fails.

Workflows are mutable while being built and are expected to be frozen -- by
convention, not enforcement -- once handed to an engine.  A run never mutates the
definition it was started from; it keeps its own state map instead.

Edges carry an optional condition, a string in the expression language.  The
condition is stored as text here and compiled by the validation layer, which
keeps this module free of any dependency on the expression package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..errors import (
    CycleError,
    DuplicateTaskError,
    UnknownTaskError,
    WorkflowDefinitionError,
)
from ..util.ordered import OrderedSet, stable_unique
from ..util.topology import (
    ancestors,
    depth_levels,
    descendants,
    find_cycle,
    leaves,
    roots,
    topological_sort,
)
from .identifiers import validate_task_name, validate_workflow_name
from .params import ParamSchema, ParamSpec
from .policy import FailurePolicy, RetryPolicy, TriggerRule
from .resource import ResourcePool, check_request
from .task import TaskCallable, TaskSpec, coerce_retry

__all__ = ["Edge", "Workflow"]


@dataclass(frozen=True)
class Edge:
    """A dependency from ``upstream`` to ``downstream``.

    ``condition`` is an expression evaluated after the upstream finishes.  When
    it evaluates false the *edge* is considered unsatisfied, which the trigger
    rule then interprets: under the default ``ALL_SUCCESS`` the downstream is
    skipped, under ``ANY_SUCCESS`` it may still run via another edge.
    """

    upstream: str
    downstream: str
    condition: str | None = None

    def __post_init__(self) -> None:
        validate_task_name(self.upstream)
        validate_task_name(self.downstream)
        if self.upstream == self.downstream:
            raise WorkflowDefinitionError(
                f"task {self.upstream!r} cannot depend on itself"
            )
        if self.condition is not None:
            if not isinstance(self.condition, str) or not self.condition.strip():
                raise WorkflowDefinitionError(
                    f"condition on edge {self.upstream}->{self.downstream} "
                    "must be a non-empty string"
                )

    @property
    def is_conditional(self) -> bool:
        return self.condition is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "upstream": self.upstream,
            "downstream": self.downstream,
            "condition": self.condition,
        }

    def describe(self) -> str:
        arrow = "-->" if self.condition is None else f"--[{self.condition}]-->"
        return f"{self.upstream} {arrow} {self.downstream}"


@dataclass
class Workflow:
    """A named task graph."""

    name: str
    description: str = ""
    default_retry: RetryPolicy | None = None
    failure_policy: FailurePolicy = FailurePolicy.CONTINUE
    max_parallelism: int | None = None
    tasks: dict[str, TaskSpec] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    pools: dict[str, ResourcePool] = field(default_factory=dict)
    params: ParamSchema = field(default_factory=ParamSchema)
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_workflow_name(self.name)
        if self.max_parallelism is not None:
            if isinstance(self.max_parallelism, bool) or not isinstance(self.max_parallelism, int):
                raise WorkflowDefinitionError(
                    "max_parallelism must be an integer", workflow=self.name
                )
            if self.max_parallelism < 1:
                raise WorkflowDefinitionError(
                    f"max_parallelism must be at least 1, got {self.max_parallelism}",
                    workflow=self.name,
                )
        if not isinstance(self.failure_policy, FailurePolicy):
            self.failure_policy = FailurePolicy(str(self.failure_policy))

    # -- declaration ---------------------------------------------------------

    def add(
        self,
        name: str,
        fn: TaskCallable | None = None,
        *,
        after: Iterable[str] | str = (),
        before: Iterable[str] | str = (),
        retry: RetryPolicy | int | Mapping[str, Any] | None = None,
        timeout: float | str | None = None,
        resources: Any = None,
        priority: int = 0,
        trigger_rule: TriggerRule | str = TriggerRule.ALL_SUCCESS,
        tags: Iterable[str] = (),
        params: Mapping[str, Any] | None = None,
        description: str = "",
        condition: str | None = None,
    ) -> TaskSpec:
        """Declare a task and, optionally, its edges in one call.

        ``after`` and ``before`` accept a single name or a list.  ``condition``
        applies to every edge created by ``after``, which is the shorthand for
        "run this only when the upstream produced something interesting".
        """

        spec = TaskSpec.build(
            name,
            fn,
            retry=coerce_retry(retry) if retry is not None else self.default_retry,
            timeout=timeout,
            resources=resources,
            priority=priority,
            trigger_rule=trigger_rule,
            tags=tags,
            params=params,
            description=description,
        )
        self.add_task(spec)
        for upstream in _as_names(after):
            self.connect(upstream, name, condition=condition)
        for downstream in _as_names(before):
            self.connect(name, downstream)
        return spec

    def add_task(self, spec: TaskSpec) -> TaskSpec:
        """Add an already-built spec."""

        if spec.name in self.tasks:
            raise DuplicateTaskError(spec.name, workflow=self.name)
        self.tasks[spec.name] = spec
        return spec

    def replace_task(self, spec: TaskSpec) -> TaskSpec:
        """Swap an existing spec for a new one with the same name."""

        if spec.name not in self.tasks:
            raise UnknownTaskError(spec.name, workflow=self.name, known=self.tasks)
        self.tasks[spec.name] = spec
        return spec

    def connect(
        self, upstream: str, downstream: str, *, condition: str | None = None
    ) -> Edge:
        """Declare that ``downstream`` depends on ``upstream``.

        Re-declaring an identical edge is a no-op so that ``after=`` and an
        explicit :meth:`connect` can coexist.  Re-declaring the *same* pair with
        a different condition is an error, because silently keeping one of two
        conditions would be a trap.
        """

        edge = Edge(upstream, downstream, condition)
        for existing in self.edges:
            if existing.upstream == edge.upstream and existing.downstream == edge.downstream:
                if existing.condition == edge.condition:
                    return existing
                raise WorkflowDefinitionError(
                    f"edge {upstream}->{downstream} is declared twice with "
                    f"different conditions ({existing.condition!r} and {condition!r})",
                    workflow=self.name,
                )
        self.edges.append(edge)
        return edge

    def chain(self, *names: str) -> None:
        """Connect ``names`` in sequence: ``a -> b -> c``."""

        for upstream, downstream in zip(names, names[1:]):
            self.connect(upstream, downstream)

    def fan_out(self, upstream: str, downstreams: Iterable[str]) -> None:
        for downstream in downstreams:
            self.connect(upstream, downstream)

    def fan_in(self, upstreams: Iterable[str], downstream: str) -> None:
        for upstream in upstreams:
            self.connect(upstream, downstream)

    def pool(self, name: str, capacity: int, description: str = "") -> ResourcePool:
        """Declare a resource pool."""

        if name in self.pools:
            raise WorkflowDefinitionError(
                f"resource pool {name!r} is declared twice", workflow=self.name
            )
        created = ResourcePool(name=name, capacity=capacity, description=description)
        self.pools[name] = created
        return created

    def param(
        self,
        name: str,
        type: str = "string",
        *,
        required: bool = False,
        default: Any = None,
        choices: Iterable[Any] = (),
        description: str = "",
    ) -> ParamSpec:
        """Declare a run parameter."""

        return self.params.declare(
            name,
            type,
            required=required,
            default=default,
            choices=choices,
            description=description,
        )

    # -- queries -------------------------------------------------------------

    def __contains__(self, name: object) -> bool:
        return name in self.tasks

    def __len__(self) -> int:
        return len(self.tasks)

    def __iter__(self):
        return iter(self.tasks.values())

    @property
    def task_names(self) -> tuple[str, ...]:
        """Task names in declaration order."""

        return tuple(self.tasks)

    def task(self, name: str) -> TaskSpec:
        try:
            return self.tasks[name]
        except KeyError:
            raise UnknownTaskError(name, workflow=self.name, known=self.tasks) from None

    def upstream_map(self) -> dict[str, list[str]]:
        """Map every task to its upstreams, in edge declaration order."""

        mapping: dict[str, list[str]] = {name: [] for name in self.tasks}
        for edge in self.edges:
            if edge.downstream in mapping and edge.upstream in self.tasks:
                mapping[edge.downstream].append(edge.upstream)
        return {name: stable_unique(values) for name, values in mapping.items()}

    def downstream_map(self) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {name: [] for name in self.tasks}
        for edge in self.edges:
            if edge.upstream in mapping and edge.downstream in self.tasks:
                mapping[edge.upstream].append(edge.downstream)
        return {name: stable_unique(values) for name, values in mapping.items()}

    def upstreams_of(self, name: str) -> tuple[str, ...]:
        self.task(name)
        return tuple(self.upstream_map()[name])

    def downstreams_of(self, name: str) -> tuple[str, ...]:
        self.task(name)
        return tuple(self.downstream_map()[name])

    def edges_into(self, name: str) -> tuple[Edge, ...]:
        """Edges whose downstream is ``name``, in declaration order."""

        return tuple(edge for edge in self.edges if edge.downstream == name)

    def edges_out_of(self, name: str) -> tuple[Edge, ...]:
        return tuple(edge for edge in self.edges if edge.upstream == name)

    def edge_between(self, upstream: str, downstream: str) -> Edge | None:
        for edge in self.edges:
            if edge.upstream == upstream and edge.downstream == downstream:
                return edge
        return None

    def roots(self) -> tuple[str, ...]:
        return tuple(roots(self.task_names, self.upstream_map()))

    def leaves(self) -> tuple[str, ...]:
        return tuple(leaves(self.task_names, self.upstream_map()))

    def ancestors_of(self, name: str) -> tuple[str, ...]:
        self.task(name)
        return tuple(ancestors(name, self.task_names, self.upstream_map()))

    def descendants_of(self, name: str) -> tuple[str, ...]:
        self.task(name)
        return tuple(descendants(name, self.task_names, self.upstream_map()))

    def topological_order(self) -> tuple[str, ...]:
        """Task names ordered so every task follows its upstreams."""

        cycle = find_cycle(self.task_names, self.upstream_map())
        if cycle is not None:
            raise CycleError(cycle, workflow=self.name)
        return tuple(topological_sort(self.task_names, self.upstream_map()))

    def levels(self) -> dict[str, int]:
        return depth_levels(self.task_names, self.upstream_map())

    def tasks_with_tag(self, tag: str) -> tuple[TaskSpec, ...]:
        return tuple(spec for spec in self.tasks.values() if spec.has_tag(tag))

    def tasks_using_pool(self, pool: str) -> tuple[TaskSpec, ...]:
        return tuple(spec for spec in self.tasks.values() if spec.requires(pool))

    def declared_pools(self) -> tuple[str, ...]:
        return tuple(self.pools)

    def referenced_pools(self) -> OrderedSet[str]:
        referenced: OrderedSet[str] = OrderedSet()
        for spec in self.tasks.values():
            referenced.update(spec.pools)
        return referenced

    def conditional_edges(self) -> tuple[Edge, ...]:
        return tuple(edge for edge in self.edges if edge.is_conditional)

    # -- integrity -----------------------------------------------------------

    def check_references(self) -> None:
        """Raise if any edge or resource request points at something missing.

        This is the cheap structural half of validation; the full pass lives in
        ``campanile.dsl.validate`` because it also compiles edge conditions.
        """

        for edge in self.edges:
            if edge.upstream not in self.tasks:
                raise UnknownTaskError(edge.upstream, workflow=self.name, known=self.tasks)
            if edge.downstream not in self.tasks:
                raise UnknownTaskError(edge.downstream, workflow=self.name, known=self.tasks)
        for spec in self.tasks.values():
            check_request(spec.resources, self.pools, task=spec.name)
        cycle = find_cycle(self.task_names, self.upstream_map())
        if cycle is not None:
            raise CycleError(cycle, workflow=self.name)

    def copy(self) -> "Workflow":
        """A shallow copy safe to mutate without touching the original."""

        clone = Workflow(
            name=self.name,
            description=self.description,
            default_retry=self.default_retry,
            failure_policy=self.failure_policy,
            max_parallelism=self.max_parallelism,
            tags=self.tags,
        )
        clone.tasks = dict(self.tasks)
        clone.edges = list(self.edges)
        clone.pools = dict(self.pools)
        clone.params = ParamSchema(dict(self.params.specs), self.params.allow_extra)
        return clone

    def subgraph(self, names: Iterable[str]) -> "Workflow":
        """A workflow holding only ``names`` and the edges between them.

        Used by ``campanile run --only`` and by report rendering.  Tasks are
        validated to exist; edges that would dangle are dropped rather than
        rewritten, so the subgraph is a genuine subset.
        """

        keep = OrderedSet[str]()
        for name in names:
            self.task(name)
            keep.add(name)
        clone = self.copy()
        clone.tasks = {name: spec for name, spec in self.tasks.items() if name in keep}
        clone.edges = [
            edge
            for edge in self.edges
            if edge.upstream in keep and edge.downstream in keep
        ]
        return clone

    # -- rendering -----------------------------------------------------------

    def describe(self) -> str:
        lines = [f"workflow {self.name}"]
        if self.description:
            lines.append(f"  {self.description}")
        lines.append(f"  failure policy: {self.failure_policy}")
        if self.max_parallelism is not None:
            lines.append(f"  max parallelism: {self.max_parallelism}")
        if self.pools:
            lines.append("  pools:")
            lines.extend(f"    {pool.describe()}" for pool in self.pools.values())
        if len(self.params):
            lines.append("  params:")
            lines.extend(f"    {line}" for line in self.params.describe())
        lines.append("  tasks:")
        lines.extend(f"    {spec.describe()}" for spec in self.tasks.values())
        if self.edges:
            lines.append("  edges:")
            lines.extend(f"    {edge.describe()}" for edge in self.edges)
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "failure_policy": self.failure_policy.value,
            "max_parallelism": self.max_parallelism,
            "tags": list(self.tags),
            "pools": [pool.as_dict() for pool in self.pools.values()],
            "params": self.params.as_dict(),
            "tasks": [spec.as_dict() for spec in self.tasks.values()],
            "edges": [edge.as_dict() for edge in self.edges],
        }

    def __repr__(self) -> str:
        return (
            f"Workflow({self.name!r}, tasks={len(self.tasks)}, edges={len(self.edges)})"
        )


def _as_names(value: Iterable[str] | str) -> tuple[str, ...]:
    """Accept ``"a"``, ``["a", "b"]`` or a spec-like object with a ``.name``."""

    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        else:
            name = getattr(item, "name", None)
            if not isinstance(name, str):
                raise WorkflowDefinitionError(
                    f"cannot read a task name from {item!r}"
                )
            names.append(name)
    return tuple(names)


def merge(name: str, workflows: Sequence[Workflow], *, prefix: bool = True) -> Workflow:
    """Combine several workflows into one, optionally prefixing task names.

    Used to assemble a suite from independently authored pieces.  Without
    ``prefix`` the inputs must already have disjoint task names.
    """

    merged = Workflow(name=name)
    for source in workflows:
        rename: Callable[[str], str] = (
            (lambda task, source=source: f"{source.name}.{task}") if prefix else (lambda task: task)
        )
        for spec in source.tasks.values():
            merged.add_task(spec.evolve(name=rename(spec.name)))
        for edge in source.edges:
            merged.connect(
                rename(edge.upstream), rename(edge.downstream), condition=edge.condition
            )
        for pool in source.pools.values():
            if pool.name not in merged.pools:
                merged.pools[pool.name] = pool
            elif merged.pools[pool.name].capacity != pool.capacity:
                raise WorkflowDefinitionError(
                    f"pool {pool.name!r} is declared with conflicting capacities "
                    f"({merged.pools[pool.name].capacity} and {pool.capacity})",
                    workflow=name,
                )
    return merged
