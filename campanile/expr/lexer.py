"""Tokeniser for the edge-condition expression language.

The language is small on purpose -- it exists so a workflow author can write

    results.extract.rows > 0 and params.region in ["eu", "us"]

on an edge, not so anyone can compute in it.  The lexer is a straightforward
scanner; the only subtlety is that every token carries its source offset, which
is what lets error messages point a caret at the offending character.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterator, NamedTuple

from ..errors import ExpressionSyntaxError

__all__ = ["TokenKind", "Token", "tokenize", "KEYWORDS", "OPERATORS"]


class TokenKind(Enum):
    NUMBER = "number"
    STRING = "string"
    NAME = "name"
    KEYWORD = "keyword"
    OPERATOR = "operator"
    END = "end"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Token(NamedTuple):
    """One lexical unit plus where it came from."""

    kind: TokenKind
    text: str
    position: int
    value: object = None

    def describe(self) -> str:
        if self.kind is TokenKind.END:
            return "end of expression"
        return f"{self.kind} {self.text!r}"


#: Words that are operators or literals rather than identifiers.
KEYWORDS: frozenset[str] = frozenset(
    {"and", "or", "not", "in", "true", "false", "null"}
)

#: Multi-character operators are listed before their prefixes so that greedy
#: matching finds ``>=`` before ``>``.
OPERATORS: tuple[str, ...] = (
    "==",
    "!=",
    "<=",
    ">=",
    "//",
    "??",
    "<",
    ">",
    "+",
    "-",
    "*",
    "/",
    "%",
    "(",
    ")",
    "[",
    "]",
    ",",
    ".",
)

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "0": "\0",
}


def tokenize(source: str) -> list[Token]:
    """Scan ``source`` into a token list terminated by a single ``END``."""

    return list(_scan(source))


def _scan(source: str) -> Iterator[Token]:
    if not isinstance(source, str):
        raise ExpressionSyntaxError(
            f"expression must be a string, got {type(source).__name__}"
        )

    position = 0
    length = len(source)

    while position < length:
        char = source[position]

        if char.isspace():
            position += 1
            continue

        if char.isdigit() or (char == "." and position + 1 < length and source[position + 1].isdigit()):
            token, position = _scan_number(source, position)
            yield token
            continue

        if char in "'\"":
            token, position = _scan_string(source, position)
            yield token
            continue

        if char.isalpha() or char == "_":
            start = position
            while position < length and (source[position].isalnum() or source[position] == "_"):
                position += 1
            text = source[start:position]
            kind = TokenKind.KEYWORD if text in KEYWORDS else TokenKind.NAME
            yield Token(kind, text, start, text)
            continue

        matched = _match_operator(source, position)
        if matched is not None:
            yield Token(TokenKind.OPERATOR, matched, position, matched)
            position += len(matched)
            continue

        raise ExpressionSyntaxError(
            f"unexpected character {char!r}", source=source, position=position
        )

    yield Token(TokenKind.END, "", length)


def _match_operator(source: str, position: int) -> str | None:
    for operator in OPERATORS:
        if source.startswith(operator, position):
            return operator
    return None


def _scan_number(source: str, position: int) -> tuple[Token, int]:
    start = position
    length = len(source)
    seen_dot = False
    seen_exponent = False

    while position < length:
        char = source[position]
        if char.isdigit():
            position += 1
        elif char == "." and not seen_dot and not seen_exponent:
            seen_dot = True
            position += 1
        elif char in "eE" and not seen_exponent and position > start:
            seen_exponent = True
            position += 1
            if position < length and source[position] in "+-":
                position += 1
            if position >= length or not source[position].isdigit():
                raise ExpressionSyntaxError(
                    "exponent has no digits", source=source, position=start
                )
        elif char == "_":
            raise ExpressionSyntaxError(
                "numeric separators are not supported", source=source, position=position
            )
        else:
            break

    text = source[start:position]
    if position < length and (source[position].isalpha() or source[position] == "_"):
        raise ExpressionSyntaxError(
            f"number {text!r} is followed by a name character",
            source=source,
            position=position,
        )

    value: object
    if seen_dot or seen_exponent:
        value = float(text)
    else:
        value = int(text)
    return Token(TokenKind.NUMBER, text, start, value), position


def _scan_string(source: str, position: int) -> tuple[Token, int]:
    quote = source[position]
    start = position
    position += 1
    length = len(source)
    pieces: list[str] = []

    while True:
        if position >= length:
            raise ExpressionSyntaxError(
                "string is not closed", source=source, position=start
            )
        char = source[position]
        if char == quote:
            position += 1
            break
        if char == "\\":
            position += 1
            if position >= length:
                raise ExpressionSyntaxError(
                    "string ends with a dangling escape", source=source, position=start
                )
            escape = source[position]
            if escape not in _ESCAPES:
                raise ExpressionSyntaxError(
                    f"unknown escape sequence '\\{escape}'",
                    source=source,
                    position=position - 1,
                )
            pieces.append(_ESCAPES[escape])
            position += 1
            continue
        if char == "\n":
            raise ExpressionSyntaxError(
                "string must not span lines", source=source, position=start
            )
        pieces.append(char)
        position += 1

    text = source[start:position]
    return Token(TokenKind.STRING, text, start, "".join(pieces)), position
