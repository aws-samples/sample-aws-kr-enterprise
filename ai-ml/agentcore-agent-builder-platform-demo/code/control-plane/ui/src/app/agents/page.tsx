'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import Header from '@/components/common/Header';
import AgentCard from '@/components/agents/AgentCard';
import LoadingSpinner from '@/components/common/LoadingSpinner';
import { agents as agentsApi } from '@/lib/api-client';
import type { AgentConfig } from '@/lib/types';

export default function AgentRegistryPage() {
  const [agentList, setAgentList] = useState<AgentConfig[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    agentsApi
      .list()
      .then((data) => {
        setAgentList(data.agents);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  return (
    <div>
      <Header title="Agent Registry" />
      <div className="p-6">
        <div className="flex items-center justify-between mb-6">
          <p className="text-[var(--text-dim)]">{agentList.length} agents registered</p>
          <Link
            href="/builder"
            className="px-4 py-2 bg-[var(--purple)] text-white rounded-lg text-sm hover:bg-[#7c3aed] transition-colors"
          >
            + Create Agent
          </Link>
        </div>
        {loading ? (
          <LoadingSpinner text="Loading agents..." />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {agentList.map((agent) => (
              <AgentCard
                key={agent.agentId}
                agent={agent}
                runtimeStatus={agent.healthiness || 'NOT_DEPLOYED'}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
