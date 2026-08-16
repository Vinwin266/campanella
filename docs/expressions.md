# Edge conditions

An edge may carry a condition. When the upstream task succeeds, the condition is
evaluated; if it is false, that edge contributes `skipped` instead of
`succeeded`, and the downstream task's trigger rule decides what that means.

```python
extract >> transform
transform.when("results.transform.rows > 0") >> publish
```

The language is small on purpose. It exists so a workflow can branch on what a
task produced, not so anyone can compute in it: there is no assignment, no
loops, no I/O and no clock.

## Bindings

Four names are in scope, and nothing else:

| name | contents |
| --- | --- |
| `params` | the run's bound parameters |
| `results` | the values returned by tasks that have succeeded so far |
| `run` | `{"id": ..., "workflow": ...}` |
| `task` | `{"name": <downstream>, "upstream": <upstream>}` |

Reading anything else is a validation error, caught before the run starts.

## Grammar

```
or          a or b
and         a and b
not         not a
comparison  ==  !=  <  <=  >  >=  in  "not in"
coalesce    a ?? b
additive    +  -
multiply    *  /  //  %
unary       -a
postfix     a.b   a[k]   f(x)
primary     123  1.5  "text"  'text'  true  false  null  [a, b]  (a)
```

## Semantics worth knowing

**Truthiness.** `null`, `false`, `0`, `""`, `[]` and `{}` are falsy; everything
else is truthy.

**`and` / `or` return values, not booleans.** They short-circuit and yield
whichever operand decided the result, so `params.region or "eu"` is a default.

**Booleans are not numbers.** `true == 1` is *false*. Comparing a flag to a
count is a mistake, and agreeing with it would hide one.

**Ordering needs matching types.** `<`, `<=`, `>`, `>=` accept two numbers or
two strings. Comparing a number to a string is an error, not a silent false.

**Missing keys are errors.** `results.ghost` raises rather than yielding null.
For a defensive read use `get(results, "ghost", 0)` or `??`.

**`+` concatenates.** Two strings or two lists join; anything else must be
numeric.

## Functions

All pure, all with declared arities.

| group | functions |
| --- | --- |
| collections | `len`, `sorted`, `sum`, `min`, `max`, `count`, `any`, `all`, `contains` |
| objects | `keys`, `values`, `has`, `get` |
| strings | `lower`, `upper`, `trim`, `starts_with`, `ends_with`, `replace`, `split`, `join` |
| conversion | `int`, `number`, `string`, `bool`, `is_null`, `coalesce` |
| numbers | `abs`, `round` |
| durations | `duration` |

`duration("90s")` returns `90.0`, which lets a condition compare against a
budget written the same way a timeout is.

## What the validator checks

Before a run starts, every condition is compiled and inspected:

* it parses;
* every variable it reads is one of the four bindings;
* every function it calls exists;
* every `results.X` it reads names a real task, and one that is an *ancestor* of
  the downstream — reading a task that has not run yet is a definition bug, not
  a runtime surprise;
* every `params.X` it reads is a declared parameter.

```
error: nightly.aggregate->publish: condition reads results of 'archive', which
is not an ancestor of 'publish' and so has no result when the edge is evaluated
[condition_forward_reference]
```

## Examples

```python
# only publish a non-empty rollup, and only outside a dry run
"results.aggregate.rows > 0 and not params.dry_run"

# fan out per region, gated on the parameter
"params.region in ['eu', 'us']"

# tolerate a missing optional result
"get(results, 'enrich', 0) > 0"

# compare against a duration written as a string
"results.extract.seconds < duration('5m')"
```
