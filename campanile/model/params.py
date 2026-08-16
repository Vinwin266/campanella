"""Run parameters.

A workflow may declare parameters that callers supply per run -- a date to
process, a region, a dry-run flag.  Declared parameters are typed, so the engine
can reject a bad invocation before any task starts rather than halfway through.

Coercion is deliberately narrow.  Strings coerce to numbers and booleans because
parameters usually arrive from a command line; nothing else converts implicitly,
because silently turning a list into a string is how a nightly job ends up
writing to the wrong table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..errors import (
    ConfigurationError,
    MissingParameterError,
    ParameterError,
    ParameterTypeError,
)
from .identifiers import validate_task_name

__all__ = ["ParamType", "ParamSpec", "ParamSchema", "PARAM_TYPES"]

#: Accepted values for a parameter's declared type.
PARAM_TYPES: tuple[str, ...] = (
    "string",
    "integer",
    "number",
    "boolean",
    "list",
    "object",
    "any",
)

ParamType = str

_TRUE_WORDS = frozenset({"true", "yes", "on", "1"})
_FALSE_WORDS = frozenset({"false", "no", "off", "0"})


@dataclass(frozen=True)
class ParamSpec:
    """One declared parameter."""

    name: str
    type: ParamType = "string"
    required: bool = False
    default: Any = None
    choices: tuple[Any, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        validate_task_name(self.name)
        if self.type not in PARAM_TYPES:
            raise ConfigurationError(
                f"parameter {self.name!r} has unknown type {self.type!r}; "
                f"expected one of {', '.join(PARAM_TYPES)}"
            )
        if self.required and self.default is not None:
            raise ConfigurationError(
                f"parameter {self.name!r} is required and therefore cannot have a default"
            )
        if self.default is not None:
            object.__setattr__(self, "default", self.coerce(self.default))
        if self.choices:
            coerced = tuple(self.coerce(choice) for choice in self.choices)
            object.__setattr__(self, "choices", coerced)
            if self.default is not None and self.default not in coerced:
                raise ConfigurationError(
                    f"parameter {self.name!r} default {self.default!r} is not one of its choices"
                )

    def coerce(self, value: Any) -> Any:
        """Convert ``value`` to this parameter's declared type, or raise."""

        expected = self.type
        if expected == "any":
            return value
        if expected == "string":
            if isinstance(value, str):
                return value
            raise ParameterTypeError(self.name, "a string", value)
        if expected == "boolean":
            return self._coerce_boolean(value)
        if expected == "integer":
            return self._coerce_integer(value)
        if expected == "number":
            return self._coerce_number(value)
        if expected == "list":
            if isinstance(value, (list, tuple)):
                return list(value)
            raise ParameterTypeError(self.name, "a list", value)
        if expected == "object":
            if isinstance(value, Mapping):
                return dict(value)
            raise ParameterTypeError(self.name, "an object", value)
        raise AssertionError(f"unhandled parameter type {expected!r}")  # pragma: no cover

    def _coerce_boolean(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in _TRUE_WORDS:
                return True
            if lowered in _FALSE_WORDS:
                return False
        raise ParameterTypeError(self.name, "a boolean", value)

    def _coerce_integer(self, value: Any) -> int:
        if isinstance(value, bool):
            raise ParameterTypeError(self.name, "an integer", value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip(), 10)
            except ValueError:
                pass
        raise ParameterTypeError(self.name, "an integer", value)

    def _coerce_number(self, value: Any) -> float:
        if isinstance(value, bool):
            raise ParameterTypeError(self.name, "a number", value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise ParameterTypeError(self.name, "a number", value)

    def validate(self, value: Any) -> Any:
        """Coerce and then check ``choices``."""

        coerced = self.coerce(value)
        if self.choices and coerced not in self.choices:
            rendered = ", ".join(repr(choice) for choice in self.choices)
            raise ParameterError(
                f"parameter {self.name!r} must be one of {rendered}, got {coerced!r}",
                parameter=self.name,
            )
        return coerced

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "choices": list(self.choices),
            "description": self.description,
        }


@dataclass
class ParamSchema:
    """An ordered collection of :class:`ParamSpec`."""

    specs: dict[str, ParamSpec] = field(default_factory=dict)
    allow_extra: bool = False

    @classmethod
    def of(cls, *specs: ParamSpec, allow_extra: bool = False) -> "ParamSchema":
        schema = cls(allow_extra=allow_extra)
        for spec in specs:
            schema.add(spec)
        return schema

    def add(self, spec: ParamSpec) -> ParamSpec:
        if spec.name in self.specs:
            raise ConfigurationError(f"parameter {spec.name!r} is declared twice")
        self.specs[spec.name] = spec
        return spec

    def declare(
        self,
        name: str,
        type: ParamType = "string",
        *,
        required: bool = False,
        default: Any = None,
        choices: Iterable[Any] = (),
        description: str = "",
    ) -> ParamSpec:
        """Declare a parameter inline, the form the builder uses."""

        return self.add(
            ParamSpec(
                name=name,
                type=type,
                required=required,
                default=default,
                choices=tuple(choices),
                description=description,
            )
        )

    def __contains__(self, name: object) -> bool:
        return name in self.specs

    def __len__(self) -> int:
        return len(self.specs)

    def __iter__(self):
        return iter(self.specs.values())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self.specs)

    @property
    def required_names(self) -> tuple[str, ...]:
        return tuple(name for name, spec in self.specs.items() if spec.required)

    def bind(self, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Validate ``values`` against the schema and fill in defaults.

        The returned mapping has one entry per declared parameter, in
        declaration order, plus any extras when ``allow_extra`` is set.
        """

        supplied = dict(values or {})
        bound: dict[str, Any] = {}

        for name, spec in self.specs.items():
            if name in supplied:
                bound[name] = spec.validate(supplied.pop(name))
            elif spec.required:
                raise MissingParameterError(name)
            else:
                bound[name] = spec.default

        if supplied:
            if not self.allow_extra:
                unknown = ", ".join(sorted(supplied))
                known = ", ".join(self.specs) or "none"
                raise ParameterError(
                    f"unknown parameter(s): {unknown} (declared: {known})"
                )
            for name in sorted(supplied):
                bound[name] = supplied[name]

        return bound

    def as_dict(self) -> dict[str, Any]:
        return {
            "allow_extra": self.allow_extra,
            "params": [spec.as_dict() for spec in self.specs.values()],
        }

    def describe(self) -> list[str]:
        lines: list[str] = []
        for spec in self.specs.values():
            marker = "required" if spec.required else f"default={spec.default!r}"
            line = f"{spec.name}: {spec.type} ({marker})"
            if spec.choices:
                line += " choices=" + ", ".join(repr(choice) for choice in spec.choices)
            if spec.description:
                line += f" -- {spec.description}"
            lines.append(line)
        return lines


def merge_param_values(
    schema: ParamSchema, *layers: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Bind after overlaying successive layers, later layers winning."""

    merged: dict[str, Any] = {}
    for layer in layers:
        if layer:
            merged.update(layer)
    return schema.bind(merged)


def sorted_values(values: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """Parameter values as a sorted list of pairs, for stable rendering."""

    return sorted(values.items())


def describe_values(values: Mapping[str, Any], *, limit: int = 6) -> str:
    """Render bound parameter values compactly for a report header."""

    pairs: Sequence[tuple[str, Any]] = sorted_values(values)
    if not pairs:
        return "(none)"
    shown = pairs[:limit]
    rendered = ", ".join(f"{name}={value!r}" for name, value in shown)
    if len(pairs) > limit:
        rendered += f", ... (+{len(pairs) - limit} more)"
    return rendered
