# Architecture

campanile is nine packages stacked in one direction. Each imports only from the
ones beneath it, and `tests/test_packaging.py` fails the build if that stops
being true.

```
cli        argument parsing, workflow loading, output
report     summaries, timelines, metrics, exports
runtime    the scheduling loop, executors, resources, retries
store      events, the append-only log, replay, queries
dsl        the builder, the decorator registry, validation
expr       the edge-condition language
schedule   cron, intervals, calendars, timetables
model      task specs, workflows, policies, state machines
util       clocks, durations, ordered collections, graphs, canonical JSON
```

`util` and `model` are pure values. `expr` and `schedule` are pure functions
over them. Nothing below `cli` reads the wall clock, opens a socket, or touches
the filesystem except through a store it was handed.

## The scheduling loop

One `Scheduler` runs one workflow once. The loop is five steps:

1. **Release retries.** Any task in `RETRY_WAIT` whose backoff has elapsed on
   the run's clock becomes `READY` again.
2. **Resolve pending tasks.** For each `PENDING` task, ask its trigger rule
   about the effective states of its incoming edges. The answer is `RUN`
   (become `READY`), `WAIT` (leave it), `SKIP` or `UPSTREAM_FAILED` (terminal).
   This repeats to a fixpoint, because resolving one task can resolve another.
3. **Admit.** Order the `READY` tasks by priority, then declaration order, then
   name; walk the list admitting each one whose resource request fits and whose
   admission stays under the parallelism cap. Admission is greedy, so a small
   task never waits behind a large one it could have run alongside.
4. **Run the wave.** Execute the admitted tasks through the executor, recording
   every outcome as an event, then release their reservations.
5. **Wait.** If nothing is ready but something is waiting to retry, move the
   clock to the earliest retry and go round again.

The loop ends when no task is ready and none is waiting. Because the graph is
acyclic and every task reaches a terminal state, that always happens; a
`MAX_ITERATIONS` guard turns a bug in the loop into an error rather than a hang.

### Waves, not threads

The default executor runs task bodies on the calling thread, one at a time.
Concurrency limits are therefore *admission* limits: a pool with one slot means
one task per wave, and the tasks that did not fit produce `resource_blocked`
events and are reconsidered next time round.

That keeps a run reproducible. Real overlapping execution would make the
interleaving depend on how fast each body happened to be, and the event log
would stop being a function of the definition.

## Effective edge states

A trigger rule does not look at upstream task states directly. It looks at the
*effective* state of each incoming edge, which is the upstream's state with one
adjustment: if the upstream succeeded and the edge carries a condition, the
condition decides.

| upstream state | condition | effective state |
| --- | --- | --- |
| `SUCCEEDED` | none | `SUCCEEDED` |
| `SUCCEEDED` | true | `SUCCEEDED` |
| `SUCCEEDED` | false | `SKIPPED` |
| `SUCCEEDED` | raised | `UPSTREAM_FAILED` |
| anything else | not evaluated | unchanged |

Conditions are only evaluated for successful upstreams: a condition reading the
result of a task that failed has nothing to read. A condition that *raises* is a
real fault, not "the branch was not taken", so it blocks the downstream rather
than skipping it.

## Failure policies

| policy | effect |
| --- | --- |
| `continue` | Descendants of the failure become `UPSTREAM_FAILED`; unrelated branches keep running. The run ends `failed`. |
| `fail_fast` | Nothing new starts, including the rest of the current wave. Tasks already running finish; everything else is cancelled. The run still ends `failed`. |
| `isolate` | Descendants are blocked as with `continue`, but the run itself reports `succeeded`. |

## The event log is the truth

Every state change is written to the log *before* it is believed. Nothing else
is authoritative: the scheduler's in-memory state is a cache, reports are folds
over the log, and `campanile.store.replay` reconstructs the whole run from the
events alone.

Replay is strict. A `task_succeeded` for a task that never started, a second
terminal event for one task, or a gap in the sequence numbers means the log is
corrupt and says so, rather than producing a plausible-looking lie.

That strictness is what lets `RunResult.snapshot()` be used as a self-check:
`tests/test_determinism.py` asserts the replayed picture matches the one the
scheduler had while it was writing it.

## Determinism, concretely

Five rules, each enforced somewhere:

* **No wall-clock reads.** `campanile.util.clock` is the only module that imports
  `time`, plus the scheduler's one sleep for real-clock runs.
  `tests/test_packaging.py` checks this with an AST scan.
* **No hash-order iteration.** `OrderedSet` and plain dicts everywhere;
  `topological_sort` breaks ties by declaration order.
* **No randomness.** Retry jitter is a hash of the task name and attempt
  number, not a random draw.
* **Canonical encoding.** Every event line is JSON with sorted keys and compact
  separators, so two runs produce identical bytes.
* **Stable ids.** Run ids are derived from the workflow name, the logical time
  and a counter, not from a UUID.

## Extension points

* **Executors** implement `run(invocation) -> Completion`. `RecordingExecutor`
  wraps one and remembers every call, which is what most tests assert against.
* **Stores** implement `create`, `log`, `has`, `run_ids` and `flush`.
  `InMemoryStore` and `FileStore` ship; both are exercised by the same tests.
* **Triggers** implement `next_after(timestamp)` and `describe()`. Cron,
  interval, one-shot and never ship.
