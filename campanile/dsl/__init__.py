"""Two ways to declare a workflow, and one way to check it.

:mod:`campanile.dsl.builder` is the fluent form, :mod:`campanile.dsl.decorators` the
decorator form.  Both produce a plain :class:`campanile.model.Workflow`; neither is
privileged, and a workflow assembled by hand is exactly as valid.

:mod:`campanile.dsl.validate` is where the whole-graph checks live -- the ones that
need every task and edge in hand before they can say anything.
"""

from __future__ import annotations

from .builder import TaskGroup, TaskHandle, WorkflowBuilder
from .decorators import (
    TaskDeclaration,
    TaskRegistry,
    build_workflow,
    collect_declarations,
    declaration_of,
    default_registry,
    task,
)
from .validate import (
    CONDITION_VARIABLES,
    assert_valid,
    is_valid,
    param_references,
    render_issues,
    result_references,
    validate,
)

__all__ = [
    "CONDITION_VARIABLES",
    "TaskDeclaration",
    "TaskGroup",
    "TaskHandle",
    "TaskRegistry",
    "WorkflowBuilder",
    "assert_valid",
    "build_workflow",
    "collect_declarations",
    "declaration_of",
    "default_registry",
    "is_valid",
    "param_references",
    "render_issues",
    "result_references",
    "task",
    "validate",
]
