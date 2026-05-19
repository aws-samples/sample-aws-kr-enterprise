'use client';

import { useMemo } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  Handle,
  Position,
  type Node,
  type Edge,
} from '@xyflow/react';
import dagre from 'dagre';
import type { AgentConfig } from '@/lib/types';
import '@xyflow/react/dist/style.css';

function AgentNode({
  data,
}: {
  data: {
    label: string;
    boundary: string;
    model: string;
    gatewayCount: number;
    delegationCount: number;
  };
}) {
  return (
    <div className="rounded-xl border-2 border-[var(--purple)] bg-[var(--surface)] min-w-[220px] max-w-[280px] shadow-[0_4px_24px_rgba(0,0,0,0.3)]">
      <Handle type="source" position={Position.Right} className="!bg-[var(--purple)]" />
      <Handle type="source" position={Position.Bottom} id="bottom" className="!bg-[var(--border)]" />

      <div className="flex items-center gap-2 px-4 pt-3 pb-1">
        <span className="px-2 py-0.5 rounded-md text-[10px] font-bold uppercase tracking-wide text-white bg-[var(--purple)]">
          Agent
        </span>
        <span className="ml-auto flex items-center gap-1 text-[10px] font-semibold text-[#10b981] bg-[#10b981]/15 px-2 py-0.5 rounded-full">
          <span className="w-1.5 h-1.5 rounded-full bg-[#10b981]" />
          Ready
        </span>
      </div>

      <div className="px-4 pb-1 text-sm font-semibold text-white">{data.label}</div>

      <div className="px-4 pb-3 text-xs text-[var(--text-dim)] space-y-0.5">
        <div className="flex justify-between">
          <span className="text-[var(--text-muted)]">Context</span>
          <span className="font-mono text-[11px]">{data.boundary}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-[var(--text-muted)]">Model</span>
          <span className="font-mono text-[11px] text-[#22d3ee]">{data.model}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-[var(--text-muted)]">Delegations</span>
          <span className="font-mono text-[11px]">{data.delegationCount} agents</span>
        </div>
        <div className="flex justify-between">
          <span className="text-[var(--text-muted)]">Gateways</span>
          <span className="font-mono text-[11px]">{data.gatewayCount} connected</span>
        </div>
      </div>
    </div>
  );
}

function GatewayNode({
  data,
}: {
  data: { gatewayId: string; toolInfo: string; tools: string[] };
}) {
  return (
    <div className="rounded-xl border-2 border-[#0ea5e9] bg-[var(--surface)] min-w-[200px] max-w-[240px] shadow-[0_4px_24px_rgba(0,0,0,0.3)]">
      <Handle type="target" position={Position.Left} className="!bg-[#0ea5e9]" />

      <div className="px-4 pt-3 pb-1">
        <span className="px-2 py-0.5 rounded-md text-[10px] font-bold uppercase tracking-wide text-white bg-[#0ea5e9]">
          Gateway
        </span>
      </div>

      <div className="px-4 pb-1 text-sm font-semibold text-white">{data.gatewayId}</div>

      <div className="px-4 pb-2 text-xs">
        <div className="flex justify-between">
          <span className="text-[var(--text-muted)]">Tools</span>
          <span className="text-[var(--text-dim)] font-mono text-[11px]">{data.toolInfo}</span>
        </div>
      </div>

      {data.tools.length > 0 && (
        <div className="px-4 pb-3 pt-2 border-t border-[var(--border)] flex flex-wrap gap-1">
          {data.tools.map((t) => (
            <span
              key={t}
              className="px-2 py-0.5 bg-[#0ea5e9]/10 text-[#38bdf8] rounded text-[10px] border border-[#0ea5e9]/20"
            >
              {t}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function DelegationNode({
  data,
}: {
  data: { target: string; purpose: string; condition: string; timeout: number };
}) {
  return (
    <div className="rounded-xl border-2 border-[#f59e0b] bg-[var(--surface)] min-w-[200px] max-w-[240px] shadow-[0_4px_24px_rgba(0,0,0,0.3)]">
      <Handle type="target" position={Position.Left} className="!bg-[#f59e0b]" />
      <Handle type="source" position={Position.Bottom} id="dep-out" className="!bg-[#E4FF30]" />
      <Handle type="target" position={Position.Top} id="dep-in" className="!bg-[#E4FF30]" />

      <div className="px-4 pt-3 pb-1">
        <span className="px-2 py-0.5 rounded-md text-[10px] font-bold uppercase tracking-wide text-white bg-[#f59e0b]">
          A2A
        </span>
      </div>

      <div className="px-4 pb-1 text-sm font-semibold text-white">{data.target}</div>

      <div className="px-4 pb-3 text-xs text-[var(--text-dim)] space-y-0.5">
        <div className="flex justify-between">
          <span className="text-[var(--text-muted)]">Purpose</span>
          <span className="font-mono text-[11px]">{data.purpose}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-[var(--text-muted)]">Condition</span>
          <span className="font-mono text-[11px]">{data.condition}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-[var(--text-muted)]">Timeout</span>
          <span className="font-mono text-[11px] text-[#22d3ee]">{data.timeout}s</span>
        </div>
      </div>
    </div>
  );
}

function HarnessNode({ data }: { data: { hooks: string[] } }) {
  return (
    <div className="rounded-xl border-2 border-dashed border-[#EA5455] bg-[var(--surface)] min-w-[220px] max-w-[280px] shadow-[0_4px_24px_rgba(0,0,0,0.3)]">
      <Handle type="target" position={Position.Top} className="!bg-[#EA5455]" />

      <div className="flex items-center gap-2 px-4 pt-3 pb-2">
        <span className="px-2 py-0.5 rounded-md text-[10px] font-bold uppercase tracking-wide text-white bg-[#EA5455]">
          Harness
        </span>
        <span className="ml-auto text-[10px] text-[var(--text-muted)]">Tier 2</span>
      </div>

      <div className="px-4 pb-3 flex flex-wrap gap-1">
        {data.hooks.map((h) => {
          const isPost = h.startsWith('post:');
          const isHitl = h.startsWith('hitl:');
          const color = isHitl ? '#c084fc' : isPost ? '#fbbf24' : '#f87171';
          const bgColor = isHitl
            ? 'rgba(139,92,246,0.1)'
            : isPost
              ? 'rgba(245,158,11,0.1)'
              : 'rgba(234,84,85,0.1)';
          const borderColor = isHitl
            ? 'rgba(139,92,246,0.2)'
            : isPost
              ? 'rgba(245,158,11,0.2)'
              : 'rgba(234,84,85,0.2)';
          return (
            <span
              key={h}
              className="px-2 py-0.5 rounded text-[10px] border"
              style={{ background: bgColor, color, borderColor }}
            >
              {h}
            </span>
          );
        })}
      </div>
    </div>
  );
}

const nodeTypes = {
  agentNode: AgentNode,
  gatewayNode: GatewayNode,
  delegationNode: DelegationNode,
  harnessNode: HarnessNode,
};

export default function WorkflowCanvas({ config }: { config: AgentConfig }) {
  const { nodes, edges } = useMemo(() => {
    const g = new dagre.graphlib.Graph();
    g.setDefaultEdgeLabel(() => ({}));
    g.setGraph({ rankdir: 'LR', nodesep: 40, ranksep: 100 });

    const flowNodes: Node[] = [];
    const flowEdges: Edge[] = [];

    const agentId = 'agent-center';
    g.setNode(agentId, { width: 260, height: 180 });
    flowNodes.push({
      id: agentId,
      type: 'agentNode',
      position: { x: 0, y: 0 },
      data: {
        label: config.name,
        boundary: config.contextBoundary,
        model: config.model,
        gatewayCount: (config.gateways ?? []).length,
        delegationCount: (config.delegations ?? []).length,
      },
    });

    (config.gateways ?? []).forEach((gw, i) => {
      const nid = `gw-${i}`;
      const tools: string[] = Array.isArray(gw.toolFilter)
        ? gw.toolFilter
        : [];
      const toolInfo =
        gw.toolFilter === 'all'
          ? 'All tools'
          : Array.isArray(gw.toolFilter)
            ? `${gw.toolFilter.length} tools`
            : String(gw.toolFilter);
      g.setNode(nid, { width: 220, height: tools.length > 0 ? 130 : 90 });
      flowNodes.push({
        id: nid,
        type: 'gatewayNode',
        position: { x: 0, y: 0 },
        data: { gatewayId: gw.gatewayId, toolInfo, tools },
      });
      g.setEdge(agentId, nid);
      flowEdges.push({
        id: `e-agent-gw-${i}`,
        source: agentId,
        target: nid,
        animated: true,
        style: { stroke: '#0ea5e9' },
        label: 'MCP',
        labelStyle: { fill: '#38bdf8', fontSize: 10, fontWeight: 600 },
        labelBgStyle: { fill: '#0ea5e9', fillOpacity: 1 },
        labelBgPadding: [6, 4] as [number, number],
        labelBgBorderRadius: 8,
      });
    });

    (config.delegations ?? []).forEach((d, i) => {
      const nid = `del-${i}`;
      g.setNode(nid, { width: 220, height: 120 });
      flowNodes.push({
        id: nid,
        type: 'delegationNode',
        position: { x: 0, y: 0 },
        data: {
          target: d.targetAgent,
          purpose: d.purpose,
          condition: d.condition,
          timeout: d.timeout,
        },
      });
      g.setEdge(agentId, nid);
      flowEdges.push({
        id: `e-agent-del-${i}`,
        source: agentId,
        target: nid,
        style: { stroke: '#f59e0b', strokeDasharray: '6,4' },
        label: 'A2A',
        labelStyle: { fill: '#fff', fontSize: 10, fontWeight: 600 },
        labelBgStyle: { fill: '#f59e0b', fillOpacity: 1 },
        labelBgPadding: [6, 4] as [number, number],
        labelBgBorderRadius: 8,
      });

      if (i > 0) {
        const prevNid = `del-${i - 1}`;
        g.setEdge(prevNid, nid);
        flowEdges.push({
          id: `e-dep-${i - 1}-${i}`,
          source: prevNid,
          sourceHandle: 'dep-out',
          target: nid,
          targetHandle: 'dep-in',
          style: { stroke: '#E4FF30', strokeDasharray: '3,3' },
          label: 'depends',
          labelStyle: { fill: '#E4FF30', fontSize: 10, fontWeight: 600 },
          labelBgStyle: { fill: '#1e2d40', fillOpacity: 1, stroke: '#E4FF30', strokeWidth: 1 },
          labelBgPadding: [6, 4] as [number, number],
          labelBgBorderRadius: 8,
        });
      }
    });

    const allHooks = [
      ...(config.harness?.preHooks ?? []).map((h) => `pre: ${h}`),
      ...(config.harness?.postHooks ?? []).map((h) => `post: ${h}`),
      ...(config.harness?.hitlActions ?? []).map((h) => `hitl: ${h}`),
    ];
    if (allHooks.length > 0) {
      const hid = 'harness';
      g.setNode(hid, { width: 260, height: 80 });
      flowNodes.push({
        id: hid,
        type: 'harnessNode',
        position: { x: 0, y: 0 },
        data: { hooks: allHooks },
      });
      g.setEdge(agentId, hid);
      flowEdges.push({
        id: 'e-agent-harness',
        source: agentId,
        sourceHandle: 'bottom',
        target: hid,
        style: { stroke: '#EA5455', strokeDasharray: '4,4', opacity: 0.6 },
      });
    }

    dagre.layout(g);

    flowNodes.forEach((node) => {
      const pos = g.node(node.id);
      if (pos) {
        node.position = {
          x: pos.x - (pos.width ?? 0) / 2,
          y: pos.y - (pos.height ?? 0) / 2,
        };
      }
    });

    return { nodes: flowNodes, edges: flowEdges };
  }, [config]);

  return (
    <div className="bg-[var(--bg)] rounded-lg border border-[var(--border)] h-[400px]">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        minZoom={0.3}
        maxZoom={1.5}
        defaultEdgeOptions={{ type: 'smoothstep' }}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="var(--border)" gap={20} />
        <Controls className="!bg-[var(--surface)] !border-[var(--border)] [&>button]:!bg-[var(--surface)] [&>button]:!border-[var(--border)] [&>button]:!text-[var(--text-dim)]" />
      </ReactFlow>
    </div>
  );
}
