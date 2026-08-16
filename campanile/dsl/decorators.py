"""Declaring tasks with a decorator.

For workflows whose tasks are ordinary module-level functions, the decorator
form keeps the declaration next to the code::

    registry = TaskRegistry("nightly")

    @registry.task(retry=3, timeout="5m")
    def extract(ctx):
        return {"rows": 100}

    @registry.task(after=["extract"])
    def load(ctx):
        return ctx.result("extract")["rows"]

    flow = registry.build()

The decorator returns the *original* function untouched, so the module stays
importable and testable without the engine in the picture.  Declaration order is
the order the decorators run, which is source order within a module.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..errors import ConfigurationError, WorkflowDefinitionError
from ..model.policy import FailurePolicy, RetryPolicy, TriggerRule
from ..model.task import TaskCallable
from ..model.workflow import Workflow

__all__ = ["TaskRegistry", "TaskDeclaration", "task", "default_registry", "build_workflow"]

#: Attribute the decorator leaves on the function it wrapped.
DECLARATION_ATTRIBUTE = "__campanile_task__"


class TaskDeclaration:
    """What the decorator recorded about one function."""

    __slots__ = (
        "name",
        "fn",
        "after",
        "options",
        "order",
    )

    def __init__(
        self,
        name: str,
        fn: TaskCallable,
        after: tuple[str, ...],
        options: Mapping[str, Any],
        order: int,
    ) -> None:
        self.name = name
        self.fn = fn
        self.after = after
        self.options = dict(options)
        self.order = order

    def __repr__(self) -> str:
        return f"TaskDeclaration({self.name!r}, after={list(self.after)!r})"


class TaskRegistry:
    """Collects decorated functions and assembles them into a workflow."""

    def __init__(
        self,
        name: str = "workflow",
        *,
        description: str = "",
        default_retry: RetryPolicy | int | None = None,
        failure_policy: FailurePolicy | str = FailurePolicy.CONTINUE,
        max_parallelism: int | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.default_retry = (
            RetryPolicy(max_attempts=default_retry)
            if isinstance(default_retry, int) and not isinstance(default_retry, bool)
            else default_retry
        )
        self.failure_policy = (
            failure_policy
            if isinstance(failure_policy, FailurePolicy)
            else FailurePolicy(str(failure_policy))
        )
        self.max_parallelism = max_parallelism
        self.declarations: dict[str, TaskDeclaration] = {}
        self.pools: list[tuple[str, int, str]] = []
        self.parameters: list[dict[str, Any]] = []
        self._counter = 0

    # -- declaration ---------------------------------------------------------

    def task(
        self,
        fn: TaskCallable | None = None,
        *,
        name: str | None = None,
        after: Iterable[str] | str = (),
        retry: RetryPolicy | int | Mapping[str, Any] | None = None,
        timeout: float | str | None = None,
        resources: Any = None,
        priority: int = 0,
        trigger_rule: TriggerRule | str = TriggerRule.ALL_SUCCESS,
        tags: Iterable[str] = (),
        params: Mapping[str, Any] | None = None,
        description: str | None = None,
        condition: str | None = None,
    ) -> Any:
        """Decorator.  Usable bare (``@registry.task``) or called with options."""

        options: dict[str, Any] = {
            "retry": retry,
            "timeout": timeout,
            "resources": resources,
            "priority": priority,
            "trigger_rule": trigger_rule,
            "tags": tuple(tags),
            "params": dict(params or {}),
            "condition": condition,
        }

        def register(target: TaskCallable) -> TaskCallable:
            if not callable(target):
                raise ConfigurationError("@task must decorate a callable")
            task_name = name or getattr(target, "__name__", None)
            if not task_name:
                raise ConfigurationError("cannot infer a task name; pass name=")
            if task_name in self.declarations:
                raise WorkflowDefinitionError(
                    f"task {task_name!r} is declared twice", workflow=self.name
                )
            doc = description
            if doc is None:
                doc = _first_line(getattr(target, "__doc__", "") or "")
            declaration = TaskDeclaration(
                name=task_name,
                fn=target,
                after=_as_tuple(after),
                options={**options, "description": doc},
                order=self._counter,
            )
            self._counter += 1
            self.declarations[task_name] = declaration
            setattr(target, DECLARATION_ATTRIBUTE, declaration)
            return target

        if fn is not None:
            return register(fn)
        return register

    def gate(self, name: str, *, after: Iterable[str] | str = (), **options: Any) -> None:
        """Declare a body-less join task, which has no function to decorate."""

        if name in self.declarations:
            raise WorkflowDefinitionError(
                f"task {name!r} is declared twice", workflow=self.name
            )
        merged: dict[str, Any] = {
            "retry": None,
            "timeout": None,
            "resources": None,
            "priority": 0,
            "trigger_rule": TriggerRule.ALL_SUCCESS,
            "tags": (),
            "params": {},
            "condition": None,
            "description": "",
        }
        unknown = sorted(set(options) - set(merged))
        if unknown:
            raise ConfigurationError(f"unknown task option(s): {', '.join(unknown)}")
        merged.update(options)
        self.declarations[name] = TaskDeclaration(
            name=name, fn=None, after=_as_tuple(after), options=merged, order=self._counter
        )
        self._counter += 1

    def pool(self, name: str, capacity: int, description: str = "") -> "TaskRegistry":
        self.pools.append((name, capacity, description))
        return self

    def param(self, name: str, type: str = "string", **options: Any) -> "TaskRegistry":
        self.parameters.append({"name": name, "type": type, **options})
        return self

    # -- assembly ------------------------------------------------------------

    def build(self, name: str | None = None) -> Workflow:
        """Assemble every declaration into a workflow, in declaration order."""

        flow = Workflow(
            name=name or self.name,
            description=self.description,
            default_retry=self.default_retry,
            failure_policy=self.failure_policy,
            max_parallelism=self.max_parallelism,
        )
        for pool_name, capacity, pool_description in self.pools:
            flow.pool(pool_name, capacity, pool_description)
        for parameter in self.parameters:
            flow.param(**parameter)

        ordered = sorted(self.declarations.values(), key=lambda item: item.order)
        for declaration in ordered:
            options = dict(declaration.options)
            condition = options.pop("condition", None)
            flow.add(
                declaration.name,
                declaration.fn,
                after=declaration.after,
                condition=condition,
                **options,
            )
        flow.check_references()
        return flow

    def __contains__(self, name: object) -> bool:
        return name in self.declarations

    def __len__(self) -> int:
        return len(self.declarations)

    def names(self) -> tuple[str, ...]:
        return tuple(
            declaration.name
            for declaration in sorted(self.declarations.values(), key=lambda item: item.order)
        )

    def clear(self) -> None:
        self.declarations.clear()
        self.pools.clear()
        self.parameters.clear()
        self._counter = 0

    def __repr__(self) -> str:
        return f"TaskRegistry({self.name!r}, tasks={len(self.declarations)})"


#: A registry for callers who only ever build one workflow per process.
default_registry = TaskRegistry("default")


def task(fn: TaskCallable | None = None, **options: Any) -> Any:
    """Decorator bound to :data:`default_registry`."""

    return default_registry.task(fn, **options)


def build_workflow(name: str | None = None) -> Workflow:
    """Build from :data:`default_registry`."""

    return default_registry.build(name)


def declaration_of(fn: TaskCallable) -> TaskDeclaration | None:
    """The declaration a decorator left on ``fn``, if any."""

    found = getattr(fn, DECLARATION_ATTRIBUTE, None)
    return found if isinstance(found, TaskDeclaration) else None


def collect_declarations(namespace: Mapping[str, Any]) -> tuple[TaskDeclaration, ...]:
    """Find every decorated function in a module namespace, in declaration order.

    Used by the CLI loader, which imports a file and needs the tasks in it
    without the module having to expose a registry by a fixed name.
    """

    found: list[TaskDeclaration] = []
    for value in namespace.values():
        declaration = declaration_of(value) if callable(value) else None
        if declaration is not None and declaration not in found:
            found.append(declaration)
    return tuple(sorted(found, key=lambda item: item.order))


def _as_tuple(value: Iterable[str] | str) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
            continue
        declaration = declaration_of(item) if callable(item) else None
        if declaration is None:
            raise WorkflowDefinitionError(f"cannot read a task name from {item!r}")
        names.append(declaration.name)
    return tuple(names)


def _first_line(text: str) -> str:
    for line in text.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
