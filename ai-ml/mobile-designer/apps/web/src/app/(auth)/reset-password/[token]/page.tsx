"use client";

import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { confirmPasswordReset } from "@/lib/api/auth";
import { useToast } from "@/components/common/Toast";

export default function ResetPasswordConfirmPage() {
  const { token } = useParams<{ token: string }>();
  const [password, setPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const { showToast } = useToast();
  const router = useRouter();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    try {
      await confirmPasswordReset(token, password);
      showToast("비밀번호가 변경되었습니다", "success");
      router.push("/login");
    } catch (err: any) {
      showToast(err.message || "재설정에 실패했습니다", "error");
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <h1 className="text-2xl font-bold text-center mb-6">새 비밀번호 설정</h1>
      <Input label="새 비밀번호" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8} />
      <Button type="submit" className="w-full" isLoading={isLoading}>비밀번호 변경</Button>
    </form>
  );
}
