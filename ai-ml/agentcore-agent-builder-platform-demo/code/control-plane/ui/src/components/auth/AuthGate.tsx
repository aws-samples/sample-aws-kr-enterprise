'use client';

import { useEffect, useState } from 'react';
import { isAuthenticated, login } from '@/lib/auth';

/**
 * Client-side auth gate. Renders a login form until a Cognito access token is
 * present in local storage, then renders the app. The Platform API enforces
 * JWT on all /api/* routes, so nothing useful renders without a token anyway.
 */
export default function AuthGate({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [authed, setAuthed] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    setAuthed(isAuthenticated());
    setReady(true);
  }, []);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    setSubmitting(true);
    try {
      await login(email, password);
      setAuthed(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed');
    } finally {
      setSubmitting(false);
    }
  }

  // Avoid a flash of the login form before localStorage is read.
  if (!ready) return null;

  if (authed) return <>{children}</>;

  return (
    <div className="flex items-center justify-center min-h-screen w-full bg-slate-900">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm rounded-xl border border-slate-700 bg-slate-800 p-8 shadow-xl"
      >
        <h1 className="mb-1 text-2xl font-bold text-white">
          <span className="text-purple-400">AIOps</span> Platform
        </h1>
        <p className="mb-6 text-sm text-slate-400">Sign in to continue</p>

        <label className="mb-1 block text-xs text-slate-400">Email</label>
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="username"
          required
          className="mb-4 w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-white focus:border-purple-500 focus:outline-none"
        />

        <label className="mb-1 block text-xs text-slate-400">Password</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          required
          className="mb-4 w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-white focus:border-purple-500 focus:outline-none"
        />

        {error && (
          <p className="mb-4 rounded-md bg-red-500/10 px-3 py-2 text-sm text-red-400">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded-md bg-purple-600 px-4 py-2 font-semibold text-white transition hover:bg-purple-500 disabled:opacity-50"
        >
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  );
}
