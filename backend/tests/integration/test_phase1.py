"""Phase 1 acceptance tests. Every test starts a REAL runner subprocess (no mocks)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import Counter

import pytest
from helpers import (
    EXAMPLES,
    example_source,
    finished,
    of_type,
    reduce_events,
    run,
    stdout_text,
    strip_time,
)
from test_parity import SAMPLES

from glassbox.schemas.events import validate_event

TRACERS = ["monitoring", "settrace"]
ALL_EXAMPLES = sorted(p.name for p in EXAMPLES.glob("*.py"))


# ----------------------------------------------------------------------------- fib_naive
@pytest.mark.parametrize("tracer", TRACERS)
def test_fib_calls_balanced_unique_frames_and_repeat_calls(tracer: str) -> None:
    events = run(example_source("fib_naive.py"), tracer=tracer)
    calls = of_type(events, "call", kind="user")
    returns = [e for e in of_type(events, "return") if e.get("kind") != "boundary"]
    assert len(calls) == len(returns) == 1973 + 1  # fib(15) makes 1973 calls + the module frame
    frame_ids = [c["frame"] for c in calls]
    assert len(set(frame_ids)) == len(frame_ids), "every (recursive) call needs its own frame id"
    assert {r["frame"] for r in returns} == set(frame_ids)
    done = finished(events)
    assert done["status"] == "ok"
    assert done["func_stats"]["fib"]["calls"] == 1973
    assert any(r["func"] == "fib" and r["count"] > 1 for r in done["repeat_calls"])
    assert stdout_text(events) == "fib(15) = 610\n"


# ----------------------------------------------------------------------------- noise filter
JSON_SOURCE = """
import json

def key_fn(v):
    return -v

text = json.dumps({"a": [1, 2, 3]}, sort_keys=True)
ordered = sorted([3, 1, 2], key=key_fn)
print(text, ordered)
"""


@pytest.mark.parametrize("tracer", TRACERS)
def test_library_internals_are_not_traced_but_a_boundary_pair_exists(tracer: str) -> None:
    events = run(JSON_SOURCE, tracer=tracer)
    assert {e["file"] for e in of_type(events, "call", kind="user")} == {"main.py"}
    # no line event may belong to a library frame: every line event's frame is a user frame
    user_frames = {e["frame"] for e in of_type(events, "call", kind="user")}
    assert all(e["frame"] in user_frames for e in of_type(events, "line"))
    boundaries = [e for e in of_type(events, "call", kind="boundary") if e["module"] == "json"]
    assert [b["func"] for b in boundaries] == ["dumps"]
    returns = of_type(events, "return", kind="boundary", frame=boundaries[0]["frame"])
    assert len(returns) == 1
    assert returns[0]["value_summary"] == "'{\"a\": [1, 2, 3]}'"
    # json.dumps' own helpers must not show up as boundaries (only the direct call counts)
    assert len([e for e in of_type(events, "call", kind="boundary")]) == 1
    # the user key function called back from sorted() IS traced
    assert len([e for e in of_type(events, "call", kind="user") if e["func"] == "key_fn"]) == 3
    assert of_type(events, "import", module="json")[0]["lib"] == "stdlib"


def test_user_only_filter_hides_boundaries() -> None:
    events = run(JSON_SOURCE, filter="user_only")
    assert not of_type(events, "call", kind="boundary")
    assert finished(events)["status"] == "ok"


def test_verbose_filter_serializes_boundary_arguments() -> None:
    events = run(JSON_SOURCE, filter="boundaries_verbose")
    boundary = of_type(events, "call", kind="boundary")[0]
    assert "obj" in boundary["args"]
    assert boundary["args"]["sort_keys"] == {"k": "prim", "t": "bool", "r": "True"}


def test_c_level_library_call_is_a_boundary() -> None:
    events = run("import math\nx = math.sqrt(16)\nprint(x)\n")
    boundaries = of_type(events, "call", kind="boundary")
    assert [(b["module"], b["func"], b["lib"]) for b in boundaries] == [("math", "sqrt", "stdlib")]


# ----------------------------------------------------------------------------- delta correctness
@pytest.mark.parametrize("tracer", TRACERS)
@pytest.mark.parametrize("name", [*ALL_EXAMPLES, *sorted(SAMPLES)])
def test_keyframe_plus_delta_equals_live_snapshot_at_every_step(tracer: str, name: str) -> None:
    source = SAMPLES[name] if name in SAMPLES else example_source(name)
    events = run(
        source, tracer=tracer, full_snapshots=True, keyframe_interval=7, stdin="Ada\n", limits={"max_steps": 800}
    )
    snapshots = of_type(events, "snapshot")
    assert snapshots, "full_snapshots must emit ground-truth snapshots"
    assert of_type(events, "keyframe"), "keyframe_interval=7 must produce keyframes"
    for snap in snapshots:
        for from_keyframe in (False, True):
            state = reduce_events(events, snap["seq"], from_keyframe=from_keyframe)
            where = f"step {snap['step']} (from_keyframe={from_keyframe})"
            assert state.structure() == [(f["frame"], f["func"], f["line"]) for f in snap["stack"]], where
            assert state.globals == snap["globals"], where
            if snap["locals"] is not None:
                frame = next(f for f in state.stack if f["frame"] == snap["frame"])
                assert frame["locals"] == snap["locals"], where
            reachable = state.reachable_heap()
            for label, entry in snap["heap"].items():
                assert reachable.get(label) == entry, f"{where}: heap {label}"


ALIAS_SOURCE = """
items = [1, 2]
other = items
items.append(3)
x = 5
"""


@pytest.mark.parametrize("tracer", TRACERS)
def test_in_place_append_is_a_delta_and_aliases_share_a_ref(tracer: str) -> None:
    events = run(ALIAS_SOURCE, tracer=tracer)
    lines = {e["line"]: e for e in of_type(events, "line", frame=1)}
    # a line event reports the effects of the PREVIOUS line (state before executing its own line)
    items_ref = lines[3]["delta"]["set"]["items"]["ref"]
    assert lines[4]["delta"]["set"]["other"] == {"ref": items_ref}, "two names, one object: same ref"
    # after `items.append(3)` both names are reported as changed although their ref is unchanged
    changed = lines[5]["delta"]["set"]
    assert changed["items"] == {"ref": items_ref} and changed["other"] == {"ref": items_ref}
    assert lines[5]["heap"][items_ref]["len"] == 3
    assert "x" not in changed
    state = reduce_events(events, events[-1]["seq"], from_keyframe=False)
    assert state.globals["items"] == state.globals["other"]


# ----------------------------------------------------------------------------- repr safety
REPR_SOURCE = """
class Loud:
    def __init__(self):
        self.value = 1

    def __repr__(self):
        print("REPR CALLED")
        return "Loud()"

    def __str__(self):
        print("STR CALLED")
        return "Loud"

obj = Loud()
box = [obj, {"k": obj}]
obj.value = 2
done = True
"""


@pytest.mark.parametrize("tracer", TRACERS)
def test_user_repr_is_never_called(tracer: str) -> None:
    events = run(REPR_SOURCE, tracer=tracer)
    assert stdout_text(events) == ""
    deltas = [e["delta"]["set"] for e in of_type(events, "line", frame=1) if "obj" in e["delta"]["set"]]
    heap = {}
    for e in of_type(events, "line", frame=1):
        heap.update(e.get("heap", {}))
    ref = deltas[0]["obj"]["ref"]
    assert heap[ref]["k"] == "obj" and heap[ref]["t"] == "Loud"


# ----------------------------------------------------------------------------- limits & watchdog
@pytest.mark.parametrize("tracer", TRACERS)
def test_infinite_loop_hits_the_step_limit_quickly(tracer: str) -> None:
    started = time.monotonic()
    events = run(example_source("infinite_loop.py"), tracer=tracer)
    elapsed = time.monotonic() - started
    done = finished(events)
    assert done["status"] == "limit" and done["steps"] == 20000
    limit = of_type(events, "limit_exceeded")[0]
    assert limit["limit"] == "max_steps" and limit["step"] == 20000
    assert done["wall_ms"] < 1000, f"runner took {done['wall_ms']} ms"
    assert elapsed < 5


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_watchdog_kills_a_c_level_hang_and_leaves_no_zombie() -> None:
    source = "import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(999)\nprint('never')\n"
    started = time.monotonic()
    events = run(source, limits={"timeout_s": 2})
    elapsed = time.monotonic() - started
    assert elapsed < 8
    done = finished(events)
    assert done["status"] == "killed"
    assert of_type(events, "limit_exceeded")[0]["limit"] == "timeout"
    pid = int(stdout_text(events).split()[0])
    time.sleep(0.5)
    assert not _pid_alive(pid), "the runner process must not survive the watchdog"
    assert "never" not in stdout_text(events)


def test_output_limit_stops_the_run() -> None:
    events = run("while True:\n    print('x' * 100)\n", limits={"max_output_kb": 4, "max_steps": 100000})
    done = finished(events)
    assert done["status"] == "limit"
    assert of_type(events, "limit_exceeded")[0]["limit"] == "output"
    assert len(stdout_text(events).encode()) <= 4 * 1024


def test_oversized_events_are_shrunk_not_dropped() -> None:
    source = "data = {str(i): 'v' * 200 for i in range(50)}\nbig = [data for _ in range(50)]\nend = 1\n"
    events = run(source)
    assert finished(events)["status"] == "ok"
    assert all(len(str(e)) < 400_000 for e in events)


# ----------------------------------------------------------------------------- protocol channel
@pytest.mark.parametrize("tracer", TRACERS)
def test_raw_fd_write_does_not_corrupt_the_protocol(tracer: str) -> None:
    events = run("import os\nos.write(1, b'raw-out')\nos.write(2, b'raw-err')\nprint('after')\n", tracer=tracer)
    assert finished(events)["status"] == "ok"
    assert stdout_text(events) == "raw-outafter\n"
    assert stdout_text(events, "stderr") == "raw-err"


def test_input_reads_the_job_stdin() -> None:
    events = run("a = input('a? ')\nb = input()\nprint(a.upper() + b)\n", stdin="foo\nbar\n")
    assert stdout_text(events) == "a? FOObar\n"


# ----------------------------------------------------------------------------- isolation
def test_child_environment_has_no_parent_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEYS", "gsk_secret_value_1")
    monkeypatch.setenv("GEMINI_API_KEY", "gem_secret_value_2")
    monkeypatch.setenv("SOME_PARENT_VAR", "visible-if-leaked")
    events = run("import os\nfor k, v in sorted(os.environ.items()):\n    print(k + '=' + v)\n")
    dump = stdout_text(events)
    for needle in ("GROQ", "GEMINI", "secret_value", "SOME_PARENT_VAR", "visible-if-leaked"):
        assert needle not in dump
    assert "PYTHONHASHSEED=0" in dump and "PYTHONDONTWRITEBYTECODE=1" in dump


BLOCKED_SOURCE = """
import socket, subprocess, os

results = {}
for name, call in {
    "connect": lambda: socket.socket().connect(("127.0.0.1", 9)),
    "popen": lambda: subprocess.Popen(["echo", "hi"]),
    "system": lambda: os.system("echo hi"),
    "write_outside": lambda: open(os.path.join(os.path.dirname(os.getcwd()), "escape.txt"), "w"),
}.items():
    try:
        call()
        results[name] = "ALLOWED"
    except PermissionError:
        results[name] = "blocked"
with open("inside.txt", "w") as f:
    f.write("fine")
results["write_inside"] = "ok"
print(sorted(results.items()))
"""


def test_audit_hook_blocks_network_processes_and_writes_outside_cwd() -> None:
    events = run(BLOCKED_SOURCE)
    assert stdout_text(events) == (
        "[('connect', 'blocked'), ('popen', 'blocked'), ('system', 'blocked'), "
        "('write_inside', 'ok'), ('write_outside', 'blocked')]\n"
    )
    done = finished(events)
    assert done["status"] == "ok"
    assert {b["event"] for b in done["blocked"]} == {"socket.connect", "subprocess.Popen", "os.system", "open"}


def test_uncaught_blocked_operation_ends_with_blocked_status() -> None:
    events = run("import subprocess\nsubprocess.Popen(['echo', 'x'])\n")
    done = finished(events)
    assert done["status"] == "blocked" and done["exit_code"] == 3
    assert "PermissionError" in stdout_text(events, "stderr")


# ----------------------------------------------------------------------------- determinism
DETERMINISM_SOURCE = """
import random

def roll():
    return random.random()

values = [roll() for _ in range(3)]
names = {"b", "a", "c"}
print(values, sorted(names), list(names))
"""


@pytest.mark.parametrize("tracer", TRACERS)
def test_same_seed_same_events_different_seed_different_output(tracer: str) -> None:
    first = run(DETERMINISM_SOURCE, seed=7, tracer=tracer)
    second = run(DETERMINISM_SOURCE, seed=7, tracer=tracer)
    other = run(DETERMINISM_SOURCE, seed=8, tracer=tracer)
    assert strip_time(first) == strip_time(second)
    assert stdout_text(first) != stdout_text(other)


# ----------------------------------------------------------------------------- What-If
def test_whatif_override_changes_the_run_and_is_reported() -> None:
    source = "x = 1\nprint(x)\nprint(x + 1)\n"
    base = run(source)
    assert stdout_text(base) == "1\n2\n"
    # step 1 = <module> call, step 2 = line 1, step 3 = line 2 (print(x)): override x right after step 3
    events = run(source, overrides=[{"step": 3, "frame_depth": 0, "name": "x", "value_literal": "41"}])
    assert stdout_text(events) == "41\n42\n"
    applied = of_type(events, "whatif_applied")
    assert len(applied) == 1 and applied[0]["step"] == 3 and applied[0]["name"] == "x"
    assert applied[0]["old"]["r"] == "1" and applied[0]["new"]["r"] == "41"


def test_whatif_in_a_function_frame_and_unapplied_target() -> None:
    source = "def f(n):\n    return n * 2\n\nprint(f(4))\n"
    # steps: 1 module call, 2 line 1, 3 line 4, 4 call f, 5 line 2 (inside f)
    events = run(source, overrides=[{"step": 5, "frame_depth": 1, "name": "n", "value_literal": "10"}])
    assert stdout_text(events) == "20\n"
    events = run(source, overrides=[{"step": 5, "frame_depth": 9, "name": "n", "value_literal": "10"}])
    assert finished(events)["whatif_unapplied"] == 1 and stdout_text(events) == "8\n"


@pytest.mark.parametrize("literal", ["__import__('os')", "open('x')", "1 + 1", "[x for x in ()]"])
def test_whatif_rejects_non_literals_before_running(literal: str) -> None:
    events = run(
        "print('ran')\n", overrides=[{"step": 2, "frame_depth": 0, "name": "x", "value_literal": literal}]
    )
    done = finished(events)
    assert done["status"] == "blocked" and "literal" in done["detail"]
    assert stdout_text(events) == ""


# ----------------------------------------------------------------------------- schema & misc
@pytest.mark.parametrize("name", [*ALL_EXAMPLES, *sorted(SAMPLES)])
def test_every_runner_event_validates_against_the_schema(name: str) -> None:
    source = SAMPLES[name] if name in SAMPLES else example_source(name)
    events = run(source, stdin="Ada\n", keyframe_interval=20, full_snapshots=True, limits={"max_steps": 500},
                 filter="boundaries_verbose")  # fmt: skip
    for event in events:
        validate_event(event)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1)), "seq must be gap-free"
    assert Counter(e["type"] for e in events)["run_started"] == 1


def test_status_for_uncaught_exception_and_syntax_error() -> None:
    crash = run(example_source("crash_chain.py"))
    done = finished(crash)
    assert done["status"] == "exception" and "ZeroDivisionError" in done["detail"]
    unhandled = [e for e in of_type(crash, "exception") if not e["handled"]]
    assert [e["frame"] for e in unhandled][-1] == 1  # propagates all the way to <module>
    assert "ZeroDivisionError: division by zero" in stdout_text(crash, "stderr")
    broken = run("def (:\n")
    assert finished(broken)["status"] == "exception"
    assert "SyntaxError" in stdout_text(broken, "stderr")


def test_sys_exit_code_is_reported() -> None:
    done = finished(run("import sys\nsys.exit(3)\n"))
    assert done["status"] == "ok" and done["exit_code"] == 3


def test_multi_file_project_and_path_validation() -> None:
    files = {"main.py": "import helper\nprint(helper.twice(4))\n", "helper.py": "def twice(n):\n    return n * 2\n"}
    events = run(files)
    assert stdout_text(events) == "8\n"
    assert {e["file"] for e in of_type(events, "call", kind="user")} == {"main.py", "helper.py"}
    assert of_type(events, "import", module="helper")[0]["lib"] == "user"
    for bad in ("../evil.py", "/abs.py", "a/../../b.py", "C:evil.py"):
        with pytest.raises(ValueError):
            run({"main.py": "", bad: ""})
    with pytest.raises(ValueError):
        run({"other.py": ""}, entry="main.py")


def test_internal_runner_failure_is_reported_not_swallowed() -> None:
    events = run("print('x')\n", limits={"max_steps": "not-a-number"})  # type: ignore[dict-item]
    done = finished(events)
    assert done["status"] == "killed" and "code 70" in done["detail"]
    assert "internal error" in stdout_text(events, "stderr") and "ValueError" in stdout_text(events, "stderr")


@pytest.mark.parametrize("from_keyframe", [False, True])
def test_long_linked_list_keeps_every_arrow_past_the_depth_limit(from_keyframe: bool) -> None:
    source = (
        "class Node:\n    def __init__(self, val):\n        self.val = val\n        self.next = None\n\n"
        "head = None\nfor v in [5, 4, 3, 2, 1]:\n    node = Node(v)\n    node.next = head\n    head = node\n"
        "del node\nx = 0\n"
    )
    events = run(source, keyframe_interval=7)
    assert '"trunc": true' not in json.dumps(events)  # no depth-limit placeholder anywhere
    state = reduce_events(events, events[-1]["seq"], from_keyframe=from_keyframe)
    heap = state.reachable_heap()
    vals, cur = [], state.globals["head"]
    while "ref" in cur:
        entry = heap[cur["ref"]]
        vals.append(entry["attrs"]["val"]["r"])
        cur = entry["attrs"]["next"]
    assert vals == ["1", "2", "3", "4", "5"]
    assert cur == {"k": "prim", "t": "NoneType", "r": "None"}
