import { apiClient } from "./client";

export type HandoffType = "full_project" | "design_tokens" | "figma_tokens" | "compose_theme" | "design_spec";

interface HandoffResponse {
  project_id: string;
  version_id: string;
  artifacts: Array<{ type: string; key: string; size: number }>;
}

interface BuildVerifyResponse {
  status: string;
  message: string;
  errors?: string[];
}

export interface TaskLogEntry {
  timestamp: string;
  step: string;
  detail: string;
}

export interface TaskStatusResponse {
  task_id: string;
  status: "pending" | "running" | "completed" | "failed" | "not_found";
  progress: number;
  current_step?: string;
  logs: TaskLogEntry[];
  result?: {
    project_id: string;
    version_id: string;
    artifacts: Array<{ type: string; key: string; size: number }>;
  };
  error?: string;
}

export async function generateHandoff(projectId: string, handoffType: HandoffType = "full_project", versionId?: string): Promise<HandoffResponse> {
  return apiClient.post("/handoff/generate", { project_id: projectId, version_id: versionId, handoff_type: handoffType });
}

export async function startProjectGeneration(projectId: string, versionId?: string): Promise<{ task_id: string }> {
  return apiClient.post("/handoff/generate-project", { project_id: projectId, version_id: versionId });
}

export async function getTaskStatus(taskId: string): Promise<TaskStatusResponse> {
  return apiClient.get(`/handoff/task/${taskId}`);
}

export async function getActiveHandoffTask(projectId: string): Promise<TaskStatusResponse> {
  return apiClient.get(`/handoff/active/${projectId}`);
}

export async function getDownloadUrl(projectId: string, versionId: string, artifactKey?: string): Promise<string> {
  const params = artifactKey ? `?artifact_key=${encodeURIComponent(artifactKey)}` : "";
  const res = await apiClient.get<{ download_url: string }>(`/handoff/${projectId}/${versionId}/download${params}`);
  return res.download_url;
}

export async function downloadProject(projectId: string): Promise<string> {
  const res = await apiClient.get<{ download_url: string }>(`/handoff/${projectId}/download-project`);
  return res.download_url;
}

export async function buildVerify(projectId: string, versionId: string): Promise<BuildVerifyResponse> {
  return apiClient.post("/handoff/build-verify", { project_id: projectId, version_id: versionId });
}
