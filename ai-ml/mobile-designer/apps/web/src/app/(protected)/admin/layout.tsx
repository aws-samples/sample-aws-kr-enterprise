"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/contexts/AuthContext";
import { clsx } from "clsx";

const NAV_ITEMS = [
  { href: "/admin/users", label: "사용자 관리" },
  { href: "/admin/prompts", label: "프롬프트 관리" },
  { href: "/admin/settings", label: "시스템 설정" },
];

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const { user, isLoading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!isLoading && user?.role !== "admin") {
      router.push("/dashboard");
    }
  }, [isLoading, user, router]);

  if (isLoading || user?.role !== "admin") {
    return null;
  }

  return (
    <div className="max-w-6xl mx-auto px-6 py-8">
      <h1 className="text-2xl font-bold mb-6">관리자</h1>
      <div className="flex gap-8">
        <nav className="w-48 shrink-0">
          <ul className="space-y-1">
            {NAV_ITEMS.map((item) => (
              <li key={item.href}>
                <Link
                  href={item.href}
                  className={clsx(
                    "block px-3 py-2 rounded-mdesigner text-sm transition-colors",
                    pathname.startsWith(item.href)
                      ? "bg-primary/10 text-primary font-medium"
                      : "text-gray-600 hover:bg-gray-100",
                  )}
                >
                  {item.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
        <main className="flex-1 min-w-0">{children}</main>
      </div>
    </div>
  );
}
