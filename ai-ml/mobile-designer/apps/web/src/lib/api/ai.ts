import { apiClient } from "./client";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface ChatResponse {
  reply: string;
  ready_to_proceed: boolean;
}

export interface ChatHistoryResponse {
  messages: ChatMessage[];
  ready_to_proceed: boolean;
}

export interface TaskLog {
  timestamp: string;
  step: string;
  detail: string;
}

export interface TaskStatus {
  task_id: string;
  project_id: string;
  stage: string;
  status: "pending" | "running" | "completed" | "failed" | "none" | "not_found";
  progress: number;
  current_step: string;
  logs: TaskLog[];
  result: Record<string, unknown> | null;
  error: string | null;
}

export async function getChatHistory(projectId: string, sessionId: string): Promise<ChatHistoryResponse> {
  return apiClient.get(`/ai/chat/history/${projectId}/${sessionId}`);
}

export async function getChatStatus(projectId: string, sessionId: string): Promise<{ is_responding: boolean }> {
  return apiClient.get(`/ai/chat/status/${projectId}/${sessionId}`);
}

export async function startChat(
  projectId: string,
  message: string,
  sessionId: string,
  fileIds: string[] = [],
  stage: string = "requirements",
): Promise<{ task_id: string }> {
  return apiClient.post("/ai/chat", {
    project_id: projectId,
    message,
    session_id: sessionId,
    stage,
    file_ids: fileIds,
  });
}

export async function startGenerate(projectId: string, command: string, stage: string, fileIds: string[] = []): Promise<{ task_id: string }> {
  return apiClient.post("/ai/generate", { project_id: projectId, command, stage, file_ids: fileIds });
}

export async function startModify(projectId: string, command: string, stage: string, selectedComponentId?: string): Promise<{ task_id: string }> {
  return apiClient.post("/ai/modify", { project_id: projectId, command, stage, selected_component_id: selectedComponentId });
}

export async function getTaskStatus(taskId: string): Promise<TaskStatus> {
  return apiClient.get(`/ai/tasks/${taskId}`);
}

export async function getActiveTask(projectId: string, stage: string): Promise<TaskStatus> {
  return apiClient.get(`/ai/tasks/active/${projectId}/${stage}`);
}
