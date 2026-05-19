// Spec Section 5.1 기반 TypeScript 타입 정의

export interface GatewayBinding {
  gatewayId: string;
  toolFilter: string | string[];
}

export interface Delegation {
  targetAgent: string;
  purpose: string;
  scope: string[];
  condition: string;
  timeout: number;
}

export interface EvaluatorConfig {
  enabled: boolean;
  criteria?: string;
}

export interface HarnessConfig {
  preHooks: string[];
  postHooks: string[];
  hitlActions: string[];
  evaluator: EvaluatorConfig;
}

export interface InternalToolConfig {
  name: string;
  description: string;
  type: string;
  table?: string;
  module?: string;
}

export interface TriggerConfig {
  type: string;
  source?: string;
  pattern?: Record<string, unknown>;
  cron?: string;
  description?: string;
}

export interface AgentConfig {
  agentId: string;
  name: string;
  contextBoundary: string;
  model: string;
  systemPrompt: string;
  gateways: GatewayBinding[];
  delegations: Delegation[];
  harness: HarnessConfig;
  triggers: TriggerConfig[];
  internalTools: InternalToolConfig[];
  createdBy: string;
  version: number;
  metadata?: Record<string, unknown>;
  healthiness?: string;
  healthCheckedAt?: string;
}

export interface AgentCard {
  agentId: string;
  name: string;
  description: string;
  capabilities: string[];
  status: string;
  delegatesTo: string[];
  contextBoundary: string;
}

export interface AgentRuntime {
  agentId: string;
  runtimeArn: string;
  status: 'provisioning' | 'active' | 'stopped' | 'pre-provisioned';
  createdAt: string;
  version: number;
}

export interface AgentDetail {
  config: AgentConfig;
  runtime: AgentRuntime | null;
}

export interface SSEEvent {
  type: string;
  data: Record<string, unknown>;
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: string;
  events?: SSEEvent[];
}

export interface BuilderResponse {
  message: string;
  state: string;
  sessionId: string;
  agentConfig?: AgentConfig;
}

export interface GatewayInfo {
  gatewayId: string;
  name: string;
  description: string;
  status: string;
  toolCount: number;
}

export interface ToolInfo {
  toolId: string;
  name: string;
  description: string;
  permission: 'read' | 'write';
}

export interface GatewayListResponse {
  gateways: GatewayInfo[];
  totalGateways: number;
  totalTools: number;
}

export interface SessionSpan {
  eventId: string;
  type: string;
  agentId: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface SessionMeta {
  sessionId: string;
  agentId: string;
  trigger: string;
  startedAt: string;
  status: string;
}

export interface SessionDetail {
  sessionId: string;
  agentId: string;
  trigger: string;
  startedAt: string;
  status: string;
  spans: SessionSpan[];
  spanCount: number;
}

export interface OtelSpan {
  name: string;
  spanId: string;
  parentSpanId: string;
  startTimeUnixNano: number | null;
  endTimeUnixNano: number | null;
  operation: string;
  agentName: string;
  model: string;
  toolName: string;
  toolCallId: string;
  toolStatus: string;
  inputTokens: number | null;
  outputTokens: number | null;
  totalTokens: number | null;
  ttft: number | null;
  requestDuration: number | null;
  sessionId: string;
  service: string;
}

export interface TraceSession {
  traceId: string;
  duration: number | null;
  startTime: string;
  agentName?: string;
  model?: string;
  inputTokens?: number;
  outputTokens?: number;
  totalTokens?: number;
  sessionId?: string;
}

export interface ServiceNode {
  name: string;
  type: string;
  edges: { target: number; aliases: string[] }[];
  summaryStats: Record<string, unknown>;
}

export type BuilderSSEEventType = 'status' | 'text' | 'done' | 'error';

export interface BuilderSSEEvent {
  type: BuilderSSEEventType;
  data: {
    content?: string;
    phase?: string;
    state?: string;
    sessionId?: string;
    fullMessage?: string;
    agentConfig?: AgentConfig;
    error?: string;
  };
}
