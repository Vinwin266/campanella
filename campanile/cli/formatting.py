"""Output plumbing for the command line.

Two things live here: exit codes, and a tiny writer that every command uses
instead of ``print``.  Commands take a :class:`Console` so a test can capture
their output without redirecting the process's streams.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import IO, Any, Sequence

from ..errors import CampanileError, Issue

__all__ = ["Console", "ExitCode", "exit_code_for", "render_error", "parse_assignment"]


class ExitCode:
    """The statuses ``campanile`` exits with."""

    OK = 0
    RUN_FAILED = 1
    USAGE = 2
    DEFINITION = 3
    STORAGE = 4
    INTERRUPTED = 130


#: Error family -> exit code.  Anything unrecognised is a definition problem,
#: because that is the overwhelmingly common case for a CLI invocation.
_FAMILY_CODES = {
    "definition": ExitCode.DEFINITION,
    "execution": ExitCode.RUN_FAILED,
    "storage": ExitCode.STORAGE,
}


def exit_code_for(error: BaseException) -> int:
    """Map an exception onto an exit status."""

    if isinstance(error, KeyboardInterrupt):
        return ExitCode.INTERRUPTED
    if isinstance(error, CampanileError):
        return _FAMILY_CODES.get(error.family, ExitCode.DEFINITION)
    return ExitCode.DEFINITION


@dataclass
class Console:
    """Where a command writes.

    ``quiet`` suppresses ordinary output but never errors, so a script can rely
    on the exit status while still seeing why something failed.
    """

    out: IO[str] = field(default_factory=lambda: sys.stdout)
    err: IO[str] = field(default_factory=lambda: sys.stderr)
    quiet: bool = False

    def write(self, text: str = "") -> None:
        """Write a line of ordinary output."""

        if self.quiet:
            return
        self.out.write(text if text.endswith("\n") else text + "\n")

    def block(self, text: str) -> None:
        """Write a pre-rendered multi-line block, trimming trailing blanks."""

        if self.quiet:
            return
        self.out.write(text.rstrip("\n") + "\n")

    def raw(self, text: str) -> None:
        """Write exactly ``text``, adding nothing.  For machine-readable output."""

        self.out.write(text)

    def error(self, text: str) -> None:
        """Write to the error stream, regardless of ``quiet``."""

        self.err.write(text if text.endswith("\n") else text + "\n")

    def issues(self, issues: Sequence[Issue]) -> None:
        """Render validation issues, errors first."""

        ordered = sorted(
            issues, key=lambda issue: (0 if issue.is_error else 1, issue.location, issue.code)
        )
        for issue in ordered:
            stream = self.error if issue.is_error else self.write
            stream(issue.render())


def render_error(error: BaseException) -> str:
    """One line describing ``error``, with its code when it has one."""

    if isinstance(error, CampanileError):
        annotated = getattr(error, "annotate", None)
        message = annotated() if callable(annotated) else error.message
        return f"error [{error.code}]: {message}"
    return f"error: {type(error).__name__}: {error}"


def parse_assignment(text: str) -> tuple[str, Any]:
    """Parse a ``key=value`` command-line assignment.

    Values are read as JSON when they parse as JSON, and as plain strings
    otherwise, so ``--param retries=3`` gives an integer while
    ``--param day=2026-03-11`` gives a string.
    """

    import json

    if "=" not in text:
        raise ValueError(f"expected key=value, got {text!r}")
    key, _, raw = text.partition("=")
    key = key.strip()
    if not key:
        raise ValueError(f"assignment {text!r} has an empty key")
    try:
        return key, json.loads(raw)
    except json.JSONDecodeError:
        return key, raw
