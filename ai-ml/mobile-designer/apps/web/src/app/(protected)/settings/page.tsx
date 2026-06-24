"use client";

import { useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { useAuth } from "@/lib/contexts/AuthContext";
import { useToast } from "@/components/common/Toast";
import * as adminApi from "@/lib/api/admin";

export default function SettingsPage() {
  const { user, refreshUser } = useAuth();
  const { showToast } = useToast();

  const [name, setName] = useState(user?.name || "");
  const [email, setEmail] = useState(user?.email || "");
  const [profileLoading, setProfileLoading] = useState(false);

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordLoading, setPasswordLoading] = useState(false);

  const handleProfileSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      showToast("이름을 입력해주세요", "error");
      return;
    }
    setProfileLoading(true);
    try {
      await adminApi.updateProfile(name.trim(), email.trim() || undefined);
      await refreshUser();
      showToast("프로필이 업데이트되었습니다", "success");
    } catch (err: any) {
      showToast(err.message || "프로필 업데이트에 실패했습니다", "error");
    } finally {
      setProfileLoading(false);
    }
  };

  const handlePasswordChange = async (e: React.FormEvent) => {
    e.preventDefault();
    if (newPassword.length < 8) {
      showToast("새 비밀번호는 8자 이상이어야 합니다", "error");
      return;
    }
    if (newPassword !== confirmPassword) {
      showToast("비밀번호가 일치하지 않습니다", "error");
      return;
    }
    setPasswordLoading(true);
    try {
      await adminApi.changePassword(currentPassword, newPassword);
      showToast("비밀번호가 변경되었습니다", "success");
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err: any) {
      showToast(err.message || "비밀번호 변경에 실패했습니다", "error");
    } finally {
      setPasswordLoading(false);
    }
  };

  return (
    <div className="max-w-2xl mx-auto px-6 py-8">
      <h1 className="text-2xl font-bold mb-8">설정</h1>

      <section className="mb-10">
        <h2 className="text-lg font-semibold mb-4">프로필</h2>
        <form onSubmit={handleProfileSave} className="space-y-4">
          <Input
            label="이름"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            data-testid="settings-name"
          />
          <Input
            label="이메일 (선택)"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            data-testid="settings-email"
          />
          <Button type="submit" isLoading={profileLoading} data-testid="settings-profile-save">
            프로필 저장
          </Button>
        </form>
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-4">비밀번호 변경</h2>
        <form onSubmit={handlePasswordChange} className="space-y-4">
          <Input
            label="현재 비밀번호"
            type="password"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            required
            data-testid="settings-current-password"
          />
          <Input
            label="새 비밀번호"
            type="password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            required
            data-testid="settings-new-password"
          />
          <Input
            label="새 비밀번호 확인"
            type="password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            required
            data-testid="settings-confirm-password"
          />
          <Button type="submit" isLoading={passwordLoading} data-testid="settings-password-save">
            비밀번호 변경
          </Button>
        </form>
      </section>
    </div>
  );
}
