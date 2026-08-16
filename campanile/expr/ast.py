"""Expression syntax tree.

Nodes are frozen dataclasses with two behaviours: ``render`` puts them back into
source form (used when echoing a workflow definition) and ``walk`` yields the
whole subtree (used by the validator to collect variable and function
references without evaluating anything).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

__all__ = [
    "Node",
    "Literal",
    "Name",
    "Attribute",
    "Index",
    "Call",
    "Unary",
    "Binary",
    "Logical",
    "ListLiteral",
    "Coalesce",
    "walk",
    "variable_names",
    "function_names",
]


@dataclass(frozen=True)
class Node:
    """Base class.  ``position`` is the source offset the node starts at."""

    position: int = field(default=0, kw_only=True)

    def children(self) -> tuple["Node", ...]:
        return ()

    def render(self) -> str:  # pragma: no cover - overridden everywhere
        raise NotImplementedError

    def __str__(self) -> str:
        return self.render()


@dataclass(frozen=True)
class Literal(Node):
    """A number, string, boolean or null."""

    value: Any = None

    def render(self) -> str:
        if self.value is None:
            return "null"
        if self.value is True:
            return "true"
        if self.value is False:
            return "false"
        if isinstance(self.value, str):
            escaped = self.value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return repr(self.value)


@dataclass(frozen=True)
class Name(Node):
    """A bare identifier, resolved against the environment."""

    identifier: str = ""

    def render(self) -> str:
        return self.identifier


@dataclass(frozen=True)
class Attribute(Node):
    """``target.attribute``."""

    target: Node = field(default_factory=lambda: Literal(None))
    attribute: str = ""

    def children(self) -> tuple[Node, ...]:
        return (self.target,)

    def render(self) -> str:
        return f"{self.target.render()}.{self.attribute}"


@dataclass(frozen=True)
class Index(Node):
    """``target[key]``."""

    target: Node = field(default_factory=lambda: Literal(None))
    key: Node = field(default_factory=lambda: Literal(None))

    def children(self) -> tuple[Node, ...]:
        return (self.target, self.key)

    def render(self) -> str:
        return f"{self.target.render()}[{self.key.render()}]"


@dataclass(frozen=True)
class Call(Node):
    """``name(arg, ...)``.  Only bare names may be called."""

    function: str = ""
    arguments: tuple[Node, ...] = ()

    def children(self) -> tuple[Node, ...]:
        return tuple(self.arguments)

    def render(self) -> str:
        inner = ", ".join(argument.render() for argument in self.arguments)
        return f"{self.function}({inner})"


@dataclass(frozen=True)
class Unary(Node):
    """``-operand`` or ``not operand``."""

    operator: str = "-"
    operand: Node = field(default_factory=lambda: Literal(None))

    def children(self) -> tuple[Node, ...]:
        return (self.operand,)

    def render(self) -> str:
        if self.operator == "not":
            return f"not {self.operand.render()}"
        return f"{self.operator}{self.operand.render()}"


@dataclass(frozen=True)
class Binary(Node):
    """Arithmetic and comparison; both operands are always evaluated."""

    operator: str = "+"
    left: Node = field(default_factory=lambda: Literal(None))
    right: Node = field(default_factory=lambda: Literal(None))

    def children(self) -> tuple[Node, ...]:
        return (self.left, self.right)

    def render(self) -> str:
        return f"({self.left.render()} {self.operator} {self.right.render()})"


@dataclass(frozen=True)
class Logical(Node):
    """``and`` / ``or``, which short-circuit."""

    operator: str = "and"
    left: Node = field(default_factory=lambda: Literal(None))
    right: Node = field(default_factory=lambda: Literal(None))

    def children(self) -> tuple[Node, ...]:
        return (self.left, self.right)

    def render(self) -> str:
        return f"({self.left.render()} {self.operator} {self.right.render()})"


@dataclass(frozen=True)
class Coalesce(Node):
    """``left ?? right`` -- ``right`` only when ``left`` is null."""

    left: Node = field(default_factory=lambda: Literal(None))
    right: Node = field(default_factory=lambda: Literal(None))

    def children(self) -> tuple[Node, ...]:
        return (self.left, self.right)

    def render(self) -> str:
        return f"({self.left.render()} ?? {self.right.render()})"


@dataclass(frozen=True)
class ListLiteral(Node):
    """``[a, b, c]``."""

    items: tuple[Node, ...] = ()

    def children(self) -> tuple[Node, ...]:
        return tuple(self.items)

    def render(self) -> str:
        return "[" + ", ".join(item.render() for item in self.items) + "]"


def walk(node: Node) -> Iterator[Node]:
    """Yield ``node`` and every node beneath it, parents before children."""

    yield node
    for child in node.children():
        yield from walk(child)


def variable_names(node: Node) -> tuple[str, ...]:
    """Every bare identifier the expression reads, in source order.

    Attribute chains report only their root, so ``results.extract.rows`` yields
    ``results``.  That is what the validator needs: whether the *binding* exists.
    """

    seen: list[str] = []
    for child in walk(node):
        if isinstance(child, Name) and child.identifier not in seen:
            seen.append(child.identifier)
    return tuple(seen)


def function_names(node: Node) -> tuple[str, ...]:
    """Every function the expression calls, in source order."""

    seen: list[str] = []
    for child in walk(node):
        if isinstance(child, Call) and child.function not in seen:
            seen.append(child.function)
    return tuple(seen)


def depth(node: Node) -> int:
    """Height of the tree, used to reject pathologically nested expressions."""

    children: Sequence[Node] = node.children()
    if not children:
        return 1
    return 1 + max(depth(child) for child in children)
