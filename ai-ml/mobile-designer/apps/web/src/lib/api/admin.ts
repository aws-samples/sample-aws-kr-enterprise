import { apiClient } from "./client";

// --- Types ---

export interface AdminUser {
  userId: string;
  email: string;
  name: string;
  role: "admin" | "user";
  status: "active" | "inactive";
  personalTeamId: string;
  createdAt: string;
}

export interface CreateUserPayload {
  email: string;
  name: string;
  password: string;
  is_admin?: boolean;
}

export interface SystemConfig {
  registrationOpen: boolean;
  maxUsers: number;
  maintenanceMode: boolean;
  models?: Record<string, string>;
}

export interface PromptSlot {
  slot: string;
  active_version: string | null;
  total_versions: number;
}

export interface PromptVersion {
  prompt_slot: string;
  version: string;
  title: string;
  is_active: boolean;
  content_key: string;
  created_by: string;
  created_at: string;
}

export interface PromptContent {
  slot: string;
  version: string;
  content: string;
}

// --- Users ---

export async function listUsers(): Promise<AdminUser[]> {
  return apiClient.get("/admin/users");
}

export async function createUser(payload: CreateUserPayload): Promise<AdminUser> {
  return apiClient.post("/admin/users", payload);
}

export async function resetUserPassword(userId: string): Promise<{ temp_password: string }> {
  return apiClient.patch(`/admin/users/${userId}/reset-password`, {});
}

export async function changeUserRole(userId: string, role: "admin" | "user"): Promise<void> {
  return apiClient.patch(`/admin/users/${userId}/role`, { role });
}

export async function deactivateUser(userId: string): Promise<void> {
  return apiClient.patch(`/admin/users/${userId}/deactivate`, {});
}

export async function deleteUser(userId: string): Promise<void> {
  return apiClient.delete(`/admin/users/${userId}`);
}

// --- Settings ---

export async function getSettings(): Promise<SystemConfig> {
  return apiClient.get("/admin/settings");
}

export async function updateSettings(config: Partial<SystemConfig>): Promise<SystemConfig> {
  return apiClient.patch<SystemConfig>("/admin/settings", config);
}

// --- Prompts ---

export async function listPromptSlots(): Promise<PromptSlot[]> {
  return apiClient.get("/admin/prompts");
}

export async function getPromptVersions(slot: string): Promise<PromptVersion[]> {
  return apiClient.get(`/admin/prompts/${slot}`);
}

export async function createPromptVersion(slot: string, title: string, content: string): Promise<PromptVersion> {
  return apiClient.post(`/admin/prompts/${slot}`, { title, content });
}

export async function activatePromptVersion(slot: string, version: string): Promise<void> {
  return apiClient.patch(`/admin/prompts/${slot}/${version}/activate`, {});
}

export async function getPromptContent(slot: string, version: string): Promise<PromptContent> {
  return apiClient.get(`/admin/prompts/${slot}/${version}`);
}

// --- Auth self-service ---

export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  return apiClient.patch("/auth/password", { current_password: currentPassword, new_password: newPassword });
}

export async function updateProfile(name: string, email?: string): Promise<void> {
  const body: Record<string, string> = { name };
  if (email) body.email = email;
  return apiClient.patch("/auth/profile", body);
}
