"use client";

import type { ReactNode } from "react";
import { AuthProvider } from "./AuthContext";
import { ToastProvider } from "@/components/common/Toast";

export function Providers({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <ToastProvider>
        {children}
      </ToastProvider>
    </AuthProvider>
  );
}
