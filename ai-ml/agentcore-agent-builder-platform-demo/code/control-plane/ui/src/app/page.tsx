'use client';

import { useEffect, useState } from 'react';
import { Bot, Play, Link2, Wrench } from 'lucide-react';
import Header from '@/components/common/Header';
import StatsCard from '@/components/dashboard/StatsCard';
import AgentStatusGrid from '@/components/dashboard/AgentStatusGrid';
import { agents as agentsApi, gateways as gatewaysApi } from '@/lib/api-client';
import type { AgentConfig } from '@/lib/types';

interface GatewayItem {
  gatewayId: string;
  name: string;
  description: string;
  status: string;
  toolCount: number;
}

export default function DashboardPage() {
  const [agentList, setAgentList] = useState<AgentConfig[]>([]);
  const [gatewayList, setGatewayList] = useState<GatewayItem[]>([]);
  const [totalTools, setTotalTools] = useState(0);
  const [loading, setLoading] = useState(true);
  const [expandedGateway, setExpandedGateway] = useState<string | null>(null);
  const [gatewayTools, setGatewayTools] = useState<Record<string, { name: string; description: string }[]>>({});

  useEffect(() => {
    Promise.all([agentsApi.list(), gatewaysApi.list()])
      .then(([agentData, gwData]) => {
        setAgentList(agentData.agents);
        setGatewayList(gwData.gateways || []);
        setTotalTools(gwData.totalTools ?? 0);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  const activeCount = agentList.filter((a) => a.healthiness === 'READY').length;
  const activeAgents = agentList.filter((a) => a.healthiness === 'READY');

  const loadGatewayTools = async (gatewayId: string) => {
    if (expandedGateway === gatewayId) {
      setExpandedGateway(null);
      return;
    }
    if (!gatewayTools[gatewayId]) {
      const data = await gatewaysApi.tools(gatewayId);
      setGatewayTools((prev) => ({ ...prev, [gatewayId]: data.tools }));
    }
    setExpandedGateway(gatewayId);
  };

  return (
    <div>
      <Header title="Dashboard" />
      <div className="p-6 space-y-8">
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <StatsCard label="Total Agents" value={agentList.length} icon={Bot} href="#agents" />
          <StatsCard label="Active Runtimes" value={activeCount} icon={Play} color="var(--success)" href="#runtimes" />
          <StatsCard label="Gateways" value={gatewayList.length} icon={Link2} color="#0ea5e9" href="#gateways" />
          <StatsCard label="MCP Tools" value={totalTools} icon={Wrench} color="var(--warning)" href="#tools" />
        </div>

        {loading ? (
          <div className="space-y-3">
            <div className="skeleton h-16 w-full" />
            <div className="skeleton h-16 w-full" />
          </div>
        ) : (
          <>
            {/* Agent Status */}
            <section id="agents" className="scroll-mt-4">
              <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-[var(--purple)]" />
                Agent Status
              </h2>
              <AgentStatusGrid
                agents={agentList.map((a) => ({
                  agentId: a.agentId,
                  name: a.name,
                  contextBoundary: a.contextBoundary,
                  healthiness: a.healthiness,
                }))}
              />
            </section>

            {/* Active Runtimes */}
            <section id="runtimes" className="scroll-mt-4">
              <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-[var(--success)]" />
                Active Runtimes
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {activeAgents.map((a) => (
                  <div key={a.agentId} className="bg-[var(--surface)] rounded-lg p-4 border border-[var(--border)] flex items-center gap-4">
                    <span className="w-2.5 h-2.5 rounded-full bg-green-500 flex-shrink-0" />
                    <div className="min-w-0">
                      <div className="text-sm font-medium text-white">{a.name}</div>
                      <div className="text-xs text-[var(--text-dim)] font-mono truncate">{a.agentId}</div>
                    </div>
                    <span className="ml-auto text-xs px-2 py-0.5 rounded bg-green-500/15 text-green-400 border border-green-500/30">
                      READY
                    </span>
                  </div>
                ))}
              </div>
            </section>

            {/* Gateways */}
            <section id="gateways" className="scroll-mt-4">
              <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-[#0ea5e9]" />
                Gateways
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
                {gatewayList.map((gw) => (
                  <div key={gw.gatewayId} className="bg-[var(--surface)] rounded-lg p-4 border border-[var(--border)]">
                    <div className="text-sm font-semibold text-[#0ea5e9] mb-1">{gw.name}</div>
                    <div className="text-xs text-[var(--text-dim)] mb-2">{gw.description}</div>
                    <div className="text-xs text-[var(--warning)]">{gw.toolCount} targets</div>
                  </div>
                ))}
              </div>
            </section>

            {/* MCP Tools by Gateway */}
            <section id="tools" className="scroll-mt-4">
              <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-[var(--warning)]" />
                MCP Tools by Gateway
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
                {gatewayList.map((gw) => (
                  <div key={gw.gatewayId}>
                    <button
                      onClick={() => loadGatewayTools(gw.gatewayId)}
                      className="w-full text-left bg-[var(--surface)] rounded-lg p-3 border border-[var(--border)] hover:border-[var(--warning)]/50 transition-colors"
                    >
                      <div className="flex items-center justify-between mb-1">
                        <div className="text-xs font-medium text-white">{gw.name}</div>
                        <span className="text-[10px] text-[var(--warning)]">
                          {expandedGateway === gw.gatewayId ? '▼' : '▶'} {gw.toolCount} tools
                        </span>
                      </div>
                    </button>
                    {expandedGateway === gw.gatewayId && gatewayTools[gw.gatewayId] && (
                      <div className="mt-1 bg-[var(--bg)] rounded-lg border border-[var(--border)] p-2 max-h-48 overflow-y-auto">
                        {gatewayTools[gw.gatewayId].map((tool) => (
                          <div key={tool.name} className="py-1 px-2 text-xs border-b border-[var(--border)] last:border-0">
                            <div className="font-mono text-[var(--warning)]">{tool.name}</div>
                            {tool.description && (
                              <div className="text-[var(--text-muted)] mt-0.5 truncate">{tool.description}</div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
              <div className="mt-3 text-xs text-[var(--text-dim)]">
                Total: {totalTools} MCP tool targets across {gatewayList.length} gateways
              </div>
            </section>

          </>
        )}
      </div>
    </div>
  );
}
