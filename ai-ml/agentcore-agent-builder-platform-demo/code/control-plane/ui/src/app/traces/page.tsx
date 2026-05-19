'use client';

import { useEffect, useState, useMemo } from 'react';
import { ReactFlow, Background, Controls, type Node, type Edge } from '@xyflow/react';
import dagre from 'dagre';
import Header from '@/components/common/Header';
import AgentNode from '@/components/traces/AgentNode';
import SpanDetailPanel from '@/components/traces/SpanDetailPanel';
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
  const [selectedSpan, setSelectedSpan] = useState<OtelSpan | null>(null);

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
            {selectedTrace && spans.length > 0 && (
              <div className="ml-auto flex items-center gap-3 text-[10px]">
                <span className="font-mono text-white">
                  {((timeRange.max - timeRange.min) / 1_000_000).toFixed(0)}ms
                </span>
                <span className="text-[var(--text-muted)]">
                  {spans.length} spans
                </span>
                {(() => {
                  const totalTokens = spans.reduce((sum, s) => sum + (s.totalTokens ?? 0), 0);
                  return totalTokens > 0 ? (
                    <span className="font-mono text-[var(--purple)]">{totalTokens.toLocaleString()} tokens</span>
                  ) : null;
                })()}
                {(() => {
                  const errorCount = spans.filter((s) => s.toolStatus === 'error').length;
                  return errorCount > 0 ? (
                    <span className="font-mono text-[var(--red)] font-bold">{errorCount} errors</span>
                  ) : null;
                })()}
              </div>
            )}
          </div>

          {/* Content */}
          <div className="flex-1 flex overflow-hidden">
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
                <WaterfallView
                  spans={sortedSpans}
                  timeRange={timeRange}
                  selectedSpan={selectedSpan}
                  onSelectSpan={setSelectedSpan}
                />
              ) : viewMode === 'graph' ? (
                <GraphView spans={sortedSpans} onSelectSpan={setSelectedSpan} />
              ) : (
                <JsonView spans={spans} traceId={selectedTrace} />
              )}
            </div>
            {selectedSpan && (
              <SpanDetailPanel
                span={selectedSpan}
                allSpans={sortedSpans}
                onClose={() => setSelectedSpan(null)}
              />
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

function computeCriticalPath(spans: OtelSpan[]): Set<string> {
  const criticalIds = new Set<string>();
  if (spans.length === 0) return criticalIds;

  const spanMap = new Map(spans.map((s) => [s.spanId, s]));
  const childrenMap = new Map<string, OtelSpan[]>();
  spans.forEach((s) => {
    if (s.parentSpanId && spanMap.has(s.parentSpanId)) {
      const children = childrenMap.get(s.parentSpanId) ?? [];
      children.push(s);
      childrenMap.set(s.parentSpanId, children);
    }
  });

  function longestPath(spanId: string): string[] {
    const children = childrenMap.get(spanId) ?? [];
    if (children.length === 0) return [spanId];
    let best: string[] = [];
    let bestDur = 0;
    for (const child of children) {
      const dur = (child.endTimeUnixNano ?? 0) - (child.startTimeUnixNano ?? 0);
      const path = longestPath(child.spanId);
      if (dur > bestDur || (dur === bestDur && path.length > best.length)) {
        best = path;
        bestDur = dur;
      }
    }
    return [spanId, ...best];
  }

  const roots = spans.filter((s) => !s.parentSpanId || !spanMap.has(s.parentSpanId));
  for (const root of roots) {
    longestPath(root.spanId).forEach((id) => criticalIds.add(id));
  }
  return criticalIds;
}

function hasErrorInSubtree(span: OtelSpan, spans: OtelSpan[]): boolean {
  if (span.toolStatus === 'error') return true;
  const children = spans.filter((s) => s.parentSpanId === span.spanId);
  return children.some((c) => hasErrorInSubtree(c, spans));
}

function computeDepth(span: OtelSpan, spans: OtelSpan[]): number {
  let depth = 0;
  let currentId = span.parentSpanId;
  const spanMap = new Map(spans.map((s) => [s.spanId, s]));
  while (currentId && spanMap.has(currentId)) {
    depth++;
    currentId = spanMap.get(currentId)!.parentSpanId;
  }
  return depth;
}

const nodeTypes = { agentNode: AgentNode };

function GraphView({ spans, onSelectSpan }: { spans: OtelSpan[]; onSelectSpan: (span: OtelSpan) => void }) {
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
        onNodeClick={(_event, node) => {
          const span = (node.data as { span: OtelSpan }).span;
          onSelectSpan(span);
        }}
      >
        <Background color="var(--border)" gap={20} />
        <Controls className="!bg-[var(--surface)] !border-[var(--border)] [&>button]:!bg-[var(--surface)] [&>button]:!border-[var(--border)] [&>button]:!text-[var(--text-dim)]" />
      </ReactFlow>
    </div>
  );
}

function WaterfallView({
  spans,
  timeRange,
  selectedSpan,
  onSelectSpan,
}: {
  spans: OtelSpan[];
  timeRange: { min: number; max: number };
  selectedSpan: OtelSpan | null;
  onSelectSpan: (span: OtelSpan) => void;
}) {
  const totalDuration = timeRange.max - timeRange.min;
  const criticalPath = useMemo(() => computeCriticalPath(spans), [spans]);

  return (
    <div className="space-y-0.5">
      {spans.map((span, idx) => {
        const start = ((span.startTimeUnixNano ?? timeRange.min) - timeRange.min) / totalDuration;
        const end = ((span.endTimeUnixNano ?? span.startTimeUnixNano ?? timeRange.min) - timeRange.min) / totalDuration;
        const width = Math.max(end - start, 0.005);
        const dur = span.startTimeUnixNano && span.endTimeUnixNano
          ? ((span.endTimeUnixNano - span.startTimeUnixNano) / 1_000_000).toFixed(0)
          : null;
        const depth = computeDepth(span, spans);
        const color = spanColor(span.name);
        const isCritical = criticalPath.has(span.spanId);
        const isError = span.toolStatus === 'error' || hasErrorInSubtree(span, spans);
        const isSelected = selectedSpan?.spanId === span.spanId;

        return (
          <div
            key={span.spanId || idx}
            onClick={() => onSelectSpan(span)}
            className={`flex items-center gap-2 cursor-pointer rounded px-1 py-0.5 transition-colors ${
              isSelected
                ? 'bg-[var(--purple)]/15 ring-1 ring-[var(--purple)]/40'
                : 'hover:bg-[var(--surface-hover)]'
            }`}
          >
            <div
              className="w-52 shrink-0 flex items-center gap-1.5 text-xs truncate"
              style={{ paddingLeft: `${depth * 14}px` }}
            >
              {isError && <span className="w-1.5 h-1.5 rounded-full bg-[var(--red)] shrink-0" />}
              <span className={`truncate ${isError ? 'text-[var(--red)]' : 'text-[var(--text-dim)]'}`}>
                {span.toolName || span.name}
              </span>
            </div>
            <div className="flex-1 h-7 relative bg-[var(--surface)] rounded overflow-hidden">
              <div
                className={`absolute h-full rounded transition-opacity ${
                  isCritical ? 'opacity-100' : 'opacity-60'
                } ${isSelected ? 'ring-1 ring-white/30' : ''}`}
                style={{
                  left: `${start * 100}%`,
                  width: `${width * 100}%`,
                  backgroundColor: isError ? 'var(--red)' : color,
                  minWidth: '3px',
                }}
              />
              {dur && (
                <span
                  className="absolute text-[10px] font-mono text-white/90 top-1.5"
                  style={{ left: `${(start + width) * 100 + 0.5}%` }}
                >
                  {dur}ms
                </span>
              )}
            </div>
            <div className="w-36 shrink-0 flex items-center gap-1.5">
              {span.totalTokens != null && span.totalTokens > 0 && (
                <span className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-[var(--purple)]/15 text-[var(--purple)]">
                  {span.totalTokens}t
                </span>
              )}
              {span.toolStatus && (
                <span className={`text-[9px] px-1.5 py-0.5 rounded font-medium ${
                  span.toolStatus === 'success'
                    ? 'bg-[var(--success)]/15 text-[var(--success)]'
                    : 'bg-[var(--red)]/15 text-[var(--red)]'
                }`}>
                  {span.toolStatus}
                </span>
              )}
              {isCritical && (
                <span className="text-[9px] px-1.5 py-0.5 rounded bg-[var(--warning)]/15 text-[var(--warning)]">
                  critical
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
