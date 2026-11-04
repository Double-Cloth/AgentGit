import React, { useMemo } from 'react';
import { ReactFlow, Background, Controls, type Node, type Edge } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import type { JsonObject } from '../shared/bridge';
import { str, short } from './ui';

export default function HistoryGraph({ commits, selected, onSelect }: { commits: JsonObject[]; selected: string; onSelect: (id: string) => void }) {
  const { nodes, edges } = useMemo(() => {
    const ordered = [...commits].reverse();
    const ids = new Set(ordered.map(item => str(item.commit_id)));
    const nodes: Node[] = ordered.map((item, index) => ({ id: str(item.commit_id), position: { x: 70 + index % 2 * 90, y: index * 96 },
      data: { label: <><span className="small muted">检查点 {index + 1}</span><br /><code>{short(item.commit_id)}</code></> },
      selected: item.commit_id === selected, ariaLabel: `检查点 ${str(item.commit_id)}` }));
    const edges: Edge[] = ordered.flatMap(item => Array.isArray(item.parents) ? item.parents.flatMap(parent =>
      ids.has(str(parent)) ? [{ id: `${str(parent)}:${str(item.commit_id)}`, source: str(parent), target: str(item.commit_id) }] : []) : []);
    return { nodes, edges };
  }, [commits, selected]);
  return <div className="history-graph" aria-label="检查点关系图"><ReactFlow nodes={nodes} edges={edges} fitView minZoom={0.25} maxZoom={1.5}
    nodesDraggable={false} nodesConnectable={false} onNodesChange={changes => {
      for (const change of changes) if (change.type === 'select' && change.selected && change.id !== selected) { onSelect(change.id); break; }
    }} proOptions={{ hideAttribution: false }}>
    <Background color="#e6dfd8" gap={20} /><Controls showInteractive={false} /></ReactFlow></div>;
}
