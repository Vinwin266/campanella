"""Names, parameters, resources, task specs and workflow assembly."""

from __future__ import annotations

import unittest

from campanile.errors import (
    ConfigurationError,
    CycleError,
    DuplicateTaskError,
    MissingParameterError,
    ParameterError,
    ParameterTypeError,
    ResourceCapacityError,
    UnknownPoolError,
    UnknownTaskError,
    WorkflowDefinitionError,
)
from campanile.model.identifiers import (
    qualify,
    split_qualified,
    validate_tag,
    validate_task_name,
    validate_workflow_name,
)
from campanile.model.params import ParamSchema, ParamSpec
from campanile.model.policy import RetryPolicy, TriggerRule
from campanile.model.resource import ResourcePool, normalise_request
from campanile.model.task import TaskSpec
from campanile.model.workflow import Edge, Workflow, merge


class NameTests(unittest.TestCase):
    def test_accepts_reasonable_names(self) -> None:
        for name in ("extract", "extract_eu", "extract-eu", "step.1", "A1"):
            with self.subTest(name=name):
                self.assertEqual(validate_task_name(name), name)

    def test_rejects_bad_names(self) -> None:
        for name in ("", "1step", "_hidden", "has space", "sym$bol", "x" * 200):
            with self.subTest(name=name), self.assertRaises(WorkflowDefinitionError):
                validate_task_name(name)

    def test_rejects_reserved_names(self) -> None:
        for name in ("params", "results", "run", "task"):
            with self.subTest(name=name), self.assertRaises(WorkflowDefinitionError):
                validate_task_name(name)

    def test_workflow_names_may_be_reserved_words(self) -> None:
        self.assertEqual(validate_workflow_name("run"), "run")

    def test_tags_are_looser_than_names(self) -> None:
        self.assertEqual(validate_tag("team:payments"), "team:payments")
        self.assertEqual(validate_tag("2026"), "2026")
        with self.assertRaises(WorkflowDefinitionError):
            validate_tag("has space")

    def test_qualified_names(self) -> None:
        self.assertEqual(qualify("nightly", "extract"), "nightly::extract")
        self.assertEqual(split_qualified("nightly::extract"), ("nightly", "extract"))
        with self.assertRaises(ValueError):
            split_qualified("nightly.extract")


class ParamTests(unittest.TestCase):
    def test_string_coercion_is_strict(self) -> None:
        spec = ParamSpec("region")
        self.assertEqual(spec.coerce("eu"), "eu")
        with self.assertRaises(ParameterTypeError):
            spec.coerce(3)

    def test_integer_coercion(self) -> None:
        spec = ParamSpec("n", "integer")
        self.assertEqual(spec.coerce("42"), 42)
        self.assertEqual(spec.coerce(42.0), 42)
        with self.assertRaises(ParameterTypeError):
            spec.coerce(1.5)
        with self.assertRaises(ParameterTypeError):
            spec.coerce(True)

    def test_boolean_words(self) -> None:
        spec = ParamSpec("flag", "boolean")
        for text in ("true", "YES", "on", "1"):
            self.assertTrue(spec.coerce(text))
        for text in ("false", "no", "OFF", "0"):
            self.assertFalse(spec.coerce(text))
        with self.assertRaises(ParameterTypeError):
            spec.coerce("maybe")

    def test_number_and_containers(self) -> None:
        self.assertEqual(ParamSpec("n", "number").coerce("1.5"), 1.5)
        self.assertEqual(ParamSpec("xs", "list").coerce((1, 2)), [1, 2])
        self.assertEqual(ParamSpec("o", "object").coerce({"a": 1}), {"a": 1})
        self.assertEqual(ParamSpec("any", "any").coerce(object) is object, True)

    def test_choices_are_enforced(self) -> None:
        spec = ParamSpec("region", choices=("eu", "us"))
        self.assertEqual(spec.validate("eu"), "eu")
        with self.assertRaises(ParameterError):
            spec.validate("apac")

    def test_required_cannot_have_a_default(self) -> None:
        with self.assertRaises(ConfigurationError):
            ParamSpec("x", required=True, default="y")

    def test_unknown_type_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            ParamSpec("x", "colour")

    def test_default_must_match_choices(self) -> None:
        with self.assertRaises(ConfigurationError):
            ParamSpec("x", choices=("a", "b"), default="c")

    def test_bind_fills_defaults(self) -> None:
        schema = ParamSchema.of(
            ParamSpec("region", default="eu"), ParamSpec("n", "integer", default=1)
        )
        self.assertEqual(schema.bind({}), {"region": "eu", "n": 1})
        self.assertEqual(schema.bind({"n": "5"}), {"region": "eu", "n": 5})

    def test_bind_requires_required(self) -> None:
        schema = ParamSchema.of(ParamSpec("day", required=True))
        with self.assertRaises(MissingParameterError):
            schema.bind({})

    def test_bind_rejects_unknown_by_default(self) -> None:
        schema = ParamSchema.of(ParamSpec("day"))
        with self.assertRaises(ParameterError):
            schema.bind({"nope": 1})

    def test_allow_extra_keeps_unknown(self) -> None:
        schema = ParamSchema.of(ParamSpec("day"), allow_extra=True)
        self.assertEqual(schema.bind({"nope": 1})["nope"], 1)

    def test_duplicate_declaration_is_rejected(self) -> None:
        schema = ParamSchema()
        schema.declare("x")
        with self.assertRaises(ConfigurationError):
            schema.declare("x")

    def test_bind_preserves_declaration_order(self) -> None:
        schema = ParamSchema()
        schema.declare("b", default="one")
        schema.declare("a", default="two")
        self.assertEqual(list(schema.bind({})), ["b", "a"])


class ResourceTests(unittest.TestCase):
    def test_pool_validation(self) -> None:
        self.assertEqual(ResourcePool("wh", 4).capacity, 4)
        with self.assertRaises(ConfigurationError):
            ResourcePool("wh", 0)
        with self.assertRaises(ConfigurationError):
            ResourcePool("wh", True)  # type: ignore[arg-type]

    def test_normalise_accepts_several_shapes(self) -> None:
        self.assertEqual(normalise_request(None), {})
        self.assertEqual(normalise_request("wh"), {"wh": 1})
        self.assertEqual(normalise_request(["wh", "lic"]), {"lic": 1, "wh": 1})
        self.assertEqual(normalise_request({"wh": 2}), {"wh": 2})

    def test_normalise_sorts_by_pool_name(self) -> None:
        self.assertEqual(list(normalise_request({"z": 1, "a": 1})), ["a", "z"])

    def test_normalise_rejects_bad_amounts(self) -> None:
        for request in ({"wh": 0}, {"wh": -1}, {"wh": 1.5}, {"wh": True}):
            with self.subTest(request=request), self.assertRaises(ConfigurationError):
                normalise_request(request)  # type: ignore[arg-type]

    def test_normalise_rejects_duplicate_names(self) -> None:
        with self.assertRaises(ConfigurationError):
            normalise_request(["wh", "wh"])


class TaskSpecTests(unittest.TestCase):
    def test_defaults(self) -> None:
        spec = TaskSpec("extract")
        self.assertTrue(spec.is_noop)
        self.assertIs(spec.trigger_rule, TriggerRule.ALL_SUCCESS)
        self.assertEqual(spec.resources, {})
        self.assertIsNone(spec.timeout)

    def test_build_accepts_loose_forms(self) -> None:
        spec = TaskSpec.build(
            "extract",
            lambda ctx: None,
            retry=3,
            timeout="2m",
            resources="wh",
            trigger_rule="all_done",
            tags=["a"],
        )
        self.assertEqual(spec.retry.max_attempts, 3)
        self.assertEqual(spec.timeout, 120.0)
        self.assertEqual(spec.resources, {"wh": 1})
        self.assertIs(spec.trigger_rule, TriggerRule.ALL_DONE)
        self.assertTrue(spec.has_tag("a"))

    def test_retry_may_be_a_mapping(self) -> None:
        spec = TaskSpec.build("t", retry={"max_attempts": 2, "delay": "5s"})
        self.assertEqual(spec.retry.schedule(), [5.0])

    def test_rejects_bad_fields(self) -> None:
        with self.assertRaises(ConfigurationError):
            TaskSpec("t", fn=42)  # type: ignore[arg-type]
        with self.assertRaises(ConfigurationError):
            TaskSpec("t", timeout=-1)
        with self.assertRaises(ConfigurationError):
            TaskSpec("t", priority=True)
        with self.assertRaises(ConfigurationError):
            TaskSpec.build("t", trigger_rule="whenever")

    def test_evolve_produces_a_copy(self) -> None:
        spec = TaskSpec.build("t", retry=2)
        other = spec.evolve(name="u")
        self.assertEqual(other.name, "u")
        self.assertEqual(spec.name, "t")
        with self.assertRaises(ConfigurationError):
            spec.evolve(nonsense=1)

    def test_sort_key_orders_by_priority_then_position(self) -> None:
        low = TaskSpec.build("a", priority=-1)
        high = TaskSpec.build("b", priority=1)
        self.assertLess(low.sort_key(5), high.sort_key(0))

    def test_matches_filters(self) -> None:
        spec = TaskSpec.build("t", tags=["sink"], resources="wh")
        self.assertTrue(spec.matches(tag="sink"))
        self.assertFalse(spec.matches(tag="source"))
        self.assertTrue(spec.matches(pool="wh"))
        self.assertFalse(spec.matches(pool="other"))

    def test_specs_are_frozen(self) -> None:
        spec = TaskSpec("t")
        with self.assertRaises(Exception):
            spec.name = "u"  # type: ignore[misc]


class EdgeTests(unittest.TestCase):
    def test_self_dependency_is_rejected(self) -> None:
        with self.assertRaises(WorkflowDefinitionError):
            Edge("a", "a")

    def test_blank_condition_is_rejected(self) -> None:
        with self.assertRaises(WorkflowDefinitionError):
            Edge("a", "b", "   ")

    def test_describe_shows_the_condition(self) -> None:
        self.assertIn("[x > 1]", Edge("a", "b", "x > 1").describe())


class WorkflowTests(unittest.TestCase):
    def build(self) -> Workflow:
        flow = Workflow("nightly")
        flow.pool("wh", 2)
        flow.add("extract", lambda ctx: 1, resources="wh")
        flow.add("transform", lambda ctx: 2, after="extract")
        flow.add("load", lambda ctx: 3, after=["transform"])
        return flow

    def test_declaration_order_is_kept(self) -> None:
        self.assertEqual(self.build().task_names, ("extract", "transform", "load"))

    def test_duplicate_task_is_rejected(self) -> None:
        flow = self.build()
        with self.assertRaises(DuplicateTaskError):
            flow.add("extract")

    def test_unknown_task_lookup(self) -> None:
        with self.assertRaises(UnknownTaskError):
            self.build().task("ghost")

    def test_edges_and_relationships(self) -> None:
        flow = self.build()
        self.assertEqual(flow.upstreams_of("transform"), ("extract",))
        self.assertEqual(flow.downstreams_of("extract"), ("transform",))
        self.assertEqual(flow.roots(), ("extract",))
        self.assertEqual(flow.leaves(), ("load",))
        self.assertEqual(flow.ancestors_of("load"), ("extract", "transform"))
        self.assertEqual(flow.descendants_of("extract"), ("transform", "load"))

    def test_repeated_identical_edge_is_a_noop(self) -> None:
        flow = self.build()
        before = len(flow.edges)
        flow.connect("extract", "transform")
        self.assertEqual(len(flow.edges), before)

    def test_conflicting_conditions_on_one_edge_are_rejected(self) -> None:
        flow = self.build()
        with self.assertRaises(WorkflowDefinitionError):
            flow.connect("extract", "transform", condition="x > 1")

    def test_topological_order(self) -> None:
        self.assertEqual(
            self.build().topological_order(), ("extract", "transform", "load")
        )

    def test_cycle_is_reported(self) -> None:
        flow = self.build()
        flow.connect("load", "extract")
        with self.assertRaises(CycleError):
            flow.topological_order()

    def test_check_references_catches_dangling_edges(self) -> None:
        flow = self.build()
        flow.edges.append(Edge("extract", "ghost"))
        with self.assertRaises(UnknownTaskError):
            flow.check_references()

    def test_check_references_catches_unknown_pools(self) -> None:
        flow = Workflow("w")
        flow.add("t", resources="missing")
        with self.assertRaises(UnknownPoolError):
            flow.check_references()

    def test_check_references_catches_oversized_requests(self) -> None:
        flow = Workflow("w")
        flow.pool("wh", 1)
        flow.add("t", resources={"wh": 2})
        with self.assertRaises(ResourceCapacityError):
            flow.check_references()

    def test_chain_and_fan_helpers(self) -> None:
        flow = Workflow("w")
        for name in ("a", "b", "c", "d"):
            flow.add(name)
        flow.chain("a", "b", "c")
        flow.fan_out("a", ["d"])
        flow.fan_in(["b", "c"], "d")
        self.assertEqual(set(flow.upstreams_of("d")), {"a", "b", "c"})

    def test_copy_is_independent(self) -> None:
        flow = self.build()
        clone = flow.copy()
        clone.add("extra")
        self.assertNotIn("extra", flow)
        self.assertIn("extra", clone)

    def test_subgraph_keeps_only_internal_edges(self) -> None:
        flow = self.build()
        sub = flow.subgraph(["extract", "transform"])
        self.assertEqual(sub.task_names, ("extract", "transform"))
        self.assertEqual(len(sub.edges), 1)

    def test_subgraph_validates_names(self) -> None:
        with self.assertRaises(UnknownTaskError):
            self.build().subgraph(["ghost"])

    def test_levels(self) -> None:
        self.assertEqual(
            self.build().levels(), {"extract": 0, "transform": 1, "load": 2}
        )

    def test_default_retry_applies_to_new_tasks(self) -> None:
        flow = Workflow("w", default_retry=RetryPolicy(max_attempts=3))
        flow.add("t")
        self.assertEqual(flow.task("t").retry.max_attempts, 3)

    def test_per_task_retry_overrides_the_default(self) -> None:
        flow = Workflow("w", default_retry=RetryPolicy(max_attempts=3))
        flow.add("t", retry=1)
        self.assertEqual(flow.task("t").retry.max_attempts, 1)

    def test_max_parallelism_is_validated(self) -> None:
        with self.assertRaises(WorkflowDefinitionError):
            Workflow("w", max_parallelism=0)

    def test_referenced_pools(self) -> None:
        flow = self.build()
        self.assertEqual(list(flow.referenced_pools()), ["wh"])


class MergeTests(unittest.TestCase):
    def test_prefixes_task_names(self) -> None:
        first = Workflow("a")
        first.add("t")
        second = Workflow("b")
        second.add("t")
        merged = merge("both", [first, second])
        self.assertEqual(merged.task_names, ("a.t", "b.t"))

    def test_without_prefix_names_must_be_disjoint(self) -> None:
        first = Workflow("a")
        first.add("t")
        second = Workflow("b")
        second.add("t")
        with self.assertRaises(DuplicateTaskError):
            merge("both", [first, second], prefix=False)

    def test_conflicting_pool_capacities_are_rejected(self) -> None:
        first = Workflow("a")
        first.pool("wh", 1)
        first.add("t", resources="wh")
        second = Workflow("b")
        second.pool("wh", 4)
        second.add("t", resources="wh")
        with self.assertRaises(WorkflowDefinitionError):
            merge("both", [first, second])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
