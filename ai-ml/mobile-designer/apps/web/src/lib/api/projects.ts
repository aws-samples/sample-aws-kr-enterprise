import { apiClient } from "./client";

export interface Project {
  project_id: string;
  team_id: string;
  name: string;
  current_stage: string;
  stage_status: Record<string, string>;
  created_at: string;
  updated_at: string;
  created_by: string;
}

export interface Version {
  version_id: string;
  project_id: string;
  stage_id: string;
  action: string;
  command: string;
  parent_version_id: string | null;
  created_at: string;
  created_by: string;
}

export async function createProject(name: string, teamId?: string): Promise<Project> {
  return apiClient.post("/projects", { name, team_id: teamId });
}

export async function listProjects(teamId?: string, limit = 20, cursor?: string): Promise<{ items: Project[]; next_cursor: string | null }> {
  const params = new URLSearchParams();
  if (teamId) params.set("team_id", teamId);
  params.set("limit", String(limit));
  if (cursor) params.set("cursor", cursor);
  return apiClient.get(`/projects?${params}`);
}

export async function getProject(projectId: string, teamId?: string): Promise<Project> {
  const params = teamId ? `?team_id=${teamId}` : "";
  return apiClient.get(`/projects/${projectId}${params}`);
}

export async function updateProject(projectId: string, name: string): Promise<Project> {
  return apiClient.patch(`/projects/${projectId}`, { name });
}

export async function deleteProject(projectId: string): Promise<void> {
  return apiClient.delete(`/projects/${projectId}`);
}

export async function advanceStage(projectId: string): Promise<Project> {
  return apiClient.post(`/projects/${projectId}/advance-stage`);
}

export async function listVersions(projectId: string, stageId?: string): Promise<{ items: Version[]; next_cursor: string | null }> {
  const params = new URLSearchParams();
  if (stageId) params.set("stage_id", stageId);
  return apiClient.get(`/projects/${projectId}/versions?${params}`);
}

export async function revertVersion(projectId: string, targetVersionId: string): Promise<Version> {
  return apiClient.post(`/projects/${projectId}/revert`, { target_version_id: targetVersionId });
}

export async function getStageSnapshot(projectId: string, stageId: string): Promise<{ design: Record<string, unknown> | null }> {
  return apiClient.get(`/projects/${projectId}/stages/${stageId}/snapshot`);
}

export async function updateTokens(projectId: string, stageId: string, tokens: Record<string, unknown>): Promise<{ version_id: string; status: string }> {
  return apiClient.post(`/projects/${projectId}/tokens`, { stage_id: stageId, tokens });
}
