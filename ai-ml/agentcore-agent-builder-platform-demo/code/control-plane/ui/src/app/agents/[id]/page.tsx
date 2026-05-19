'use client';

import { useEffect, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import Link from 'next/link';
import Header from '@/components/common/Header';
import AgentStatusBadge from '@/components/agents/AgentStatusBadge';
import LoadingSpinner from '@/components/common/LoadingSpinner';
import { agents as agentsApi } from '@/lib/api-client';
import type { AgentDetail } from '@/lib/types';

export default function AgentDetailPage() {
  const params = useParams();
  const router = useRouter();
  const agentId = params.id as string;
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [deleting, setDeleting] = useState(false);

  const handleDelete = async () => {
    if (!confirm(`Delete agent "${detail?.config?.name}"? This will undeploy the runtime and remove all config.`))
      return;
    setDeleting(true);
    try {
      if (detail?.runtime?.runtimeArn) {
        try { await agentsApi.undeploy(agentId); } catch { /* runtime cleanup best-effort */ }
      }
      await agentsApi.delete(agentId);
      router.push('/agents');
    } catch (e) {
      alert(`Delete failed: ${e instanceof Error ? e.message : e}`);
      setDeleting(false);
    }
  };

  useEffect(() => {
    agentsApi
      .get(agentId)
      .then((data) => {
        setDetail(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [agentId]);

  if (loading)
    return (
      <div className="p-6">
        <LoadingSpinner />
      </div>
    );
  if (!detail?.config)
    return <div className="p-6 text-red-400">Agent not found</div>;

  const { config, runtime } = detail;

  return (
    <div>
      <Header title={config.name} />
      <div className="p-6 space-y-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <AgentStatusBadge status={runtime?.status || 'stopped'} />
            <span className="text-[#94a3b8] text-sm">v{config.version}</span>
          </div>
          <div className="flex gap-2">
            <Link
              href={`/agents/${agentId}/playground`}
              className="px-4 py-2 bg-[#6366f1] text-white rounded-lg text-sm hover:bg-[#4f46e5] transition-colors"
            >
              Playground
            </Link>
            <Link
              href={`/agents/${agentId}/design`}
              className="px-4 py-2 bg-[#334155] text-white rounded-lg text-sm hover:bg-[#475569] transition-colors"
            >
              Design
            </Link>
            <button
              onClick={handleDelete}
              disabled={deleting}
              className="px-4 py-2 bg-red-600/20 text-red-400 border border-red-500/30 rounded-lg text-sm hover:bg-red-600/40 transition-colors disabled:opacity-50"
            >
              {deleting ? 'Deleting...' : 'Delete'}
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div className="bg-[#1e1e2e] rounded-lg border border-[#334155] p-5">
            <h3 className="text-sm font-semibold text-white mb-3">
              Agent Info
            </h3>
            <dl className="space-y-2 text-sm">
              <div>
                <dt className="text-[#64748b]">ID</dt>
                <dd className="text-white font-mono">{config.agentId}</dd>
              </div>
              <div>
                <dt className="text-[#64748b]">Context Boundary</dt>
                <dd className="text-white">{config.contextBoundary}</dd>
              </div>
              <div>
                <dt className="text-[#64748b]">Model</dt>
                <dd className="text-white">{config.model}</dd>
              </div>
              <div>
                <dt className="text-[#64748b]">Created By</dt>
                <dd className="text-white">{config.createdBy}</dd>
              </div>
            </dl>
          </div>

          <div className="bg-[#1e1e2e] rounded-lg border border-[#334155] p-5">
            <h3 className="text-sm font-semibold text-white mb-3">Runtime</h3>
            {runtime ? (
              <dl className="space-y-2 text-sm">
                <div>
                  <dt className="text-[#64748b]">Status</dt>
                  <dd>
                    <AgentStatusBadge status={runtime.status} />
                  </dd>
                </div>
                <div>
                  <dt className="text-[#64748b]">ARN</dt>
                  <dd className="text-white font-mono text-xs break-all">
                    {runtime.runtimeArn}
                  </dd>
                </div>
                <div>
                  <dt className="text-[#64748b]">Created</dt>
                  <dd className="text-white">{runtime.createdAt}</dd>
                </div>
              </dl>
            ) : (
              <p className="text-[#94a3b8] text-sm">Not deployed</p>
            )}
          </div>

          <div className="bg-[#1e1e2e] rounded-lg border border-[#334155] p-5 md:col-span-2">
            <h3 className="text-sm font-semibold text-white mb-3">
              System Prompt
            </h3>
            {config.systemPrompt ? (
              <pre className="text-sm text-[#e2e8f0] whitespace-pre-wrap break-words font-mono bg-[#0f172a] rounded-lg p-4 max-h-64 overflow-y-auto">
                {config.systemPrompt}
              </pre>
            ) : (
              <p className="text-[#64748b] text-sm">No system prompt configured</p>
            )}
          </div>

          <div className="bg-[#1e1e2e] rounded-lg border border-[#334155] p-5">
            <h3 className="text-sm font-semibold text-white mb-3">
              Gateways ({config.gateways.length})
            </h3>
            <div className="space-y-2">
              {config.gateways.map((gw) => (
                <div
                  key={gw.gatewayId}
                  className="flex items-center justify-between text-sm"
                >
                  <span className="text-[#38bdf8]">{gw.gatewayId}</span>
                  <span className="text-[#64748b] text-xs">
                    {gw.toolFilter === 'all'
                      ? 'All tools'
                      : Array.isArray(gw.toolFilter)
                        ? `${gw.toolFilter.length} tools`
                        : gw.toolFilter}
                  </span>
                </div>
              ))}
              {config.gateways.length === 0 && (
                <p className="text-[#64748b] text-sm">No gateways</p>
              )}
            </div>
          </div>

          <div className="bg-[#1e1e2e] rounded-lg border border-[#334155] p-5">
            <h3 className="text-sm font-semibold text-white mb-3">
              Delegations ({config.delegations.length})
            </h3>
            <div className="space-y-2">
              {config.delegations.map((d) => (
                <div key={d.targetAgent} className="text-sm">
                  <span className="text-[#f87171]">
                    &rarr; {d.targetAgent}
                  </span>
                  <span className="text-[#64748b] ml-2">{d.purpose}</span>
                </div>
              ))}
              {config.delegations.length === 0 && (
                <p className="text-[#64748b] text-sm">No delegations</p>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
