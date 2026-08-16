"""Built-in functions available to expressions.

The set is small and pure.  Nothing here reads the clock, touches the file
system, or mutates its arguments -- an edge condition must be a function of the
run state and nothing else, or replaying an event log would not reproduce the
run.

Arity is declared rather than inferred so that a wrong-arity call is reported as
a definition error with a helpful message instead of a ``TypeError`` from deep
inside a lambda.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..errors import ExpressionEvaluationError
from ..util.duration import parse_duration

__all__ = ["Builtin", "BUILTINS", "builtin_names", "lookup"]


@dataclass(frozen=True)
class Builtin:
    """One callable together with the arity the evaluator should enforce."""

    name: str
    fn: Callable[..., Any]
    min_args: int
    max_args: int | None
    doc: str = ""

    def check_arity(self, count: int) -> None:
        if count < self.min_args or (self.max_args is not None and count > self.max_args):
            expected = _describe_arity(self.min_args, self.max_args)
            raise ExpressionEvaluationError(
                f"function {self.name!r} takes {expected}, got {count}"
            )

    def __call__(self, *args: Any) -> Any:
        self.check_arity(len(args))
        return self.fn(*args)


def _describe_arity(low: int, high: int | None) -> str:
    if high is None:
        return f"at least {low} argument(s)"
    if low == high:
        return f"exactly {low} argument(s)"
    return f"between {low} and {high} arguments"


def _fail(message: str) -> Any:
    raise ExpressionEvaluationError(message)


def _length(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (str, bytes, list, tuple, dict, set, frozenset)):
        return len(value)
    return _fail(f"len() does not apply to {type(value).__name__}")


def _contains(haystack: Any, needle: Any) -> bool:
    if haystack is None:
        return False
    if isinstance(haystack, str):
        if not isinstance(needle, str):
            return _fail("contains() on a string needs a string needle")
        return needle in haystack
    if isinstance(haystack, Mapping):
        return needle in haystack
    if isinstance(haystack, (list, tuple, set, frozenset)):
        return needle in haystack
    return _fail(f"contains() does not apply to {type(haystack).__name__}")


def _get(container: Any, key: Any, fallback: Any = None) -> Any:
    if container is None:
        return fallback
    if isinstance(container, Mapping):
        return container.get(key, fallback)
    if isinstance(container, (list, tuple)):
        if not isinstance(key, int) or isinstance(key, bool):
            return fallback
        if -len(container) <= key < len(container):
            return container[key]
        return fallback
    return fallback


def _has(container: Any, key: Any) -> bool:
    if isinstance(container, Mapping):
        return key in container
    if isinstance(container, (list, tuple)):
        return isinstance(key, int) and not isinstance(key, bool) and -len(container) <= key < len(container)
    return False


def _keys(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        return sorted(value.keys(), key=_sort_key)
    return _fail(f"keys() needs an object, got {type(value).__name__}")


def _values(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        return [value[key] for key in _keys(value)]
    return _fail(f"values() needs an object, got {type(value).__name__}")


def _sort_key(value: Any) -> tuple[int, Any]:
    """Order mixed types deterministically: numbers, then strings, then the rest."""

    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (0, value)
    if isinstance(value, str):
        return (1, value)
    return (2, repr(value))


def _sorted(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return sorted(value, key=_sort_key)
    if isinstance(value, str):
        return sorted(value)
    return _fail(f"sorted() needs a list, got {type(value).__name__}")


def _numeric(name: str, values: Iterable[Any]) -> list[float]:
    numbers: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail(f"{name}() needs numbers, found {type(value).__name__}")
        numbers.append(float(value))
    return numbers


def _sum(value: Any) -> float:
    if not isinstance(value, (list, tuple)):
        return _fail(f"sum() needs a list, got {type(value).__name__}")
    return sum(_numeric("sum", value))


def _min(value: Any) -> Any:
    items = _sequence("min", value)
    return min(items, key=_sort_key)


def _max(value: Any) -> Any:
    items = _sequence("max", value)
    return max(items, key=_sort_key)


def _sequence(name: str, value: Any) -> Sequence[Any]:
    if not isinstance(value, (list, tuple)):
        return _fail(f"{name}() needs a list, got {type(value).__name__}")
    if not value:
        return _fail(f"{name}() of an empty list is undefined")
    return value


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return _fail(f"int() cannot read {value!r}")
    return _fail(f"int() does not apply to {type(value).__name__}")


def _to_number(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return _fail(f"number() cannot read {value!r}")
    return _fail(f"number() does not apply to {type(value).__name__}")


def _to_string(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _join(items: Any, separator: Any = ",") -> str:
    if not isinstance(items, (list, tuple)):
        return _fail(f"join() needs a list, got {type(items).__name__}")
    if not isinstance(separator, str):
        return _fail("join() separator must be a string")
    return separator.join(_to_string(item) for item in items)


def _split(text: Any, separator: Any = ",") -> list[str]:
    if not isinstance(text, str) or not isinstance(separator, str):
        return _fail("split() needs two strings")
    if not separator:
        return _fail("split() separator must not be empty")
    return text.split(separator)


def _string_method(name: str, method: Callable[[str], Any]) -> Callable[[Any], Any]:
    def call(value: Any) -> Any:
        if not isinstance(value, str):
            return _fail(f"{name}() needs a string, got {type(value).__name__}")
        return method(value)

    return call


def _starts_with(text: Any, prefix: Any) -> bool:
    if not isinstance(text, str) or not isinstance(prefix, str):
        return _fail("starts_with() needs two strings")
    return text.startswith(prefix)


def _ends_with(text: Any, suffix: Any) -> bool:
    if not isinstance(text, str) or not isinstance(suffix, str):
        return _fail("ends_with() needs two strings")
    return text.endswith(suffix)


def _replace(text: Any, old: Any, new: Any) -> str:
    if not all(isinstance(value, str) for value in (text, old, new)):
        return _fail("replace() needs three strings")
    return text.replace(old, new)


def _round(value: Any, digits: Any = 0) -> float:
    number = _to_number(value)
    places = _to_int(digits)
    return round(number, places)


def _abs(value: Any) -> float:
    return abs(_to_number(value))


def _any(value: Any) -> bool:
    if not isinstance(value, (list, tuple)):
        return _fail(f"any() needs a list, got {type(value).__name__}")
    return any(bool(item) for item in value)


def _all(value: Any) -> bool:
    if not isinstance(value, (list, tuple)):
        return _fail(f"all() needs a list, got {type(value).__name__}")
    return all(bool(item) for item in value)


def _count(value: Any, wanted: Any) -> int:
    if isinstance(value, str):
        if not isinstance(wanted, str):
            return _fail("count() on a string needs a string")
        return value.count(wanted)
    if isinstance(value, (list, tuple)):
        return sum(1 for item in value if item == wanted)
    return _fail(f"count() does not apply to {type(value).__name__}")


def _duration(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return parse_duration(value)
        except ValueError as exc:
            return _fail(str(exc))
    return _fail(f"duration() does not apply to {type(value).__name__}")


def _define(*entries: Builtin) -> dict[str, Builtin]:
    table: dict[str, Builtin] = {}
    for entry in entries:
        if entry.name in table:  # pragma: no cover - guards against typos
            raise ValueError(f"builtin {entry.name!r} declared twice")
        table[entry.name] = entry
    return dict(sorted(table.items()))


#: Every function an expression may call, keyed by name.
BUILTINS: dict[str, Builtin] = _define(
    Builtin("len", _length, 1, 1, "length of a string, list or object"),
    Builtin("contains", _contains, 2, 2, "membership test"),
    Builtin("get", _get, 2, 3, "indexed read with a fallback"),
    Builtin("has", _has, 2, 2, "whether a key or index exists"),
    Builtin("keys", _keys, 1, 1, "sorted keys of an object"),
    Builtin("values", _values, 1, 1, "values of an object, ordered by key"),
    Builtin("sorted", _sorted, 1, 1, "a list in deterministic order"),
    Builtin("sum", _sum, 1, 1, "sum of a list of numbers"),
    Builtin("min", _min, 1, 1, "smallest item of a non-empty list"),
    Builtin("max", _max, 1, 1, "largest item of a non-empty list"),
    Builtin("int", _to_int, 1, 1, "convert to an integer"),
    Builtin("number", _to_number, 1, 1, "convert to a number"),
    Builtin("string", _to_string, 1, 1, "convert to a string"),
    Builtin("bool", lambda value: bool(value), 1, 1, "truthiness as a boolean"),
    Builtin("coalesce", _coalesce, 1, None, "first non-null argument"),
    Builtin("is_null", lambda value: value is None, 1, 1, "null test"),
    Builtin("join", _join, 1, 2, "join a list into a string"),
    Builtin("split", _split, 1, 2, "split a string into a list"),
    Builtin("lower", _string_method("lower", str.lower), 1, 1, "lowercase"),
    Builtin("upper", _string_method("upper", str.upper), 1, 1, "uppercase"),
    Builtin("trim", _string_method("trim", str.strip), 1, 1, "strip whitespace"),
    Builtin("starts_with", _starts_with, 2, 2, "prefix test"),
    Builtin("ends_with", _ends_with, 2, 2, "suffix test"),
    Builtin("replace", _replace, 3, 3, "substring replacement"),
    Builtin("round", _round, 1, 2, "round to a number of places"),
    Builtin("abs", _abs, 1, 1, "absolute value"),
    Builtin("any", _any, 1, 1, "true if any item is truthy"),
    Builtin("all", _all, 1, 1, "true if every item is truthy"),
    Builtin("count", _count, 2, 2, "occurrences of a value"),
    Builtin("duration", _duration, 1, 1, "seconds from a duration string"),
)


def builtin_names() -> tuple[str, ...]:
    """Every callable name, sorted."""

    return tuple(BUILTINS)


def lookup(name: str) -> Builtin | None:
    return BUILTINS.get(name)
