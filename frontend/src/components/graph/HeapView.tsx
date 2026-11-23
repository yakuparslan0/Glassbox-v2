import "@xyflow/react/dist/style.css";
import { Background, Controls, Handle, Position, ReactFlow, type Node, type NodeProps } from "@xyflow/react";
import { useMemo, useState } from "react";
import { useT } from "../../i18n";
import { buildHeapGraph, type BoxData } from "../../lib/heapGraph";
import { useViewStore } from "../../store/viewStore";

function Box({ data }: NodeProps<Node<BoxData>>): React.ReactElement {
  const t = useT();
  const vertical = data.dir === "TB";
  return (
    <div className={`gb-box ${data.kind === "object" ? "gb-box-object" : ""} ${data.alias > 1 ? "gb-box-alias" : ""} ${data.structure ? `gb-box-${data.structure}` : ""} ${data.isNew ? "gb-box-new" : ""} ${data.isUpdated ? "gb-box-updated" : ""} ${data.inCycle ? "gb-box-cycle" : ""}`} style={data.w ? { width: data.w, maxWidth: data.w } : undefined}>
      <Handle type="target" position={vertical ? Position.Top : Position.Left} />
      <div className="font-semibold text-xs mb-1 gb-box-title">
        {data.alias > 1 && <span aria-hidden>⛓ </span>}
        {data.title}
      </div>
      {data.alias > 1 && <div className="text-xs gb-chip mb-1">{t("heap.alias", { n: data.alias })}</div>}
      {data.isNew && <div className="gb-badge-new">{t("heap.new")}</div>}
      {data.rows.map((r, i) =>
        data.nullFields?.some((f) => r.startsWith(`${f} = `)) ? null : (
          <div key={i} className={`font-mono text-xs truncate gb-box-row ${data.changedRows?.includes(i) ? "gb-row-changed" : ""}`}>{r}</div>
        ),
      )}
      {data.nullFields && data.nullFields.length > 0 && (
        <div className="gb-null-row">
          {data.nullFields.map((f) => (<span key={f} className="gb-null-badge">{f}: None</span>))}
        </div>
      )}
      <Handle type="source" position={vertical ? Position.Bottom : Position.Right} />
    </div>
  );
}
const nodeTypes = { box: Box };

export function HeapView(): React.ReactElement {
  const t = useT();
  const view = useViewStore((s) => s.state);
  const prevView = useViewStore((s) => s.prevView);
  const [diff, setDiff] = useState(true);
  const graph = useMemo(() => (view ? buildHeapGraph(view, diff ? prevView : null) : null), [view, prevView, diff]);
  if (!graph || !graph.nodes.some((n) => n.data.kind === "object")) return <p className="p-4 text-sm opacity-60">{t("heap.empty")}</p>;
  const added = graph.nodes.filter((n) => n.data.isNew).length;
  const changed = graph.nodes.filter((n) => n.data.isUpdated).length;
  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-2 py-1 text-xs border-b" style={{ borderColor: "var(--border)" }}>
        <label className="flex items-center gap-1 cursor-pointer">
          <input type="checkbox" checked={diff} onChange={(e) => setDiff(e.target.checked)} />
          {t("heap.diff")}
        </label>
        {graph.hasCycle && <span className="gb-cycle-badge" role="alert">⟳ {t("heap.cycle")}</span>}
        {diff && prevView && <span className="gb-chip">{t("heap.diffSummary", { added, changed })}</span>}
      </div>
      <div className="flex-1 min-h-0">
        <ReactFlow nodes={graph.nodes} edges={graph.edges} nodeTypes={nodeTypes} colorMode="dark" fitView nodesDraggable={false} minZoom={0.2}>
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
    </div>
  );
}
