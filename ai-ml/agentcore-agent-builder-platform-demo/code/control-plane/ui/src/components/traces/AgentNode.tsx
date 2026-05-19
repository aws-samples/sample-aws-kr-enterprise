'use client';

import { memo } from 'react';
import { Handle, Position } from '@xyflow/react';
import type { OtelSpan } from '@/lib/types';

function spanColor(name: string): string {
  if (name.includes('invoke_agent')) return 'var(--purple)';
  if (name.includes('chat')) return 'var(--accent-cyan)';
  if (name.includes('execute_tool')) return 'var(--accent-orange)';
  if (name.includes('DynamoDB')) return '#3b48cc';
  if (name.includes('execute_event_loop')) return 'var(--success)';
  return 'var(--text-muted)';
}

function spanLabel(name: string): string {
  if (name.includes('invoke_agent')) return 'Agent';
  if (name.includes('chat')) return 'LLM';
  if (name.includes('execute_tool')) return 'Tool';
  if (name.includes('DynamoDB')) return 'DB';
  if (name.includes('execute_event_loop')) return 'Loop';
  if (name.includes('POST') || name.includes('GET') || name.includes('PUT')) return 'HTTP';
  return 'Span';
}

interface AgentNodeProps {
  data: {
    span: OtelSpan;
    durationMs: string | null;
  };
}

function AgentNode({ data }: AgentNodeProps) {
  const { span, durationMs } = data;
  const color = spanColor(span.name);
  const label = spanLabel(span.name);

  return (
    <div
      className="rounded-lg border px-3 py-2 min-w-[160px] max-w-[240px] text-xs"
      style={{ borderColor: color, background: 'var(--surface)' }}
    >
      <Handle type="target" position={Position.Top} className="!bg-[var(--border)]" />

      <div className="flex items-center gap-2 mb-1">
        <span
          className="px-1.5 py-0.5 rounded text-[10px] font-bold text-white"
          style={{ backgroundColor: color }}
        >
          {label}
        </span>
        {durationMs && (
          <span className="font-mono text-[var(--accent-cyan)] ml-auto">{durationMs}ms</span>
        )}
      </div>

      <div className="text-white font-medium truncate mb-1" title={span.name}>
        {span.toolName || span.name.replace('invoke_agent ', '').replace('execute_tool ', '').replace('chat ', '')}
      </div>

      <div className="space-y-0.5 text-[var(--text-dim)]">
        {span.model && (
          <div className="truncate">model: <span className="text-[var(--text-muted)]">{span.model.split('.').pop()}</span></div>
        )}
        {span.inputTokens != null && span.outputTokens != null && (
          <div>tokens: <span className="font-mono text-[var(--accent-cyan)]">{span.inputTokens}&rarr;{span.outputTokens}</span></div>
        )}
        {span.toolName && span.toolStatus && (
          <div>status: <span className={span.toolStatus === 'error' ? 'text-[var(--accent-red)]' : 'text-[var(--success)]'}>{span.toolStatus}</span></div>
        )}
        {span.ttft != null && (
          <div>TTFT: <span className="font-mono">{span.ttft}ms</span></div>
        )}
      </div>

      <Handle type="source" position={Position.Bottom} className="!bg-[var(--border)]" />
    </div>
  );
}

export default memo(AgentNode);
