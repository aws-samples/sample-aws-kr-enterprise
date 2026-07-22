// Client-side auth token store + Cognito login helpers.
// The Platform API enforces Cognito JWT on all /api/* routes except /api/auth/*,
// so the SPA stores the access token and injects it on every API call.

const ACCESS_KEY = 'aiops.accessToken';
const ID_KEY = 'aiops.idToken';
const REFRESH_KEY = 'aiops.refreshToken';
const EMAIL_KEY = 'aiops.email';

export interface LoginResult {
  access_token: string;
  id_token?: string;
  refresh_token?: string;
  expires_in?: number;
  token_type?: string;
}

export function getToken(): string | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage.getItem(ACCESS_KEY);
}

export function getEmail(): string | null {
  if (typeof window === 'undefined') return null;
  return window.localStorage.getItem(EMAIL_KEY);
}

export function isAuthenticated(): boolean {
  return !!getToken();
}

export function setSession(result: LoginResult, email: string): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(ACCESS_KEY, result.access_token);
  if (result.id_token) window.localStorage.setItem(ID_KEY, result.id_token);
  if (result.refresh_token) window.localStorage.setItem(REFRESH_KEY, result.refresh_token);
  window.localStorage.setItem(EMAIL_KEY, email);
}

export function clearSession(): void {
  if (typeof window === 'undefined') return;
  [ACCESS_KEY, ID_KEY, REFRESH_KEY, EMAIL_KEY].forEach((k) =>
    window.localStorage.removeItem(k),
  );
}

/** Authenticate against the Platform API and persist the returned tokens. */
export async function login(email: string, password: string): Promise<void> {
  const res = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(typeof err.detail === 'string' ? err.detail : 'Login failed');
  }
  const data: LoginResult = await res.json();
  if (!data.access_token) throw new Error('No access token returned');
  setSession(data, email);
}

/** Clear the local session and return to the login screen. */
export function logout(): void {
  clearSession();
  if (typeof window !== 'undefined') window.location.href = '/';
}
