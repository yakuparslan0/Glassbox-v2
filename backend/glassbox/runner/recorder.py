"""Recorder: step counter, delta/keyframe state log, statistics and event emission.

Both tracer backends translate their native callbacks into the methods of this class, so the event
sequence is produced by exactly one piece of code (that is what makes the two backends comparable).

State model
- ``delta`` of a frame event = locals whose *deep fingerprint* changed since that frame's previous event.
- The entry module's locals ARE the globals: its delta is reported as ``delta`` (never duplicated in
  ``globals_delta``); for every other frame ``globals_delta`` carries changes of the entry globals.
- A keyframe is built from the recorder's mirror of what was reported (no extra serialization), so
  "nearest keyframe + deltas" and "all deltas from the start" give identical results by construction.
- ``--full-snapshots`` (job.full_snapshots) additionally emits ``snapshot`` events taken from the LIVE
  frames, which tests compare against the reconstruction.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from types import CodeType, FrameType
from typing import Any

from .limits import MAX_EVENT_BYTES, MAX_TOTAL_EVENT_BYTES
from .noise import Classifier
from .protocol import FdCapture, ProtocolWriter
from .serializer import MAX_STR, Serializer, iter_refs

_dumps = json.dumps
_SKIP_EXC = (StopIteration, StopAsyncIteration, GeneratorExit)
_NONE_VALUE = {"k": "prim", "t": "NoneType", "r": "None"}
_OUTPUT_CHUNK = 8 * 1024
_EXIT_CODES = {"ok": 0, "exception": 1, "limit": 2, "blocked": 3, "killed": 4}


def _hidden(name: str) -> bool:
    return name.startswith(".") or (name.startswith("__") and name.endswith("__"))


class Entry:
    """One stack entry: a user frame activation or a library boundary call."""

    __slots__ = (
        "call_line",
        "callable",
        "child_ns",
        "code",
        "cur_line",
        "file",
        "fps",
        "frame",
        "func",
        "hits",
        "id",
        "is_c",
        "is_entry",
        "kind",
        "last_exc",
        "last_exc_line",
        "parent",
        "pending",
        "qualname",
        "stat",
        "t0",
        "vals",
    )  # fmt: skip

    def __init__(self, kind: str, entry_id: int) -> None:
        self.kind = kind
        self.id = entry_id
        self.frame: FrameType | None = None
        self.code: CodeType | None = None
        self.func = ""
        self.qualname = ""
        self.file = ""
        self.parent: int | None = None
        self.call_line: int | None = None
        self.cur_line = 0
        self.t0 = 0
        self.child_ns = 0
        self.fps: dict[str, int] = {}
        self.vals: dict[str, dict[str, Any]] = {}
        self.is_entry = False
        self.pending: tuple[BaseException, int] | None = None
        self.last_exc: BaseException | None = None  # last exception seen propagating in this frame
        self.last_exc_line = 0
        self.stat: dict[str, int] | None = None
        self.hits: dict[int, int] | None = None
        self.callable: Any = None
        self.is_c = False


class Recorder:
    def __init__(
        self,
        job: dict[str, Any],
        limits: dict[str, int],
        writer: ProtocolWriter,
        serializer: Serializer,
        classifier: Classifier,
        captures: tuple[FdCapture, FdCapture] | None,
    ) -> None:
        self.job = job
        self.limits = limits
        self.max_steps = limits["max_steps"]
        self.max_output = limits["max_output_kb"] * 1024
        self.writer = writer
        self.ser = serializer
        self.classifier = classifier
        self.captures = captures
        self.filter_mode: str = job.get("filter", "boundaries")
        self.keyframe_interval: int = int(job.get("keyframe_interval", 200))
        self.full_snapshots: bool = bool(job.get("full_snapshots", False))
        self.overrides: dict[int, list[tuple[int, str, Any]]] = {}
        self.whatif_unapplied = 0
        self.blocked: list[dict[str, str]] = []

        self.t0 = time.perf_counter_ns()
        self.seq = 0
        self.step = 0
        self.frame_ctr = 0
        self.stack: list[Entry] = []
        self.entry_globals: dict[str, Any] = {}
        self.entry_code: CodeType | None = None
        self.entry_rel = ""
        self._entry_taken = False
        self.gfps: dict[str, int] = {}
        self.gvals: dict[str, dict[str, Any]] = {}
        self.heap_sent: dict[str, int] = {}
        self.heap_store: dict[str, dict[str, Any]] = {}
        self.ser.known = self.heap_store  # past the depth limit, already-sent objects stay references

        self.line_hits: dict[str, dict[int, int]] = {}
        self.func_stats: dict[str, dict[str, int]] = {}
        self.repeat: Counter[tuple[str, str]] = Counter()
        self.out_bytes = 0
        self.total_bytes = 0
        self._finished = False
        self._limiting = False
        self._seen_imports: set[str] = set()
        self.extra_finish: dict[str, Any] = {}

    # ------------------------------------------------------------------ plumbing
    @property
    def top(self) -> Entry | None:
        return self.stack[-1] if self.stack else None

    def emit(self, typ: str, **fields: Any) -> None:
        self.seq += 1
        ev: dict[str, Any] = {"v": 1, "seq": self.seq, "t": time.perf_counter_ns() - self.t0, "type": typ}
        ev.update(fields)
        line = _dumps(ev, separators=(",", ":"))
        if len(line) > MAX_EVENT_BYTES:
            line = self._shrink(ev)
        self.total_bytes += len(line) + 1
        self.writer.write_line(line)
        if self.total_bytes > MAX_TOTAL_EVENT_BYTES:
            self.limit("event_bytes", f"total event bytes exceeded {MAX_TOTAL_EVENT_BYTES}")

    @staticmethod
    def _placeholder(values: dict[str, Any]) -> dict[str, Any]:
        return {name: {"k": "other", "t": "?", "r": "<value too large>", "trunc": True} for name in values}

    def _shrink(self, ev: dict[str, Any]) -> str:
        """Respect the 64 KB per-event limit by dropping heap entries and replacing values."""
        ev["trunc"] = True
        ev.pop("heap", None)
        for key in ("delta", "globals_delta"):
            if key in ev:
                ev[key] = {"set": self._placeholder(ev[key]["set"]), "del": ev[key]["del"]}
        if "args" in ev and isinstance(ev["args"], dict):
            ev["args"] = self._placeholder(ev["args"])
        if ev.get("type") == "keyframe":
            for frame in ev["stack"]:
                frame["locals"] = self._placeholder(frame["locals"])
            ev["globals"] = self._placeholder(ev["globals"])
            ev["heap"] = {}
        line = _dumps(ev, separators=(",", ":"))
        if len(line) > MAX_EVENT_BYTES:
            ev = {k: ev[k] for k in ("v", "seq", "t", "type", "step") if k in ev}
            ev["trunc"] = True
            line = _dumps(ev, separators=(",", ":"))
        return line

    def _heap_delta(self) -> dict[str, dict[str, Any]]:
        changed = self.ser.take_changed(self.heap_sent)
        self.heap_store.update(changed)
        return changed

    def now(self) -> int:
        return time.perf_counter_ns()

    # ------------------------------------------------------------------ run lifecycle
    def run_started(self, python: str, tracer: str, seed: int, rlimit: bool) -> None:
        self.emit(
            "run_started",
            python=python,
            tracer=tracer,
            seed=seed,
            limits=dict(self.limits),
            filter=self.filter_mode,
            capabilities={"whatif": True, "threads": False, "asyncio": False, "rlimit": rlimit},
        )

    def limit(self, name: str, detail: str) -> None:
        """Emit limit_exceeded + run_finished and terminate the process immediately."""
        if self._limiting:
            return
        self._limiting = True
        self.emit("limit_exceeded", step=self.step, limit=name, detail=detail)
        self.finish("limit", None)
        self.writer.flush()
        os._exit(_EXIT_CODES["limit"])

    def _check_steps(self) -> None:
        if self.step >= self.max_steps:
            self.limit("max_steps", f"step limit of {self.max_steps} reached")

    def finish(self, status: str, exit_code: int | None, detail: str | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        self.poll_capture(final=True)
        if exit_code is None:
            exit_code = _EXIT_CODES.get(status, 1)
        stats = {
            key: {"calls": s["calls"], "steps": s["steps"], "self_ns": s["self_ns"], "total_ns": s["total_ns"]}
            for key, s in self.func_stats.items()
        }
        fields: dict[str, Any] = {
            "status": status,
            "exit_code": exit_code,
            "steps": self.step,
            "wall_ms": (time.perf_counter_ns() - self.t0) // 1_000_000,
            "line_hits": {f: {str(n): c for n, c in sorted(h.items())} for f, h in self.line_hits.items()},
            "func_stats": stats,
            "executed_lines": {f: sorted(h) for f, h in self.line_hits.items()},
            "repeat_calls": [
                {"func": func, "args_fp": fp, "count": count}
                for (func, fp), count in sorted(self.repeat.items(), key=lambda kv: (-kv[1], kv[0]))
                if count > 1
            ][:100],
        }
        if detail:
            fields["detail"] = detail
        if self.blocked:
            fields["blocked"] = self.blocked
        if self.whatif_unapplied:
            fields["whatif_unapplied"] = self.whatif_unapplied
        fields.update(self.extra_finish)
        self.emit("run_finished", **fields)
        self.writer.flush()

    # ------------------------------------------------------------------ output / imports
    def on_output(self, stream: str, text: str) -> None:
        if not text:
            return
        remaining = self.max_output - self.out_bytes
        size = len(text.encode("utf-8", "replace"))
        over = size > remaining
        if over:
            text = text.encode("utf-8", "replace")[: max(remaining, 0)].decode("utf-8", "ignore")
        self.out_bytes += size if not over else max(remaining, 0)
        for i in range(0, len(text), _OUTPUT_CHUNK):
            self.emit("output", step=self.step, stream=stream, text=text[i : i + _OUTPUT_CHUNK])
        if over:
            self.limit("output", f"output limit of {self.limits['max_output_kb']} KB reached")

    def poll_capture(self, final: bool = False) -> None:
        """Turn raw fd 1/2 writes (os.write etc.) into output events."""
        if self.captures is None:
            return
        for stream, cap in zip(("stdout", "stderr"), self.captures, strict=True):
            text = cap.poll(final)
            if text:
                self.on_output(stream, text)

    def on_import(self, module: str, lib: str) -> None:
        if module in self._seen_imports:
            return
        self._seen_imports.add(module)
        self.emit("import", step=self.step, module=module, lib=lib)

    # ------------------------------------------------------------------ state diffing
    def _diff(
        self, mapping: Any, fps: dict[str, int], vals: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        ser = self.ser
        sets: dict[str, Any] = {}
        seen: set[str] = set()
        for name, value in list(mapping.items()):
            if not isinstance(name, str) or _hidden(name):
                continue
            seen.add(name)
            inline, fp = ser.fingerprint(value)
            if fps.get(name) != fp:
                fps[name] = fp
                vals[name] = inline
                sets[name] = inline
        dels = [name for name in fps if name not in seen]
        for name in dels:
            del fps[name]
            vals.pop(name, None)
        return {"set": sets, "del": dels}

    @staticmethod
    def _frame_locals(frame: FrameType) -> Any:
        """Locals mapping of a frame. For module frames this is the module's globals dict: while a
        comprehension is inlined at module level, ``frame.f_locals`` would hide every global."""
        return frame.f_globals if frame.f_code.co_name == "<module>" else frame.f_locals

    def _globals_delta(self) -> dict[str, Any] | None:
        delta = self._diff(self.entry_globals, self.gfps, self.gvals)
        return delta if delta["set"] or delta["del"] else None

    # ------------------------------------------------------------------ stack helpers
    def _find(self, frame: FrameType) -> Entry | None:
        stack = self.stack
        for i in range(len(stack) - 1, -1, -1):
            entry = stack[i]
            if entry.frame is frame and entry.kind == "user":
                return entry
        return None

    def _close_above(self, entry: Entry) -> None:
        """Close stale entries above ``entry`` (a boundary whose end was never observed)."""
        stack = self.stack
        while stack and stack[-1] is not entry:
            stale = stack.pop()
            self._check_steps()
            self.step += 1
            dur = self.now() - stale.t0
            if stale.kind == "boundary":
                self.emit("return", step=self.step, frame=stale.id, kind="boundary", value_summary=None,
                          dur_ns=dur, synthetic=True)  # fmt: skip
            else:
                self.emit("return", step=self.step, frame=stale.id, value=_NONE_VALUE, dur_ns=dur,
                          unwound=True)  # fmt: skip

    def _stat_key(self, qualname: str, rel: str) -> str:
        if rel == self.entry_rel:
            return qualname
        return f"{rel[:-3] if rel.endswith('.py') else rel}:{qualname}"

    # ------------------------------------------------------------------ user calls
    def on_call(self, frame: FrameType, code: CodeType) -> None:
        self._check_steps()
        parent = self.stack[-1] if self.stack else None
        self.step += 1
        self.frame_ctr += 1
        entry = Entry("user", self.frame_ctr)
        entry.frame = frame
        entry.code = code
        entry.func = code.co_name
        entry.qualname = code.co_qualname
        entry.file = self.classifier.relpath(code.co_filename)
        entry.cur_line = code.co_firstlineno
        entry.t0 = self.now()
        if parent is not None:
            entry.parent = parent.id
            entry.call_line = parent.cur_line
        is_gen = bool(code.co_flags & 0x20)  # CO_GENERATOR
        if code is self.entry_code and not self._entry_taken:
            self._entry_taken = True
            entry.is_entry = True
            entry.fps = self.gfps
            entry.vals = self.gvals
        key = self._stat_key(entry.qualname, entry.file)
        stat = self.func_stats.get(key)
        if stat is None:
            stat = self.func_stats[key] = {"calls": 0, "steps": 0, "self_ns": 0, "total_ns": 0}
        stat["calls"] += 1
        stat["steps"] += 1
        entry.stat = stat
        entry.hits = self.line_hits.setdefault(entry.file, {})

        self.ser.begin()
        args = self._diff(self._frame_locals(frame), entry.fps, entry.vals)["set"]
        if not is_gen and entry.func != "<module>":
            fp = hashlib.blake2b(repr(sorted(entry.fps.items())).encode(), digest_size=6).hexdigest()
            self.repeat[(key, fp)] += 1
        gdelta = None if entry.is_entry else self._globals_delta()
        heap = self._heap_delta()
        fields: dict[str, Any] = {
            "step": self.step, "frame": entry.id, "parent": entry.parent, "kind": "user",
            "func": entry.func, "qualname": entry.qualname, "file": entry.file,
            "line": entry.cur_line, "call_line": entry.call_line, "args": args,
        }  # fmt: skip
        if gdelta is not None:
            fields["globals_delta"] = gdelta
        if heap:
            fields["heap"] = heap
        if is_gen:
            fields["generator"] = True
        self.emit("call", **fields)
        self.stack.append(entry)
        self._after_step(entry)

    def on_line(self, frame: FrameType, line: int) -> None:
        entry = self._find(frame)
        if entry is None:
            return
        if self.stack[-1] is not entry:
            self._close_above(entry)
        if entry.pending is not None:
            self._emit_exception(entry, handled=True)
        self._check_steps()
        self.step += 1
        entry.cur_line = line
        hits = entry.hits
        assert hits is not None and entry.stat is not None
        hits[line] = hits.get(line, 0) + 1
        entry.stat["steps"] += 1

        self.ser.begin()
        delta = self._diff(self._frame_locals(frame), entry.fps, entry.vals)
        gdelta = None if entry.is_entry else self._globals_delta()
        heap = self._heap_delta()
        fields: dict[str, Any] = {"step": self.step, "frame": entry.id, "line": line, "delta": delta}
        if gdelta is not None:
            fields["globals_delta"] = gdelta
        if heap:
            fields["heap"] = heap
        self.emit("line", **fields)
        self._after_step(entry)

    def on_return(
        self, frame: FrameType, retval: Any, unwound: bool = False, exc_hint: BaseException | None = None
    ) -> None:
        """A user frame is left: by return, by yield (suspension), or (``unwound``) by an exception."""
        entry = self._find(frame)
        if entry is None:
            return
        if self.stack[-1] is not entry:
            self._close_above(entry)
        if unwound:
            exc = entry.pending[0] if entry.pending is not None else (entry.last_exc or exc_hint)
            if exc is None or isinstance(exc, _SKIP_EXC):  # generator close / StopIteration: a plain return
                unwound = False
            elif entry.pending is None:
                entry.pending = (exc, entry.last_exc_line or frame.f_lineno)
        if entry.pending is not None:
            self._emit_exception(entry, handled=not unwound)
        self._check_steps()
        self.step += 1
        self.ser.begin()
        value = _NONE_VALUE if unwound else self.ser.value(retval)
        gdelta = self._globals_delta()
        heap = self._heap_delta()
        dur = self.now() - entry.t0
        self.stack.pop()
        assert entry.stat is not None
        entry.stat["steps"] += 1
        entry.stat["total_ns"] += dur
        entry.stat["self_ns"] += dur - entry.child_ns
        if self.stack:
            self.stack[-1].child_ns += dur
        fields: dict[str, Any] = {"step": self.step, "frame": entry.id, "value": value, "dur_ns": dur}
        if unwound:
            fields["unwound"] = True
        if gdelta is not None:
            fields["globals_delta"] = gdelta
        if heap:
            fields["heap"] = heap
        self.emit("return", **fields)
        self._after_step(None)

    # ------------------------------------------------------------------ exceptions
    def exc_raised(self, frame: FrameType, exc: BaseException) -> None:
        if isinstance(exc, _SKIP_EXC):
            return
        entry = self._find(frame)
        if entry is None:
            return
        if entry.last_exc is exc:
            return  # re-raise of the exception already being reported in this frame
        previous = entry.pending
        if previous is not None:
            self._emit_exception(entry, handled=True)
        line = frame.f_lineno
        entry.pending = (exc, line)
        entry.last_exc = exc
        entry.last_exc_line = line

    def exc_handled(self, frame: FrameType) -> None:
        entry = self._find(frame)
        if entry is not None and entry.pending is not None:
            self._emit_exception(entry, handled=True)

    @staticmethod
    def _message(exc: BaseException) -> str:
        """str(exc) without running a user-defined __str__."""
        try:
            safe = True
            for cls in type(exc).__mro__:
                if "__str__" in cls.__dict__:
                    safe = cls.__module__ == "builtins"
                    break
            text = str(exc) if safe else BaseException.__str__(exc)
        except Exception:
            text = "<unprintable>"
        return text[:MAX_STR]

    def _emit_exception(self, entry: Entry, handled: bool) -> None:
        assert entry.pending is not None and entry.stat is not None
        exc, line = entry.pending
        entry.pending = None
        self._check_steps()
        self.step += 1
        entry.stat["steps"] += 1
        self.ser.begin()
        gdelta = self._globals_delta()
        heap = self._heap_delta()
        fields: dict[str, Any] = {
            "step": self.step, "frame": entry.id, "line": line, "exc_type": type(exc).__name__,
            "message": self._message(exc), "handled": handled,
        }  # fmt: skip
        if gdelta is not None:
            fields["globals_delta"] = gdelta
        if heap:
            fields["heap"] = heap
        self.emit("exception", **fields)
        self._after_step(entry)

    # ------------------------------------------------------------------ boundaries
    def _boundary_call(
        self, kind_c: bool, lib: str, module: str, func: str, callable_: Any, frame: FrameType | None,
        args_summary: str | None, args: dict[str, Any] | None,
    ) -> None:  # fmt: skip
        parent = self.stack[-1]
        self._check_steps()
        self.step += 1
        self.frame_ctr += 1
        entry = Entry("boundary", self.frame_ctr)
        entry.is_c = kind_c
        entry.callable = callable_
        entry.frame = frame
        entry.parent = parent.id
        entry.call_line = parent.cur_line
        entry.func = func
        entry.t0 = self.now()
        gdelta = self._globals_delta()
        heap = self._heap_delta()
        fields: dict[str, Any] = {
            "step": self.step, "frame": entry.id, "parent": entry.parent, "kind": "boundary",
            "lib": lib, "module": module, "func": func, "call_line": entry.call_line,
        }  # fmt: skip
        if gdelta is not None:
            fields["globals_delta"] = gdelta
        if args_summary is not None:
            fields["args_summary"] = args_summary
        if args is not None:
            fields["args"] = args
        if heap:
            fields["heap"] = heap
        self.emit("call", **fields)
        self.stack.append(entry)
        self._after_step(None)

    def boundary_py_start(self, frame: FrameType, code: CodeType, lib: str) -> None:
        """A non-user Python function was entered directly from the top user frame."""
        if self.filter_mode == "user_only":
            return
        items = [(k, v) for k, v in list(self._frame_locals(frame).items()) if isinstance(k, str) and not _hidden(k)]
        ser = self.ser
        summary = ", ".join(f"{k}={ser.describe(v)}" for k, v in items[:8])
        if len(items) > 8:
            summary += ", …"
        args = None
        ser.begin()  # the boundary call's heap/globals delta is computed from this snapshot
        if self.filter_mode == "boundaries_verbose":
            args = {k: ser.value(v, 0) for k, v in items}
        module = frame.f_globals.get("__name__", "?")
        self._boundary_call(False, lib, str(module), code.co_qualname, None, frame, f"({summary})", args)

    def boundary_c_call(self, frame: FrameType, callable_: Any, info: tuple[str, str, str]) -> None:
        """A C callable of a library module was called from the top user frame."""
        if self.filter_mode == "user_only":
            return
        module, func, lib = info
        self.ser.begin()
        self._boundary_call(True, lib, module, func, callable_, frame, None, None)

    def boundary_py_end(self, frame: FrameType, retval: Any, raised: bool) -> bool:
        top = self.stack[-1] if self.stack else None
        if top is None or top.kind != "boundary" or top.is_c or top.frame is not frame:
            return False
        summary = None if raised else self.ser.describe(retval)
        self._boundary_return(top, summary, raised)
        return True

    def boundary_c_end(self, callable_: Any, raised: bool) -> bool:
        top = self.stack[-1] if self.stack else None
        if top is None or top.kind != "boundary" or not top.is_c or top.callable is not callable_:
            return False
        self._boundary_return(top, None, raised)
        return True

    def _boundary_return(self, entry: Entry, summary: str | None, raised: bool) -> None:
        self.poll_capture()
        self._check_steps()
        self.step += 1
        self.stack.pop()
        dur = self.now() - entry.t0
        if self.stack:
            self.stack[-1].child_ns += dur
        self.ser.begin()
        gdelta = self._globals_delta()
        heap = self._heap_delta()
        fields: dict[str, Any] = {
            "step": self.step, "frame": entry.id, "kind": "boundary", "value_summary": summary, "dur_ns": dur,
        }  # fmt: skip
        if raised:
            fields["raised"] = True
        if gdelta is not None:
            fields["globals_delta"] = gdelta
        if heap:
            fields["heap"] = heap
        self.emit("return", **fields)
        self._after_step(None)

    # ------------------------------------------------------------------ per-step extras
    def _after_step(self, entry: Entry | None) -> None:
        if self.keyframe_interval and self.step % self.keyframe_interval == 0:
            self._emit_keyframe()
        if self.full_snapshots:
            self._emit_snapshot(entry)
        if self.overrides:
            items = self.overrides.pop(self.step, None)
            if items:
                self._apply_overrides(items)

    def _emit_keyframe(self) -> None:
        stack = []
        roots: list[dict[str, Any]] = []
        for e in self.stack:
            if e.kind != "user":
                continue
            locs = {} if e.is_entry else dict(e.vals)
            roots.extend(locs.values())
            stack.append({"frame": e.id, "func": e.func, "line": e.cur_line, "locals": locs})
        globals_ = dict(self.gvals)
        roots.extend(globals_.values())
        reachable: dict[str, dict[str, Any]] = {}
        todo = [r["ref"] for r in roots if "ref" in r]
        while todo:
            label = todo.pop()
            if label in reachable or label not in self.heap_store:
                continue
            reachable[label] = self.heap_store[label]
            todo.extend(iter_refs(reachable[label]))
        self.heap_sent = {label: self.heap_sent[label] for label in reachable if label in self.heap_sent}
        self.emit("keyframe", step=self.step, stack=stack, globals=globals_, heap=reachable)

    def _emit_snapshot(self, entry: Entry | None) -> None:
        """Debug ground truth from the live frames (only the event frame + globals + heap)."""
        ser = self.ser
        ser.begin()
        stack = [{"frame": e.id, "func": e.func, "line": e.cur_line} for e in self.stack if e.kind == "user"]
        locals_: dict[str, Any] | None = None
        frame_id: int | None = None
        if entry is not None and entry.frame is not None:
            frame_id = entry.id
            if not entry.is_entry:
                locals_ = {
                    n: ser.value(v) for n, v in list(self._frame_locals(entry.frame).items())
                    if isinstance(n, str) and not _hidden(n)
                }  # fmt: skip
        globals_ = {
            n: ser.value(v) for n, v in list(self.entry_globals.items()) if isinstance(n, str) and not _hidden(n)
        }
        self.emit("snapshot", step=self.step, stack=stack, frame=frame_id, locals=locals_,
                  globals=globals_, heap=dict(ser.scratch))  # fmt: skip

    def _apply_overrides(self, items: list[tuple[int, str, Any]]) -> None:
        users = [e for e in self.stack if e.kind == "user"]
        for depth, name, value in items:
            if not 0 <= depth < len(users) or users[depth].frame is None:
                self.whatif_unapplied += 1
                continue
            target = users[depth]
            assert target.frame is not None
            locals_proxy = self._frame_locals(target.frame)
            self.ser.begin()
            old = self.ser.value(locals_proxy[name]) if name in locals_proxy else None
            locals_proxy[name] = value
            new = self.ser.value(value)
            heap = self._heap_delta()
            fields: dict[str, Any] = {"step": self.step, "frame": target.id, "name": name, "old": old, "new": new}
            if heap:
                fields["heap"] = heap
            self.emit("whatif_applied", **fields)
