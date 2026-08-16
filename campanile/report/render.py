"""Text tables.

Every tabular thing the CLI prints goes through :class:`Table`, so column
widths, alignment and separators are decided in one place.  Output is plain
ASCII with no escape codes: it has to survive being piped into a file, a ticket
or a chat window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..util.text import bar, pad, truncate

__all__ = ["Column", "Table", "render_kv", "render_bars", "ALIGNMENTS"]

ALIGNMENTS: tuple[str, ...] = ("left", "right", "center")


@dataclass
class Column:
    """One column's header, alignment and width limit."""

    title: str
    align: str = "left"
    max_width: int | None = None

    def __post_init__(self) -> None:
        if self.align not in ALIGNMENTS:
            raise ValueError(f"unknown alignment {self.align!r}")
        if self.max_width is not None and self.max_width < 1:
            raise ValueError("max_width must be positive")


@dataclass
class Table:
    """A grid of strings with headers.

    >>> table = Table.of("name", "state")
    >>> table.add("extract", "succeeded")
    >>> print(table.render())
    name     state
    -------  ---------
    extract  succeeded
    """

    columns: list[Column] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    gap: int = 2
    rule: str = "-"

    @classmethod
    def of(cls, *titles: str, gap: int = 2) -> "Table":
        return cls(columns=[Column(title) for title in titles], gap=gap)

    def align(self, *alignments: str) -> "Table":
        """Set column alignments left to right; returns ``self``."""

        for column, alignment in zip(self.columns, alignments):
            if alignment not in ALIGNMENTS:
                raise ValueError(f"unknown alignment {alignment!r}")
            column.align = alignment
        return self

    def limit(self, **widths: int) -> "Table":
        """Cap named columns' widths, matching on header title."""

        by_title = {column.title: column for column in self.columns}
        for title, width in widths.items():
            column = by_title.get(title)
            if column is not None:
                column.max_width = width
        return self

    def add(self, *cells: Any) -> "Table":
        """Append a row, stringifying and padding out short rows."""

        row = [_cell(value) for value in cells]
        if len(row) > len(self.columns):
            raise ValueError(
                f"row has {len(row)} cells but the table has {len(self.columns)} columns"
            )
        row.extend("" for _ in range(len(self.columns) - len(row)))
        self.rows.append(row)
        return self

    def extend(self, rows: Iterable[Sequence[Any]]) -> "Table":
        for row in rows:
            self.add(*row)
        return self

    def __len__(self) -> int:
        return len(self.rows)

    @property
    def empty(self) -> bool:
        return not self.rows

    def widths(self) -> list[int]:
        """Final width of each column, honouring ``max_width``."""

        widths = [len(column.title) for column in self.columns]
        for row in self.rows:
            for index, cell in enumerate(row):
                widths[index] = max(widths[index], len(cell))
        for index, column in enumerate(self.columns):
            if column.max_width is not None:
                widths[index] = min(widths[index], column.max_width)
        return widths

    def render(self, *, header: bool = True) -> str:
        """Render the whole table, including a rule under the header."""

        if not self.columns:
            return ""
        widths = self.widths()
        separator = " " * self.gap
        lines: list[str] = []

        if header:
            lines.append(
                separator.join(
                    pad(column.title, widths[index], align=column.align)
                    for index, column in enumerate(self.columns)
                ).rstrip()
            )
            lines.append(
                separator.join(self.rule * widths[index] for index in range(len(widths)))
            )

        for row in self.rows:
            lines.append(
                separator.join(
                    pad(
                        truncate(cell, widths[index]),
                        widths[index],
                        align=self.columns[index].align,
                    )
                    for index, cell in enumerate(row)
                ).rstrip()
            )

        return "\n".join(lines)

    def __str__(self) -> str:
        return self.render()


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_kv(pairs: Sequence[tuple[str, Any]], *, gap: int = 2) -> str:
    """Render ``key: value`` lines with the colons aligned."""

    if not pairs:
        return ""
    width = max(len(str(key)) for key, _ in pairs)
    return "\n".join(
        f"{str(key) + ':':<{width + 1}}{' ' * gap}{_cell(value)}" for key, value in pairs
    )


def render_bars(
    pairs: Sequence[tuple[str, float]], *, width: int = 32, total: float | None = None
) -> str:
    """Render a labelled horizontal bar chart.

    ``total`` fixes the scale; without it the largest value fills the bar, which
    is what you want when comparing items and not what you want when the values
    are shares of a known whole.
    """

    if not pairs:
        return ""
    largest = total if total is not None else max((value for _, value in pairs), default=0.0)
    if largest <= 0:
        largest = 1.0
    label_width = max(len(label) for label, _ in pairs)
    lines = []
    for label, value in pairs:
        lines.append(
            f"{pad(label, label_width)}  {bar(value / largest, width)}  {_cell(value)}"
        )
    return "\n".join(lines)
