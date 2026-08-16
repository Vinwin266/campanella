"""Finding workflows in a Python file.

The CLI is handed a path.  That file is an ordinary module: it may build a
:class:`~campanile.model.Workflow` directly, assemble one with a
:class:`~campanile.dsl.WorkflowBuilder`, or decorate functions with a
:class:`~campanile.dsl.TaskRegistry`.  All three are supported, and a file may hold
several workflows.

Importing arbitrary Python is inherently a trust decision; the loader does not
pretend otherwise.  What it does guarantee is that it imports the file exactly
once, under a private module name, without touching ``sys.path`` permanently.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

from ..dsl.builder import WorkflowBuilder
from ..dsl.decorators import TaskRegistry
from ..errors import ConfigurationError, WorkflowDefinitionError
from ..model.workflow import Workflow

__all__ = ["load_module", "workflows_in", "load_workflows", "select_workflow"]

#: Prefix for the private module names loaded files are imported under.
_MODULE_PREFIX = "campanile_workflow_"


def load_module(path: str | Path) -> ModuleType:
    """Import ``path`` as a module and return it.

    The module is registered in ``sys.modules`` under a private name while it
    executes -- dataclasses and decorators inside it need to be able to find
    themselves -- and removed again afterwards, so loading two files with the
    same basename does not have them shadow each other.
    """

    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise ConfigurationError(f"{file_path} does not exist")
    if file_path.is_dir():
        raise ConfigurationError(f"{file_path} is a directory, not a Python file")
    if file_path.suffix != ".py":
        raise ConfigurationError(f"{file_path} is not a .py file")

    module_name = _MODULE_PREFIX + file_path.stem.replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:  # pragma: no cover - importlib internals
        raise ConfigurationError(f"cannot import {file_path}")

    module = importlib.util.module_from_spec(spec)
    parent = str(file_path.parent.resolve())
    added_to_path = parent not in sys.path
    if added_to_path:
        sys.path.insert(0, parent)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - report the user's error, do not mask it
        raise ConfigurationError(
            f"failed to import {file_path}: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        sys.modules.pop(module_name, None)
        if added_to_path and sys.path and sys.path[0] == parent:
            sys.path.pop(0)

    return module


def workflows_in(namespace: Mapping[str, Any] | ModuleType) -> dict[str, Workflow]:
    """Collect every workflow reachable from a module namespace.

    Three shapes are recognised: a ``Workflow`` bound to a name, a
    ``WorkflowBuilder``, and a non-empty ``TaskRegistry``.  Built workflows are
    collected first, and a builder or registry producing a name that is already
    present is ignored -- a module that keeps both its builder and the workflow
    it built is the normal way to write one, not a mistake.

    Two *distinct* workflows sharing a name is still an error, because then
    ``--workflow`` could not tell them apart.
    """

    values = (
        vars(namespace) if isinstance(namespace, ModuleType) else dict(namespace)
    )
    found: dict[str, Workflow] = {}
    origins: dict[str, str] = {}

    def remember(workflow: Workflow, origin: str, *, defer: bool = False) -> None:
        existing = found.get(workflow.name)
        if existing is not None:
            if defer or existing is workflow:
                return
            raise WorkflowDefinitionError(
                f"two workflows named {workflow.name!r} were found "
                f"({origins[workflow.name]} and {origin})",
                workflow=workflow.name,
            )
        found[workflow.name] = workflow
        origins[workflow.name] = origin

    for attribute, value in values.items():
        if attribute.startswith("_") or not isinstance(value, Workflow):
            continue
        remember(value, attribute)

    for attribute, value in values.items():
        if attribute.startswith("_"):
            continue
        if isinstance(value, WorkflowBuilder):
            remember(value.build(), attribute, defer=True)
        elif isinstance(value, TaskRegistry) and len(value):
            remember(value.build(), attribute, defer=True)

    return found


def load_workflows(path: str | Path) -> dict[str, Workflow]:
    """Import ``path`` and return the workflows it defines."""

    module = load_module(path)
    workflows = workflows_in(module)
    if not workflows:
        raise ConfigurationError(
            f"{path} defines no workflows; expected a Workflow, a WorkflowBuilder "
            "or a non-empty TaskRegistry at module level"
        )
    return workflows


def select_workflow(
    workflows: Mapping[str, Workflow], name: str | None = None
) -> Workflow:
    """Pick one workflow, defaulting to the only one when there is only one."""

    if name is not None:
        try:
            return workflows[name]
        except KeyError:
            known = ", ".join(sorted(workflows)) or "none"
            raise ConfigurationError(
                f"no workflow named {name!r} (found: {known})"
            ) from None

    if len(workflows) == 1:
        return next(iter(workflows.values()))

    known = ", ".join(sorted(workflows))
    raise ConfigurationError(
        f"several workflows were found ({known}); name one with --workflow"
    )
