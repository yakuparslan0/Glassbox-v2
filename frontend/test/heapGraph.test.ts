import { describe, expect, it } from "vitest";
import { buildHeapGraph, GRAPH_X } from "../src/lib/heapGraph";
import type { HeapMap, Val, ViewState } from "../src/types/model";

const int = (n: number): Val => ({ k: "prim", t: "int", r: String(n) }) as Val;
const ref = (l: string): Val => ({ ref: l });
const none: Val = { k: "prim", t: "NoneType", r: "None" } as Val;
const obj = (t: string, attrs: Record<string, Val>): HeapMap[string] => ({ k: "obj", t, attrs, trunc: false }) as HeapMap[string];

function view(globals: Record<string, Val>, heap: HeapMap): ViewState {
  return { stack: [], globals, heap };
}

function overlaps(a: { x: number; y: number }, b: { x: number; y: number }): boolean {
  return Math.abs(a.x - b.x) < 140 && Math.abs(a.y - b.y) < 50;
}

describe("buildHeapGraph", () => {
  it("linked list: LR chain, labelled next arrows, pointer fields not repeated inside boxes", () => {
    const heap: HeapMap = {
      o1: obj("Node", { val: int(1), next: ref("o2") }),
      o2: obj("Node", { val: int(2), next: ref("o3") }),
      o3: obj("Node", { val: int(3), next: none }),
    };
    const { nodes, edges } = buildHeapGraph(view({ head: ref("o1") }, heap));
    const o = (id: string) => nodes.find((n) => n.id === id)!;
    expect(o("o1").data.structure).toBe("linked_list");
    expect(o("o1").data.dir).toBe("LR");
    expect(o("o1").data.rows).toEqual(["val = 1"]);
    expect(o("o3").data.rows).toEqual(["val = 3", "next = None"]);
    expect(o("o1").position.x).toBeGreaterThanOrEqual(GRAPH_X);
    expect(o("o1").position.x).toBeLessThan(o("o2").position.x);
    expect(o("o2").position.x).toBeLessThan(o("o3").position.x);
    expect(edges.filter((e) => e.source.startsWith("o")).map((e) => [e.id, e.label])).toEqual([
      ["o1.next->o2", "next"],
      ["o2.next->o3", "next"],
    ]);
    expect(o("g").position.x).toBe(0);
  });

  it("binary tree: TB layout, left/right below the parent, no overlaps; same child twice keeps distinct edge ids", () => {
    const heap: HeapMap = {
      o1: obj("TreeNode", { val: int(5), left: ref("o2"), right: ref("o3") }),
      o2: obj("TreeNode", { val: int(3), left: none, right: none }),
      o3: obj("TreeNode", { val: int(8), left: ref("o4"), right: ref("o4") }),
      o4: obj("TreeNode", { val: int(9), left: none, right: none }),
    };
    const { nodes, edges } = buildHeapGraph(view({ root: ref("o1") }, heap));
    const o = (id: string) => nodes.find((n) => n.id === id)!;
    expect(o("o1").data.structure).toBe("tree");
    expect(o("o1").data.dir).toBe("TB");
    expect(o("o2").position.y).toBeGreaterThan(o("o1").position.y);
    expect(o("o4").position.y).toBeGreaterThan(o("o3").position.y);
    const ids = edges.map((e) => e.id);
    expect(ids).toContain("o3.left->o4");
    expect(ids).toContain("o3.right->o4");
    expect(new Set(ids).size).toBe(ids.length);
    const objs = nodes.filter((n) => n.data.kind === "object");
    for (let i = 0; i < objs.length; i++) for (let j = i + 1; j < objs.length; j++) expect(overlaps(objs[i].position, objs[j].position)).toBe(false);
  });

  it("cyclic references: doubly linked list and general graph lay out finitely without overlap", () => {
    const heap: HeapMap = {
      o1: obj("DNode", { val: int(1), next: ref("o2"), prev: none }),
      o2: obj("DNode", { val: int(2), next: none, prev: ref("o1") }),
      o3: obj("V", { name: int(7), neighbors: ref("o5") }),
      o4: obj("V", { name: int(8), neighbors: ref("o5") }),
      o5: { k: "seq", t: "list", len: 2, items: [ref("o3"), ref("o4")], trunc: false } as HeapMap[string],
    };
    const { nodes, edges } = buildHeapGraph(view({ a: ref("o1"), g: ref("o3") }, heap));
    const o = (id: string) => nodes.find((n) => n.id === id)!;
    expect(o("o1").data.structure).toBe("linked_list");
    expect(o("o2").position.x).toBeGreaterThan(o("o1").position.x);
    expect(o("o3").data.structure).toBe("graph");
    expect(o("o3").data.dir).toBe("LR");
    expect(edges.some((e) => e.id === "o2.prev->o1" && e.label === "prev")).toBe(true);
    expect(edges.some((e) => e.id === "o3.neighbors->o5")).toBe(true);
    for (const n of nodes) {
      expect(Number.isFinite(n.position.x)).toBe(true);
      expect(Number.isFinite(n.position.y)).toBe(true);
    }
    const objs = nodes.filter((n) => n.data.kind === "object");
    for (let i = 0; i < objs.length; i++) for (let j = i + 1; j < objs.length; j++) expect(overlaps(objs[i].position, objs[j].position)).toBe(false);
  });

  it("self reference does not crash and aliasing is still detected", () => {
    const heap: HeapMap = { o1: obj("N", { me: ref("o1") }) };
    const { nodes } = buildHeapGraph(view({ a: ref("o1"), b: ref("o1") }, heap));
    expect(nodes.find((n) => n.id === "o1")?.data.alias).toBe(2);
  });

  it("step diff: new object, updated row and re-pointed arrows are flagged; without prevView nothing is", () => {
    const prev = view({ head: ref("o1") }, {
      o1: obj("Node", { val: int(1), next: ref("o2") }),
      o2: obj("Node", { val: int(2), next: none }),
    });
    const cur = view({ head: ref("o2"), old: ref("o1") }, {
      o1: obj("Node", { val: int(10), next: none }),
      o2: obj("Node", { val: int(2), next: ref("o3") }),
      o3: obj("Node", { val: int(3), next: none }),
    });
    const { nodes, edges } = buildHeapGraph(cur, prev);
    const o = (id: string) => nodes.find((n) => n.id === id)!;
    expect(o("o3").data.isNew).toBe(true);
    expect(o("o1").data.isNew).toBeUndefined();
    expect(o("o1").data.isUpdated).toBe(true);
    expect(o("o1").data.changedRows).toContain(0); // val 1 -> 10 (next -> None also adds a row)
    expect(o("o2").data.isUpdated).toBeUndefined();
    const e = (id: string) => edges.find((x) => x.id === id)!;
    expect(e("o2.next->o3").animated).toBe(true);
    expect(e("o2.next->o3").data).toMatchObject({ isChanged: true });
    expect(e("g.head").data).toMatchObject({ isChanged: true }); // head moved o1 -> o2
    const plain = buildHeapGraph(cur);
    expect(plain.nodes.some((n) => n.data.isNew || n.data.isUpdated)).toBe(false);
    expect(plain.edges.some((x) => x.data?.isChanged)).toBe(false);
    // unchanged pointer is not flagged
    const same = buildHeapGraph(prev, prev);
    expect(same.edges.some((x) => x.data?.isChanged)).toBe(false);
  });

  it("null pointers: None left/right/next/prev become badges for tree and list, not for graph", () => {
    const heap: HeapMap = {
      o1: obj("TreeNode", { val: int(5), left: ref("o2"), right: none }),
      o2: obj("TreeNode", { val: int(3), left: none, right: none }),
      o3: obj("Plain", { child: none, x: int(1) }),
    };
    const { nodes } = buildHeapGraph(view({ r: ref("o1"), p: ref("o3") }, heap));
    const o = (id: string) => nodes.find((n) => n.id === id)!;
    expect(o("o1").data.nullFields).toEqual(["right"]);
    expect(o("o2").data.nullFields).toEqual(["left", "right"]);
    expect(o("o3").data.nullFields).toBeUndefined();
  });

  it("cycle detection: acyclic list has none; tail.next = head flags the closing edge and members", () => {
    const list = (tailNext: Val): HeapMap => ({
      o1: obj("Node", { val: int(1), next: ref("o2") }),
      o2: obj("Node", { val: int(2), next: ref("o3") }),
      o3: obj("Node", { val: int(3), next: tailNext }),
    });
    const plain = buildHeapGraph(view({ head: ref("o1") }, list(none)));
    expect(plain.hasCycle).toBe(false);
    expect(plain.edges.some((e) => e.data?.isCycleEdge)).toBe(false);
    expect(plain.nodes.some((n) => n.data.inCycle)).toBe(false);

    const cyc = buildHeapGraph(view({ head: ref("o1") }, list(ref("o1"))));
    expect(cyc.hasCycle).toBe(true);
    expect(cyc.edges.filter((e) => e.data?.isCycleEdge).map((e) => e.id)).toEqual(["o3.next->o1"]);
    expect(cyc.edges.find((e) => e.id === "o3.next->o1")?.className).toContain("gb-edge-cycle");
    expect(cyc.nodes.filter((n) => n.data.inCycle).map((n) => n.id).sort()).toEqual(["o1", "o2", "o3"]);
  });

  it("cycle detection: cycle into the middle excludes the lead-in; self reference counts; prev links do not", () => {
    const mid: HeapMap = {
      o1: obj("Node", { next: ref("o2") }),
      o2: obj("Node", { next: ref("o3") }),
      o3: obj("Node", { next: ref("o2") }),
    };
    const g = buildHeapGraph(view({ h: ref("o1") }, mid));
    expect(g.nodes.filter((n) => n.data.inCycle).map((n) => n.id).sort()).toEqual(["o2", "o3"]);
    const self = buildHeapGraph(view({ a: ref("o1") }, { o1: obj("N", { next: ref("o1") }) }));
    expect(self.hasCycle).toBe(true);
    const dll = buildHeapGraph(view({ a: ref("o1") }, {
      o1: obj("D", { next: ref("o2"), prev: none }),
      o2: obj("D", { next: none, prev: ref("o1") }),
    }));
    expect(dll.hasCycle).toBe(false);
  });

  it("cycle members reached through a cross edge are still marked (A->B->A, A->C->B)", () => {
    const heap: HeapMap = {
      o1: obj("V", { x: ref("o2"), y: ref("o3") }),
      o2: obj("V", { x: ref("o1") }),
      o3: obj("V", { x: ref("o2") }),
      o4: obj("V", { x: ref("o1") }), // leads into the cycle but is not on it
    };
    const g = buildHeapGraph(view({ a: ref("o1"), z: ref("o4") }, heap));
    expect(g.hasCycle).toBe(true);
    expect(g.nodes.filter((n) => n.data.inCycle).map((n) => n.id).sort()).toEqual(["o1", "o2", "o3"]);
  });

  it("a lone node with None pointer fields is already a tree / linked list (badges, stable direction)", () => {
    const g = buildHeapGraph(view({ root: ref("o1"), n: ref("o2") }, {
      o1: obj("TreeNode", { val: int(5), left: none, right: none }),
      o2: obj("Node", { val: int(1), next: none }),
    }));
    const o = (id: string) => g.nodes.find((n) => n.id === id)!;
    expect(o("o1").data.structure).toBe("tree");
    expect(o("o1").data.dir).toBe("TB");
    expect(o("o1").data.nullFields).toEqual(["left", "right"]);
    expect(o("o2").data.structure).toBe("linked_list");
    expect(o("o2").data.nullFields).toEqual(["next"]);
  });
});

describe("buildHeapGraph on a real runner trace", () => {
  it("a 5-node list reachable only through `head` draws every next arrow (no depth-limit cut)", async () => {
    const { loadFixture } = await import("./fixtures");
    const { StateBuilder } = await import("../src/lib/stateBuilder");
    const events = loadFixture("deep_linked_list");
    const builder = new StateBuilder();
    builder.append(events);
    // state right after the list is fully built (before `cur` walks it)
    const built = events.filter((e) => e.type === "line" && e.line === 11)[0];
    for (const jump of [false, true]) {
      const state = builder.stateAtSeq(built.seq, jump);
      const { nodes, edges } = buildHeapGraph(state);
      const list = nodes.filter((n) => n.data.kind === "object");
      expect(list).toHaveLength(5);
      expect(list.every((n) => n.data.structure === "linked_list")).toBe(true);
      expect(list.flatMap((n) => n.data.rows).some((r) => r.includes("<Node object>"))).toBe(false);
      expect(edges.filter((e) => e.label === "next")).toHaveLength(4);
      expect(list.filter((n) => n.data.nullFields?.includes("next"))).toHaveLength(1);
    }
  });
});
