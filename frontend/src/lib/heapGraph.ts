// Pure heap/alias graph construction: frames on the left (x = 0), heap objects laid out by detected
// data-structure kind on the right (x >= GRAPH_X). References between objects are labelled arrows.
import dagre from "dagre";
import { MarkerType, type Edge, type Node } from "@xyflow/react";
import type { HeapEntry, HeapMap, Val, ViewState } from "../types/model";
import { formatVal } from "./format";

export type StructureKind = "tree" | "linked_list" | "graph";
export type HandleDir = "TB" | "LR";

export interface BoxData extends Record<string, unknown> {
  title: string;
  rows: string[];
  alias: number; // how many different names point here (>= 2 = aliasing)
  kind: "frame" | "object";
  structure?: StructureKind; // only for kind === "object"
  inCycle?: boolean; // object lies on a directed reference cycle
  dir: HandleDir; // arrows enter/leave top/bottom (TB) or left/right (LR)
  isNew?: boolean; // object did not exist at the previous step
  isUpdated?: boolean; // a primitive field (or list/dict content) changed since the previous step
  changedRows?: number[]; // indices into `rows` that changed
  nullFields?: string[]; // structural pointers (left/right/next/prev) that are None (tree / linked_list only)
}

export const GRAPH_X = 320;
const TREE_FIELDS = ["left", "right"];
const LIST_FIELDS = ["next", "prev"];
// Back-pointers: drawn, but ignored when ranking so a doubly linked list / parent link keeps its natural direction.
const BACK_FIELDS = new Set(["prev", "parent"]);
const COMPONENT_GAP = 60;

/** DFS (white/gray/black) over object->object references: back edges close a cycle (`cycleEdges`). Members are
 * the strongly connected components with a cycle (Tarjan), so nodes reached via cross edges are not missed.
 * Back-pointer fields (prev/parent) are ignored so a doubly linked list / parent links are not reported as cycles. */
export function findCycles(objects: string[], refEdges: RefEdge[]): { cycleEdges: Set<string>; inCycle: Set<string> } {
  const adj = new Map<string, RefEdge[]>(objects.map((o) => [o, []]));
  for (const e of refEdges) if (!BACK_FIELDS.has(e.label)) adj.get(e.source)?.push(e);
  const state = new Map<string, 1 | 2>(); // 1 = on the DFS stack (gray), 2 = finished (black)
  const index = new Map<string, number>();
  const low = new Map<string, number>();
  const sccStack: string[] = [];
  const onScc = new Set<string>();
  const cycleEdges = new Set<string>();
  const inCycle = new Set<string>();
  const dfs = (u: string): void => {
    state.set(u, 1);
    index.set(u, index.size);
    low.set(u, index.get(u) as number);
    sccStack.push(u);
    onScc.add(u);
    for (const e of adj.get(u) ?? []) {
      const st = state.get(e.target);
      if (st === 1) cycleEdges.add(`${e.source}.${e.label}->${e.target}`);
      if (st === undefined) {
        dfs(e.target);
        low.set(u, Math.min(low.get(u) as number, low.get(e.target) as number));
      } else if (onScc.has(e.target)) low.set(u, Math.min(low.get(u) as number, index.get(e.target) as number));
    }
    if (low.get(u) === index.get(u)) {
      const comp: string[] = [];
      let w: string;
      do {
        w = sccStack.pop() as string;
        onScc.delete(w);
        comp.push(w);
      } while (w !== u);
      const selfLoop = (adj.get(u) ?? []).some((e) => e.target === u);
      if (comp.length > 1 || selfLoop) for (const n of comp) inCycle.add(n);
    }
    state.set(u, 2);
  };
  for (const o of objects) if (!state.has(o)) dfs(o);
  return { cycleEdges, inCycle };
}

function markChanged(ed: Edge): void {
  ed.data = { ...ed.data, isChanged: true };
  ed.animated = true;
  ed.className = "gb-edge-changed";
}

interface RefEdge {
  source: string;
  target: string;
  label: string;
}

/** Outgoing reference fields of a heap entry, each with the name to draw on the arrow. */
function outgoing(label: string, entry: HeapEntry, heap: HeapMap): RefEdge[] {
  const out: RefEdge[] = [];
  const add = (name: string, v: unknown): void => {
    if (v && typeof v === "object" && "ref" in v && heap[(v as { ref: string }).ref]) {
      out.push({ source: label, target: (v as { ref: string }).ref, label: name });
    }
  };
  if (entry.k === "obj") for (const [name, v] of Object.entries(entry.attrs)) add(name, v);
  else if (entry.k === "seq") entry.items.forEach((v, i) => add(`[${i}]`, v));
  else if (entry.k === "map") entry.pairs.forEach(([a, b], i) => { add(`key${i}`, a); add(`[${i}]`, b); });
  return out;
}

const isNone = (v: Val | undefined): boolean => !!v && !("ref" in v) && "t" in v && v.t === "NoneType";

/** Tree if any object has a left/right pointer field, linked list for next/prev, otherwise a general graph.
 * A pointer field is a reference or None, so a lone node (`left = right = None`) is already a tree. */
export function detectStructure(labels: string[], heap: HeapMap): StructureKind {
  const hasRefField = (fields: string[]): boolean =>
    labels.some((l) => {
      const e = heap[l];
      return e.k === "obj" && fields.some((f) => !!e.attrs[f] && ("ref" in e.attrs[f] || isNone(e.attrs[f])));
    });
  if (hasRefField(TREE_FIELDS)) return "tree";
  if (hasRefField(LIST_FIELDS)) return "linked_list";
  return "graph";
}

function boxSize(rows: string[], alias: number): { w: number; h: number } {
  const longest = Math.max(10, ...rows.map((r) => r.length));
  return { w: Math.min(280, Math.max(140, longest * 7 + 24)), h: 34 + rows.length * 16 + (alias > 1 ? 20 : 0) };
}

function frameList(view: ViewState): { id: string; title: string; vars: Record<string, Val> }[] {
  return [
    { id: "g", title: "globals", vars: view.globals },
    ...view.stack.filter((f) => f.frame !== 1).map((f) => ({ id: `f${f.frame}`, title: `${f.func}:${f.line}`, vars: f.locals })),
  ];
}

/** Primitive-only rows of an object entry as [name, text] (pointer fields are drawn as arrows instead). */
function primRows(e: HeapEntry, heap: HeapMap): [string, string][] {
  if (e.k !== "obj") return [];
  return Object.entries(e.attrs).filter(([, v]) => !("ref" in v && heap[v.ref])).map(([k, v]) => [k, formatVal(v, heap)]);
}

export interface HeapGraph {
  nodes: Node<BoxData>[];
  edges: Edge[];
  hasCycle: boolean;
}

export function buildHeapGraph(view: ViewState, prevView?: ViewState | null): HeapGraph {
  const heap: HeapMap = view.heap;
  const frames = frameList(view);
  // pointer targets of the previous step, keyed like the edges ("frameId.name" / "oN.field")
  const prevTargets = new Map<string, string>();
  const prevFrames = new Set<string>();
  if (prevView) {
    for (const f of frameList(prevView)) {
      prevFrames.add(f.id);
      for (const [name, v] of Object.entries(f.vars)) if ("ref" in v) prevTargets.set(`${f.id}.${name}`, v.ref);
    }
    for (const [label, e] of Object.entries(prevView.heap)) for (const o of outgoing(label, e, prevView.heap)) prevTargets.set(`${o.source}.${o.label}`, o.target);
  }
  const incoming = new Map<string, Set<string>>(); // object -> distinct referrers
  const addIncoming = (target: string, who: string): void => {
    if (!incoming.has(target)) incoming.set(target, new Set());
    incoming.get(target)?.add(who);
  };
  const edges: Edge[] = [];
  const objects: string[] = [];
  const seen = new Set<string>();
  const objEdges: RefEdge[] = [];
  const visit = (label: string): void => {
    if (seen.has(label) || !heap[label]) return;
    seen.add(label);
    objects.push(label);
    for (const e of outgoing(label, heap[label], heap)) {
      objEdges.push(e);
      visit(e.target);
    }
  };
  for (const f of frames) {
    for (const [name, v] of Object.entries(f.vars)) {
      if (!("ref" in v)) continue;
      visit(v.ref);
      addIncoming(v.ref, `${f.id}.${name}`);
      const ed: Edge = { id: `${f.id}.${name}`, source: f.id, target: v.ref, label: name, markerEnd: { type: MarkerType.ArrowClosed } };
      if (prevView && prevFrames.has(f.id) && prevTargets.get(ed.id) !== v.ref) markChanged(ed);
      edges.push(ed);
    }
  }
  // Object -> object arrows carry the field name; id includes it so left/right to one node never collide.
  const objSeen = new Set<string>();
  for (const e of objEdges) {
    const id = `${e.source}.${e.label}->${e.target}`;
    if (objSeen.has(id)) continue;
    objSeen.add(id);
    const ed: Edge = { id, source: e.source, target: e.target, label: e.label, markerEnd: { type: MarkerType.ArrowClosed } };
    if (prevView && prevView.heap[e.source] && prevTargets.get(`${e.source}.${e.label}`) !== e.target) markChanged(ed);
    edges.push(ed);
    addIncoming(e.target, `${e.source}->`);
  }

  // Weakly connected components of the object graph, each laid out on its own.
  const adj = new Map<string, Set<string>>(objects.map((o) => [o, new Set<string>()]));
  for (const e of objEdges) {
    adj.get(e.source)?.add(e.target);
    adj.get(e.target)?.add(e.source);
  }
  const compOf = new Map<string, number>();
  const comps: string[][] = [];
  for (const o of objects) {
    if (compOf.has(o)) continue;
    const members: string[] = [];
    const stack = [o];
    compOf.set(o, comps.length);
    while (stack.length) {
      const cur = stack.pop() as string;
      members.push(cur);
      for (const n of adj.get(cur) ?? []) {
        if (!compOf.has(n)) {
          compOf.set(n, comps.length);
          stack.push(n);
        }
      }
    }
    comps.push(members);
  }

  const { cycleEdges, inCycle } = findCycles(objects, objEdges);
  for (const ed of edges) {
    if (cycleEdges.has(ed.id)) ed.data = { ...ed.data, isCycleEdge: true };
  }
  const nodes: Node<BoxData>[] = [];
  let y = 0;
  for (const f of frames) {
    const rows = Object.entries(f.vars).map(([k, v]) => `${k} = ${"ref" in v ? `→ ${v.ref}` : formatVal(v, heap)}`);
    nodes.push({ id: f.id, type: "box", position: { x: 0, y }, data: { title: f.title, rows, alias: 0, kind: "frame", dir: "LR" } });
    y += 36 + rows.length * 16 + 24;
  }

  const diffOf = (label: string): Partial<BoxData> => {
    if (!prevView) return {};
    const before = prevView.heap[label];
    if (!before) return { isNew: true };
    const now = heap[label];
    if (now.k === "obj" && before.k === "obj") {
      const was = new Map(primRows(before, prevView.heap));
      const rows = primRows(now, heap);
      const changedRows = rows.flatMap(([k, v], i) => (was.get(k) !== v ? [i] : []));
      return changedRows.length ? { isUpdated: true, changedRows } : {};
    }
    return formatVal({ ref: label }, heap) !== formatVal({ ref: label }, prevView.heap) ? { isUpdated: true, changedRows: [0] } : {};
  };
  const nullsOf = (label: string, structure: StructureKind): Partial<BoxData> => {
    const e = heap[label];
    if (structure === "graph" || e.k !== "obj") return {};
    const nullFields = [...TREE_FIELDS, ...LIST_FIELDS].filter((f) => isNone(e.attrs[f]));
    return nullFields.length ? { nullFields } : {};
  };

  let offsetY = 0;
  comps.forEach((members, ci) => {
    const structure = detectStructure(members, heap);
    const dir: HandleDir = structure === "tree" ? "TB" : "LR";
    const info = new Map<string, { rows: string[]; alias: number; w: number; h: number }>();
    for (const label of members) {
      const e = heap[label];
      const alias = [...(incoming.get(label) ?? [])].filter((s) => !s.endsWith("->")).length;
      const rows = e.k === "obj" ? primRows(e, heap).map(([k, v]) => `${k} = ${v}`) : [formatVal({ ref: label }, heap)];
      info.set(label, { rows, alias, ...boxSize(rows, alias) });
    }
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: dir, nodesep: 40, ranksep: 70, acyclicer: "greedy" });
    g.setDefaultEdgeLabel(() => ({}));
    for (const label of members) g.setNode(label, { width: info.get(label)?.w ?? 140, height: info.get(label)?.h ?? 50 });
    for (const e of objEdges) {
      if (compOf.get(e.source) !== ci || e.source === e.target) continue;
      if (structure !== "graph" && BACK_FIELDS.has(e.label)) continue;
      if (structure === "tree" && !TREE_FIELDS.includes(e.label) && LIST_FIELDS.includes(e.label)) continue;
      g.setEdge(e.source, e.target);
    }
    dagre.layout(g);
    let maxY = 0;
    for (const label of members) {
      const p = g.node(label);
      const d = info.get(label) as { rows: string[]; alias: number; w: number; h: number };
      const top = p.y - d.h / 2 + offsetY;
      maxY = Math.max(maxY, p.y + d.h / 2);
      nodes.push({
        id: label,
        type: "box",
        position: { x: GRAPH_X + p.x - d.w / 2, y: top },
        data: { title: `${label} · ${heap[label].t}`, rows: d.rows, alias: d.alias, kind: "object", structure, dir, ...diffOf(label), ...nullsOf(label, structure), ...(inCycle.has(label) ? { inCycle: true } : {}) },
      });
    }
    offsetY += maxY + COMPONENT_GAP;
  });

  for (const ed of edges) {
    const refs = [...(incoming.get(ed.target) ?? [])].filter((s) => !s.endsWith("->")).length;
    const aliased = refs > 1;
    const changed = ed.className === "gb-edge-changed";
    ed.className = [ed.data?.isCycleEdge ? "gb-edge-cycle" : "", changed ? "gb-edge-changed" : "", aliased ? "gb-edge-alias" : ""].filter(Boolean).join(" ");
    ed.animated = aliased || changed;
  }
  return { nodes, edges, hasCycle: cycleEdges.size > 0 };
}
