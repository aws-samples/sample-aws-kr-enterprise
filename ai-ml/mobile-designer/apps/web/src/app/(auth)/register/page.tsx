"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { useAuth } from "@/lib/contexts/AuthContext";
import { useToast } from "@/components/common/Toast";

export default function RegisterPage() {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const { register } = useAuth();
  const { showToast } = useToast();
  const router = useRouter();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);
    try {
      await register(email, name, password);
      router.push("/dashboard");
    } catch (err: any) {
      showToast(err.message || "가입에 실패했습니다", "error");
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4" data-testid="register-form">
      <h1 className="text-2xl font-bold text-center mb-6">회원가입</h1>
      <Input label="이름" value={name} onChange={(e) => setName(e.target.value)} required minLength={2} data-testid="register-name" />
      <Input label="이메일" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required data-testid="register-email" />
      <Input label="비밀번호" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8} helper="8자 이상" data-testid="register-password" />
      <Button type="submit" className="w-full" isLoading={isLoading} data-testid="register-submit">가입하기</Button>
      <p className="text-center text-sm">
        이미 계정이 있으신가요? <Link href="/login" className="text-primary hover:underline">로그인</Link>
      </p>
    </form>
  );
}
