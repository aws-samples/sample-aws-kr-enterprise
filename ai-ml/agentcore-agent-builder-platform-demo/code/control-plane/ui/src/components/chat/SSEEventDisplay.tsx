import type { SSEEvent } from '@/lib/types';

const STEP_MAP: Record<string, string> = {
  routing: 'Routing',
  status: 'Routing',
  action: 'Gathering',
  observation: 'Analyzing',
  message: 'Reporting',
};

function getActiveStep(events: SSEEvent[]): string {
  if (events.length === 0) return 'Routing';
  const lastType = events[events.length - 1].type;
  return STEP_MAP[lastType] || 'Routing';
}

const STEPS = ['Routing', 'Gathering', 'Analyzing', 'Reporting'];

export default function SSEEventDisplay({ events }: { events: SSEEvent[] }) {
  const activeStep = getActiveStep(events);

  return (
    <div>
      <h4 className="text-xs font-semibold text-[var(--text-dim)] mb-3 uppercase tracking-wider">
        Progress
      </h4>
      <div className="space-y-2 mb-4">
        {STEPS.map((step) => {
          const isActive = step === activeStep;
          const stepIdx = STEPS.indexOf(step);
          const activeIdx = STEPS.indexOf(activeStep);
          const isDone = stepIdx < activeIdx;
          return (
            <div
              key={step}
              className={`flex items-center gap-2 px-3 py-2 rounded-lg text-xs transition-colors ${
                isActive
                  ? 'bg-[var(--purple-dim)] text-[var(--purple)] font-medium'
                  : isDone
                    ? 'text-[var(--success)]'
                    : 'text-[var(--text-dim)]'
              }`}
            >
              {isActive && (
                <span className="w-2 h-2 rounded-full bg-[var(--purple)] animate-pulse-dot" />
              )}
              {isDone && <span className="w-2 h-2 rounded-full bg-[var(--success)]" />}
              {!isActive && !isDone && (
                <span className="w-2 h-2 rounded-full bg-[var(--border)]" />
              )}
              <span>{step}</span>
            </div>
          );
        })}
      </div>
      <h4 className="text-xs font-semibold text-[var(--text-dim)] mb-2 uppercase tracking-wider">
        Events ({events.length})
      </h4>
      <div className="space-y-1 max-h-60 overflow-y-auto">
        {events.slice(-10).map((ev, i) => (
          <div
            key={i}
            className="text-xs text-[var(--text-dim)] bg-[var(--bg)] rounded px-2 py-1 border border-[var(--border)] truncate"
          >
            <span className="text-[var(--purple)] font-mono">{ev.type}</span>{' '}
            {typeof ev.data?.content === 'string' ? ev.data.content.slice(0, 50) : JSON.stringify(ev.data).slice(0, 50)}
          </div>
        ))}
      </div>
    </div>
  );
}
