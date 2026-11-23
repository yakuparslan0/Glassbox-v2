"""Regenerate the shared trace fixtures (real runner, ground-truth `snapshot` events, small keyframe interval).

The Python state reducer (backend/tests/helpers.py) and the frontend reducer (vitest) are both checked against the
snapshots stored in these files. Usage: python scripts/gen_fixtures.py [name ...]   (no names = regenerate every fixture)
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from glassbox.sandbox.manager import RunRequest, SandboxManager

OUT = ROOT / "backend" / "tests" / "fixtures" / "traces"
SAMPLES = {
    "json_boundary": "import json\nd = {'a': [1, 2, 3]}\nprint(json.dumps(d))\nxs = sorted([3, 1, 2], key=lambda v: -v)\n",
    "generator": "def gen(n):\n    for i in range(n):\n        yield i * i\n\ntotal = sum(gen(5))\nprint(total)\n",
    "nested_objects": (
        "class Node:\n    def __init__(self, v):\n        self.v = v\n        self.next = None\n\n"
        "a = Node(1)\nb = Node(2)\na.next = b\nb.next = a\nb.v = 5\nitems = [a, b, a]\n"
    ),
    # 5 nodes reachable only through `head`: deeper than the serializer depth limit (3)
    "deep_linked_list": (
        "class Node:\n    def __init__(self, val):\n        self.val = val\n        self.next = None\n\n"
        "head = None\nfor v in [5, 4, 3, 2, 1]:\n    node = Node(v)\n    node.next = head\n    head = node\n"
        "del node\ncur = head\nwhile cur is not None:\n    cur = cur.next\n"
    ),
}
EXAMPLES = ["fib_naive.py", "alias_bug.py", "dead_code_zoo.py", "bubble_sort.py", "crash_chain.py", "infinite_loop.py"]


async def record(source: str, entry: str) -> list[dict]:
    request = RunRequest(
        files={entry: source}, entry=entry, full_snapshots=True, keyframe_interval=7, limits={"max_steps": 600}
    )
    return [event async for event in SandboxManager().run(request)]


async def main(only: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = {Path(n).stem: ((ROOT / "examples" / n).read_text(encoding="utf-8"), n) for n in EXAMPLES}
    jobs.update({name: (src, "main.py") for name, src in SAMPLES.items()})
    unknown = set(only) - set(jobs)
    if unknown:
        raise SystemExit(f"unknown fixture(s): {', '.join(sorted(unknown))}")
    for name, (source, entry) in jobs.items():
        if only and name not in only:
            continue
        events = await record(source, entry)
        path = OUT / f"{name}.ndjson"
        path.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events), encoding="utf-8", newline="\n")
        print(f"{path.relative_to(ROOT)}: {len(events)} events")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
