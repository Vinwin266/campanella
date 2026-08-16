"""A ledger reconciliation, written with the decorator registry.

Three regional feeds are pulled in parallel against a small connection pool,
compared against the ledger, and either signed off or escalated. The escalation
branch uses ``all_done`` so that it runs whether or not the comparison did.

    campanile validate examples/reconcile.py
    campanile run examples/reconcile.py --param cutoff=2026-03-11 -v
"""

from campanile import TaskRegistry

#: Pretend feed contents, keyed by region.
FEEDS = {
    "eu": [("a", 100), ("b", 250), ("c", 75)],
    "us": [("a", 100), ("b", 250), ("d", 400)],
    "apac": [("e", 20)],
}

#: What the ledger thinks the totals are.
LEDGER = {"a": 100, "b": 250, "c": 75, "d": 400, "e": 25}

registry = TaskRegistry(
    "reconcile",
    description="Compare regional feeds against the ledger",
    max_parallelism=2,
)
registry.pool("feeds", 2, "concurrent feed connections")
registry.param("cutoff", "string", required=True)
registry.param("tolerance", "integer", default=0)


def _pull(region):
    """Read one region's feed into a total-per-account mapping."""

    totals = {}
    for account, amount in FEEDS.get(region, ()):
        totals[account] = totals.get(account, 0) + amount
    return totals


@registry.task(resources="feeds", retry=2, timeout="2m", tags=["source"])
def pull_eu(ctx):
    """Pull the European feed."""

    return _pull("eu")


@registry.task(resources="feeds", retry=2, timeout="2m", tags=["source"])
def pull_us(ctx):
    """Pull the American feed."""

    return _pull("us")


@registry.task(resources="feeds", retry=2, timeout="2m", tags=["source"])
def pull_apac(ctx):
    """Pull the Asia-Pacific feed."""

    return _pull("apac")


@registry.task(after=["pull_eu", "pull_us", "pull_apac"], tags=["transform"])
def merge(ctx):
    """Combine the three feeds into one set of totals."""

    combined = {}
    for source in ("pull_eu", "pull_us", "pull_apac"):
        for account, amount in ctx.result(source).items():
            combined[account] = max(combined.get(account, 0), amount)
    ctx.note("merged", accounts=len(combined))
    return combined


@registry.task(after=["merge"], tags=["transform"])
def compare(ctx):
    """Find the accounts whose feed total disagrees with the ledger."""

    totals = ctx.result("merge")
    tolerance = ctx.param("tolerance", 0)
    breaks = {
        account: LEDGER.get(account, 0) - amount
        for account, amount in sorted(totals.items())
        if abs(LEDGER.get(account, 0) - amount) > tolerance
    }
    ctx.note("compared", breaks=len(breaks), cutoff=ctx.param("cutoff"))
    return {"breaks": breaks, "count": len(breaks), "checked": len(totals)}


@registry.task(after=["compare"], condition="results.compare.count == 0", tags=["sink"])
def sign_off(ctx):
    """Mark the day reconciled."""

    return {"cutoff": ctx.param("cutoff"), "signed": True}


@registry.task(after=["compare"], condition="results.compare.count > 0", tags=["sink"])
def escalate(ctx):
    """Raise the breaks with whoever owns them."""

    breaks = ctx.result("compare")["breaks"]
    return {"escalated": sorted(breaks), "count": len(breaks)}


@registry.task(after=["sign_off", "escalate"], trigger_rule="all_done", tags=["sink"])
def record(ctx):
    """Write the outcome down, whichever branch was taken."""

    return {
        "signed": ctx.has_result("sign_off"),
        "escalated": ctx.has_result("escalate"),
        "cutoff": ctx.param("cutoff"),
    }


reconcile = registry.build()
