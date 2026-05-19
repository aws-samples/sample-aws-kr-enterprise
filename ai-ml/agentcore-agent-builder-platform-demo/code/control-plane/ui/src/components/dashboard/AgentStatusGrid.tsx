'use client';

import Link from 'next/link';

interface Agent {
  agentId: string;
  name: string;
  contextBoundary: string;
  healthiness?: string;
}

const healthColors: Record<string, string> = {
  READY: 'bg-green-500',
  CREATING: 'bg-yellow-500',
  UPDATING: 'bg-yellow-500',
  CREATE_FAILED: 'bg-red-500',
  UPDATE_FAILED: 'bg-red-500',
  DELETING: 'bg-gray-500',
  NOT_DEPLOYED: 'bg-gray-500',
};

export default function AgentStatusGrid({ agents }: { agents: Agent[] }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {agents.map((agent) => (
        <Link
          key={agent.agentId}
          href={`/agents/${agent.agentId}`}
          className="bg-[#1e1e2e] rounded-lg p-4 border border-[#334155] hover:border-[#6366f1] transition-colors"
        >
          <div className="flex items-center gap-2 mb-2">
            <div
              className={`w-2.5 h-2.5 rounded-full ${healthColors[agent.healthiness || 'NOT_DEPLOYED']}`}
            />
            <h3 className="font-medium text-white">{agent.name}</h3>
          </div>
          <p className="text-[#94a3b8] text-sm line-clamp-2">
            {agent.contextBoundary}
          </p>
        </Link>
      ))}
    </div>
  );
}
