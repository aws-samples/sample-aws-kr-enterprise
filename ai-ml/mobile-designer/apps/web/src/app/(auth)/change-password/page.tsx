"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { useAuth } from "@/lib/contexts/AuthContext";
import { useToast } from "@/components/common/Toast";
import * as adminApi from "@/lib/api/admin";

export default function ChangePasswordPage() {
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const { clearMustChangePassword, isAuthenticated, mustChangePassword } = useAuth();
  const { showToast } = useToast();
  const router = useRouter();

  if (!isAuthenticated || !mustChangePassword) {
    if (typeof window !== "undefined") {
      router.push("/login");
    }
    return null;
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (newPassword.length < 8) {
      showToast("비밀번호는 8자 이상이어야 합니다", "error");
      return;
    }

    if (newPassword !== confirmPassword) {
      showToast("비밀번호가 일치하지 않습니다", "error");
      return;
    }

    setIsLoading(true);
    try {
      const currentPassword = localStorage.getItem("_temp_login_password") || "";
      await adminApi.changePassword(currentPassword, newPassword);
      clearMustChangePassword();
      showToast("비밀번호가 변경되었습니다", "success");
      router.push("/dashboard");
    } catch (err: any) {
      showToast(err.message || "비밀번호 변경에 실패했습니다", "error");
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4" data-testid="change-password-form">
      <h1 className="text-2xl font-bold text-center mb-2">비밀번호 변경</h1>
      <p className="text-sm text-gray-500 text-center mb-6">
        보안을 위해 비밀번호를 변경해야 합니다.
      </p>
      <Input
        label="새 비밀번호"
        type="password"
        value={newPassword}
        onChange={(e) => setNewPassword(e.target.value)}
        required
        data-testid="new-password"
      />
      <Input
        label="비밀번호 확인"
        type="password"
        value={confirmPassword}
        onChange={(e) => setConfirmPassword(e.target.value)}
        required
        data-testid="confirm-password"
      />
      <Button type="submit" className="w-full" isLoading={isLoading} data-testid="change-password-submit">
        비밀번호 변경
      </Button>
    </form>
  );
}
