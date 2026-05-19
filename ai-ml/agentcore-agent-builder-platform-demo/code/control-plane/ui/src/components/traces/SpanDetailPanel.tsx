'use client';

import type { OtelSpan } from '@/lib/types';

// --- Model Pricing (per million tokens) ---
const MODEL_PRICING: Record<string, { input: number; output: number }> = {
  opus: { input: 15, output: 75 },
  sonnet: { input: 3, output: 15 },
  haiku: { input: 0.8, output: 4 },
};

function estimateCost(model: string, inputTokens: number | null, outputTokens: number | null): string | null {
  if (!model || inputTokens == null || outputTokens == null) return null;
  const modelLower = model.toLowerCase();
  const pricing = Object.entries(MODEL_PRICING).find(([key]) => modelLower.includes(key));
  if (!pricing) return null;
  const [, rates] = pricing;
  const cost = (inputTokens / 1_000_000) * rates.input + (outputTokens / 1_000_000) * rates.output;
  if (cost < 0.0001) return `$${(cost * 1000).toFixed(4)}m`;
  return `$${cost.toFixed(4)}`;
}

function formatNanoTime(nanos: number | null): string {
  if (nanos == null) return '-';
  const ms = nanos / 1_000_000;
  const date = new Date(ms);
  return date.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3 } as Intl.DateTimeFormatOptions);
}

function durationMs(start: number | null, end: number | null): number | null {
  if (start == null || end == null) return null;
  return (end - start) / 1_000_000;
}

// --- MetricCard ---
interface MetricCardProps {
  label: string;
  value: string | number | null | undefined;
  highlight?: boolean;
  wide?: boolean;
  mono?: boolean;
  status?: 'success' | 'error';
}

function MetricCard({ label, value, highlight, wide, mono, status }: MetricCardProps) {
  const displayValue = value == null || value === '' ? '-' : String(value);

  let valueColor = 'var(--text)';
  if (status === 'success') valueColor = 'var(--success)';
  if (status === 'error') valueColor = 'var(--red)';

  return (
    <div
      className={`rounded px-2 py-1.5 ${wide ? 'col-span-2' : ''}`}
      style={{ background: 'rgba(58, 80, 112, 0.2)' }}
    >
      <div className="text-[10px] mb-0.5" style={{ color: 'var(--text-muted)' }}>
        {label}
      </div>
      <div
        className={`text-xs truncate ${mono ? 'font-mono text-[10px]' : ''} ${highlight ? 'font-bold' : ''}`}
        style={{ color: highlight ? '#ffffff' : valueColor }}
        title={displayValue}
      >
        {displayValue}
      </div>
    </div>
  );
}

// --- Section wrapper ---
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-4">
      <div
        className="text-[10px] uppercase tracking-wider font-semibold mb-2 px-1"
        style={{ color: 'var(--purple)' }}
      >
        {title}
      </div>
      <div className="grid grid-cols-2 gap-1.5">{children}</div>
    </div>
  );
}

// --- Main Component ---
interface Props {
  span: OtelSpan;
  allSpans: OtelSpan[];
  onClose: () => void;
}

export default function SpanDetailPanel({ span, allSpans, onClose }: Props) {
  const spanDuration = durationMs(span.startTimeUnixNano, span.endTimeUnixNano);

  // Calculate % of trace: find root span (no parent) or earliest/latest across all spans
  const traceStart = allSpans.reduce<number | null>((min, s) => {
    if (s.startTimeUnixNano == null) return min;
    return min == null ? s.startTimeUnixNano : Math.min(min, s.startTimeUnixNano);
  }, null);
  const traceEnd = allSpans.reduce<number | null>((max, s) => {
    if (s.endTimeUnixNano == null) return max;
    return max == null ? s.endTimeUnixNano : Math.max(max, s.endTimeUnixNano);
  }, null);
  const traceDuration = durationMs(traceStart, traceEnd);

  const pctOfTrace =
    spanDuration != null && traceDuration != null && traceDuration > 0
      ? ((spanDuration / traceDuration) * 100).toFixed(1)
      : null;

  // Tokens/sec: output_tokens / (requestDuration - ttft) * 1000
  const tokensPerSec =
    span.outputTokens != null && span.requestDuration != null && span.ttft != null && span.requestDuration - span.ttft > 0
      ? ((span.outputTokens / (span.requestDuration - span.ttft)) * 1000).toFixed(1)
      : null;

  // LLM section visibility
  const showLlm = !!(span.model || span.inputTokens != null || span.outputTokens != null || span.totalTokens != null);

  // Tool section visibility
  const showTool = !!span.toolName;

  // Relations
  const parentSpan = allSpans.find((s) => s.spanId === span.parentSpanId);
  const childSpans = allSpans.filter((s) => s.parentSpanId === span.spanId);

  return (
    <div
      className="w-96 h-full border-l overflow-y-auto flex flex-col"
      style={{ borderColor: 'var(--border)', background: 'var(--bg-deep)' }}
    >
      {/* Header */}
      <div
        className="flex items-center justify-between px-4 py-3 border-b sticky top-0 z-10"
        style={{ borderColor: 'var(--border)', background: 'var(--bg-deep)' }}
      >
        <div className="text-sm font-semibold text-white truncate pr-2" title={span.name}>
          {span.name}
        </div>
        <button
          onClick={onClose}
          className="text-lg leading-none px-1 rounded hover:bg-white/10 transition-colors"
          style={{ color: 'var(--text-muted)' }}
          aria-label="Close"
        >
          &times;
        </button>
      </div>

      {/* Body */}
      <div className="px-4 py-3 flex-1">
        {/* Timing Section */}
        <Section title="Timing">
          <MetricCard
            label="Duration"
            value={spanDuration != null ? `${spanDuration.toFixed(1)}ms` : null}
            highlight
          />
          <MetricCard label="% of Trace" value={pctOfTrace != null ? `${pctOfTrace}%` : null} />
          <MetricCard label="Start Time" value={formatNanoTime(span.startTimeUnixNano)} mono />
          <MetricCard label="End Time" value={formatNanoTime(span.endTimeUnixNano)} mono />
          {span.ttft != null && <MetricCard label="TTFT" value={`${span.ttft}ms`} />}
          {span.requestDuration != null && (
            <MetricCard label="Request Duration" value={`${span.requestDuration}ms`} />
          )}
        </Section>

        {/* LLM Section */}
        {showLlm && (
          <Section title="LLM">
            {span.model && (
              <MetricCard label="Model" value={span.model.split('.').pop()} wide />
            )}
            <MetricCard label="Input Tokens" value={span.inputTokens?.toLocaleString()} />
            <MetricCard label="Output Tokens" value={span.outputTokens?.toLocaleString()} />
            <MetricCard label="Total Tokens" value={span.totalTokens?.toLocaleString()} />
            <MetricCard
              label="Estimated Cost"
              value={estimateCost(span.model, span.inputTokens, span.outputTokens)}
            />
            {tokensPerSec && <MetricCard label="Tokens/sec" value={tokensPerSec} />}
          </Section>
        )}

        {/* Tool Section */}
        {showTool && (
          <Section title="Tool">
            <MetricCard label="Tool Name" value={span.toolName} wide />
            <MetricCard label="Call ID" value={span.toolCallId} mono wide />
            <MetricCard
              label="Status"
              value={span.toolStatus || '-'}
              status={
                span.toolStatus === 'success'
                  ? 'success'
                  : span.toolStatus === 'error'
                    ? 'error'
                    : undefined
              }
            />
          </Section>
        )}

        {/* Context Section */}
        <Section title="Context">
          <MetricCard label="Span ID" value={span.spanId} mono wide />
          <MetricCard label="Parent Span ID" value={span.parentSpanId || '-'} mono wide />
          <MetricCard label="Session ID" value={span.sessionId} mono wide />
          <MetricCard label="Service" value={span.service} />
          <MetricCard label="Agent Name" value={span.agentName} />
          <MetricCard label="Operation" value={span.operation} />
        </Section>

        {/* Relations Section */}
        {(parentSpan || childSpans.length > 0) && (
          <Section title="Relations">
            {parentSpan && (
              <MetricCard label="Parent" value={parentSpan.name} wide />
            )}
            {childSpans.length > 0 && (
              <div className="col-span-2 space-y-1">
                <div className="text-[10px] px-2" style={{ color: 'var(--text-muted)' }}>
                  Children ({childSpans.length})
                </div>
                {childSpans.map((child) => {
                  const childDur = durationMs(child.startTimeUnixNano, child.endTimeUnixNano);
                  return (
                    <div
                      key={child.spanId}
                      className="flex items-center justify-between px-2 py-1 rounded text-xs"
                      style={{ background: 'rgba(58, 80, 112, 0.2)' }}
                    >
                      <span className="truncate text-[var(--text)]" title={child.name}>
                        {child.name}
                      </span>
                      {childDur != null && (
                        <span className="font-mono text-[10px] ml-2 shrink-0" style={{ color: 'var(--accent-cyan)' }}>
                          {childDur.toFixed(1)}ms
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </Section>
        )}
      </div>
    </div>
  );
}
