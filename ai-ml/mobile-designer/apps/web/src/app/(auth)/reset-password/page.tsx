"use client";

import Link from "next/link";
import { useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { requestPasswordReset } from "@/lib/api/auth";
import { useToast } from "@/components/common/Toast";

export default function ResetPasswordPage() {
  const [email, setEmail] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [sent, setSent] = useState(false);
  const { showToast } = useToast();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    try {
      await requestPasswordReset(email);
      setSent(true);
    } catch (err: any) {
      showToast(err.message || "오류가 발생했습니다", "error");
    } finally {
      setIsLoading(false);
    }
  };

  if (sent) {
    return (
      <div className="text-center space-y-4">
        <h1 className="text-2xl font-bold">이메일을 확인하세요</h1>
        <p className="text-gray-500">비밀번호 재설정 링크를 발송했습니다.</p>
        <Link href="/login" className="text-primary hover:underline">로그인으로 돌아가기</Link>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <h1 className="text-2xl font-bold text-center mb-6">비밀번호 재설정</h1>
      <Input label="이메일" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
      <Button type="submit" className="w-full" isLoading={isLoading}>재설정 링크 발송</Button>
      <Link href="/login" className="block text-center text-sm text-gray-500 hover:underline">로그인으로 돌아가기</Link>
    </form>
  );
}
