"""The builder, the decorator registry, and the validation pass."""

from __future__ import annotations

import unittest

from campanile.dsl.builder import TaskGroup, TaskHandle, WorkflowBuilder
from campanile.dsl.decorators import TaskRegistry, collect_declarations, declaration_of
from campanile.dsl.validate import (
    assert_valid,
    is_valid,
    param_references,
    render_issues,
    result_references,
    validate,
)
from campanile.errors import (
    ConfigurationError,
    ValidationError,
    WorkflowDefinitionError,
)
from campanile.expr.parser import parse
from campanile.model.policy import FailurePolicy, TriggerRule


def body(ctx):
    return 1


class BuilderTests(unittest.TestCase):
    def test_tasks_and_edges(self) -> None:
        builder = WorkflowBuilder("flow")
        first = builder.task("a", body)
        second = builder.task("b", body)
        first >> second
        flow = builder.build()
        self.assertEqual(flow.task_names, ("a", "b"))
        self.assertEqual(flow.upstreams_of("b"), ("a",))

    def test_rshift_returns_the_downstream_group(self) -> None:
        builder = WorkflowBuilder("flow")
        first = builder.task("a", body)
        second = builder.task("b", body)
        result = first >> second
        self.assertIsInstance(result, TaskGroup)
        self.assertEqual(result.names, ("b",))

    def test_fan_out_and_fan_in_in_one_line(self) -> None:
        builder = WorkflowBuilder("flow")
        root = builder.task("root", body)
        left = builder.task("left", body)
        right = builder.task("right", body)
        join = builder.task("join", body)
        root >> [left, right] >> join
        flow = builder.build()
        self.assertEqual(set(flow.downstreams_of("root")), {"left", "right"})
        self.assertEqual(set(flow.upstreams_of("join")), {"left", "right"})

    def test_lshift_reverses_the_direction(self) -> None:
        builder = WorkflowBuilder("flow")
        first = builder.task("a", body)
        second = builder.task("b", body)
        second << first
        self.assertEqual(builder.build().upstreams_of("b"), ("a",))

    def test_when_attaches_a_condition(self) -> None:
        builder = WorkflowBuilder("flow")
        first = builder.task("a", body)
        second = builder.task("b", body)
        first.when("results.a > 0") >> second
        edge = builder.build().edge_between("a", "b")
        assert edge is not None
        self.assertEqual(edge.condition, "results.a > 0")

    def test_chaining_settings(self) -> None:
        builder = (
            WorkflowBuilder("flow")
            .describe("a description")
            .pool("wh", 2)
            .param("day", "string", required=True)
            .tag("nightly")
            .failure("fail_fast")
            .parallelism(3)
        )
        builder.task("a", body, resources="wh")
        flow = builder.build()
        self.assertEqual(flow.description, "a description")
        self.assertEqual(flow.pools["wh"].capacity, 2)
        self.assertEqual(flow.params.required_names, ("day",))
        self.assertEqual(flow.tags, ("nightly",))
        self.assertIs(flow.failure_policy, FailurePolicy.FAIL_FAST)
        self.assertEqual(flow.max_parallelism, 3)

    def test_gate_declares_a_body_less_task(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.gate("join")
        self.assertTrue(builder.build().task("join").is_noop)

    def test_handle_and_group_lookups_validate(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("a", body)
        self.assertIsInstance(builder.handle("a"), TaskHandle)
        with self.assertRaises(WorkflowDefinitionError):
            builder.handle("ghost")
        with self.assertRaises(WorkflowDefinitionError):
            builder.group("ghost")

    def test_default_retry_flows_into_tasks(self) -> None:
        builder = WorkflowBuilder("flow", default_retry=3)
        builder.task("a", body)
        self.assertEqual(builder.build().task("a").retry.max_attempts, 3)

    def test_connecting_a_non_task_is_rejected(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("a", body)
        with self.assertRaises(WorkflowDefinitionError):
            builder.connect_many("a", 42)  # type: ignore[arg-type]

    def test_build_with_runs_the_callback(self) -> None:
        flow = WorkflowBuilder("flow").build_with(lambda b: b.task("a", body))
        self.assertEqual(flow.task_names, ("a",))

    def test_build_checks_references(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("a", body, resources="missing")
        with self.assertRaises(ConfigurationError):
            builder.build()


class RegistryTests(unittest.TestCase):
    def test_decorator_records_declaration_order(self) -> None:
        registry = TaskRegistry("flow")

        @registry.task
        def first(ctx):
            return 1

        @registry.task(after=["first"])
        def second(ctx):
            return 2

        flow = registry.build()
        self.assertEqual(flow.task_names, ("first", "second"))
        self.assertEqual(flow.upstreams_of("second"), ("first",))

    def test_decorator_returns_the_original_function(self) -> None:
        registry = TaskRegistry("flow")

        @registry.task
        def fn(ctx):
            return "value"

        self.assertEqual(fn(None), "value")
        self.assertIsNotNone(declaration_of(fn))

    def test_docstring_becomes_the_description(self) -> None:
        registry = TaskRegistry("flow")

        @registry.task
        def fn(ctx):
            """Pull the rows."""
            return 1

        self.assertEqual(registry.build().task("fn").description, "Pull the rows.")

    def test_options_reach_the_spec(self) -> None:
        registry = TaskRegistry("flow")
        registry.pool("wh", 2)

        @registry.task(retry=2, timeout="30s", resources="wh", priority=-1, tags=["x"])
        def fn(ctx):
            return 1

        spec = registry.build().task("fn")
        self.assertEqual(spec.retry.max_attempts, 2)
        self.assertEqual(spec.timeout, 30.0)
        self.assertEqual(spec.resources, {"wh": 1})
        self.assertEqual(spec.priority, -1)
        self.assertEqual(spec.tags, ("x",))

    def test_after_accepts_decorated_functions(self) -> None:
        registry = TaskRegistry("flow")

        @registry.task
        def first(ctx):
            return 1

        @registry.task(after=[first])
        def second(ctx):
            return 2

        self.assertEqual(registry.build().upstreams_of("second"), ("first",))

    def test_explicit_name_overrides_the_function_name(self) -> None:
        registry = TaskRegistry("flow")

        @registry.task(name="renamed")
        def fn(ctx):
            return 1

        self.assertEqual(registry.build().task_names, ("renamed",))

    def test_duplicate_names_are_rejected(self) -> None:
        registry = TaskRegistry("flow")

        @registry.task(name="same")
        def first(ctx):
            return 1

        with self.assertRaises(WorkflowDefinitionError):

            @registry.task(name="same")
            def second(ctx):
                return 2

    def test_gate_needs_no_function(self) -> None:
        registry = TaskRegistry("flow")

        @registry.task
        def fn(ctx):
            return 1

        registry.gate("join", after=["fn"])
        flow = registry.build()
        self.assertTrue(flow.task("join").is_noop)

    def test_gate_rejects_unknown_options(self) -> None:
        registry = TaskRegistry("flow")
        with self.assertRaises(ConfigurationError):
            registry.gate("join", nonsense=1)

    def test_decorating_a_non_callable_is_rejected(self) -> None:
        registry = TaskRegistry("flow")
        with self.assertRaises(ConfigurationError):
            registry.task(42)  # type: ignore[arg-type]

    def test_collect_declarations_finds_decorated_functions(self) -> None:
        registry = TaskRegistry("flow")

        @registry.task
        def first(ctx):
            return 1

        @registry.task
        def second(ctx):
            return 2

        found = collect_declarations({"a": first, "b": second, "c": 42})
        self.assertEqual([item.name for item in found], ["first", "second"])

    def test_clear_empties_the_registry(self) -> None:
        registry = TaskRegistry("flow")

        @registry.task
        def fn(ctx):
            return 1

        registry.clear()
        self.assertEqual(len(registry), 0)


class ValidationTests(unittest.TestCase):
    def codes(self, flow) -> set[str]:
        return {issue.code for issue in validate(flow)}

    def test_a_good_workflow_has_no_issues(self) -> None:
        builder = WorkflowBuilder("flow").pool("wh", 1)
        first = builder.task("a", body, resources="wh")
        second = builder.task("b", body)
        first >> second
        self.assertEqual(validate(builder.workflow), [])
        self.assertTrue(is_valid(builder.workflow))

    def test_empty_workflow(self) -> None:
        self.assertIn("empty_workflow", self.codes(WorkflowBuilder("flow").workflow))

    def test_unknown_pool(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("a", body, resources="missing")
        self.assertIn("unknown_pool", self.codes(builder.workflow))

    def test_request_bigger_than_the_pool(self) -> None:
        builder = WorkflowBuilder("flow").pool("wh", 1)
        builder.task("a", body, resources={"wh": 2})
        self.assertIn("resource_capacity", self.codes(builder.workflow))

    def test_unused_pool_is_only_a_warning(self) -> None:
        builder = WorkflowBuilder("flow").pool("wh", 1)
        builder.task("a", body)
        issues = validate(builder.workflow)
        unused = [issue for issue in issues if issue.code == "unused_pool"]
        self.assertTrue(unused)
        self.assertFalse(unused[0].is_error)
        self.assertTrue(is_valid(builder.workflow))
        self.assertFalse(is_valid(builder.workflow, strict=True))

    def test_cycle(self) -> None:
        builder = WorkflowBuilder("flow")
        first = builder.task("a", body)
        second = builder.task("b", body)
        first >> second
        builder.connect("b", "a")
        self.assertIn("dependency_cycle", self.codes(builder.workflow))

    def test_condition_syntax_error(self) -> None:
        builder = WorkflowBuilder("flow")
        first = builder.task("a", body)
        second = builder.task("b", body)
        first.when("1 +") >> second
        self.assertIn("condition_syntax", self.codes(builder.workflow))

    def test_condition_unknown_variable(self) -> None:
        builder = WorkflowBuilder("flow")
        first = builder.task("a", body)
        second = builder.task("b", body)
        first.when("nonsense") >> second
        self.assertIn("condition_unknown_variable", self.codes(builder.workflow))

    def test_condition_unknown_function(self) -> None:
        builder = WorkflowBuilder("flow")
        first = builder.task("a", body)
        second = builder.task("b", body)
        first.when("nope(results.a)") >> second
        self.assertIn("condition_unknown_function", self.codes(builder.workflow))

    def test_condition_reading_a_non_ancestor(self) -> None:
        builder = WorkflowBuilder("flow")
        first = builder.task("a", body)
        second = builder.task("b", body)
        builder.task("c", body)
        first.when("results.c > 0") >> second
        self.assertIn("condition_forward_reference", self.codes(builder.workflow))

    def test_condition_reading_an_ancestor_is_fine(self) -> None:
        builder = WorkflowBuilder("flow")
        first = builder.task("a", body)
        second = builder.task("b", body)
        third = builder.task("c", body)
        first >> second
        second.when("results.a > 0") >> third
        self.assertNotIn("condition_forward_reference", self.codes(builder.workflow))

    def test_condition_reading_an_unknown_task(self) -> None:
        builder = WorkflowBuilder("flow")
        first = builder.task("a", body)
        second = builder.task("b", body)
        first.when("results.ghost") >> second
        self.assertIn("condition_unknown_task", self.codes(builder.workflow))

    def test_condition_reading_an_undeclared_parameter(self) -> None:
        builder = WorkflowBuilder("flow")
        first = builder.task("a", body)
        second = builder.task("b", body)
        first.when("params.ghost") >> second
        self.assertIn("condition_unknown_param", self.codes(builder.workflow))

    def test_declared_parameter_is_accepted(self) -> None:
        builder = WorkflowBuilder("flow").param("env", "string", default="dev")
        first = builder.task("a", body)
        second = builder.task("b", body)
        first.when("params.env == 'prod'") >> second
        self.assertNotIn("condition_unknown_param", self.codes(builder.workflow))

    def test_trigger_rule_without_upstreams_warns(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("a", body, trigger_rule=TriggerRule.ALL_DONE)
        builder.task("b", body, after="a")
        self.assertIn("trigger_rule_without_upstreams", self.codes(builder.workflow))

    def test_isolated_task_warns(self) -> None:
        builder = WorkflowBuilder("flow")
        first = builder.task("a", body)
        second = builder.task("b", body)
        builder.task("island", body)
        first >> second
        self.assertIn("isolated_task", self.codes(builder.workflow))

    def test_noop_leaf_warns(self) -> None:
        builder = WorkflowBuilder("flow")
        first = builder.task("a", body)
        gate = builder.gate("dead_end")
        first >> gate
        self.assertIn("noop_leaf", self.codes(builder.workflow))

    def test_assert_valid_raises_with_every_issue(self) -> None:
        builder = WorkflowBuilder("flow")
        builder.task("a", body, resources="missing")
        with self.assertRaises(ValidationError) as caught:
            assert_valid(builder.workflow)
        self.assertTrue(caught.exception.errors)

    def test_assert_valid_strict_raises_on_warnings(self) -> None:
        builder = WorkflowBuilder("flow").pool("wh", 1)
        builder.task("a", body)
        assert_valid(builder.workflow)
        with self.assertRaises(ValidationError):
            assert_valid(builder.workflow, strict=True)

    def test_render_issues_puts_errors_first(self) -> None:
        builder = WorkflowBuilder("flow").pool("wh", 1)
        builder.task("a", body, resources="missing")
        rendered = render_issues(validate(builder.workflow))
        self.assertLess(rendered.index("error:"), rendered.index("warning:"))


class ReferenceExtractionTests(unittest.TestCase):
    def test_attribute_and_index_forms(self) -> None:
        self.assertEqual(result_references(parse("results.a + results['b']")), ("a", "b"))

    def test_dynamic_reads_are_skipped(self) -> None:
        self.assertEqual(result_references(parse("results[params.which]")), ())

    def test_param_references(self) -> None:
        self.assertEqual(param_references(parse("params.x == params['y']")), ("x", "y"))

    def test_unrelated_attributes_are_ignored(self) -> None:
        self.assertEqual(result_references(parse("run.id")), ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
