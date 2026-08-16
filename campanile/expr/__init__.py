"""A tiny expression language for edge conditions.

The language exists so that a workflow author can gate an edge on what a task
produced::

    flow.add("publish", publish, after=["build"],
             condition="results.build.artifacts > 0 and params.env == 'prod'")

It is pure by construction: no assignment, no loops, no I/O, no clock.  That is
what lets a run be replayed from its event log and reach the same decisions.
"""

from __future__ import annotations

from .ast import Node, function_names, variable_names, walk
from .eval import (
    Environment,
    Expression,
    clear_cache,
    compile_expression,
    evaluate,
    is_truthy,
)
from .functions import BUILTINS, Builtin, builtin_names
from .lexer import Token, TokenKind, tokenize
from .parser import parse

__all__ = [
    "BUILTINS",
    "Builtin",
    "Environment",
    "Expression",
    "Node",
    "Token",
    "TokenKind",
    "builtin_names",
    "clear_cache",
    "compile_expression",
    "evaluate",
    "function_names",
    "is_truthy",
    "parse",
    "tokenize",
    "variable_names",
    "walk",
]
