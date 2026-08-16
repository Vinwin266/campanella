"""Dependency-free helpers shared by every other layer.

Nothing in ``campanile.util`` imports from ``campanile.model`` or above, so this
package can be read on its own.  The rule that shapes it: no wall-clock reads,
no hash-order iteration, no I/O beyond what a caller hands in.
"""

from __future__ import annotations

from .clock import Clock, Deadline, ManualClock, OffsetClock, SystemClock
from .duration import coerce_duration, format_duration, humanize_duration, parse_duration
from .ids import IdFactory, content_hash, format_iso, format_timestamp, short_hash, slugify
from .jsonio import canonical_dumps, canonical_loads, dump_lines, load_lines, to_jsonable
from .ordered import OrderedSet, group_by, index_map, partition, stable_unique
from .priority import PriorityQueue
from .text import bar, columnize, indent, join_human, pad, pluralize, truncate, wrap
from .topology import (
    ancestors,
    depth_levels,
    descendants,
    find_cycle,
    leaves,
    longest_path_length,
    reachable_from,
    roots,
    topological_sort,
    transitive_reduction,
)

__all__ = [
    "Clock",
    "Deadline",
    "ManualClock",
    "OffsetClock",
    "SystemClock",
    "IdFactory",
    "OrderedSet",
    "PriorityQueue",
    "ancestors",
    "bar",
    "canonical_dumps",
    "canonical_loads",
    "coerce_duration",
    "columnize",
    "content_hash",
    "depth_levels",
    "descendants",
    "dump_lines",
    "find_cycle",
    "format_duration",
    "format_iso",
    "format_timestamp",
    "group_by",
    "humanize_duration",
    "indent",
    "index_map",
    "join_human",
    "leaves",
    "load_lines",
    "longest_path_length",
    "pad",
    "parse_duration",
    "partition",
    "pluralize",
    "reachable_from",
    "roots",
    "short_hash",
    "slugify",
    "stable_unique",
    "to_jsonable",
    "topological_sort",
    "transitive_reduction",
    "truncate",
    "wrap",
]
