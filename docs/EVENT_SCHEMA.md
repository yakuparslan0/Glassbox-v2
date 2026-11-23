# GlassBox event schema (Phase 1)

Source of truth: `backend/glassbox/schemas/events.py` (Pydantic v2). The runner emits plain dicts as
NDJSON (one JSON object per line, ASCII-escaped UTF-8); the API process validates them against these models
(tests validate every event of every example). TypeScript types are generated from the same models later
(`scripts/gen_types.py`, Phase 2).

## Envelope

```json
{"v": 1, "seq": 1042, "t": 18234511, "type": "line", "step": 8, "...": "..."}
```

* `seq` gap-free counter per run (starts at 1). `t` nanoseconds since runner start (monotonic).
* `step` is the time-travel unit. It increases by one on **call, line, return and exception** events only
  (boundary call/return pairs count: they are call/return events). Other events carry the current step.
* `trunc: true` on any event means the 64 KB per-event limit forced the runner to drop `heap` entries and
  replace values by `<value too large>` placeholders.

## Values

| form | meaning |
|---|---|
| `{"k":"prim","t":"int","r":"42"}` | primitive, `r` = repr (strings cut at 200 chars → `"trunc":true`) |
| `{"ref":"o12"}` | reference to a heap entry; labels `o1, o2 …` are stable per object for the whole run |
| `{"k":"func"\|"module"\|"other","t":…,"r":…}` | non-data values (never calls user code) |

Heap entries (`heap` map, label → entry): `seq` (list, tuple, set, frozenset, deque; `items`), `map`
(dict; `pairs`), `obj` (user objects; `attrs` from `__dict__`/`__slots__`). Limits: depth 3, 50 items per
collection, 2000 nodes per snapshot; beyond that a `{"k":"other",…,"trunc":true}` placeholder is used,
except for an object whose entry the client already has (sent earlier or in the same event): that stays `{"ref":"oN"}`.
User `__repr__`/`__str__`/`__getattr__`/`__len__`/`__iter__` are never invoked.

## Event types

| type | fields (besides envelope) |
|---|---|
| `run_started` | `python, tracer, seed, limits, filter, capabilities{whatif,threads,asyncio,rlimit}` |
| `import` | `step, module, lib` (`user`\|`stdlib`\|`third_party`); first import of a module from user code |
| `call` (user) | `step, frame, parent, kind:"user", func, qualname, file, line, call_line, args, [globals_delta], [heap], [generator]` |
| `call` (boundary) | `step, frame, parent, kind:"boundary", lib, module, func, call_line, [args_summary], [args], [globals_delta], [heap]` |
| `line` | `step, frame, line, delta{set,del}, [globals_delta], [heap]` |
| `return` (user) | `step, frame, value, dur_ns, [unwound], [globals_delta], [heap]` |
| `return` (boundary) | `step, frame, kind:"boundary", value_summary, dur_ns, [raised], [synthetic], [globals_delta], [heap]` |
| `exception` | `step, frame, line, exc_type, message, handled, [globals_delta], [heap]` |
| `output` | `step, stream, text` (stdout/stderr, chunks of ≤ 8 KB) |
| `keyframe` | `step, stack[{frame,func,line,locals}], globals, heap` |
| `snapshot` | debug only (`full_snapshots`): ground truth from the live frames after a step |
| `whatif_applied` | `step, frame, name, old, new, [heap]` |
| `limit_exceeded` | `step, limit` (`max_steps`\|`timeout`\|`memory`\|`output`\|`event_bytes`), `detail` |
| `run_finished` | `status, exit_code, steps, wall_ms, line_hits, func_stats, executed_lines, repeat_calls, [detail], [blocked], [whatif_unapplied]` |

`status`: `ok` (also for `sys.exit(n)`; see `exit_code`), `exception` (uncaught exception or syntax error),
`limit`, `killed` (watchdog or runner crash; synthesized by the parent), `blocked` (sandbox denied an
operation and the program died of it, or a What-If literal was rejected). Exit codes: ok 0, exception 1,
limit 2, blocked 3, killed 4, runner crash 70.

## Frames and state reconstruction

* `frame` ids are unique within a run. Every call is a new frame, including each activation of a recursive
  function. **Generators**: every resume is a `call` (new frame id, `generator: true`, `args` = all locals)
  and every suspension (yield) or completion is a `return`.
* `parent` is the nearest enclosing stack entry (user frame or boundary) at call time; a user function called
  back from a library appears as a child of the boundary.
* `line` events are emitted BEFORE the line runs. `delta` therefore reports the effects of the previous
  line(s) of that frame: `delta.set` holds locals whose *deep fingerprint* changed (an in-place
  `lst.append(x)` shows up as a changed local whose `ref` is unchanged, plus the updated heap entry);
  `delta.del` lists removed names. Hidden names (`__dunder__`, `.0`) are skipped.
* The entry module's locals ARE the globals. Frame `1` is the entry module: its `delta` is the globals delta.
  For every other frame, `globals_delta` (also present on call/return/exception/boundary events) carries
  changes of the entry globals. Imported user modules are ordinary frames whose locals are their module
  namespace.
* A client keeps a stack of user frames, the globals and a heap map. `call(user)` pushes (locals = `args`),
  `return(user)` pops, `heap` entries are merged (latest wins). The state "after step k" includes the effect
  of event k (a `return` has already popped its frame).
* `keyframe` (every `KEYFRAME_INTERVAL`, default 200 steps, emitted right after the step-k event) is built
  from the recorder's mirror of what was reported, so *nearest keyframe + following deltas* equals
  *all deltas from the start* by construction. Its `heap` contains every entry reachable from the state; after
  a keyframe the runner re-sends any entry that is not part of it.
* Known granularity limit: locals of frames other than the one that produced an event are only refreshed at
  that frame's next event (changes made within a single source line are visible from the next event of the
  frame). Globals are exact at every structural event.
* `exception` events are emitted when the exception is resolved: `handled: true` as soon as an `except`
  or `finally` handler starts, `handled: false` when the frame unwinds (followed by a `return` with
  `unwound: true`). The same exception object re-raised in the same frame is not reported twice.
  `StopIteration`, `StopAsyncIteration` and `GeneratorExit` are not reported.

## Boundaries (noise filter)

Code objects are classified once: `user` (files in the run's working directory), `stdlib`, `third_party`,
or the runner itself (never traced). Only user code is traced line by line. A call from the top user frame into
library code is ONE `call(kind:"boundary")` / `return(kind:"boundary")` pair:

* Python library function called directly by user code: `args_summary` lists its arguments; `value_summary`
  summarizes the return value (`boundaries_verbose` also serializes `args` fully).
* C function of a non-`builtins` module (`time.sleep`, `os.write`, `math.sqrt`): neither backend can see its
  arguments or result, so `args_summary`/`value_summary` are `null`.
* Never boundaries: callables of `builtins` and methods of builtin types (`len`, `print`, `list.append`),
  class instantiation, and non-user generators/coroutines. A user function called back from a library
  (`sorted(key=f)`, `json.dumps(default=f)`) is traced normally.
* `raised: true` marks a boundary left by an exception; `synthetic: true` marks one closed by the recorder
  because its end was never observed.
* Filter levels (`filter`): `user_only` (no boundaries), `boundaries` (default), `boundaries_verbose`.

## Debug: `snapshot`

With `full_snapshots: true` the runner emits, after every step event, `{stack, frame, locals, globals, heap}`
taken from the live frames (the event's frame, the globals and the heap reachable from them). The test suite
compares it with the state rebuilt from keyframes and deltas after every single step.

## Phase 2 additions
- `run_started.tracer` may also be `profile_time` / `profile_memory`: untraced measurement passes (`job.profile`).
  They emit no `call`/`line`/`return` events; their result is `run_finished.profile`
  (`{"kind":"time","functions":[...]}` or `{"kind":"memory","peak_kb":..,"live_lines":[...]}`).
- TypeScript types and JSON Schema are generated (`make schema`): `frontend/src/types/generated.ts`,
  `docs/schema/glassbox.schema.json`.
