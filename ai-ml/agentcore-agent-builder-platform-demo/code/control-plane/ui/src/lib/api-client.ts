// Platform API client. Spec Section 3.2.

import type { AgentConfig, AgentDetail, SessionMeta, SessionDetail, OtelSpan, TraceSession, ServiceNode } from './types';

const API_BASE = '/api';

async function fetchJson<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  });
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail);
    throw new Error(detail || res.statusText);
  }
  return res.json();
}

// Agent Lifecycle CRUD
export const agents = {
  list: () => fetchJson<{ agents: AgentConfig[]; count: number }>('/agents'),
  get: (id: string) => fetchJson<AgentDetail>(`/agents/${id}`),
  create: (data: Partial<AgentConfig>) =>
    fetchJson<{ agentId: string }>('/agents', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: Partial<AgentConfig>) =>
    fetchJson<AgentConfig>(`/agents/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: async (id: string) => {
    const res = await fetchJson<Record<string, unknown>>(`/agents/${id}`, { method: 'DELETE' });
    return { message: (res as { message?: string }).message ?? `Agent ${id} deleted` };
  },
  deploy: (id: string) =>
    fetchJson<{ runtimeArn: string }>(`/agents/${id}/deploy`, {
      method: 'POST',
    }),
  undeploy: async (id: string) => {
    const res = await fetchJson<Record<string, unknown>>(`/agents/${id}/undeploy`, { method: 'POST' });
    return { message: (res as { message?: string }).message ?? `Agent ${id} undeployed` };
  },
  status: (id: string) =>
    fetchJson<{ status: string }>(`/agents/${id}/status`),
};

// Builder (SSE streaming)
export const builder = {
  chatStream: (
    messages: { role: string; content: string }[],
    sessionId: string,
    state: string,
  ): Promise<Response> =>
    fetch(`${API_BASE}/builder/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages, sessionId, state }),
    }),
};

// Gateways
export const gateways = {
  list: () =>
    fetchJson<{ gateways: { gatewayId: string; name: string; description: string; status: string; toolCount: number }[]; totalGateways: number; totalTools: number }>(
      '/gateways',
    ),
  tools: (id: string) =>
    fetchJson<{ tools: { name: string; description: string }[] }>(
      `/gateways/${id}/tools`,
    ),
};

// Sessions / Task History
export const sessions = {
  list: (limit = 20) =>
    fetchJson<{ sessions: SessionMeta[]; count: number }>(
      `/sessions?limit=${limit}`,
    ),
  get: (sessionId: string) =>
    fetchJson<SessionDetail>(`/sessions/${sessionId}`),
};

// Observability — X-Ray + CloudWatch
export const obs = {
  sessions: (hours = 1) =>
    fetchJson<{ sessions: Record<string, TraceSession[]>; totalTraces: number }>(
      `/obs/sessions?hours=${hours}`,
    ),
  trace: (traceId: string) =>
    fetchJson<{ traceId: string; spans: OtelSpan[]; spanCount: number }>(
      `/obs/traces/${traceId}/logs`,
    ),
  serviceMap: (hours = 1) =>
    fetchJson<{ services: ServiceNode[]; count: number }>(
      `/obs/service-map?hours=${hours}`,
    ),
};

// HITL Feedback
export const feedback = {
  send: (
    agentId: string,
    sessionId: string,
    data: { approved: boolean; comment?: string },
  ) =>
    fetchJson<{ message: string }>(`/agents/${agentId}/feedback/${sessionId}`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
};
