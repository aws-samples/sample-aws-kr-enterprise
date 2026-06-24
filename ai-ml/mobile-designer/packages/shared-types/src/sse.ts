export type SSEEventType =
  | "start"
  | "progress"
  | "component_update"
  | "design_complete"
  | "error"
  | "ping"
  | "heartbeat"
  | "done";

export interface SSEEvent {
  event: SSEEventType;
  data: Record<string, unknown>;
  id?: string;
  retry?: number;
}
