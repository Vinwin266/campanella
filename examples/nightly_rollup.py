"""A nightly warehouse rollup, written with the fluent builder.

Run it with::

    campanile describe examples/nightly_rollup.py
    campanile run examples/nightly_rollup.py --param region=eu --store /tmp/runs
    campanile show last --store /tmp/runs

Nothing here touches a real warehouse -- the task bodies are arithmetic -- but
the shape is the real one: a pool that serialises the expensive steps, retries
on the flaky extract, a conditional publish, and a notification that runs
whatever happened.
"""

from campanile import WorkflowBuilder

#: How many rows each region pretends to have.
ROW_COUNTS = {"eu": 1_200, "us": 4_800, "apac": 0}


def extract(ctx):
    """Pull the day's rows for the requested region."""

    region = ctx.param("region", "eu")
    rows = ROW_COUNTS.get(region, 0)
    ctx.note("extracted", region=region, rows=rows)
    if rows == 0 and ctx.attempt < ctx.max_attempts:
        raise RuntimeError(f"no rows for region {region!r} yet")
    return {"region": region, "rows": rows}


def deduplicate(ctx):
    """Drop the rows the previous run already handled."""

    source = ctx.result("extract")
    kept = int(source["rows"] * 0.9)
    ctx.note("deduplicated", kept=kept, dropped=source["rows"] - kept)
    return {"rows": kept}


def aggregate(ctx):
    """Roll the deduplicated rows up into daily totals."""

    rows = ctx.result("deduplicate")["rows"]
    return {"rows": rows, "buckets": max(1, rows // 100)}


def publish(ctx):
    """Swap the new tables in."""

    buckets = ctx.result("aggregate")["buckets"]
    ctx.note("published", buckets=buckets)
    return {"buckets": buckets, "published": True}


def notify(ctx):
    """Tell someone what happened, whether or not the publish ran."""

    published = ctx.result("publish", None)
    return {"published": bool(published), "region": ctx.param("region", "eu")}


builder = (
    WorkflowBuilder("nightly_rollup", description="Rebuild the daily rollup tables")
    .param("region", "string", default="eu", choices=["eu", "us", "apac"])
    .param("dry_run", "boolean", default=False)
    .pool("warehouse", 2, "concurrent warehouse connections")
)

_extract = builder.task(
    "extract",
    extract,
    retry={"max_attempts": 3, "delay": "10s", "backoff": "exponential"},
    timeout="5m",
    resources="warehouse",
    tags=["source"],
)
_dedupe = builder.task("deduplicate", deduplicate, resources="warehouse", tags=["transform"])
_aggregate = builder.task("aggregate", aggregate, resources={"warehouse": 2}, tags=["transform"])
_publish = builder.task("publish", publish, priority=-1, tags=["sink"])
_notify = builder.task("notify", notify, trigger_rule="all_done", tags=["sink"])

_extract >> _dedupe >> _aggregate
_aggregate.when("results.aggregate.rows > 0 and not params.dry_run") >> _publish
builder.connect("aggregate", "notify")
builder.connect("publish", "notify")

nightly_rollup = builder.build()
