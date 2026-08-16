# campanile

A deterministic workflow orchestration engine written in pure Python, with no
runtime dependencies.

You describe a workflow as a graph of tasks. The engine works out what can run,
runs it, retries what fails, respects the resource limits you declared, gates
branches on conditions, and writes an append-only event log describing exactly
what happened.

The constraint that shapes the design is **determinism**: nothing below the CLI
reads the wall clock, opens a socket, or iterates a hash set. Hand the engine a
manual clock and the same workflow produces the same events, in the same order,
with the same timestamps, every single time — which is what makes a run
something you can diff, replay and reason about after the fact.

```python
from campanile import Engine, WorkflowBuilder

builder = WorkflowBuilder("nightly").pool("warehouse", 2)

extract = builder.task("extract", extract_fn, retry=3, resources="warehouse")
eu = builder.task("transform_eu", transform_eu, resources="warehouse")
us = builder.task("transform_us", transform_us, resources="warehouse")
publish = builder.task("publish", publish_fn)

extract >> [eu, us] >> publish

result = Engine().run(builder.build())
assert result.succeeded
print(result.value("publish"))
```

## What it does

**Dependency scheduling.** Tasks run when their upstreams allow it. Seven
trigger rules — `all_success`, `all_done`, `none_failed`, `any_success`,
`any_failed`, `all_failed`, `always` — cover the usual "run this whatever
happened" and "run this only if something broke" shapes.

**Conditional edges.** An edge can carry an expression evaluated against the
run's parameters and the results so far:

```python
aggregate.when("results.aggregate.rows > 0 and not params.dry_run") >> publish
```

A false condition skips the branch; a *broken* condition blocks it, because a
typo is not the same as "the branch was not taken". Conditions are validated
before the run starts — including whether the task they read is actually an
ancestor of the task they gate.

**Retries with real backoff.** Fixed, linear or exponential, clamped by
`max_delay`, optionally jittered — deterministically, from a hash of the task
name and attempt number, so a retry schedule is reproducible.

**Resource pools.** Named integer capacities that tasks hold while they run.
A pool with one slot serialises everything that touches it; a task asking for
more than a pool has is rejected at validation time rather than waiting for ever.

**Failure policies.** `continue` blocks only the descendants of a failure,
`fail_fast` stops the run mid-wave, `isolate` lets one branch break without
failing the run.

**An event log you can trust.** Every state change is written before it is
believed. `campanile.store.replay` reconstructs the entire run from the events
alone, which is how `campanile show` works on a run this process never executed.

**Scheduling.** A cron parser with the traditional day-of-month / day-of-week
semantics, interval and one-shot triggers, business calendars with holidays and
opening hours, and a timetable that merges many of them into one ordered stream
of fires. All UTC, all pure arithmetic — no timezone database, no host locale.

## Two ways to declare a workflow

The fluent builder, where `>>` connects tasks and accepts lists on either side:

```python
builder = WorkflowBuilder("nightly").param("region", "string", required=True)
extract = builder.task("extract", extract_fn)
[eu, us] >> builder.task("publish", publish_fn)
```

Or the decorator registry, which keeps the declaration next to the code:

```python
registry = TaskRegistry("nightly")

@registry.task(retry=3, timeout="5m", resources="warehouse")
def extract(ctx):
    return {"rows": fetch(ctx.param("region"))}

@registry.task(after=["extract"])
def load(ctx):
    return ctx.result("extract")["rows"]

flow = registry.build()
```

Both produce a plain `Workflow`; neither is privileged.

## Command line

```console
$ campanile validate examples/nightly_rollup.py
nightly_rollup: 0 error(s), 0 warning(s)

$ campanile run examples/nightly_rollup.py --param region=eu --store ./runs
$ campanile show last --store ./runs
$ campanile timeline last --store ./runs --file examples/nightly_rollup.py
2026-03-01T00:00:00Z .. 2026-03-01T00:00:09Z (9.5s)
task     timeline                              duration  attempts
-------  ------------------------------------  --------  --------
extract  ==========                                  2s         1
t_eu              ============================        7s         2
publish                                   ====     500ms         1

$ campanile metrics --store ./runs
runs: 3 (success rate 100%)
task duration: n=13 mean=2.307s p50=0s max=30s
retries per task: extract=2
```

See [docs/cli.md](docs/cli.md) for the full surface.

## Layout

```
campanile/util       clocks, durations, ordered collections, graphs, canonical JSON
campanile/model      task specs, workflows, policies, state machines
campanile/expr       the edge-condition language
campanile/schedule   cron, intervals, calendars, timetables
campanile/dsl        the builder, the decorator registry, validation
campanile/store      events, the append-only log, replay, queries
campanile/runtime    the scheduling loop, executors, resources, retries
campanile/report     summaries, timelines, metrics, exports
campanile/cli        argument parsing, workflow loading, output
```

Each layer imports only from the ones above it in that list, and the test suite
fails if that stops being true.

## Documentation

* [docs/architecture.md](docs/architecture.md) — the scheduling loop, effective
  edge states, and how determinism is enforced
* [docs/expressions.md](docs/expressions.md) — the condition language
* [docs/cli.md](docs/cli.md) — every command

## Development

Python 3.10 or newer. No dependencies, at runtime or to run the tests.

```console
$ python -m unittest discover -s tests -t .
Ran 690 tests in 2.6s

OK
```

The suite is plain `unittest`, so `pytest tests` works too if you prefer it.
