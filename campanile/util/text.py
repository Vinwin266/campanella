"""Text helpers shared by the reporting and CLI layers.

None of this is clever; it is here so that every table, error message, and CLI
line in the package is laid out by the same code and therefore looks the same.
"""

from __future__ import annotations

from typing import Iterable, Sequence

__all__ = [
    "indent",
    "truncate",
    "pad",
    "pluralize",
    "join_human",
    "columnize",
    "wrap",
    "bar",
]


def indent(text: str, prefix: str = "  ") -> str:
    """Prefix every non-empty line of ``text``."""

    return "\n".join(prefix + line if line else line for line in text.split("\n"))


def truncate(text: str, width: int, *, ellipsis: str = "...") -> str:
    """Shorten ``text`` to ``width`` characters, marking the cut.

    >>> truncate("orchestration", 8)
    'orche...'
    """

    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= len(ellipsis):
        return text[:width]
    return text[: width - len(ellipsis)] + ellipsis


def pad(text: str, width: int, *, align: str = "left") -> str:
    """Pad ``text`` to ``width``, truncating if it is already longer."""

    clipped = truncate(text, width)
    if align == "left":
        return clipped.ljust(width)
    if align == "right":
        return clipped.rjust(width)
    if align == "center":
        return clipped.center(width)
    raise ValueError(f"unknown alignment {align!r}")


def pluralize(count: int, singular: str, plural: str | None = None) -> str:
    """``pluralize(1, "task")`` -> ``'1 task'``; ``pluralize(2, "task")`` -> ``'2 tasks'``."""

    if count == 1:
        return f"{count} {singular}"
    return f"{count} {plural or singular + 's'}"


def join_human(items: Sequence[str], *, conjunction: str = "and") -> str:
    """Join with commas and a final conjunction.

    >>> join_human(["a", "b", "c"])
    'a, b and c'
    """

    values = list(items)
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} {conjunction} {values[1]}"
    return ", ".join(values[:-1]) + f" {conjunction} {values[-1]}"


def wrap(text: str, width: int = 78) -> list[str]:
    """A minimal greedy word wrapper; long words are left overlong."""

    if width <= 0:
        raise ValueError("width must be positive")
    lines: list[str] = []
    current: list[str] = []
    length = 0
    for word in text.split():
        addition = len(word) + (1 if current else 0)
        if current and length + addition > width:
            lines.append(" ".join(current))
            current = [word]
            length = len(word)
        else:
            current.append(word)
            length += addition
    if current:
        lines.append(" ".join(current))
    return lines or [""]


def columnize(rows: Iterable[Sequence[str]], *, gap: int = 2) -> list[str]:
    """Lay out rows as space-aligned columns sized to their widest cell."""

    materialised = [list(row) for row in rows]
    if not materialised:
        return []
    column_count = max(len(row) for row in materialised)
    widths = [0] * column_count
    for row in materialised:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    separator = " " * gap
    lines: list[str] = []
    for row in materialised:
        cells = [
            pad(cell, widths[index]) if index < column_count - 1 else cell
            for index, cell in enumerate(row)
        ]
        lines.append(separator.join(cells).rstrip())
    return lines


def bar(fraction: float, width: int = 20, *, filled: str = "#", empty: str = ".") -> str:
    """Render a proportion as a fixed-width text bar.

    >>> bar(0.5, width=10)
    '#####.....'
    """

    if width <= 0:
        return ""
    clamped = min(1.0, max(0.0, fraction))
    count = int(round(clamped * width))
    return filled * count + empty * (width - count)
