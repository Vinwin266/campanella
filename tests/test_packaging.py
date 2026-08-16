"""The package's own shape: version, exports, layering and examples.

These are cheap structural checks that catch the mistakes nobody notices until
someone imports the package a different way -- a name in ``__all__`` that does
not exist, a layer importing upwards, a version that drifted from the one in
``pyproject.toml``.
"""

from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path

import campanile
from campanile.version import VERSION, VERSION_INFO, version_string

PACKAGE_ROOT = Path(campanile.__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

#: Which packages each layer is allowed to import from.  ``util`` sits at the
#: bottom and imports from nothing but the standard library and ``errors``.
LAYERS: dict[str, set[str]] = {
    "util": set(),
    "model": {"util"},
    "expr": {"util", "model"},
    "schedule": {"util", "model"},
    "dsl": {"util", "model", "expr", "schedule"},
    "store": {"util", "model"},
    "runtime": {"util", "model", "expr", "schedule", "dsl", "store"},
    "report": {"util", "model", "expr", "schedule", "store"},
    "cli": {"util", "model", "expr", "schedule", "dsl", "store", "runtime", "report"},
}


def package_modules(package: str) -> list[Path]:
    return sorted((PACKAGE_ROOT / package).glob("*.py"))


#: Parsed once and shared; these checks read every module several times over.
_TREES: dict[Path, ast.Module] = {}
_SOURCES: dict[Path, str] = {}


def source_of(path: Path) -> str:
    if path not in _SOURCES:
        _SOURCES[path] = path.read_text(encoding="utf-8")
    return _SOURCES[path]


def tree_of(path: Path) -> ast.Module:
    if path not in _TREES:
        _TREES[path] = ast.parse(source_of(path), filename=str(path))
    return _TREES[path]


class VersionTests(unittest.TestCase):
    def test_version_string_matches_the_tuple(self) -> None:
        self.assertEqual(VERSION, ".".join(str(part) for part in VERSION_INFO))

    def test_version_is_three_parts(self) -> None:
        self.assertEqual(len(VERSION_INFO), 3)
        self.assertTrue(all(isinstance(part, int) for part in VERSION_INFO))

    def test_display_form(self) -> None:
        self.assertEqual(version_string(), f"campanile {VERSION}")

    def test_pyproject_agrees(self) -> None:
        text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'version = "{VERSION}"', text)


class ExportTests(unittest.TestCase):
    def test_top_level_all_resolves(self) -> None:
        for name in campanile.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(campanile, name), f"campanile.{name} is missing")

    def test_top_level_all_is_sorted(self) -> None:
        """Constants come first, then everything else in sorted order."""

        names = list(campanile.__all__)
        constants = [name for name in names if name.isupper()]
        rest = [name for name in names if not name.isupper()]
        self.assertEqual(names, constants + rest)
        self.assertEqual(constants, sorted(constants))
        self.assertEqual(rest, sorted(rest))

    def test_subpackage_exports_resolve(self) -> None:
        for package in LAYERS:
            module = importlib.import_module(f"campanile.{package}")
            for name in getattr(module, "__all__", ()):
                with self.subTest(package=package, name=name):
                    self.assertTrue(hasattr(module, name))

    def test_the_headline_names_are_importable(self) -> None:
        for name in ("Engine", "Workflow", "WorkflowBuilder", "TaskRegistry", "RunResult"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(campanile, name))


class LayeringTests(unittest.TestCase):
    def imported_packages(self, path: Path) -> set[str]:
        """Which sibling packages ``path`` imports from."""

        tree = tree_of(path)
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 2 and node.module:
                found.add(node.module.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("campanile."):
                found.add(node.module.split(".")[1])
        return found & set(LAYERS)

    def test_each_layer_only_imports_downwards(self) -> None:
        for package, allowed in LAYERS.items():
            for path in package_modules(package):
                with self.subTest(module=f"{package}/{path.name}"):
                    imported = self.imported_packages(path)
                    illegal = imported - allowed - {package}
                    self.assertEqual(
                        illegal,
                        set(),
                        f"campanile/{package}/{path.name} imports upwards from {sorted(illegal)}",
                    )

    def test_util_depends_on_nothing_above_it(self) -> None:
        for path in package_modules("util"):
            with self.subTest(module=path.name):
                self.assertEqual(self.imported_packages(path), set())

    def test_every_package_has_a_docstring(self) -> None:
        for package in LAYERS:
            module = importlib.import_module(f"campanile.{package}")
            with self.subTest(package=package):
                self.assertTrue((module.__doc__ or "").strip())

    def test_every_module_has_a_docstring(self) -> None:
        for package in LAYERS:
            for path in package_modules(package):
                if path.name == "__init__.py":
                    continue
                with self.subTest(module=f"{package}/{path.name}"):
                    tree = tree_of(path)
                    self.assertIsNotNone(ast.get_docstring(tree))

    def test_no_module_imports_time_outside_the_clock(self) -> None:
        """Only the clock and the scheduler's sleep may touch ``time``."""

        allowed = {"util/clock.py", "runtime/scheduler.py"}
        for package in LAYERS:
            for path in package_modules(package):
                relative = f"{package}/{path.name}"
                if relative in allowed:
                    continue
                with self.subTest(module=relative):
                    tree = tree_of(path)
                    names = {
                        alias.name
                        for node in ast.walk(tree)
                        if isinstance(node, ast.Import)
                        for alias in node.names
                    }
                    self.assertNotIn("time", names)

    def test_nothing_imports_random(self) -> None:
        for package in LAYERS:
            for path in package_modules(package):
                with self.subTest(module=f"{package}/{path.name}"):
                    source = source_of(path)
                    self.assertNotIn("import random", source)

    def test_nothing_opens_a_socket(self) -> None:
        for package in LAYERS:
            for path in package_modules(package):
                with self.subTest(module=f"{package}/{path.name}"):
                    source = source_of(path)
                    self.assertNotIn("import socket", source)
                    self.assertNotIn("urllib", source)


class ExampleTests(unittest.TestCase):
    def test_the_examples_load_and_validate(self) -> None:
        from campanile.cli.loader import load_workflows
        from campanile.dsl.validate import validate

        examples = sorted((PROJECT_ROOT / "examples").glob("*.py"))
        self.assertTrue(examples, "no examples found")
        for path in examples:
            with self.subTest(example=path.name):
                workflows = load_workflows(path)
                self.assertTrue(workflows)
                for workflow in workflows.values():
                    errors = [issue for issue in validate(workflow) if issue.is_error]
                    self.assertEqual(errors, [], f"{path.name}: {errors}")

    def test_the_examples_run(self) -> None:
        from campanile.cli.loader import load_workflows
        from campanile.runtime.engine import Engine
        from campanile.util.clock import ManualClock

        for path in sorted((PROJECT_ROOT / "examples").glob("*.py")):
            for name, workflow in load_workflows(path).items():
                with self.subTest(example=path.name, workflow=name):
                    result = Engine(clock=ManualClock(0.0)).run(
                        workflow, _placeholder_params(workflow)
                    )
                    self.assertTrue(result.state.is_terminal)


def _placeholder_params(workflow) -> dict[str, object]:
    """Fill in a stand-in for each required parameter.

    An example is allowed to require parameters; this test only cares that the
    workflow runs to a terminal state, not what it computes.
    """

    placeholders = {
        "string": "placeholder",
        "integer": 1,
        "number": 1.0,
        "boolean": False,
        "list": [],
        "object": {},
        "any": None,
    }
    supplied: dict[str, object] = {}
    for spec in workflow.params:
        if not spec.required:
            continue
        supplied[spec.name] = spec.choices[0] if spec.choices else placeholders[spec.type]
    return supplied


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
