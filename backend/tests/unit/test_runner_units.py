"""Unit tests for runner pieces that do not need a subprocess."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from glassbox.runner.serializer import Serializer, iter_refs
from glassbox.runner.whatif import OverrideError, load_overrides
from glassbox.sandbox.subprocess_backend import build_argv, build_env

RUNNER_DIR = Path(__file__).resolve().parents[2] / "glassbox" / "runner"


def test_runner_package_imports_only_stdlib_and_itself() -> None:
    allowed = set(sys.stdlib_module_names)
    offenders = []
    for path in sorted(RUNNER_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            elif isinstance(node, ast.ImportFrom) and node.level > 2:
                offenders.append(f"{path.name}: relative import beyond the runner package")
            offenders += [f"{path.name}: {n}" for n in names if n not in allowed]
    assert not offenders, offenders


def test_subprocess_is_started_without_isolated_mode_and_with_a_scrubbed_env() -> None:
    argv = build_argv("job.json")
    assert argv[1:] == ["-s", "-P", "-X", "utf8", "-m", "glassbox.runner", "job.json"]
    assert "-I" not in argv  # -I would make the child ignore PYTHONHASHSEED
    env = build_env()
    assert env["PYTHONHASHSEED"] == "0" and env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert set(env) <= {"PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE", "LANG", "SYSTEMROOT"}


# ------------------------------------------------------------------------- serializer
def ser_value(value: object) -> tuple[dict, dict]:
    s = Serializer()
    s.begin()
    inline = s.value(value)
    return inline, s.scratch


def test_primitives_and_limits() -> None:
    assert ser_value(42)[0] == {"k": "prim", "t": "int", "r": "42"}
    assert ser_value(None)[0] == {"k": "prim", "t": "NoneType", "r": "None"}
    long_text, _ = ser_value("x" * 500)
    assert long_text["trunc"] is True and len(long_text["r"]) <= 202
    huge, _ = ser_value(10**5000)  # beyond the int->str conversion limit
    assert huge["trunc"] is True


def test_collections_use_heap_refs_and_cap_items() -> None:
    inline, heap = ser_value(list(range(200)))
    entry = heap[inline["ref"]]
    assert entry["len"] == 200 and len(entry["items"]) == 50 and entry["trunc"] is True
    inline, heap = ser_value({"a": [1, 2], "b": (3,)})
    entry = heap[inline["ref"]]
    assert entry["k"] == "map" and len(entry["pairs"]) == 2
    assert all("ref" in value for _key, value in entry["pairs"])


def test_depth_limit_cycles_and_aliases() -> None:
    deep: list = []
    node = deep
    for _ in range(10):
        child: list = []
        node.append(child)
        node = child
    inline, heap = ser_value(deep)
    assert len(heap) == 3  # depth 3 from the root, the rest is a placeholder
    cyclic: list = []
    cyclic.append(cyclic)
    inline, heap = ser_value(cyclic)
    assert heap[inline["ref"]]["items"] == [{"ref": inline["ref"]}]
    shared = [1]
    s = Serializer()
    s.begin()
    first, second = s.value(shared), s.value(shared)
    assert first == second


def test_user_objects_are_read_without_running_user_code() -> None:
    class Evil:
        def __init__(self) -> None:
            self.a = 1

        def __repr__(self) -> str:
            raise AssertionError("__repr__ must not be called")

        def __getattr__(self, name: str) -> object:
            raise AssertionError("__getattr__ must not be called")

    class Slotted:
        __slots__ = ("x", "y")

        def __init__(self) -> None:
            self.x = 1

    inline, heap = ser_value(Evil())
    assert heap[inline["ref"]]["attrs"] == {"a": {"k": "prim", "t": "int", "r": "1"}}
    inline, heap = ser_value(Slotted())  # `y` is unset: skipped, no crash
    assert list(heap[inline["ref"]]["attrs"]) == ["x"]


def test_fingerprint_changes_on_in_place_mutation_and_labels_are_stable() -> None:
    s = Serializer()
    data = [1, 2]
    s.begin()
    inline_a, fp_a = s.fingerprint(data)
    data.append(3)
    s.begin()
    inline_b, fp_b = s.fingerprint(data)
    assert inline_a == inline_b and fp_a != fp_b
    refs = list(iter_refs({"k": "seq", "items": [inline_a, {"k": "prim", "t": "int", "r": "1"}]}))
    assert refs == [inline_a["ref"]]


# ------------------------------------------------------------------------- what-if parsing
def test_override_parsing() -> None:
    parsed = load_overrides([{"step": 5, "frame_depth": 1, "name": "n", "value_literal": "[1, {'a': 2}]"}])
    assert parsed == {5: [(1, "n", [1, {"a": 2}])]}
    for bad in (
        {"step": 1, "name": "n", "value_literal": "__import__('os')"},
        {"step": 1, "name": "not an identifier", "value_literal": "1"},
        {"step": 0, "name": "n", "value_literal": "1"},
        {"name": "n", "value_literal": "1"},
    ):
        with pytest.raises(OverrideError):
            load_overrides([bad])


def test_depth_limit_keeps_references_to_objects_the_client_already_has() -> None:
    class Node:
        def __init__(self, val: int, nxt: object = None) -> None:
            self.val = val
            self.next = nxt

    fourth = Node(4, Node(5))
    head = Node(1, Node(2, Node(3, fourth)))  # depth 0, 1, 2; `fourth` sits at the depth limit (3)
    s = Serializer()
    s.begin()
    known_label = s.value(fourth)["ref"]  # the client received it earlier (e.g. through its own variable)
    s.known = dict(s.scratch)
    s.begin()
    third = s.scratch[s.scratch[s.scratch[s.value(head)["ref"]]["attrs"]["next"]["ref"]]["attrs"]["next"]["ref"]]
    assert third["attrs"]["val"]["r"] == "3"
    assert third["attrs"]["next"] == {"ref": known_label}  # reference, not a placeholder
    assert known_label not in s.scratch  # and it is not re-serialized past the limit
    fresh = Serializer()  # nothing known: the placeholder contract is unchanged
    fresh.begin()
    fresh.value(head)
    cut = [e["attrs"]["next"] for e in fresh.scratch.values() if e["attrs"]["next"].get("trunc")]
    assert cut == [{"k": "other", "t": "Node", "r": "<Node object>", "trunc": True}]
