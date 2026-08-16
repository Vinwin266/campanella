"""Recursive-descent parser for the expression language.

Precedence, loosest first::

    or
    and
    not          (prefix)
    comparison   ==  !=  <  <=  >  >=  in  "not in"
    coalesce     ??
    additive     +  -
    multiplicative  *  /  //  %
    unary        -  +
    postfix      .name  [key]  (args)

The parser is deliberately strict: trailing input, empty parentheses in the
wrong place, and calls on anything other than a bare name are all errors, so a
typo in a workflow definition surfaces at declaration time.
"""

from __future__ import annotations

from typing import Callable

from ..errors import ExpressionSyntaxError
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
)
from .lexer import Token, TokenKind, tokenize

__all__ = ["parse", "Parser", "MAX_DEPTH"]

#: Guard against deeply nested input; a workflow condition is never this deep.
MAX_DEPTH = 64

_COMPARISONS = frozenset({"==", "!=", "<", "<=", ">", ">="})
_ADDITIVE = frozenset({"+", "-"})
_MULTIPLICATIVE = frozenset({"*", "/", "//", "%"})

_LITERAL_KEYWORDS: dict[str, object] = {"true": True, "false": False, "null": None}


def parse(source: str) -> Node:
    """Parse ``source`` into a syntax tree.

    >>> parse("1 + 2 * 3").render()
    '(1 + (2 * 3))'
    >>> parse("a.b[0]").render()
    'a.b[0]'
    """

    return Parser(source).parse()


class Parser:
    """A single-use parser bound to one source string."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens: list[Token] = tokenize(source)
        self.index = 0
        self.depth = 0

    # -- token helpers -------------------------------------------------------

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.tokens[self.index]
        if token.kind is not TokenKind.END:
            self.index += 1
        return token

    def at_operator(self, *texts: str) -> bool:
        token = self.current
        return token.kind is TokenKind.OPERATOR and token.text in texts

    def at_keyword(self, *texts: str) -> bool:
        token = self.current
        return token.kind is TokenKind.KEYWORD and token.text in texts

    def accept_operator(self, *texts: str) -> Token | None:
        if self.at_operator(*texts):
            return self.advance()
        return None

    def accept_keyword(self, *texts: str) -> Token | None:
        if self.at_keyword(*texts):
            return self.advance()
        return None

    def expect_operator(self, text: str) -> Token:
        token = self.accept_operator(text)
        if token is None:
            raise self.error(f"expected {text!r} but found {self.current.describe()}")
        return token

    def error(self, message: str, token: Token | None = None) -> ExpressionSyntaxError:
        where = token or self.current
        return ExpressionSyntaxError(
            message, source=self.source, position=where.position
        )

    # -- entry point ---------------------------------------------------------

    def parse(self) -> Node:
        if self.current.kind is TokenKind.END:
            raise self.error("expression is empty")
        node = self.parse_or()
        if self.current.kind is not TokenKind.END:
            raise self.error(f"unexpected trailing {self.current.describe()}")
        return node

    # -- precedence levels ---------------------------------------------------

    def parse_or(self) -> Node:
        return self._left_associative_keyword(self.parse_and, ("or",), Logical)

    def parse_and(self) -> Node:
        return self._left_associative_keyword(self.parse_not, ("and",), Logical)

    def parse_not(self) -> Node:
        token = self.accept_keyword("not")
        if token is not None:
            operand = self.parse_not()
            return Unary(operator="not", operand=operand, position=token.position)
        return self.parse_comparison()

    def parse_comparison(self) -> Node:
        left = self.parse_coalesce()
        while True:
            if self.at_operator(*_COMPARISONS):
                token = self.advance()
                right = self.parse_coalesce()
                left = Binary(
                    operator=token.text, left=left, right=right, position=token.position
                )
                continue
            if self.at_keyword("in"):
                token = self.advance()
                right = self.parse_coalesce()
                left = Binary(operator="in", left=left, right=right, position=token.position)
                continue
            if self.at_keyword("not") and self._peek_is_keyword(1, "in"):
                token = self.advance()
                self.advance()
                right = self.parse_coalesce()
                left = Binary(
                    operator="not in", left=left, right=right, position=token.position
                )
                continue
            return left

    def parse_coalesce(self) -> Node:
        left = self.parse_additive()
        while self.at_operator("??"):
            token = self.advance()
            right = self.parse_additive()
            left = Coalesce(left=left, right=right, position=token.position)
        return left

    def parse_additive(self) -> Node:
        return self._left_associative_operator(self.parse_multiplicative, _ADDITIVE)

    def parse_multiplicative(self) -> Node:
        return self._left_associative_operator(self.parse_unary, _MULTIPLICATIVE)

    def parse_unary(self) -> Node:
        token = self.accept_operator("-", "+")
        if token is not None:
            operand = self.parse_unary()
            if token.text == "+":
                return operand
            return Unary(operator="-", operand=operand, position=token.position)
        return self.parse_postfix()

    def parse_postfix(self) -> Node:
        node = self.parse_primary()
        while True:
            if self.at_operator("."):
                token = self.advance()
                name = self.current
                if name.kind not in (TokenKind.NAME, TokenKind.KEYWORD):
                    raise self.error("expected an attribute name after '.'")
                self.advance()
                node = Attribute(target=node, attribute=name.text, position=token.position)
                continue
            if self.at_operator("["):
                token = self.advance()
                key = self.parse_or()
                self.expect_operator("]")
                node = Index(target=node, key=key, position=token.position)
                continue
            if self.at_operator("("):
                if not isinstance(node, Name):
                    raise self.error("only plain names can be called")
                token = self.advance()
                arguments = self.parse_arguments()
                node = Call(
                    function=node.identifier, arguments=arguments, position=node.position
                )
                continue
            return node

    def parse_arguments(self) -> tuple[Node, ...]:
        arguments: list[Node] = []
        if self.accept_operator(")") is not None:
            return ()
        while True:
            arguments.append(self.parse_or())
            if self.accept_operator(",") is not None:
                if self.at_operator(")"):
                    raise self.error("trailing comma in argument list")
                continue
            self.expect_operator(")")
            return tuple(arguments)

    def parse_primary(self) -> Node:
        token = self.current

        if token.kind is TokenKind.NUMBER or token.kind is TokenKind.STRING:
            self.advance()
            return Literal(value=token.value, position=token.position)

        if token.kind is TokenKind.KEYWORD and token.text in _LITERAL_KEYWORDS:
            self.advance()
            return Literal(value=_LITERAL_KEYWORDS[token.text], position=token.position)

        if token.kind is TokenKind.NAME:
            self.advance()
            return Name(identifier=token.text, position=token.position)

        if self.at_operator("("):
            self.advance()
            node = self._nested(self.parse_or)
            self.expect_operator(")")
            return node

        if self.at_operator("["):
            open_token = self.advance()
            items: list[Node] = []
            if self.accept_operator("]") is None:
                while True:
                    items.append(self._nested(self.parse_or))
                    if self.accept_operator(",") is not None:
                        if self.at_operator("]"):
                            raise self.error("trailing comma in list literal")
                        continue
                    self.expect_operator("]")
                    break
            return ListLiteral(items=tuple(items), position=open_token.position)

        raise self.error(f"unexpected {token.describe()}")

    # -- internals -----------------------------------------------------------

    def _peek_is_keyword(self, offset: int, text: str) -> bool:
        position = self.index + offset
        if position >= len(self.tokens):
            return False
        token = self.tokens[position]
        return token.kind is TokenKind.KEYWORD and token.text == text

    def _nested(self, parse_fn: Callable[[], Node]) -> Node:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise self.error(f"expression nests deeper than {MAX_DEPTH} levels")
        try:
            return parse_fn()
        finally:
            self.depth -= 1

    def _left_associative_operator(
        self, sub: Callable[[], Node], operators: frozenset[str]
    ) -> Node:
        left = sub()
        while self.current.kind is TokenKind.OPERATOR and self.current.text in operators:
            token = self.advance()
            right = sub()
            left = Binary(operator=token.text, left=left, right=right, position=token.position)
        return left

    def _left_associative_keyword(
        self,
        sub: Callable[[], Node],
        keywords: tuple[str, ...],
        factory: type[Logical],
    ) -> Node:
        left = sub()
        while self.at_keyword(*keywords):
            token = self.advance()
            right = sub()
            left = factory(operator=token.text, left=left, right=right, position=token.position)
        return left
