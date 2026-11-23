# Design decisions

Decisions the master spec left open (one paragraph each). Newest phase last within a topic.

## Phase 1

**Python and environment.** The dev interpreter is Python 3.14.5 (spec: >= 3.13). The virtualenv lives outside
the OneDrive-synced project tree (`%USERPROFILE%\.venvs\glassbox`) so that OneDrive does not lock/sync thousands
of small files; the Makefile takes `PYTHON=` so any interpreter works.

**`sys.monitoring` facts verified on 3.14.** `DISABLE` may only be returned for LINE and CALL (and PY_START /
PY_RETURN / PY_YIELD / PY_RESUME / PY_THROW); RAISE, EXCEPTION_HANDLED, PY_UNWIND and C_RETURN/C_RAISE raise
`ValueError: Cannot disable ... events`. So non-user locations are disabled for LINE/CALL only. PY_START /
PY_RETURN stay enabled for non-user code on purpose: they open and close Python-level library boundaries, and a
disabled location would only report the first call. Callback arities: PY_THROW/PY_UNWIND/RAISE/
EXCEPTION_HANDLED get `(code, offset, exception)`, CALL/C_RETURN/C_RAISE `(code, offset, callable, arg0)`,
JUMP `(code, offset, destination_offset)`.

**Backend parity.** `sys.settrace` reports a backward jump that stays on the same line (one-line loops,
comprehensions) as a `line` event, `LINE` does not; the monitoring backend therefore also registers JUMP and
re-creates those events. A settrace `return` event does not say whether a frame returned or is unwinding, so
the opcode at `f_lasti` decides (RETURN_VALUE/YIELD_VALUE = normal exit). Exceptions are tracked by one shared
state machine in the recorder (pending exception per frame; "last seen exception" for frames that unwind after
their `finally` handler already reported it), so both backends emit the same `exception` events.

**Boundaries.** `builtins` callables and builtin-type methods are never boundaries (they would drown the trace);
neither are class instantiations or non-user generators/coroutines. C boundaries carry no argument/return
summary because `CALL` only exposes the first argument and no backend exposes the result. A Python library
function counts as a boundary when its frame's caller is the top user frame (same rule in both backends, so it
also covers `map(json.dumps, xs)`-style C-mediated calls identically).

**Generators.** Each resume (PY_START/PY_RESUME/PY_THROW) is a `call` with a new frame id and each suspension a
`return`, in both backends; there is no separate "yield" flag because settrace cannot distinguish it cheaply.

**State model.** Module frames use `f_globals` as their locals mapping (inside an inlined module-level
comprehension `f_locals` hides every global). `globals_delta` is also sent on call and boundary events so that
globals are exact at every structural event. Keyframes are built from the recorder's mirror (no extra
serialization, deterministic); the optional `snapshot` debug event is the independent ground truth used by tests.
The serializer keeps a strong reference to every object it labels so `id()`-based labels never get reused; this
changes GC/`__del__` timing of traced objects slightly and bounds memory by the step limit.

**Limits and exit.** Step/output/event-byte limits emit `limit_exceeded` + `run_finished` and terminate the
runner with `os._exit` (clean for the protocol, user `finally`/`atexit` blocks do not run). `sys.exit(n)` is
`status: ok` with `exit_code: n`. A rejected What-If literal ends the run as `blocked` before any user code runs.
After normal completion user `atexit` handlers run untraced and user threads are not waited for.

**What-If.** `frame_depth` indexes the user frames of the stack (0 = entry module). An override is applied right
after the event of its target step; an override whose frame does not exist at that step is skipped and counted in
`run_finished.whatif_unapplied`.

**Output ordering.** `sys.stdout/stderr` writes become `output` events immediately. Raw fd 1/2 writes
(`os.write`) go to capture files (no reader threads) which are drained after every boundary return and at the
end, so they appear right after the call that produced them.

**Streaming.** A helper thread flushes the protocol buffer every 50 ms, so events stream live and are not lost
if the main thread hangs inside C code (the watchdog then kills the process).

**Timing fields.** `func_stats[*].self_ns/total_ns` and `dur_ns` come from the traced run: they are inflated by
tracing and non-deterministic (excluded from determinism/parity comparisons); `steps`/`calls`/`line_hits` are the
exact metrics. Real timings come from the untraced profile pass (Phase 2).

**Audit hook.** Beyond the spec list it also blocks `os.fork*`, `os.spawn*`/`posix_spawn`, DNS lookups
(`socket.getaddrinfo`, `gethostbyname`) and `os.remove/rmdir/mkdir/rename` outside the working directory.
Loopback is not exempt, so asyncio on Windows (its self-pipe is a loopback socket pair) needs `allow_network`;
asyncio is out of scope in v1 anyway.

**Process management.** POSIX: new session + `killpg(SIGKILL)`. Windows: `CREATE_NEW_PROCESS_GROUP` and
`taskkill /T /F` (the venv `python.exe` is a launcher that starts the real interpreter as a child, so killing
the tree matters). Windows has no `resource`, so `capabilities.rlimit` is `false` there. If the runner dies
without `run_finished`, the parent synthesizes `limit_exceeded(timeout)` (if its watchdog fired) and
`run_finished(status: killed)`.

**Deferred.** The `hot_loop` finding (5000 identical-state repeats of a line) is derived from the trace and is
produced by `analysis/dynamic_profile.py` in Phase 2. Docker backend, API, config and the other analysis
modules are Phase 2+.

## Phase 2

**Spec storage.** The full original spec lives in `docs/SPEC.md`; `CLAUDE.md` imports it with `@docs/SPEC.md`
(loaded every session) instead of pasting ~12k tokens into the file itself.

**Profile pass.** `job.profile = "time" | "memory"` runs the program UNTRACED in the runner (`runner/profiler.py`):
cProfile and tracemalloc run in two SEPARATE processes because each distorts the other. The result travels in
`run_finished.profile`. tracemalloc reports what is still allocated at the end of the run per line, plus the
process-wide traced peak; the report labels it that way (per-line peaks are not available from tracemalloc).

**Finding semantics.** `static_dead` reports direct facts (never referenced, unused, unreachable) with confidence
`confirmed`/`likely`/`suspect`; `marksweep` adds transitive dead code (call graph by simple name, an
over-approximation that only errs towards "reachable") and upgrades `likely` -> `confirmed` only when the lines also
did not run. Static-dead code that DID run is downgraded to `suspect` (dynamic access). Dynamic findings
(hot_loop, repeated_computation, loop_invariant, mutable_default, alias_mutation) and clones are part of the same
`MarkSweepReport.findings` list; `high_memory` lives in the `ProfileReport`.

**hot_loop** = one line ran N (5000) times in one frame with no local/global/heap change anywhere in that frame since
its counters were last reset (alternating lines of `while True: pass` count). **repeated_computation** is `likely`
(it assumes purity). **mutable_default** needs a static mutable default AND the same object passed in >= 2 calls with
different contents; **alias_mutation** needs one object bound to >= 2 names in a frame, mutated through one name
(attributed from the source line before the event that shows the change) and later read through another.

**Clones.** A statement subtree qualifies with >= 5 statements OR >= 40 nodes (OR, because a 3-statement function with
a loop is already a useful clone); identifiers (except builtins) and constants are normalized; only maximal groups are
reported.

**API.** Status codes: illegal path/duplicate path/missing entry/syntax error 422; too many files or too many bytes 413;
all run slots busy 429 + `Retry-After`; unknown run 404; report/trace requested while running 409. Slots are counted
(not queued) and released BEFORE the final state becomes visible. WebSocket backpressure: events are stored once per
run, each connection holds only a cursor and one batch (<= 200 events, flushed every 50 ms), so a slow client lags
behind its cursor instead of growing server memory; a send blocked for 30 s closes the socket. `?from_seq=N` resumes
after event N. Browser `Origin` headers must be in `GLASSBOX_CORS_ORIGINS` (CORS does not cover WebSockets).
Evicted/expired runs: `/trace` falls back to `GLASSBOX_RUNS_DIR/<run_id>.ndjson` when persistence is on.

**Validation.** `GLASSBOX_VALIDATE_EVENTS`: `strict` (every event; a violation fails the run), `sampled` (default:
every 100th event, logged only), `off`. The test-suite uses `strict`.

**Docker.** `DockerBackend` reuses the subprocess backend (watchdog, synthesized `run_finished`, cleanup) and only
changes argv/env/kill. Paths in a copy of `job.json` are rewritten to the container mounts (`/work`, `/cap`).
`--network none` becomes `bridge` only when the run opts in with `allow_network`. NOT verified end-to-end: Docker is not
installed on the development machine, only the generated command line is unit-tested.

**Deferred.** Export routes (Phase 5), mentor/refactor routes (Phase 4), rootcause and bigo routes (Phase 5).
Docker `API`/frontend images arrive with the Dockerfiles of Phase 5; `docker-compose.yml` only builds the runner image.

## Phase 3

**Stack versions.** Installed the latest stable at install time and committed `frontend/package-lock.json`
(React 19, Vite 8, TypeScript 7, Vitest 5, Tailwind 4, react-resizable-panels 4, xyflow 12). Tailwind 4 is CSS-first, so
there is no `tailwind.config.ts`; theme colours are CSS variables in `src/index.css`.

**Monaco is bundled, not loaded from a CDN** (`lib/monaco.ts`: editor core + Python grammar + editor worker only), so the
app works offline. This makes the main chunk ~3.2 MB (870 KB gzip); `chunkSizeWarningLimit` is raised to 4000 KB in
`vite.config.ts` on purpose, so the build prints no warning for this known size.

**Known audit finding.** `npm audit` reports a DOMPurify advisory pulled in by `monaco-editor` (0.56); `npm audit fix`
does not resolve it without a Monaco upgrade/downgrade. Monaco only sanitizes content produced by this app (hover
texts), but track it.

**State flow.** Events live in plain structures outside React state (`lib/runData.ts`); the zustand stores carry only
counters. WebSocket batches are applied once per animation frame. The Web Worker (`workers/stateWorker.ts`) owns a
`StateBuilder` (nearest keyframe + deltas, sequential cache) and answers latest-wins requests; the main thread keeps a
64-entry LRU of results. A step's state = state after the last event before the next step's event (`endSeqOfStep`).
`lib/stateBuilder.ts` is checked against the backend's `snapshot` ground truth on shared fixtures
(`backend/tests/fixtures/traces/`, regenerate with `python scripts/gen_fixtures.py`).

**Live follow.** The playhead follows the newest step until the user scrubs (any manual move); reaching the end again
by stepping forward re-enables following.

**Timeline sentences** are produced client-side by a small deterministic formatter (`lib/format.ts`); the backend
`mentor/narrator.py` arrives in Phase 4 and will replace it.

**Single-file editor.** The UI submits one file (the entry). Multi-file projects work through the API and CLI.

**Not done in this phase (by design):** What-If pencil, mentor/refactor/lab tabs, presentation mode, export (Phases 4-5).
The Phase 3 acceptance items that need to SEE the UI could not be ticked in the authoring session (no browser tool);
see `docs/MANUAL_CHECKLIST.md`.

## Phase 4

**Providers.** One `OpenAICompatProvider` serves Groq and Gemini (both expose an OpenAI-compatible
`/chat/completions`). Base URLs, models and keys come only from the environment; a provider with any of the three
missing is inactive. The values in `.env.example` come from the providers' docs (base URLs) and model pages (model ids);
the model ids were NOT exercised with a live key.

**KeyRing.** Round-robin per provider; 429 cools the key for `Retry-After` seconds, else 2, 4, 8 ... capped at 120 s;
401/403 disables it for the session; 5xx/timeouts try another key (up to `keys + 2` attempts) without penalising the key.
Exhausted provider -> next provider -> `LlmUnavailable` -> Layer A. An `asyncio.Lock` guards the cursor. Only the last four
characters of a key are ever logged.

**Cache.** In-memory LRU (128) keyed by (sha256 of the messages, model). Retry prompts embed the attempt number so a
retry is never served the same cached bad answer.

**Prompting.** User code / output / digests are embedded in `<<<GB_DATA:{nonce}>>>` blocks with a per-request random nonce;
look-alike delimiters inside the data are defused. The system prompt tells the model to ignore instructions inside data
and to cite only numbers from a MEASURED block. Beyond prompting, `refactor.invented_numbers` rejects answers whose
`why` / `explanation_md` / `expected_effect` contain performance-style claims ("40x", "12 ms", "30 %") whose number was
not measured; such an answer counts as invalid and is retried (max 2 retries).

**Refactor flow.** Findings that autofix can handle never reach the LLM (`source: "autofix"`). Otherwise: JSON ->
pydantic -> edit engine (overlap/range checks, every changed `.py` must `ast.parse`) -> number check; 3 attempts total,
then a clean 502 ("Nothing was changed"). With no usable provider: 503 with an actionable message. `RefactorProposal`
carries `proposed_files` (server-side result of the edits) so clients need no edit engine. Nothing is ever applied
without the user's click, and the UI requires a verification first.

**Verification** (`mentor/verify.py`): `ast.parse` -> traced runs for deterministic step counts -> one discarded warm-up
pair + 5 interleaved UNTRACED timed pairs (the runner's `timing` pass measures only the user code; the runner's own
import events are disabled in measurement passes) -> compare stdout, status, exception type, exit code.
`verified_faster` needs a >= 10 % lower median AND non-overlapping min/max ranges; `verified_equal` = same behaviour and
same step count without a measurable time difference; otherwise `no_measurable_change`; behaviour differences give
`behavior_changed`; syntax errors `broken` (not applicable in the UI).

**Example changed.** `examples/fib_naive.py` now computes `fib(15)` (1,973 calls, ~9.9k steps, still under the 20k step
limit). At `fib(10)` (177 calls, ~70 microseconds) memoization genuinely does not pay off and the honest verdict is
`no_measurable_change`; the example was too small for the "verified faster" story, so the example, not the measurement,
was changed.

**Narration.** `mentor/digest.py` splits a finished trace into scenes (top-level calls, module-level stretches between
them, and the unwinding after an unhandled exception) and compresses each into <= 1.8 KB (args at call time, last
values, return, stdout excerpt, executed source lines, measured step/call/loop counts). `narrator.py` (Layer A,
tr/en) turns the same facts into sentences with no LLM. SSE per scene: `layer_a` first, then `token`s if the LLM is
used, `fallback` if it becomes unavailable, `scene_done`. The UI loads Layer A automatically after each run and calls
the LLM only on an explicit click (quota / privacy: source code leaves the machine only then).

**Timeline sentences** stay client-side (`lib/format.ts`); the backend narrator works on scenes, not single steps.

## Phase 5

**What-If.** Overrides (`step`, `frame_depth` = index in the stack of USER frames, module frame = 0, `name`,
`value_literal`) are checked with `ast.literal_eval` in the API (HTTP 422 for anything else, e.g. `__import__('os')`) and
again in the runner. The override is applied when the runner reaches step `step`, so the value shown at step k can be
edited "at step k". Branches are ordinary runs; `GET /api/runs/{base}/compare?other={branch}` reports the first
diverging step (first step whose event differs once timings, `seq` and heap labels are removed), outputs, top-level
return values, step counts and status. The UI shows the main run and ONE branch at a time (switchable) with a divergence
marker on the scrubber and a summary line; it does not draw two overlaid tracks.

**Root-Cause Rewind.** Backward slice over the recorded deltas. A delta on a line event shows the effect of the
PREVIOUS line, so a write is attributed to that previous line. Links: assignments, arguments (parameter -> call site),
returns (a value assigned from a call -> the callee's return line) and module globals; function/class objects are
dropped as noise; re-runs of the same statement in a loop keep only the most recent write. Depth limit 6, 40 links.
The mentor receives the chain for the exception scene (Layer A sentence and LLM digest).

**Big-O lab.** The driver is appended to the user's file (input built on one module line, the measured call on the last
line); the measure is the number of steps between that call's `call` and `return` events, so input construction and any
`__main__` block are not counted. Models: 1, log n, n, n log n, n^2, n^3, 2^n, fitted as y = a*f(n) + b by least squares;
R^2 decides, and within 0.001 of the best R^2 the simpler model wins; negative slopes score 0. Sizes stop growing when a
run hits the step limit or the 10 s watchdog (reported as `stopped_early`); at least 4 points are required. The static
estimate (loop depth, recursion shape) is shown beside the measurement and a disagreement is flagged, as for iterative
binary search (static: O(n), measured: O(log n)). Extra arguments accept only literals or `$n $last $first $missing`.

**Exports.** Mermaid uses generated node ids and numeric entity codes for every character that could break a quoted
label; a regex grammar test guards it (the Mermaid CLI is not used). The Replay HTML embeds the trace as JSON in a
`<script type="application/json">` (with `</` escaped), the source and a vanilla-JS player; it contains no external
reference (tested by scanning the file). Above the size limit (default 1.5 MB) the trace is thinned by doubling the
keyframe stride; the player then jumps between snapshots and says so. The player's reducer is executed in `node` by a
test and compared with the recorded run.

**Presentation mode.** Same components, editor read-only with a larger font, call graph and narration strip only.
Keys: Space, left/right, Shift+left/right (scene), `[` `]` (speed), `F` (fullscreen), Esc. While auto-playing, playback
stops at the start of each scene for `clamp(45 ms x text length, 1.5 s, 8 s) / speed` so the narration can be read.

**Productization.** `make dev` runs `scripts/dev.py` (API + Vite, installs the frontend dependencies on first use).
`make` is not installed on the Windows development machine, so the targets were verified there by running their
commands directly. Update: the CI workflow has since run on GitHub Actions (ubuntu-latest) and was green on `main` and
`v1.0.0`. The Docker images have still NOT been executed. The compose file mounts the Docker socket and a
host directory at an identical path for the runner bind mounts (Linux only, untested).

**Example artefacts** in `docs/examples/` are real output of `glassbox export` (not hand-written).

**Depth limit keeps known references (v2.0.0).** Past the serializer depth/node limit an object whose entry the client
already holds (in the recorder's heap store, or stored earlier in the same snapshot) is emitted as `{"ref":"oN"}`
instead of a truncated placeholder. Before, a parent reached through a deeper path was re-sent with a placeholder and
overwrote the full entry on the client, so long linked lists / deep trees lost arrows. Unknown objects still get the
placeholder (the Phase-1 contract and existing fixtures are unchanged). Accepted limitation, same as before: a
mutation that happens only beyond the limit is not re-serialized, so the referenced entry can lag until the object is
reached again within the limit.
