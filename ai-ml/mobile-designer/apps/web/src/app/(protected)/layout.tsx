"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/contexts/AuthContext";
import { Skeleton } from "@/components/common/Skeleton";

export default function ProtectedLayout({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading, user, logout } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      router.push("/login");
    }
  }, [isLoading, isAuthenticated, router]);

  if (isLoading) {
    return <div className="min-h-screen flex items-center justify-center"><Skeleton className="h-8 w-48" /></div>;
  }

  if (!isAuthenticated) return null;

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b bg-white sticky top-0 z-40">
        <div className="max-w-6xl mx-auto px-6 h-14 flex items-center justify-between">
          <div className="flex items-center gap-6">
            <Link href="/dashboard" className="font-semibold text-sm">
              Mobile Designer
            </Link>
            <nav className="flex items-center gap-4">
              <Link href="/dashboard" className="text-sm text-gray-600 hover:text-gray-900 transition-colors">
                프로젝트
              </Link>
              {user?.role === "admin" && (
                <Link href="/admin/users" className="text-sm text-gray-600 hover:text-gray-900 transition-colors">
                  관리자
                </Link>
              )}
            </nav>
          </div>
          <div className="flex items-center gap-4">
            <Link href="/settings" className="text-sm text-gray-600 hover:text-gray-900 transition-colors">
              설정
            </Link>
            <button
              onClick={() => { logout(); router.push("/login"); }}
              className="text-sm text-gray-500 hover:text-gray-700 transition-colors"
            >
              로그아웃
            </button>
          </div>
        </div>
      </header>
      {children}
    </div>
  );
}
