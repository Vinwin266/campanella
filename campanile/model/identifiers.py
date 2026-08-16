"""Name validation.

Task, workflow, pool and tag names end up in file paths, event log payloads, CLI
output and expression source, so they are restricted to a conservative shape:
start with a letter, then letters, digits, underscores, hyphens and dots.

Validation happens once, at declaration time.  Everything downstream may assume
a name it was handed is well formed.
"""

from __future__ import annotations

import re
from typing import Iterable

from ..errors import WorkflowDefinitionError

__all__ = [
    "MAX_NAME_LENGTH",
    "RESERVED_NAMES",
    "validate_task_name",
    "validate_workflow_name",
    "validate_pool_name",
    "validate_tag",
    "validate_names",
    "qualify",
    "split_qualified",
    "is_valid_name",
]

MAX_NAME_LENGTH = 96

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")

#: Names the expression evaluator binds itself.  A task may not take one of
#: these, otherwise ``results.params`` would be ambiguous.
RESERVED_NAMES = frozenset({"params", "results", "run", "task", "now", "self"})


def is_valid_name(value: str) -> bool:
    """Cheap predicate form of the validators, for callers that want no raise."""

    return (
        isinstance(value, str)
        and bool(_NAME_RE.match(value))
        and len(value) <= MAX_NAME_LENGTH
    )


def _validate(kind: str, value: object, pattern: re.Pattern[str], *, workflow: str | None) -> str:
    if not isinstance(value, str):
        raise WorkflowDefinitionError(
            f"{kind} name must be a string, got {type(value).__name__}",
            workflow=workflow,
        )
    if not value:
        raise WorkflowDefinitionError(f"{kind} name must not be empty", workflow=workflow)
    if len(value) > MAX_NAME_LENGTH:
        raise WorkflowDefinitionError(
            f"{kind} name {value!r} is longer than {MAX_NAME_LENGTH} characters",
            workflow=workflow,
        )
    if not pattern.match(value):
        raise WorkflowDefinitionError(
            f"{kind} name {value!r} must start with a letter and contain only "
            "letters, digits, '_', '-' and '.'",
            workflow=workflow,
        )
    return value


def validate_task_name(value: object, *, workflow: str | None = None) -> str:
    """Validate a task name and return it unchanged."""

    name = _validate("task", value, _NAME_RE, workflow=workflow)
    if name in RESERVED_NAMES:
        raise WorkflowDefinitionError(
            f"task name {name!r} is reserved by the expression language",
            workflow=workflow,
        )
    return name


def validate_workflow_name(value: object) -> str:
    """Validate a workflow name and return it unchanged."""

    return _validate("workflow", value, _NAME_RE, workflow=None)


def validate_pool_name(value: object, *, workflow: str | None = None) -> str:
    """Validate a resource pool name and return it unchanged."""

    return _validate("pool", value, _NAME_RE, workflow=workflow)


def validate_tag(value: object, *, workflow: str | None = None) -> str:
    """Validate a tag.  Tags are looser than names -- they may start with a digit
    and may carry a ``:`` so that ``team:payments`` reads naturally."""

    return _validate("tag", value, _TAG_RE, workflow=workflow)


def validate_names(values: Iterable[object], *, workflow: str | None = None) -> tuple[str, ...]:
    """Validate a sequence of task names, rejecting duplicates."""

    seen: list[str] = []
    for value in values:
        name = validate_task_name(value, workflow=workflow)
        if name in seen:
            raise WorkflowDefinitionError(
                f"task {name!r} appears twice in the same list", workflow=workflow
            )
        seen.append(name)
    return tuple(seen)


def qualify(workflow: str, task: str) -> str:
    """Join a workflow and task name into ``workflow::task``."""

    return f"{workflow}::{task}"


def split_qualified(qualified: str) -> tuple[str, str]:
    """Inverse of :func:`qualify`.

    >>> split_qualified("nightly::extract")
    ('nightly', 'extract')
    """

    parts = qualified.split("::")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"{qualified!r} is not a qualified task name")
    return parts[0], parts[1]
