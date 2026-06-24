"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import * as authApi from "@/lib/api/auth";

interface User {
  userId: string;
  email: string;
  name: string;
  personalTeamId: string;
  role: "admin" | "member";
}

interface AuthState {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  mustChangePassword: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, name: string, password: string) => Promise<void>;
  logout: () => void;
  clearMustChangePassword: () => void;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [mustChangePassword, setMustChangePassword] = useState(false);

  useEffect(() => {
    const token = localStorage.getItem("access_token");
    if (token) {
      authApi.getMe().then((data) => {
        setUser({
          userId: data.user_id,
          email: data.email,
          name: data.name,
          personalTeamId: data.personal_team_id,
          role: (data as any).role || "member",
        });
      }).catch(() => {
        localStorage.removeItem("access_token");
        localStorage.removeItem("refresh_token");
      }).finally(() => setIsLoading(false));
    } else {
      setIsLoading(false);
    }
  }, []);

  const refreshUser = useCallback(async () => {
    const data = await authApi.getMe();
    setUser({
      userId: data.user_id,
      email: data.email,
      name: data.name,
      personalTeamId: data.personal_team_id,
      role: (data as any).role || "member",
    });
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const res = await authApi.login(email, password);
    if ((res as any).must_change_password) {
      setMustChangePassword(true);
      localStorage.setItem("_temp_login_password", password);
    }
    const me = await authApi.getMe();
    setUser({
      userId: me.user_id,
      email: me.email,
      name: me.name,
      personalTeamId: me.personal_team_id,
      role: (me as any).role || "member",
    });
  }, []);

  const register = useCallback(async (email: string, name: string, password: string) => {
    await authApi.register(email, name, password);
    await login(email, password);
  }, [login]);

  const logout = useCallback(() => {
    authApi.logout();
    setUser(null);
    setMustChangePassword(false);
    localStorage.removeItem("_temp_login_password");
  }, []);

  const clearMustChangePassword = useCallback(() => {
    setMustChangePassword(false);
    localStorage.removeItem("_temp_login_password");
  }, []);

  return (
    <AuthContext.Provider value={{ user, isAuthenticated: !!user, isLoading, mustChangePassword, login, register, logout, clearMustChangePassword, refreshUser }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
