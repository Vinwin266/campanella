"""Evaluation of parsed expressions.

The evaluator is total in the sense that every failure is an
:class:`ExpressionEvaluationError` carrying the source and the offset -- it never
lets a ``TypeError`` or ``KeyError`` escape into the scheduler, because a broken
condition must fail the *edge* with a readable message rather than crash the run.

Semantics worth stating outright, because tests depend on them:

* ``null`` is falsy; so are ``0``, ``""``, ``[]`` and ``{}``.
* ``and`` / ``or`` short-circuit and return the *value* that decided them, so
  ``params.region or "eu"`` yields ``"eu"`` when the parameter is empty.
* Ordering comparisons require two numbers or two strings.  Comparing a number
  to a string is an error, not a silent false.
* Reading a key that does not exist is an error.  Use ``get(x, "k", fallback)``
  or ``??`` for a defensive read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..errors import (
    ExpressionEvaluationError,
    UnknownFunctionError,
    UnknownVariableError,
)
from .ast import (
    Attribute,
    Binary,
    Call,
    Coalesce,
    Index,
    ListLiteral,
    Literal,
    Logical,
    Name,
    Node,
    Unary,
    function_names,
    variable_names,
)
from .functions import BUILTINS, Builtin
from .parser import parse

__all__ = [
    "Environment",
    "Expression",
    "compile_expression",
    "evaluate",
    "is_truthy",
    "clear_cache",
]


def is_truthy(value: Any) -> bool:
    """The language's notion of truth.

    Identical to Python's except that it is spelled out here so that changing
    Python cannot silently change what an edge condition means.
    """

    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (str, bytes, list, tuple, dict, set, frozenset)):
        return len(value) > 0
    return True


@dataclass
class Environment:
    """The bindings an expression is evaluated against."""

    variables: dict[str, Any] = field(default_factory=dict)
    functions: Mapping[str, Builtin] = field(default_factory=lambda: BUILTINS)

    @classmethod
    def of(cls, **variables: Any) -> "Environment":
        return cls(variables=dict(variables))

    def bind(self, name: str, value: Any) -> "Environment":
        """Return a child environment with one more binding."""

        merged = dict(self.variables)
        merged[name] = value
        return Environment(variables=merged, functions=self.functions)

    def extend(self, values: Mapping[str, Any]) -> "Environment":
        merged = dict(self.variables)
        merged.update(values)
        return Environment(variables=merged, functions=self.functions)

    def resolve(self, name: str, *, source: str | None = None, position: int = 0) -> Any:
        if name in self.variables:
            return self.variables[name]
        raise UnknownVariableError(
            name, known=self.variables, source=source, position=position
        )

    def function(self, name: str, *, source: str | None = None, position: int = 0) -> Builtin:
        found = self.functions.get(name)
        if found is None:
            raise UnknownFunctionError(
                name, known=self.functions, source=source, position=position
            )
        return found

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.variables))


class Expression:
    """A parsed, reusable expression."""

    __slots__ = ("source", "node", "_variables", "_functions")

    def __init__(self, source: str) -> None:
        self.source = source
        self.node = parse(source)
        self._variables = variable_names(self.node)
        self._functions = function_names(self.node)

    @property
    def variables(self) -> tuple[str, ...]:
        """Root identifiers the expression reads."""

        return self._variables

    @property
    def functions(self) -> tuple[str, ...]:
        """Functions the expression calls."""

        return self._functions

    def render(self) -> str:
        """Source form reconstructed from the tree, fully parenthesised."""

        return self.node.render()

    def evaluate(self, environment: Environment | Mapping[str, Any] | None = None) -> Any:
        env = _as_environment(environment)
        return _eval(self.node, env, self.source)

    def test(self, environment: Environment | Mapping[str, Any] | None = None) -> bool:
        """Evaluate and reduce to a boolean, the form edges use."""

        return is_truthy(self.evaluate(environment))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Expression):
            return NotImplemented
        return self.node == other.node

    def __hash__(self) -> int:
        return hash(self.render())

    def __repr__(self) -> str:
        return f"Expression({self.source!r})"


_CACHE: dict[str, Expression] = {}


def compile_expression(source: str) -> Expression:
    """Parse ``source``, reusing an earlier parse of the same text.

    Conditions are compiled once per definition and evaluated once per edge per
    run, so the cache mostly saves repeated validation work.  It is keyed by
    exact source text and never evicted; expressions are short and few.
    """

    cached = _CACHE.get(source)
    if cached is None:
        cached = Expression(source)
        _CACHE[source] = cached
    return cached


def clear_cache() -> None:
    """Drop every compiled expression.  Only tests need this."""

    _CACHE.clear()


def evaluate(
    source: str, environment: Environment | Mapping[str, Any] | None = None
) -> Any:
    """Compile and evaluate in one call."""

    return compile_expression(source).evaluate(environment)


def _as_environment(
    environment: Environment | Mapping[str, Any] | None,
) -> Environment:
    if environment is None:
        return Environment()
    if isinstance(environment, Environment):
        return environment
    return Environment(variables=dict(environment))


# --------------------------------------------------------------------------
# the evaluator itself
# --------------------------------------------------------------------------


def _eval(node: Node, env: Environment, source: str) -> Any:
    if isinstance(node, Literal):
        return node.value

    if isinstance(node, Name):
        return env.resolve(node.identifier, source=source, position=node.position)

    if isinstance(node, ListLiteral):
        return [_eval(item, env, source) for item in node.items]

    if isinstance(node, Attribute):
        target = _eval(node.target, env, source)
        return _read_member(target, node.attribute, node, source)

    if isinstance(node, Index):
        target = _eval(node.target, env, source)
        key = _eval(node.key, env, source)
        return _read_index(target, key, node, source)

    if isinstance(node, Call):
        builtin = env.function(node.function, source=source, position=node.position)
        arguments = [_eval(argument, env, source) for argument in node.arguments]
        try:
            builtin.check_arity(len(arguments))
            return builtin.fn(*arguments)
        except ExpressionEvaluationError as exc:
            raise ExpressionEvaluationError(
                exc.message, source=source, position=node.position
            ) from exc

    if isinstance(node, Unary):
        return _eval_unary(node, env, source)

    if isinstance(node, Logical):
        left = _eval(node.left, env, source)
        if node.operator == "and":
            return _eval(node.right, env, source) if is_truthy(left) else left
        return left if is_truthy(left) else _eval(node.right, env, source)

    if isinstance(node, Coalesce):
        left = _eval(node.left, env, source)
        return _eval(node.right, env, source) if left is None else left

    if isinstance(node, Binary):
        return _eval_binary(node, env, source)

    raise ExpressionEvaluationError(  # pragma: no cover - defensive
        f"cannot evaluate node {type(node).__name__}", source=source, position=node.position
    )


def _eval_unary(node: Unary, env: Environment, source: str) -> Any:
    operand = _eval(node.operand, env, source)
    if node.operator == "not":
        return not is_truthy(operand)
    if isinstance(operand, bool) or not isinstance(operand, (int, float)):
        raise ExpressionEvaluationError(
            f"unary '-' needs a number, got {_type_name(operand)}",
            source=source,
            position=node.position,
        )
    return -operand


def _eval_binary(node: Binary, env: Environment, source: str) -> Any:
    left = _eval(node.left, env, source)
    right = _eval(node.right, env, source)
    operator = node.operator

    if operator == "==":
        return _equal(left, right)
    if operator == "!=":
        return not _equal(left, right)
    if operator == "in":
        return _member_of(left, right, node, source)
    if operator == "not in":
        return not _member_of(left, right, node, source)
    if operator in ("<", "<=", ">", ">="):
        return _compare(operator, left, right, node, source)
    return _arithmetic(operator, left, right, node, source)


def _equal(left: Any, right: Any) -> bool:
    """Value equality, with booleans kept distinct from numbers.

    ``true == 1`` is false here even though Python would say otherwise; a
    workflow author comparing a flag to a count has made a mistake, and silently
    agreeing with them would hide it.
    """

    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right


def _compare(operator: str, left: Any, right: Any, node: Node, source: str) -> bool:
    both_numeric = _is_number(left) and _is_number(right)
    both_text = isinstance(left, str) and isinstance(right, str)
    if not (both_numeric or both_text):
        raise ExpressionEvaluationError(
            f"cannot compare {_type_name(left)} with {_type_name(right)} using {operator!r}",
            source=source,
            position=node.position,
        )
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    if operator == ">":
        return left > right
    return left >= right


def _arithmetic(operator: str, left: Any, right: Any, node: Node, source: str) -> Any:
    if operator == "+":
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        if isinstance(left, list) and isinstance(right, list):
            return left + right

    if not (_is_number(left) and _is_number(right)):
        raise ExpressionEvaluationError(
            f"operator {operator!r} does not apply to "
            f"{_type_name(left)} and {_type_name(right)}",
            source=source,
            position=node.position,
        )

    if operator == "+":
        return left + right
    if operator == "-":
        return left - right
    if operator == "*":
        return left * right

    if right == 0:
        raise ExpressionEvaluationError(
            "division by zero", source=source, position=node.position
        )
    if operator == "/":
        return left / right
    if operator == "//":
        return left // right
    if operator == "%":
        return left % right

    raise ExpressionEvaluationError(  # pragma: no cover - parser rejects others
        f"unknown operator {operator!r}", source=source, position=node.position
    )


def _member_of(needle: Any, haystack: Any, node: Node, source: str) -> bool:
    if isinstance(haystack, str):
        if not isinstance(needle, str):
            raise ExpressionEvaluationError(
                f"'in' on a string needs a string, got {_type_name(needle)}",
                source=source,
                position=node.position,
            )
        return needle in haystack
    if isinstance(haystack, Mapping):
        return needle in haystack
    if isinstance(haystack, (list, tuple, set, frozenset)):
        return any(_equal(needle, item) for item in haystack)
    raise ExpressionEvaluationError(
        f"'in' does not apply to {_type_name(haystack)}",
        source=source,
        position=node.position,
    )


def _read_member(target: Any, attribute: str, node: Node, source: str) -> Any:
    if isinstance(target, Mapping):
        if attribute in target:
            return target[attribute]
        available = ", ".join(sorted(str(key) for key in target)) or "none"
        raise ExpressionEvaluationError(
            f"no key {attribute!r} (available: {available})",
            source=source,
            position=node.position,
        )
    if target is None:
        raise ExpressionEvaluationError(
            f"cannot read {attribute!r} of null", source=source, position=node.position
        )
    if not attribute.startswith("_") and hasattr(target, attribute):
        value = getattr(target, attribute)
        if callable(value):
            raise ExpressionEvaluationError(
                f"{attribute!r} is a method, not a value",
                source=source,
                position=node.position,
            )
        return value
    raise ExpressionEvaluationError(
        f"cannot read {attribute!r} of {_type_name(target)}",
        source=source,
        position=node.position,
    )


def _read_index(target: Any, key: Any, node: Node, source: str) -> Any:
    if isinstance(target, Mapping):
        if key in target:
            return target[key]
        available = ", ".join(sorted(str(existing) for existing in target)) or "none"
        raise ExpressionEvaluationError(
            f"no key {key!r} (available: {available})",
            source=source,
            position=node.position,
        )
    if isinstance(target, (list, tuple, str)):
        if isinstance(key, bool) or not isinstance(key, int):
            raise ExpressionEvaluationError(
                f"index must be an integer, got {_type_name(key)}",
                source=source,
                position=node.position,
            )
        if not -len(target) <= key < len(target):
            raise ExpressionEvaluationError(
                f"index {key} is out of range for a sequence of length {len(target)}",
                source=source,
                position=node.position,
            )
        return target[key]
    raise ExpressionEvaluationError(
        f"cannot index {_type_name(target)}", source=source, position=node.position
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "list"
    return type(value).__name__
