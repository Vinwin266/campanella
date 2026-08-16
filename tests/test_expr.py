"""The edge-condition expression language: lexing, parsing and evaluation."""

from __future__ import annotations

import unittest

from campanile.errors import (
    ExpressionEvaluationError,
    ExpressionSyntaxError,
    UnknownFunctionError,
    UnknownVariableError,
)
from campanile.expr.ast import Binary, Literal, Name, function_names, variable_names
from campanile.expr.eval import Environment, compile_expression, evaluate, is_truthy
from campanile.expr.functions import BUILTINS
from campanile.expr.lexer import TokenKind, tokenize
from campanile.expr.parser import parse


class LexerTests(unittest.TestCase):
    def kinds(self, source: str) -> list[str]:
        return [token.kind.value for token in tokenize(source)]

    def test_numbers(self) -> None:
        tokens = tokenize("1 2.5 1e3 1.5e-2")
        values = [token.value for token in tokens if token.kind is TokenKind.NUMBER]
        self.assertEqual(values, [1, 2.5, 1000.0, 0.015])

    def test_strings_and_escapes(self) -> None:
        tokens = tokenize(r'"a\nb" ' + "'c'")
        values = [token.value for token in tokens if token.kind is TokenKind.STRING]
        self.assertEqual(values, ["a\nb", "c"])

    def test_keywords_are_distinguished_from_names(self) -> None:
        kinds = self.kinds("a and true")
        self.assertEqual(kinds, ["name", "keyword", "keyword", "end"])

    def test_multi_character_operators_win(self) -> None:
        tokens = [token.text for token in tokenize("a >= b != c ?? d // e")]
        self.assertIn(">=", tokens)
        self.assertIn("!=", tokens)
        self.assertIn("??", tokens)
        self.assertIn("//", tokens)

    def test_positions_are_recorded(self) -> None:
        tokens = tokenize("  ab")
        self.assertEqual(tokens[0].position, 2)

    def test_unterminated_string(self) -> None:
        with self.assertRaises(ExpressionSyntaxError):
            tokenize('"open')

    def test_unknown_escape(self) -> None:
        with self.assertRaises(ExpressionSyntaxError):
            tokenize(r'"\q"')

    def test_string_must_not_span_lines(self) -> None:
        with self.assertRaises(ExpressionSyntaxError):
            tokenize('"a\nb"')

    def test_unexpected_character(self) -> None:
        with self.assertRaises(ExpressionSyntaxError) as caught:
            tokenize("a $ b")
        self.assertEqual(caught.exception.position, 2)

    def test_numeric_separators_are_rejected(self) -> None:
        with self.assertRaises(ExpressionSyntaxError):
            tokenize("1_000")

    def test_number_followed_by_a_name_is_rejected(self) -> None:
        with self.assertRaises(ExpressionSyntaxError):
            tokenize("12abc")


class ParserTests(unittest.TestCase):
    def test_arithmetic_precedence(self) -> None:
        self.assertEqual(parse("1 + 2 * 3").render(), "(1 + (2 * 3))")
        self.assertEqual(parse("(1 + 2) * 3").render(), "((1 + 2) * 3)")

    def test_comparison_binds_looser_than_arithmetic(self) -> None:
        self.assertEqual(parse("1 + 2 > 2").render(), "((1 + 2) > 2)")

    def test_and_binds_tighter_than_or(self) -> None:
        self.assertEqual(parse("a or b and c").render(), "(a or (b and c))")

    def test_not_binds_looser_than_comparison(self) -> None:
        self.assertEqual(parse("not a == b").render(), "not (a == b)")

    def test_postfix_chains(self) -> None:
        self.assertEqual(parse("a.b[0].c").render(), "a.b[0].c")

    def test_calls(self) -> None:
        self.assertEqual(parse("len(a, b)").render(), "len(a, b)")
        self.assertEqual(parse("len()").render(), "len()")

    def test_not_in(self) -> None:
        node = parse("a not in b")
        self.assertIsInstance(node, Binary)
        self.assertEqual(node.operator, "not in")

    def test_list_literals(self) -> None:
        self.assertEqual(parse("[1, 2]").render(), "[1, 2]")
        self.assertEqual(parse("[]").render(), "[]")

    def test_unary_plus_is_dropped(self) -> None:
        self.assertEqual(parse("+a").render(), "a")

    def test_literal_keywords(self) -> None:
        self.assertEqual(parse("true").render(), "true")
        self.assertEqual(parse("null").render(), "null")

    def test_syntax_errors(self) -> None:
        for source in ("", "1 +", "a b", "(1", "[1,2,]", "f(1,)", "a.", "1(2)"):
            with self.subTest(source=source), self.assertRaises(ExpressionSyntaxError):
                parse(source)

    def test_only_names_can_be_called(self) -> None:
        with self.assertRaises(ExpressionSyntaxError):
            parse("a.b(1)")

    def test_error_carries_a_position(self) -> None:
        with self.assertRaises(ExpressionSyntaxError) as caught:
            parse("a b")
        self.assertEqual(caught.exception.position, 2)
        self.assertIn("^", caught.exception.annotate())


class IntrospectionTests(unittest.TestCase):
    def test_variable_names_report_only_roots(self) -> None:
        node = parse("results.extract.rows + params.n")
        self.assertEqual(variable_names(node), ("results", "params"))

    def test_function_names(self) -> None:
        node = parse("len(keys(results))")
        self.assertEqual(function_names(node), ("len", "keys"))

    def test_literal_and_name_render(self) -> None:
        self.assertEqual(Literal(value="a\"b").render(), '"a\\"b"')
        self.assertEqual(Name(identifier="x").render(), "x")


class TruthinessTests(unittest.TestCase):
    def test_falsy_values(self) -> None:
        for value in (None, False, 0, 0.0, "", [], {}):
            with self.subTest(value=value):
                self.assertFalse(is_truthy(value))

    def test_truthy_values(self) -> None:
        for value in (True, 1, -1, 0.5, "x", [0], {"a": 1}):
            with self.subTest(value=value):
                self.assertTrue(is_truthy(value))


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Environment.of(
            params={"region": "eu", "n": 3, "flag": True},
            results={"extract": {"rows": 10, "tags": ["a", "b"]}, "load": None},
        )

    def check(self, source: str, expected: object) -> None:
        self.assertEqual(evaluate(source, self.env), expected)

    def test_literals(self) -> None:
        self.check("1", 1)
        self.check("'x'", "x")
        self.check("true", True)
        self.check("null", None)

    def test_arithmetic(self) -> None:
        self.check("2 + 3 * 4", 14)
        self.check("7 // 2", 3)
        self.check("7 % 2", 1)
        self.check("-params.n", -3)

    def test_string_and_list_concatenation(self) -> None:
        self.check("'a' + 'b'", "ab")
        self.check("[1] + [2]", [1, 2])

    def test_comparisons(self) -> None:
        self.check("results.extract.rows > 5", True)
        self.check("params.region == 'eu'", True)
        self.check("params.region != 'us'", True)

    def test_booleans_are_not_numbers(self) -> None:
        self.check("params.flag == 1", False)
        self.check("params.flag == true", True)

    def test_short_circuit_returns_the_deciding_value(self) -> None:
        self.check("'' or 'fallback'", "fallback")
        self.check("'set' or 'fallback'", "set")
        self.check("'' and 'never'", "")

    def test_membership(self) -> None:
        self.check("params.region in ['eu', 'us']", True)
        self.check("'z' not in results.extract.tags", True)
        self.check("'ro' in 'europe'", True)

    def test_coalesce(self) -> None:
        self.check("results.load ?? 'default'", "default")
        self.check("params.region ?? 'default'", "eu")

    def test_indexing(self) -> None:
        self.check("results.extract.tags[0]", "a")
        self.check("results.extract['rows']", 10)
        self.check("results.extract.tags[-1]", "b")

    def test_unknown_variable(self) -> None:
        with self.assertRaises(UnknownVariableError):
            evaluate("nope", self.env)

    def test_unknown_function(self) -> None:
        with self.assertRaises(UnknownFunctionError):
            evaluate("nope(1)", self.env)

    def test_missing_key_is_an_error_not_null(self) -> None:
        with self.assertRaises(ExpressionEvaluationError) as caught:
            evaluate("results.ghost", self.env)
        self.assertIn("available", caught.exception.message)

    def test_index_out_of_range(self) -> None:
        with self.assertRaises(ExpressionEvaluationError):
            evaluate("results.extract.tags[9]", self.env)

    def test_mixed_type_comparison_is_an_error(self) -> None:
        with self.assertRaises(ExpressionEvaluationError):
            evaluate("params.n < params.region", self.env)

    def test_division_by_zero(self) -> None:
        with self.assertRaises(ExpressionEvaluationError):
            evaluate("1 / 0", self.env)

    def test_arithmetic_on_wrong_types(self) -> None:
        with self.assertRaises(ExpressionEvaluationError):
            evaluate("params.region - 1", self.env)

    def test_reading_a_field_of_null(self) -> None:
        with self.assertRaises(ExpressionEvaluationError):
            evaluate("results.load.rows", self.env)

    def test_errors_carry_a_position(self) -> None:
        with self.assertRaises(ExpressionEvaluationError) as caught:
            evaluate("1 + results.ghost", self.env)
        self.assertIsNotNone(caught.exception.position)


class BuiltinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Environment.of(
            xs=[3, 1, 2], text="Hello", obj={"b": 2, "a": 1}, empty=[]
        )

    def check(self, source: str, expected: object) -> None:
        self.assertEqual(evaluate(source, self.env), expected)

    def test_collection_functions(self) -> None:
        self.check("len(xs)", 3)
        self.check("sorted(xs)", [1, 2, 3])
        self.check("sum(xs)", 6.0)
        self.check("min(xs)", 1)
        self.check("max(xs)", 3)
        self.check("count(xs, 1)", 1)

    def test_object_functions(self) -> None:
        self.check("keys(obj)", ["a", "b"])
        self.check("values(obj)", [1, 2])
        self.check("has(obj, 'a')", True)
        self.check("get(obj, 'z', 9)", 9)

    def test_string_functions(self) -> None:
        self.check("lower(text)", "hello")
        self.check("upper(text)", "HELLO")
        self.check("trim('  x  ')", "x")
        self.check("starts_with(text, 'He')", True)
        self.check("ends_with(text, 'lo')", True)
        self.check("replace(text, 'l', 'L')", "HeLLo")
        self.check("split('a,b', ',')", ["a", "b"])
        self.check("join(['a', 'b'], '-')", "a-b")

    def test_conversion_functions(self) -> None:
        self.check("int('42')", 42)
        self.check("number('1.5')", 1.5)
        self.check("string(42)", "42")
        self.check("bool(empty)", False)
        self.check("is_null(null)", True)
        self.check("coalesce(null, null, 'x')", "x")

    def test_numeric_functions(self) -> None:
        self.check("abs(-3)", 3.0)
        self.check("round(1.2345, 2)", 1.23)

    def test_predicate_functions(self) -> None:
        self.check("any([false, true])", True)
        self.check("all([true, false])", False)
        self.check("contains(text, 'ell')", True)

    def test_duration_function(self) -> None:
        self.check("duration('2m')", 120.0)

    def test_wrong_arity_is_reported(self) -> None:
        with self.assertRaises(ExpressionEvaluationError) as caught:
            evaluate("len()", self.env)
        self.assertIn("exactly 1", caught.exception.message)

    def test_wrong_types_are_reported(self) -> None:
        with self.assertRaises(ExpressionEvaluationError):
            evaluate("upper(xs)", self.env)

    def test_min_of_empty_list(self) -> None:
        with self.assertRaises(ExpressionEvaluationError):
            evaluate("min(empty)", self.env)

    def test_every_builtin_declares_a_sane_arity(self) -> None:
        for name, builtin in BUILTINS.items():
            with self.subTest(name=name):
                self.assertGreaterEqual(builtin.min_args, 1)
                if builtin.max_args is not None:
                    self.assertGreaterEqual(builtin.max_args, builtin.min_args)


class CompilationTests(unittest.TestCase):
    def test_compile_caches_by_source(self) -> None:
        first = compile_expression("1 + 1")
        second = compile_expression("1 + 1")
        self.assertIs(first, second)

    def test_expression_exposes_its_references(self) -> None:
        expression = compile_expression("len(results.extract) > params.n")
        self.assertEqual(expression.variables, ("results", "params"))
        self.assertEqual(expression.functions, ("len",))

    def test_test_reduces_to_a_boolean(self) -> None:
        self.assertTrue(compile_expression("'x'").test())
        self.assertFalse(compile_expression("''").test())

    def test_environment_extension_does_not_mutate(self) -> None:
        base = Environment.of(a=1)
        child = base.bind("b", 2)
        self.assertEqual(base.names, ("a",))
        self.assertEqual(child.names, ("a", "b"))

    def test_mapping_is_accepted_in_place_of_an_environment(self) -> None:
        self.assertEqual(evaluate("a + 1", {"a": 1}), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
