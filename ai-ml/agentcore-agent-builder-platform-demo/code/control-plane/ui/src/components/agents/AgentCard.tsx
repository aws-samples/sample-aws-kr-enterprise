'use client';

import Link from 'next/link';
import { Eye, Play, PenTool } from 'lucide-react';
import AgentStatusBadge from './AgentStatusBadge';
import type { AgentConfig } from '@/lib/types';

interface Props {
  agent: AgentConfig;
  runtimeStatus?: string;
}

export default function AgentCard({
  agent,
  runtimeStatus = 'stopped',
}: Props) {
  return (
    <div className="bg-[var(--surface)] rounded-xl border border-[var(--border)] shadow-[0_2px_12px_rgba(0,0,0,0.3)] hover:shadow-[0_8px_32px_rgba(139,92,246,0.15)] hover:border-[var(--purple)] transition-all duration-200 overflow-hidden">
      <div className="p-5">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-semibold text-white">{agent.name}</h3>
          <AgentStatusBadge status={runtimeStatus} />
        </div>
        <p className="text-[var(--text-dim)] text-sm mb-4 line-clamp-2">
          {agent.contextBoundary}
        </p>
        <div className="flex flex-wrap gap-1.5 mb-4">
          {(agent.gateways ?? []).map((gw) => (
            <span
              key={gw.gatewayId}
              className="px-2 py-0.5 bg-[#0ea5e9]/10 text-[#38bdf8] rounded text-xs border border-[#0ea5e9]/20"
            >
              {gw.gatewayId}
            </span>
          ))}
          {(agent.delegations ?? []).map((d) => (
            <span
              key={d.targetAgent}
              className="px-2 py-0.5 bg-[var(--red-dim)] text-[var(--red)] rounded text-xs border border-[var(--red)]/20"
            >
              A2A &rarr; {d.targetAgent}
            </span>
          ))}
        </div>
        <div className="flex gap-2">
          <Link
            href={`/agents/${agent.agentId}`}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--purple)] text-white rounded text-xs hover:bg-[#7c3aed] transition-colors"
          >
            <Eye size={12} /> Detail
          </Link>
          <Link
            href={`/agents/${agent.agentId}/playground`}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--border)] text-white rounded text-xs hover:bg-[var(--border-hover)] transition-colors"
          >
            <Play size={12} /> Playground
          </Link>
          <Link
            href={`/agents/${agent.agentId}/design`}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--border)] text-white rounded text-xs hover:bg-[var(--border-hover)] transition-colors"
          >
            <PenTool size={12} /> Design
          </Link>
        </div>
      </div>
    </div>
  );
}
