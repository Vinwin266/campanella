"""The ``campanile`` command line.

The CLI is a thin shell over the library: it loads a workflow file, builds an
engine, and renders what comes back.  Every command is a function returning an
exit status, so the whole surface can be exercised in-process.
"""

from __future__ import annotations

from .formatting import Console, ExitCode, exit_code_for, parse_assignment, render_error
from .loader import load_module, load_workflows, select_workflow, workflows_in
from .main import COMMANDS, build_parser, main, run_command

__all__ = [
    "COMMANDS",
    "Console",
    "ExitCode",
    "build_parser",
    "exit_code_for",
    "load_module",
    "load_workflows",
    "main",
    "parse_assignment",
    "render_error",
    "run_command",
    "select_workflow",
    "workflows_in",
]
