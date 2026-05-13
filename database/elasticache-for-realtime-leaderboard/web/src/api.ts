/**
 * API client for the leaderboard backend.
 */

const API_URL = __API_URL__;

export interface LeaderboardEntry {
  userId: string;
  score: number;
  rank: number;
}

export interface LeaderboardResponse {
  top: LeaderboardEntry[];
  me: { userId: string; rank: number; score: number } | null;
}

export interface StartLoadResponse {
  message: string;
  executionArn: string;
  pattern: string;
  config: {
    tps: number;
    duration_sec: number;
    game_ids: string[];
    user_pool_size: number;
  };
}

export interface StartLoadRequest {
  pattern: string;
  game_ids?: string[];
  user_pool_size?: number;
}

export async function fetchLeaderboard(
  gameId: string,
  limit: number = 100
): Promise<LeaderboardResponse> {
  const params = new URLSearchParams({
    gameId,
    limit: String(limit),
  });

  const response = await fetch(`${API_URL}/leaderboard?${params}`);
  if (!response.ok) {
    throw new Error(`Leaderboard fetch failed: ${response.status}`);
  }
  return response.json();
}

export interface MetricData {
  timestamps: string[];
  values: number[];
  label: string;
}

export interface MetricsResponse {
  sqs_depth: MetricData;
  lambda_invocations: MetricData;
  lambda_errors: MetricData;
  valkey_cpu: MetricData;
  e2e_latency: MetricData;
}

export async function startLoad(
  request: StartLoadRequest
): Promise<StartLoadResponse> {
  const response = await fetch(`${API_URL}/demo/start-load`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(
      (err as { error?: string }).error || `Start load failed: ${response.status}`
    );
  }
  return response.json();
}

export async function fetchMetrics(): Promise<MetricsResponse> {
  const response = await fetch(`${API_URL}/admin/metrics`);
  if (!response.ok) {
    throw new Error(`Metrics fetch failed: ${response.status}`);
  }
  return response.json();
}
