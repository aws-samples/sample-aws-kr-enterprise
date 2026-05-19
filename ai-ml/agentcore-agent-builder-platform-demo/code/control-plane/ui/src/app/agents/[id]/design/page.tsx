'use client';

import { useEffect, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { Maximize2, X } from 'lucide-react';
import Header from '@/components/common/Header';
import WorkflowCanvas from '@/components/design/WorkflowCanvas';
import LoadingSpinner from '@/components/common/LoadingSpinner';
import { agents as agentsApi } from '@/lib/api-client';
import type { AgentConfig } from '@/lib/types';

export default function DesignPage() {
  const params = useParams();
  const router = useRouter();
  const agentId = params.id as string;
  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [deploying, setDeploying] = useState(false);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    agentsApi
      .get(agentId)
      .then((data) => {
        setConfig(data.config);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [agentId]);

  const handleDeploy = async () => {
    setDeploying(true);
    try {
      await agentsApi.deploy(agentId);
      setTimeout(() => {
        router.push(`/agents/${agentId}`);
      }, 3000);
    } catch (error: unknown) {
      const errorMessage =
        error instanceof Error ? error.message : 'Unknown error';
      alert(`Deploy failed: ${errorMessage}`);
      setDeploying(false);
    }
  };

  if (loading)
    return (
      <div className="p-6">
        <LoadingSpinner text="Loading agent config..." />
      </div>
    );
  if (!config) return <div className="p-6 text-red-400">Agent not found</div>;

  return (
    <div>
      <Header title={`Design — ${config.name}`} />
      <div className="p-6 space-y-6">
        <div className="grid grid-cols-1 lg:grid-cols-[55fr_45fr] gap-6">
          <div className="min-w-0 relative">
            <button
              onClick={() => setExpanded(true)}
              className="absolute top-2 right-2 z-10 p-1.5 bg-[var(--surface)] border border-[var(--border)] rounded-lg text-[var(--text-dim)] hover:text-white hover:border-[var(--purple)] transition-colors"
              title="Expand diagram"
            >
              <Maximize2 size={16} />
            </button>
            <WorkflowCanvas config={config} />
          </div>
          <div className="min-w-0 bg-[#1e1e2e] rounded-lg border border-[#334155] p-4 flex flex-col">
            <h3 className="text-sm font-semibold text-white mb-3">
              Agent Config (JSON)
            </h3>
            <pre className="bg-[#0f1117] rounded-lg p-3 text-xs text-[#94a3b8] overflow-auto max-h-[400px] flex-1 whitespace-pre-wrap break-all">
              {JSON.stringify(config, null, 2)}
            </pre>
          </div>
        </div>

        <div className="flex justify-end gap-3">
          <button
            onClick={() => router.back()}
            className="px-4 py-2 bg-[#334155] text-white rounded-lg text-sm hover:bg-[#475569] transition-colors"
          >
            Back
          </button>
          <button
            onClick={handleDeploy}
            disabled={deploying}
            className="px-6 py-2 bg-[#10b981] text-white rounded-lg text-sm font-medium hover:bg-[#059669] disabled:opacity-50 transition-colors"
          >
            {deploying ? 'Deploying...' : 'Deploy Agent'}
          </button>
        </div>

        {deploying && (
          <div className="bg-[#10b981]/10 border border-[#10b981]/30 rounded-lg p-4 flex items-center gap-3">
            <div className="w-5 h-5 border-2 border-[#10b981] border-t-transparent rounded-full animate-spin" />
            <div>
              <p className="text-[#10b981] text-sm font-medium">
                Runtime provisioning in progress...
              </p>
              <p className="text-[#94a3b8] text-xs mt-1">
                Quality Gate passed. Creating AgentCore Runtime...
              </p>
            </div>
          </div>
        )}
      </div>

      {expanded && (
        <div className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-6">
          <div className="relative w-full h-full bg-[var(--bg)] rounded-xl border border-[var(--border)] overflow-hidden">
            <button
              onClick={() => setExpanded(false)}
              className="absolute top-3 right-3 z-10 p-2 bg-[var(--surface)] border border-[var(--border)] rounded-lg text-[var(--text-dim)] hover:text-white hover:border-[var(--purple)] transition-colors"
              title="Close"
            >
              <X size={18} />
            </button>
            <div className="absolute top-3 left-4 text-sm font-semibold text-white z-10">
              {config.name} — Workflow DAG
            </div>
            <div className="w-full h-full pt-10">
              <WorkflowCanvas config={config} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
