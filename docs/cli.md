# Command line

```
campanile <command> [options]
```

Every command returns an exit status rather than printing a traceback:

| status | meaning |
| --- | --- |
| 0 | success |
| 1 | the run itself failed |
| 2 | usage error |
| 3 | the workflow definition is wrong |
| 4 | the store could not be read or written |

## Working with a definition

A definition file is an ordinary Python module. It may bind a `Workflow`,
a `WorkflowBuilder`, or a non-empty `TaskRegistry`; all three are found. When a
file holds several workflows, name one with `--workflow`.

```console
$ campanile describe examples/nightly_rollup.py
workflow nightly_rollup
  Rebuild the daily rollup tables
  failure policy: continue
  pools:
    warehouse (capacity 2) -- concurrent warehouse connections
  ...

wave  tasks
----  -----------
   0  extract
   1  deduplicate
```

```console
$ campanile validate examples/nightly_rollup.py
nightly_rollup: 0 error(s), 0 warning(s)

$ campanile plan examples/nightly_rollup.py
5 task(s) in 5 wave(s); widest wave holds 1
```

`validate --strict` turns warnings into errors.

## Running

```console
$ campanile run examples/nightly_rollup.py --param region=eu --store ./runs
run:       nightly-rollup-20260101T000000Z-0001
workflow:  nightly_rollup
state:     succeeded
duration:  0s
...
```

| option | effect |
| --- | --- |
| `--param KEY=VALUE` | a run parameter; repeat for more. Values parse as JSON when they can, otherwise as strings |
| `--store DIR` | record the run as JSONL in `DIR` |
| `--only TASK...` | run these tasks and everything they depend on |
| `--at TIMESTAMP` | start the virtual clock here |
| `--real-time` | use the host clock and actually sleep through retry backoff |
| `--json` | emit JSON instead of a report |
| `-v` | also show skipped tasks and evaluated conditions |

Without `--store`, the run is kept in memory and discarded when the process
exits. With one, each invocation picks a run id the directory does not already
hold, so repeated runs accumulate rather than colliding.

## Reading history

```console
$ campanile runs --store ./runs --sort duration --desc --limit 5
$ campanile show last --store ./runs
$ campanile events <run-id> --store ./runs --kind task_failed
$ campanile timeline last --store ./runs --file examples/nightly_rollup.py
$ campanile metrics --store ./runs --top 3
$ campanile export --store ./runs --format tasks-csv > tasks.csv
```

`show` and `timeline` accept `last` (the default) instead of a run id.
`timeline --file` adds the critical path, which needs the definition to know the
dependency edges.

Export formats are `json`, `csv` (one row per run) and `tasks-csv` (one row per
task per run). All three are byte-stable, so a diff between two exports shows
real differences only.

## Explaining a schedule

```console
$ campanile schedule "0 2 * * mon-fri" --count 3 --after 2026-03-11T00:00:00Z
cron 0 2 * * mon-fri
field    matches
-------  ---------
minute   0
hour     2
day      *
month    *
weekday  1,2,3,4,5

next 3 fire(s) after 2026-03-11T00:00:00Z:
  2026-03-11T02:00:00Z  (+2h)
  2026-03-12T02:00:00Z  (+1d)
  2026-03-13T02:00:00Z  (+1d)
```

The expression may be a cron string, a macro (`@daily`, `@hourly`, …), an
interval (`every 30s`, `90s`) or `never`.
