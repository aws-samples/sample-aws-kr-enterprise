// AG-UI SSE event parser. Spec Section 3.2.

import type { SSEEvent } from './types';

export type SSEEventCallback = (event: SSEEvent) => void;

export function connectSSE(
  url: string,
  onEvent: SSEEventCallback,
  onDone: () => void,
  onError: (error: Event) => void,
): EventSource {
  const source = new EventSource(url);

  const eventTypes = [
    'thought',
    'action',
    'observation',
    'message',
    'hitl',
    'error',
    'status',
    'routing',
    'tool_call',
    'a2a_delegation',
    'agent_start',
    'evaluation',
  ];

  eventTypes.forEach((type) => {
    source.addEventListener(type, (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data);
        onEvent({ type, data });
      } catch {
        onEvent({ type, data: { raw: event.data } });
      }
    });
  });

  source.addEventListener('done', () => {
    source.close();
    onDone();
  });

  source.onerror = (event) => {
    source.close();
    onError(event);
  };

  return source;
}

export function formatSSEEvent(event: SSEEvent): string {
  const d = event.data;
  switch (event.type) {
    case 'routing':
      return `→ ${d.target || 'Agent'}에게 라우팅`;
    case 'tool_call': {
      const tool = String(d.tool || 'Tool');
      if (d.phase === 'end') {
        return d.error
          ? `✗ ${tool} 실패: ${d.error}`
          : `✓ ${tool} 완료`;
      }
      return `⟳ ${tool} 호출 중...`;
    }
    case 'a2a_delegation': {
      const target = String(d.target || 'Agent');
      if (d.phase === 'end') return `✓ ${target} 위임 완료`;
      if (d.phase === 'error') return `✗ ${target} 위임 실패`;
      return `⟳ ${target}에게 위임 중...`;
    }
    case 'agent_start':
      return `● ${d.agent || 'Agent'} 시작${d.caller ? ` (from ${d.caller})` : ''}`;
    case 'evaluation':
      return `평가: ${d.pass ? 'Pass' : 'Fail'} (${d.score || ''})`;
    case 'message':
      return String(d.content || '');
    case 'error':
      return `에러: ${d.message || 'Unknown error'}`;
    case 'status':
      return `${d.phase || '처리 중'}...`;
    case 'thought':
      return `사고: ${d.content || ''}`;
    case 'action':
      return `액션: ${d.name || ''}`;
    case 'observation':
      return `관찰: ${d.content || ''}`;
    case 'hitl':
      return `승인 요청: ${d.description || d.message || ''}`;
    default:
      return JSON.stringify(d);
  }
}
