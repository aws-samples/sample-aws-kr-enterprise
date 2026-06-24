import { apiClient } from "./client";

export interface Comment {
  comment_id: string;
  project_id: string;
  screen_id: string;
  component_id: string | null;
  stage_id: string;
  content: string;
  parent_id: string | null;
  resolved: boolean;
  created_at: string;
  created_by: string;
}

export interface ShareLink {
  share_token: string;
  project_id: string;
  permission: string;
  expires_at: string | null;
  active: boolean;
}

export async function createComment(projectId: string, screenId: string, stageId: string, content: string, componentId?: string, parentId?: string): Promise<Comment> {
  return apiClient.post("/collaboration/comments", { project_id: projectId, screen_id: screenId, stage_id: stageId, content, component_id: componentId, parent_id: parentId });
}

export async function listComments(projectId: string, screenId: string): Promise<Comment[]> {
  return apiClient.get(`/collaboration/comments?project_id=${projectId}&screen_id=${screenId}`);
}

export async function resolveComment(commentId: string, projectId: string, screenId: string, resolved = true): Promise<void> {
  await apiClient.patch(`/collaboration/comments/${commentId}/resolve?project_id=${projectId}&screen_id=${screenId}`, { resolved });
}

export async function createShareLink(projectId: string, teamId: string, permission = "read_only", expiresInHours?: number): Promise<ShareLink> {
  return apiClient.post("/collaboration/share", { project_id: projectId, team_id: teamId, permission, expires_in_hours: expiresInHours });
}

export async function verifyShareLink(token: string): Promise<{ project_id: string; permission: string }> {
  return apiClient.get(`/collaboration/share/${token}`);
}

export async function addTeamMember(teamId: string, email: string, role = "editor"): Promise<void> {
  await apiClient.post(`/collaboration/teams/${teamId}/members`, { email, role });
}

export async function listTeamMembers(teamId: string): Promise<Array<{ userId: string; role: string; joinedAt: string }>> {
  return apiClient.get(`/collaboration/teams/${teamId}/members`);
}

export async function removeTeamMember(teamId: string, userId: string): Promise<void> {
  await apiClient.delete(`/collaboration/teams/${teamId}/members/${userId}`);
}
