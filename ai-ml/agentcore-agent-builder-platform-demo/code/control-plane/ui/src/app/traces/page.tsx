'use client';

import { useEffect, useState, useMemo } from 'react';
import { ReactFlow, Background, Controls, type Node, type Edge } from '@xyflow/react';
import dagre from 'dagre';
import Header from '@/components/common/Header';
import AgentNode from '@/components/traces/AgentNode';
import { obs } from '@/lib/api-client';
import type { OtelSpan, TraceSession } from '@/lib/types';
import '@xyflow/react/dist/style.css';

type ViewMode = 'waterfall' | 'graph' | 'json';

export default function TracesPage() {
  const [sessions, setSessions] = useState<Record<string, TraceSession[]>>({});
  const [hours, setHours] = useState(1);
  const [selectedTrace, setSelectedTrace] = useState<string | null>(null);
  const [spans, setSpans] = useState<OtelSpan[]>([]);
  const [viewMode, setViewMode] = useState<ViewMode>('waterfall');
  const [loading, setLoading] = useState(true);
  const [traceLoading, setTraceLoading] = useState(false);
  const [selectedAgent, setSelectedAgent] = useState<string>('');

  useEffect(() => {
    setLoading(true);
    obs.sessions(hours)
      .then((data) => {
        setSessions(data.sessions);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [hours]);

  const loadTrace = async (traceId: string) => {
    setTraceLoading(true);
    try {
      const data = await obs.trace(traceId);
      setSpans(data.spans);
    } catch {
      setSpans([]);
    }
    setTraceLoading(false);
  };

  const serviceList = useMemo(() => Object.keys(sessions).sort(), [sessions]);

  const allTraces = useMemo(() => {
    const entries = selectedAgent
      ? Object.entries(sessions).filter(([service]) => service === selectedAgent)
      : Object.entries(sessions);
    return entries.flatMap(([service, traces]) =>
      traces.map((t) => ({ ...t, service }))
    ).sort((a, b) => new Date(b.startTime).getTime() - new Date(a.startTime).getTime());
  }, [sessions, selectedAgent]);

  const sortedSpans = useMemo(() => {
    return [...spans].sort((a, b) =>
      (a.startTimeUnixNano ?? 0) - (b.startTimeUnixNano ?? 0)
    );
  }, [spans]);

  const timeRange = useMemo(() => {
    if (sortedSpans.length === 0) return { min: 0, max: 1 };
    const min = sortedSpans[0].startTimeUnixNano ?? 0;
    const max = Math.max(
      ...sortedSpans.map((s) => s.endTimeUnixNano ?? s.startTimeUnixNano ?? 0)
    );
    return { min, max: max === min ? min + 1 : max };
  }, [sortedSpans]);

  return (
    <div>
      <Header title="Trace Viewer" />
      <div className="flex h-[calc(100vh-64px)]">
        {/* Left Panel: Session/Trace List */}
        <div className="w-80 border-r border-[var(--border)] overflow-y-auto p-4">
          <div className="space-y-2 mb-4">
            <div className="flex items-center gap-2">
              <label className="text-xs text-[var(--text-dim)]">Time</label>
              <select
                value={hours}
                onChange={(e) => setHours(Number(e.target.value))}
                className="bg-[var(--surface)] border border-[var(--border)] rounded px-2 py-1 text-xs text-white"
              >
                <option value={1}>1h</option>
                <option value={6}>6h</option>
                <option value={24}>24h</option>
              </select>
            </div>
            <div className="flex items-center gap-2">
              <label className="text-xs text-[var(--text-dim)]">Agent</label>
              <select
                value={selectedAgent}
                onChange={(e) => setSelectedAgent(e.target.value)}
                className="bg-[var(--surface)] border border-[var(--border)] rounded px-2 py-1 text-xs text-white flex-1"
              >
                <option value="">All Agents</option>
                {serviceList.map((svc) => (
                  <option key={svc} value={svc}>{svc}</option>
                ))}
              </select>
            </div>
          </div>

          {loading ? (
            <div className="space-y-2">
              <div className="skeleton h-10 w-full" />
              <div className="skeleton h-10 w-full" />
            </div>
          ) : allTraces.length === 0 ? (
            <div className="text-sm text-[var(--text-dim)] text-center py-8">
              선택된 시간 범위에 trace가 없습니다
            </div>
          ) : (
            <div className="space-y-1.5">
              {allTraces.map((t, idx) => {
                const uid = `${t.traceId}-${idx}`;
                return (
                <button
                  key={uid}
                  onClick={() => { setSelectedTrace(uid); loadTrace(t.traceId); }}
                  className={`w-full text-left p-2.5 rounded-lg border transition-colors text-xs ${
                    selectedTrace === uid
                      ? 'bg-[var(--purple)]/10 border-[var(--purple)]/40'
                      : 'bg-[var(--surface)] border-[var(--border)] hover:border-[var(--purple)]/30'
                  }`}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <span className="w-1.5 h-1.5 rounded-full bg-[var(--success)]" />
                    <span className="font-mono text-[var(--text-muted)] truncate">{t.service}</span>
                  </div>
                  {t.agentName && (
                    <div className="text-[10px] text-[var(--purple)] mb-1 truncate">{t.agentName}</div>
                  )}
                  <div className="flex items-center justify-between">
                    <span className="text-[var(--text-dim)]">
                      {new Date(t.startTime).toLocaleString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                    </span>
                    {t.duration != null && (
                      <span className="font-mono text-[var(--accent-cyan)]">
                        {(t.duration * 1000).toFixed(0)}ms
                      </span>
                    )}
                  </div>
                  {t.totalTokens != null && t.totalTokens > 0 && (
                    <div className="text-[10px] text-[var(--text-dim)] mt-0.5">
                      tokens: <span className="font-mono text-[var(--accent-cyan)]">{t.totalTokens}</span>
                    </div>
                  )}
                  <div className="font-mono text-[10px] text-[var(--text-muted)] mt-0.5 truncate">
                    {t.traceId}
                  </div>
                </button>
                );
              })}
            </div>
          )}
        </div>

        {/* Right Panel: Trace Detail */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* View Mode Tabs */}
          <div className="flex items-center gap-1 px-4 py-2 border-b border-[var(--border)] bg-[var(--surface)]">
            {(['waterfall', 'graph', 'json'] as ViewMode[]).map((mode) => (
              <button
                key={mode}
                onClick={() => setViewMode(mode)}
                className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                  viewMode === mode
                    ? 'bg-[var(--purple)]/20 text-[var(--purple)]'
                    : 'text-[var(--text-dim)] hover:text-white'
                }`}
              >
                {mode === 'waterfall' ? 'Waterfall' : mode === 'graph' ? 'Graph' : 'JSON'}
              </button>
            ))}
            {selectedTrace && (
              <span className="ml-auto text-[10px] font-mono text-[var(--text-muted)]">
                {selectedTrace} · {spans.length} spans
              </span>
            )}
          </div>

          {/* Content */}
          <div className="flex-1 overflow-auto p-4">
            {!selectedTrace ? (
              <div className="text-sm text-[var(--text-dim)] text-center py-16">
                좌측에서 trace를 선택하세요
              </div>
            ) : traceLoading ? (
              <div className="space-y-2">
                <div className="skeleton h-8 w-full" />
                <div className="skeleton h-8 w-3/4" />
                <div className="skeleton h-8 w-1/2" />
              </div>
            ) : viewMode === 'waterfall' ? (
              <WaterfallView spans={sortedSpans} timeRange={timeRange} />
            ) : viewMode === 'graph' ? (
              <GraphView spans={sortedSpans} />
            ) : (
              <JsonView spans={spans} traceId={selectedTrace} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function spanColor(name: string): string {
  if (name.includes('invoke_agent')) return 'var(--purple)';
  if (name.includes('chat')) return 'var(--accent-cyan)';
  if (name.includes('execute_tool')) return 'var(--accent-orange)';
  if (name.includes('DynamoDB')) return '#3b48cc';
  if (name.includes('execute_event_loop')) return 'var(--success)';
  return 'var(--text-muted)';
}

const nodeTypes = { agentNode: AgentNode };

function GraphView({ spans }: { spans: OtelSpan[] }) {
  const { nodes, edges } = useMemo(() => {
    if (spans.length === 0) return { nodes: [] as Node[], edges: [] as Edge[] };

    const g = new dagre.graphlib.Graph();
    g.setDefaultEdgeLabel(() => ({}));
    g.setGraph({ rankdir: 'TB', nodesep: 30, ranksep: 60 });

    const flowNodes: Node[] = [];
    const flowEdges: Edge[] = [];

    spans.forEach((span) => {
      const durationMs = span.startTimeUnixNano && span.endTimeUnixNano
        ? ((span.endTimeUnixNano - span.startTimeUnixNano) / 1_000_000).toFixed(0)
        : null;

      g.setNode(span.spanId, { width: 200, height: 90 });
      flowNodes.push({
        id: span.spanId,
        type: 'agentNode',
        position: { x: 0, y: 0 },
        data: { span, durationMs },
      });

      if (span.parentSpanId) {
        const parentExists = spans.some((s) => s.spanId === span.parentSpanId);
        if (parentExists) {
          g.setEdge(span.parentSpanId, span.spanId);
          flowEdges.push({
            id: `${span.parentSpanId}-${span.spanId}`,
            source: span.parentSpanId,
            target: span.spanId,
            animated: span.name.includes('invoke_agent') || span.name.includes('execute_tool'),
            style: { stroke: 'var(--border)' },
          });
        }
      }
    });

    dagre.layout(g);

    flowNodes.forEach((node) => {
      const pos = g.node(node.id);
      if (pos) {
        node.position = { x: pos.x - 100, y: pos.y - 45 };
      }
    });

    return { nodes: flowNodes, edges: flowEdges };
  }, [spans]);

  if (nodes.length === 0) {
    return <div className="text-sm text-[var(--text-dim)] text-center py-16">span 데이터가 없습니다</div>;
  }

  return (
    <div className="h-[calc(100vh-180px)]">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        minZoom={0.3}
        maxZoom={1.5}
        defaultEdgeOptions={{ type: 'smoothstep' }}
      >
        <Background color="var(--border)" gap={20} />
        <Controls className="!bg-[var(--surface)] !border-[var(--border)] [&>button]:!bg-[var(--surface)] [&>button]:!border-[var(--border)] [&>button]:!text-[var(--text-dim)]" />
      </ReactFlow>
    </div>
  );
}

function WaterfallView({ spans, timeRange }: { spans: OtelSpan[]; timeRange: { min: number; max: number } }) {
  const totalDuration = timeRange.max - timeRange.min;

  return (
    <div className="space-y-1">
      {spans.map((span, idx) => {
        const start = ((span.startTimeUnixNano ?? timeRange.min) - timeRange.min) / totalDuration;
        const end = ((span.endTimeUnixNano ?? span.startTimeUnixNano ?? timeRange.min) - timeRange.min) / totalDuration;
        const width = Math.max(end - start, 0.005);
        const durationMs = span.startTimeUnixNano && span.endTimeUnixNano
          ? ((span.endTimeUnixNano - span.startTimeUnixNano) / 1_000_000).toFixed(0)
          : null;
        const depth = span.parentSpanId ? 1 : 0;
        const color = spanColor(span.name);

        return (
          <div key={span.spanId || idx} className="flex items-center gap-2 group">
            <div className="w-48 shrink-0 text-xs truncate text-[var(--text-dim)]" style={{ paddingLeft: `${depth * 16}px` }}>
              {span.name}
            </div>
            <div className="flex-1 h-6 relative bg-[var(--surface)] rounded overflow-hidden">
              <div
                className="absolute h-full rounded opacity-80 group-hover:opacity-100 transition-opacity"
                style={{
                  left: `${start * 100}%`,
                  width: `${width * 100}%`,
                  backgroundColor: color,
                  minWidth: '2px',
                }}
              />
              {durationMs && (
                <span
                  className="absolute text-[10px] font-mono text-white top-1"
                  style={{ left: `${(start + width) * 100 + 0.5}%` }}
                >
                  {durationMs}ms
                </span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function JsonView({ spans, traceId }: { spans: OtelSpan[]; traceId: string }) {
  const jsonStr = JSON.stringify(spans, null, 2);

  const handleCopy = () => {
    navigator.clipboard.writeText(jsonStr);
  };

  const handleDownload = () => {
    const blob = new Blob([jsonStr], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `trace-${traceId}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div>
      <div className="flex gap-2 mb-3">
        <button
          onClick={handleCopy}
          className="px-3 py-1.5 text-xs bg-[var(--surface)] border border-[var(--border)] rounded hover:border-[var(--purple)]/50 text-[var(--text-dim)] hover:text-white transition-colors"
        >
          Copy
        </button>
        <button
          onClick={handleDownload}
          className="px-3 py-1.5 text-xs bg-[var(--surface)] border border-[var(--border)] rounded hover:border-[var(--purple)]/50 text-[var(--text-dim)] hover:text-white transition-colors"
        >
          Download
        </button>
      </div>
      <pre className="bg-[var(--surface)] rounded-lg p-4 text-xs text-[var(--text-dim)] overflow-auto max-h-[70vh] border border-[var(--border)] font-mono">
        {jsonStr}
      </pre>
    </div>
  );
}
