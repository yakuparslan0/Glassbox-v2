"""Value serializer: turns live Python values into the JSON value model (spec 2.7 / 4.2).

Safety rules
- Never calls a user-defined ``__repr__`` / ``__str__`` / ``__len__`` / ``__iter__`` / ``__getattr__``:
  containers are read with the *base class* C methods, user objects through ``__dict__`` / ``__slots__``.
- Tracing is off while serializing: callers invoke this from inside tracer callbacks, where both
  sys.monitoring and sys.settrace suppress re-entrant events.
- Bounded: depth 3, 50 items per collection, 200 chars per string, MAX_NODES nodes per snapshot.
- At the depth/node limit an object the client already holds (sent earlier: ``known``, or stored in this
  snapshot) is emitted as ``{"ref": label}`` instead of a truncated placeholder, so a long linked list or
  deep tree keeps every arrow and an entry sent in full is never overwritten by a cut-down copy. Its entry
  is the last one sent (deep mutations past the limit are not re-serialized, as before).
"""

from __future__ import annotations

import collections
import itertools
import json
import sys
import types
from collections.abc import Container, Iterator
from typing import Any

MAX_DEPTH = 3
MAX_ITEMS = 50
MAX_STR = 200
MAX_NODES = 2000

# Types from these modules have a safe, side-effect free repr() and no interesting attributes.
_SAFE_REPR_MODULES = frozenset(
    {"decimal", "_decimal", "fractions", "datetime", "_datetime", "pathlib", "pathlib._local", "uuid"}
)
_FUNC_TYPES = (
    types.FunctionType,
    types.BuiltinFunctionType,
    types.MethodType,
    types.MethodWrapperType,
    types.WrapperDescriptorType,
    types.MethodDescriptorType,
)
_EXACT_SIZED = (list, tuple, dict, set, frozenset)

_dumps = json.dumps


def _prim(t: str, r: str, trunc: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"k": "prim", "t": t, "r": r}
    if trunc:
        out["trunc"] = True
    return out


def iter_refs(entry: dict[str, Any]) -> Iterator[str]:
    """Labels directly referenced by a heap entry."""
    kind = entry.get("k")
    if kind == "seq":
        values: Any = entry["items"]
    elif kind == "map":
        values = itertools.chain.from_iterable(entry["pairs"])
    elif kind == "obj":
        values = entry["attrs"].values()
    else:
        return
    for value in values:
        ref = value.get("ref")
        if ref is not None:
            yield ref


class Serializer:
    def __init__(self) -> None:
        self._ids: dict[int, str] = {}
        self._keep: dict[str, Any] = {}  # strong refs: keeps id() -> label stable for the whole run
        self.scratch: dict[str, dict[str, Any]] = {}
        self.hashes: dict[str, int] = {}
        self._budget = MAX_NODES
        self._path: set[str] = set()
        self._visited: list[str] = []
        # labels whose entry the client already has (the recorder points this at its heap store)
        self.known: Container[str] = frozenset()

    # ------------------------------------------------------------ snapshots
    def begin(self) -> None:
        """Start a new snapshot: forget the entries collected by the previous one."""
        self.scratch = {}
        self.hashes = {}
        self._budget = MAX_NODES
        self._path = set()
        self._visited = []

    def fingerprint(self, v: Any) -> tuple[dict[str, Any], int]:
        """Serialize ``v``; return (inline value, deep fingerprint of everything reachable from it)."""
        self._visited = []
        inline = self.value(v, 0)
        hashes = self.hashes
        fp = hash((tuple(inline.items()), tuple(hashes[label] for label in self._visited)))
        return inline, fp

    def take_changed(self, sent: dict[str, int]) -> dict[str, dict[str, Any]]:
        """Heap entries of this snapshot that differ from what was last sent; updates ``sent``."""
        changed: dict[str, dict[str, Any]] = {}
        for label, entry in self.scratch.items():
            h = self.hashes[label]
            if sent.get(label) != h:
                changed[label] = entry
                sent[label] = h
        return changed

    # ------------------------------------------------------------ labels
    def _label(self, v: Any) -> str:
        key = id(v)
        label = self._ids.get(key)
        if label is None:
            label = f"o{len(self._ids) + 1}"
            self._ids[key] = label
            self._keep[label] = v
        return label

    def _held(self, label: str) -> bool:
        """True when the client has (or receives with this snapshot) an entry for ``label``."""
        return label in self.scratch or label in self.known

    def _store(self, label: str, entry: dict[str, Any]) -> dict[str, Any]:
        self.scratch[label] = entry
        self.hashes[label] = hash(_dumps(entry, separators=(",", ":")))
        self._visited.append(label)
        return {"ref": label}

    # ------------------------------------------------------------ values
    def value(self, v: Any, depth: int = 0) -> dict[str, Any]:
        try:
            return self._value(v, depth)
        except Exception:  # a hostile object must never break tracing
            return {"k": "other", "t": type(v).__name__, "r": "<unserializable>"}

    def _value(self, v: Any, depth: int) -> dict[str, Any]:
        t = type(v)
        if t is str:
            if len(v) > MAX_STR:
                return _prim("str", repr(v[:MAX_STR]), True)
            return _prim("str", repr(v))
        if t is int:
            try:
                return _prim("int", repr(v))
            except ValueError:  # int -> str conversion limit for huge ints
                return _prim("int", f"<int with {v.bit_length()} bits>", True)
        if v is None or t is bool or t is float or t is complex:
            return _prim(t.__name__, repr(v))
        if t is bytes or t is bytearray:
            return _prim(t.__name__, repr(bytes(v[:MAX_STR])), len(v) > MAX_STR)
        self._budget -= 1
        if isinstance(v, (list, tuple, set, frozenset, dict)) or t is collections.deque:
            return self._container(v, t, depth)
        if isinstance(v, _FUNC_TYPES):
            name = getattr(v, "__qualname__", None) or t.__name__
            return {"k": "func", "t": t.__name__, "r": f"<function {name}>"}
        if isinstance(v, types.ModuleType):
            return {"k": "module", "t": "module", "r": f"<module {v.__dict__.get('__name__', '?')}>"}
        if isinstance(v, type):
            name = type.__getattribute__(v, "__qualname__")
            return {"k": "other", "t": "type", "r": f"<class {name}>"}
        if getattr(t, "__module__", None) in _SAFE_REPR_MODULES:
            return _prim(t.__name__, repr(v)[:MAX_STR])
        return self._object(v, t, depth)

    def _container(self, v: Any, t: type, depth: int) -> dict[str, Any]:
        label = self._label(v)
        if label in self._path:
            return {"ref": label}  # cycle: the entry is being built further up
        if isinstance(v, list):
            n = list.__len__(v)
        elif isinstance(v, tuple):
            n = tuple.__len__(v)
        elif isinstance(v, dict):
            n = dict.__len__(v)
        elif isinstance(v, set):
            n = set.__len__(v)
        elif isinstance(v, frozenset):
            n = frozenset.__len__(v)
        else:
            n = collections.deque.__len__(v)
        if depth >= MAX_DEPTH or self._budget <= 0:
            if self._held(label):
                return {"ref": label}
            return {"k": "other", "t": t.__name__, "r": f"<{t.__name__} len={n}>", "trunc": True}
        self._path.add(label)
        try:
            size = sys.getsizeof(v) if t in _EXACT_SIZED else None
            if isinstance(v, dict):
                pairs = [
                    [self.value(k, depth + 1), self.value(val, depth + 1)]
                    for k, val in itertools.islice(dict.items(v), MAX_ITEMS)
                ]
                entry: dict[str, Any] = {"k": "map", "t": t.__name__, "len": n, "pairs": pairs}
            else:
                if isinstance(v, list):
                    raw: Any = list.__getitem__(v, slice(0, MAX_ITEMS))
                elif isinstance(v, tuple):
                    raw = tuple.__getitem__(v, slice(0, MAX_ITEMS))
                elif isinstance(v, set):
                    raw = itertools.islice(set.__iter__(v), MAX_ITEMS)
                elif isinstance(v, frozenset):
                    raw = itertools.islice(frozenset.__iter__(v), MAX_ITEMS)
                else:
                    raw = itertools.islice(collections.deque.__iter__(v), MAX_ITEMS)
                items = [self.value(item, depth + 1) for item in raw]
                entry = {"k": "seq", "t": t.__name__, "len": n, "items": items}
            entry["trunc"] = n > MAX_ITEMS
            if size is not None:
                entry["size"] = size
        finally:
            self._path.discard(label)
        return self._store(label, entry)

    def _object(self, v: Any, t: type, depth: int) -> dict[str, Any]:
        try:
            inst_dict = object.__getattribute__(v, "__dict__")
        except AttributeError:
            inst_dict = None
        slot_names = self._slot_names(t)
        if not isinstance(inst_dict, dict) and not slot_names:
            return {"k": "other", "t": t.__name__, "r": f"<{t.__name__} object>"}
        label = self._label(v)
        if label in self._path:
            return {"ref": label}
        if depth >= MAX_DEPTH or self._budget <= 0:
            if self._held(label):
                return {"ref": label}
            return {"k": "other", "t": t.__name__, "r": f"<{t.__name__} object>", "trunc": True}
        raw: list[tuple[str, Any]] = []
        if isinstance(inst_dict, dict):
            raw.extend((k, val) for k, val in dict.items(inst_dict) if isinstance(k, str))
        for name in slot_names:
            try:
                raw.append((name, object.__getattribute__(v, name)))
            except AttributeError:
                continue
        self._path.add(label)
        try:
            attrs = {name: self.value(val, depth + 1) for name, val in raw[:MAX_ITEMS]}
        finally:
            self._path.discard(label)
        entry = {"k": "obj", "t": t.__name__, "attrs": attrs, "trunc": len(raw) > MAX_ITEMS}
        return self._store(label, entry)

    @staticmethod
    def _slot_names(t: type) -> list[str]:
        names: list[str] = []
        for cls in t.__mro__:
            slots = cls.__dict__.get("__slots__")
            if not slots:
                continue
            if isinstance(slots, str):
                slots = (slots,)
            for name in slots:
                if name in ("__dict__", "__weakref__"):
                    continue
                if name.startswith("__") and not name.endswith("__"):
                    name = f"_{cls.__name__.lstrip('_')}{name}"
                names.append(name)
        return names

    # ------------------------------------------------------------ summaries
    @staticmethod
    def describe(v: Any, limit: int = 40) -> str:
        """Cheap one-line summary (no heap, no user code) for boundary args/returns."""
        t = type(v)
        try:
            if v is None or t is bool or t is float:
                return repr(v)
            if t is int:
                return repr(v) if v.bit_length() < 64 else f"<int {v.bit_length()} bits>"
            if t is str:
                return repr(v[:limit]) + ("…" if len(v) > limit else "")
            if isinstance(v, list):
                return f"{t.__name__}(len={list.__len__(v)})"
            if isinstance(v, tuple):
                return f"{t.__name__}(len={tuple.__len__(v)})"
            if isinstance(v, dict):
                return f"{t.__name__}(len={dict.__len__(v)})"
        except Exception:  # pragma: no cover - defensive
            pass
        return t.__name__
