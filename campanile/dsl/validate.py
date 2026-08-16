"""Static validation of a workflow definition.

The model classes reject anything locally wrong -- a negative timeout, a pool
with zero capacity -- at construction.  This module catches the problems that
are only visible once the whole graph exists: dangling edges, cycles, resource
requests no pool can satisfy, and conditions that read results which cannot
possibly be available yet.

Everything is reported as an :class:`Issue` rather than raised, so one pass
surfaces every problem.  :func:`assert_valid` turns a batch of errors into a
single :class:`ValidationError`.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from ..errors import ExpressionError, Issue, ValidationError
from ..expr.ast import Attribute, Index, Literal, Name, Node, walk
from ..expr.eval import compile_expression
from ..expr.functions import BUILTINS
from ..model.policy import TriggerRule
from ..model.workflow import Edge, Workflow
from ..util.topology import ancestors, find_cycle

__all__ = [
    "validate",
    "assert_valid",
    "is_valid",
    "CONDITION_VARIABLES",
    "result_references",
]

#: The bindings an edge condition may read.  Anything else is a typo.
CONDITION_VARIABLES: frozenset[str] = frozenset({"params", "results", "run", "task"})


def validate(workflow: Workflow) -> list[Issue]:
    """Return every problem found in ``workflow``, errors and warnings alike."""

    issues: list[Issue] = []
    issues.extend(_check_shape(workflow))
    issues.extend(_check_edges(workflow))
    issues.extend(_check_cycles(workflow))
    issues.extend(_check_resources(workflow))
    issues.extend(_check_conditions(workflow))
    issues.extend(_check_trigger_rules(workflow))
    issues.extend(_check_reachability(workflow))
    return issues


def assert_valid(workflow: Workflow, *, strict: bool = False) -> list[Issue]:
    """Validate and raise if anything is wrong.

    With ``strict`` a warning is fatal too, which is what the CLI's
    ``--strict`` flag turns on.
    """

    issues = validate(workflow)
    fatal = [issue for issue in issues if issue.is_error or strict]
    if fatal:
        raise ValidationError(issues, workflow=workflow.name)
    return issues


def is_valid(workflow: Workflow, *, strict: bool = False) -> bool:
    """Predicate form of :func:`assert_valid`."""

    issues = validate(workflow)
    return not any(issue.is_error or strict for issue in issues)


# --------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------


def _check_shape(workflow: Workflow) -> list[Issue]:
    issues: list[Issue] = []
    if not workflow.tasks:
        issues.append(
            Issue(
                "empty_workflow",
                "workflow declares no tasks",
                location=workflow.name,
            )
        )
    for spec in workflow.tasks.values():
        if spec.is_noop and not workflow.downstreams_of(spec.name):
            issues.append(
                Issue(
                    "noop_leaf",
                    f"task {spec.name!r} has no body and nothing depends on it, "
                    "so running it can have no effect",
                    location=_where(workflow, spec.name),
                    severity="warning",
                )
            )
    return issues


def _check_edges(workflow: Workflow) -> list[Issue]:
    issues: list[Issue] = []
    known = set(workflow.task_names)
    for edge in workflow.edges:
        if edge.upstream not in known:
            issues.append(
                Issue(
                    "unknown_upstream",
                    f"edge {edge.describe()} names an upstream that does not exist",
                    location=_edge_location(workflow, edge),
                )
            )
        if edge.downstream not in known:
            issues.append(
                Issue(
                    "unknown_downstream",
                    f"edge {edge.describe()} names a downstream that does not exist",
                    location=_edge_location(workflow, edge),
                )
            )
    return issues


def _check_cycles(workflow: Workflow) -> list[Issue]:
    cycle = find_cycle(workflow.task_names, workflow.upstream_map())
    if cycle is None:
        return []
    return [
        Issue(
            "dependency_cycle",
            "dependency cycle: " + " -> ".join(cycle),
            location=workflow.name,
        )
    ]


def _check_resources(workflow: Workflow) -> list[Issue]:
    issues: list[Issue] = []
    for spec in workflow.tasks.values():
        for pool_name, amount in spec.resources.items():
            pool = workflow.pools.get(pool_name)
            if pool is None:
                declared = ", ".join(workflow.pools) or "none"
                issues.append(
                    Issue(
                        "unknown_pool",
                        f"task {spec.name!r} requests undeclared pool {pool_name!r} "
                        f"(declared: {declared})",
                        location=_where(workflow, spec.name),
                    )
                )
                continue
            if amount > pool.capacity:
                issues.append(
                    Issue(
                        "resource_capacity",
                        f"task {spec.name!r} requests {amount} slot(s) of pool "
                        f"{pool_name!r}, which has capacity {pool.capacity}; "
                        "the task could never start",
                        location=_where(workflow, spec.name),
                    )
                )

    referenced = set(workflow.referenced_pools())
    for pool_name in workflow.pools:
        if pool_name not in referenced:
            issues.append(
                Issue(
                    "unused_pool",
                    f"pool {pool_name!r} is declared but no task uses it",
                    location=workflow.name,
                    severity="warning",
                )
            )
    return issues


def _check_conditions(workflow: Workflow) -> list[Issue]:
    issues: list[Issue] = []
    upstream_map = workflow.upstream_map()
    known_tasks = set(workflow.task_names)
    declared_params = set(workflow.params.names)

    for edge in workflow.edges:
        if edge.condition is None:
            continue
        location = _edge_location(workflow, edge)
        try:
            expression = compile_expression(edge.condition)
        except ExpressionError as exc:
            issues.append(
                Issue("condition_syntax", exc.message, location=location)
            )
            continue

        for variable in expression.variables:
            if variable not in CONDITION_VARIABLES:
                available = ", ".join(sorted(CONDITION_VARIABLES))
                issues.append(
                    Issue(
                        "condition_unknown_variable",
                        f"condition reads unknown variable {variable!r} "
                        f"(available: {available})",
                        location=location,
                    )
                )

        for function in expression.functions:
            if function not in BUILTINS:
                issues.append(
                    Issue(
                        "condition_unknown_function",
                        f"condition calls unknown function {function!r}",
                        location=location,
                    )
                )

        if edge.downstream in known_tasks:
            visible = set(
                ancestors(edge.downstream, workflow.task_names, upstream_map)
            )
            for referenced in result_references(expression.node):
                if referenced not in known_tasks:
                    issues.append(
                        Issue(
                            "condition_unknown_task",
                            f"condition reads results of unknown task {referenced!r}",
                            location=location,
                        )
                    )
                elif referenced not in visible:
                    issues.append(
                        Issue(
                            "condition_forward_reference",
                            f"condition reads results of {referenced!r}, which is not "
                            f"an ancestor of {edge.downstream!r} and so has no result "
                            "when the edge is evaluated",
                            location=location,
                        )
                    )

        for referenced in param_references(expression.node):
            if referenced not in declared_params and not workflow.params.allow_extra:
                declared = ", ".join(declared_params) or "none"
                issues.append(
                    Issue(
                        "condition_unknown_param",
                        f"condition reads undeclared parameter {referenced!r} "
                        f"(declared: {declared})",
                        location=location,
                    )
                )

    return issues


def _check_trigger_rules(workflow: Workflow) -> list[Issue]:
    issues: list[Issue] = []
    upstream_map = workflow.upstream_map()
    for spec in workflow.tasks.values():
        if spec.trigger_rule is TriggerRule.ALL_SUCCESS:
            continue
        if not upstream_map.get(spec.name):
            issues.append(
                Issue(
                    "trigger_rule_without_upstreams",
                    f"task {spec.name!r} sets trigger rule {spec.trigger_rule} but has "
                    "no upstreams, so the rule has no effect",
                    location=_where(workflow, spec.name),
                    severity="warning",
                )
            )
    return issues


def _check_reachability(workflow: Workflow) -> list[Issue]:
    """Warn about tasks that sit outside the graph entirely.

    A task with no edges in either direction still runs -- it is its own root --
    but in a workflow that otherwise forms one pipeline it is nearly always a
    forgotten ``after=``.
    """

    issues: list[Issue] = []
    if len(workflow.tasks) < 2 or not workflow.edges:
        return issues
    upstream_map = workflow.upstream_map()
    downstream_map = workflow.downstream_map()
    for name in workflow.task_names:
        if not upstream_map[name] and not downstream_map[name]:
            issues.append(
                Issue(
                    "isolated_task",
                    f"task {name!r} has no upstreams and no downstreams",
                    location=_where(workflow, name),
                    severity="warning",
                )
            )
    return issues


# --------------------------------------------------------------------------
# expression introspection
# --------------------------------------------------------------------------


def result_references(node: Node) -> tuple[str, ...]:
    """Task names an expression reads results of, in source order.

    Both spellings are recognised::

        results.extract
        results["extract"]

    A dynamic read such as ``results[params.which]`` cannot be resolved
    statically and is skipped rather than guessed at.
    """

    return _rooted_references(node, "results")


def param_references(node: Node) -> tuple[str, ...]:
    """Parameter names an expression reads, in source order."""

    return _rooted_references(node, "params")


def _rooted_references(node: Node, root: str) -> tuple[str, ...]:
    found: list[str] = []
    for child in walk(node):
        name: str | None = None
        if isinstance(child, Attribute) and _is_root(child.target, root):
            name = child.attribute
        elif (
            isinstance(child, Index)
            and _is_root(child.target, root)
            and isinstance(child.key, Literal)
            and isinstance(child.key.value, str)
        ):
            name = child.key.value
        if name is not None and name not in found:
            found.append(name)
    return tuple(found)


def _is_root(node: Node, root: str) -> bool:
    return isinstance(node, Name) and node.identifier == root


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _where(workflow: Workflow, task: str) -> str:
    return f"{workflow.name}.{task}"


def _edge_location(workflow: Workflow, edge: Edge) -> str:
    return f"{workflow.name}.{edge.upstream}->{edge.downstream}"


def render_issues(issues: Sequence[Issue]) -> str:
    """Render a batch of issues, errors first, for the CLI."""

    ordered: Iterable[Issue] = sorted(
        issues, key=lambda issue: (0 if issue.is_error else 1, issue.location, issue.code)
    )
    return "\n".join(issue.render() for issue in ordered)
