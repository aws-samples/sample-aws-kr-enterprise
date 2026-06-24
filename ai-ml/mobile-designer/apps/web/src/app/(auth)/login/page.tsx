"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { useAuth } from "@/lib/contexts/AuthContext";
import { useToast } from "@/components/common/Toast";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const { login, mustChangePassword, isAuthenticated } = useAuth();
  const { showToast } = useToast();
  const router = useRouter();

  useEffect(() => {
    if (isAuthenticated) {
      if (mustChangePassword) {
        router.push("/change-password");
      } else {
        router.push("/dashboard");
      }
    }
  }, [isAuthenticated, mustChangePassword, router]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    try {
      await login(email, password);
    } catch (err: any) {
      showToast(err.message || "로그인에 실패했습니다", "error");
      setIsLoading(false);
      return;
    }
    setIsLoading(false);
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4" data-testid="login-form">
      <h1 className="text-2xl font-bold text-center mb-6">Mobile Designer</h1>
      <Input label="이메일" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required data-testid="login-email" />
      <Input label="비밀번호" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required data-testid="login-password" />
      <Button type="submit" className="w-full" isLoading={isLoading} data-testid="login-submit">로그인</Button>
      <div className="text-center text-sm space-y-1">
        <Link href="/register" className="text-primary hover:underline block">계정 만들기</Link>
        <Link href="/reset-password" className="text-gray-500 hover:underline block">비밀번호를 잊으셨나요?</Link>
      </div>
    </form>
  );
}
