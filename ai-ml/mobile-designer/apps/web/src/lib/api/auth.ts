import { apiClient } from "./client";

interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  must_change_password?: boolean;
}

interface UserResponse {
  user_id: string;
  email: string;
  name: string;
  personal_team_id: string;
  role?: "admin" | "member";
  created_at: string;
}

export async function register(email: string, name: string, password: string): Promise<UserResponse> {
  return apiClient.post("/auth/register", { email, name, password });
}

export async function login(email: string, password: string): Promise<TokenResponse> {
  const res = await apiClient.post<TokenResponse>("/auth/login", { email, password });
  apiClient.login(res.access_token, res.refresh_token);
  return res;
}

export async function getMe(): Promise<UserResponse> {
  return apiClient.get("/auth/me");
}

export async function requestPasswordReset(email: string): Promise<void> {
  await apiClient.post("/auth/password-reset/request", { email });
}

export async function confirmPasswordReset(token: string, newPassword: string): Promise<void> {
  await apiClient.post("/auth/password-reset/confirm", { token, new_password: newPassword });
}

export function logout(): void {
  apiClient.logout();
}
