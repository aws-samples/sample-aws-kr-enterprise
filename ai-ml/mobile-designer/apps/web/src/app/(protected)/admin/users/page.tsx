"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { Modal } from "@/components/common/Modal";
import { useToast } from "@/components/common/Toast";
import * as adminApi from "@/lib/api/admin";
import type { AdminUser } from "@/lib/api/admin";
import { useAuth } from "@/lib/contexts/AuthContext";

export default function AdminUsersPage() {
  const { showToast } = useToast();
  const { user } = useAuth();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [showCreateModal, setShowCreateModal] = useState(false);

  const [newEmail, setNewEmail] = useState("");
  const [newName, setNewName] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newIsAdmin, setNewIsAdmin] = useState(false);
  const [createLoading, setCreateLoading] = useState(false);

  useEffect(() => {
    loadUsers();
  }, []);

  async function loadUsers() {
    setIsLoading(true);
    try {
      const data = await adminApi.listUsers();
      setUsers(data);
    } catch {
      showToast("사용자 목록을 불러오지 못했습니다", "error");
    } finally {
      setIsLoading(false);
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newEmail || !newName || !newPassword) return;
    setCreateLoading(true);
    try {
      const user = await adminApi.createUser({
        email: newEmail,
        name: newName,
        password: newPassword,
        is_admin: newIsAdmin,
      });
      setUsers((prev) => [...prev, user]);
      showToast("사용자가 생성되었습니다", "success");
      setShowCreateModal(false);
      resetCreateForm();
    } catch (err: any) {
      showToast(err.message || "사용자 생성에 실패했습니다", "error");
    } finally {
      setCreateLoading(false);
    }
  }

  function resetCreateForm() {
    setNewEmail("");
    setNewName("");
    setNewPassword("");
    setNewIsAdmin(false);
  }

  async function handleResetPassword(userId: string) {
    if (!window.confirm("비밀번호를 리셋하시겠습니까?")) return;
    try {
      const result = await adminApi.resetUserPassword(userId);
      window.alert(`임시 비밀번호가 생성되었습니다.\n\n${result.temp_password}\n\n사용자는 다음 로그인 시 비밀번호를 변경해야 합니다.`);
    } catch (err: any) {
      showToast(err.message || "비밀번호 리셋에 실패했습니다", "error");
    }
  }

  async function handleRoleChange(userId: string, currentRole: "admin" | "user") {
    const newRole = currentRole === "admin" ? "user" : "admin";
    if (!window.confirm(`역할을 ${newRole === "admin" ? "관리자" : "일반 사용자"}로 변경하시겠습니까?`)) return;
    try {
      await adminApi.changeUserRole(userId, newRole);
      setUsers((prev) => prev.map((u) => u.userId === userId ? { ...u, role: newRole } : u));
      showToast("역할이 변경되었습니다", "success");
    } catch (err: any) {
      showToast(err.message || "역할 변경에 실패했습니다", "error");
    }
  }

  async function handleDeactivate(userId: string) {
    if (!window.confirm("사용자를 비활성화하시겠습니까?")) return;
    try {
      await adminApi.deactivateUser(userId);
      setUsers((prev) => prev.map((u) => u.userId === userId ? { ...u, status: "inactive" } : u));
      showToast("사용자가 비활성화되었습니다", "success");
    } catch (err: any) {
      showToast(err.message || "비활성화에 실패했습니다", "error");
    }
  }

  async function handleDelete(userId: string) {
    if (!window.confirm("사용자를 완전히 삭제하시겠습니까? 이 작업은 되돌릴 수 없습니다.")) return;
    try {
      await adminApi.deleteUser(userId);
      setUsers((prev) => prev.filter((u) => u.userId !== userId));
      showToast("사용자가 삭제되었습니다", "success");
    } catch (err: any) {
      showToast(err.message || "삭제에 실패했습니다", "error");
    }
  }

  return (
    <div>
      <div className="flex justify-between items-center mb-6">
        <h2 className="text-lg font-semibold">사용자 관리</h2>
        <Button onClick={() => setShowCreateModal(true)} size="sm" data-testid="add-user-btn">
          사용자 추가
        </Button>
      </div>

      {isLoading ? (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => <div key={i} className="h-12 bg-gray-100 rounded-mdesigner animate-pulse" />)}
        </div>
      ) : users.length === 0 ? (
        <p className="text-gray-500 text-sm">사용자가 없습니다.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="users-table">
            <thead>
              <tr className="border-b text-left text-gray-500">
                <th className="py-2 pr-4">이메일</th>
                <th className="py-2 pr-4">이름</th>
                <th className="py-2 pr-4">역할</th>
                <th className="py-2 pr-4">상태</th>
                <th className="py-2 pr-4">생성일</th>
                <th className="py-2">작업</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.userId} className="border-b last:border-0">
                  <td className="py-3 pr-4">{u.email}</td>
                  <td className="py-3 pr-4">{u.name}</td>
                  <td className="py-3 pr-4">
                    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${
                      u.role === "admin" ? "bg-blue-100 text-blue-700" : "bg-gray-100 text-gray-600"
                    }`}>
                      {u.role === "admin" ? "관리자" : "일반"}
                    </span>
                  </td>
                  <td className="py-3 pr-4">
                    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${
                      u.status === "active" ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"
                    }`}>
                      {u.status === "active" ? "활성" : "비활성"}
                    </span>
                  </td>
                  <td className="py-3 pr-4 text-gray-500">{new Date(u.createdAt).toLocaleDateString("ko-KR")}</td>
                  <td className="py-3">
                    {u.userId === user?.userId ? (
                      <span className="text-xs text-gray-400">본인</span>
                    ) : (
                      <div className="flex gap-1">
                        <Button variant="ghost" size="sm" onClick={() => handleResetPassword(u.userId)}>
                          비밀번호 리셋
                        </Button>
                        <Button variant="ghost" size="sm" onClick={() => handleRoleChange(u.userId, u.role)}>
                          역할 변경
                        </Button>
                        {u.status === "active" && (
                          <Button variant="ghost" size="sm" onClick={() => handleDeactivate(u.userId)}>
                            비활성화
                          </Button>
                        )}
                        <Button variant="danger" size="sm" onClick={() => handleDelete(u.userId)}>
                          삭제
                        </Button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Modal isOpen={showCreateModal} onClose={() => { setShowCreateModal(false); resetCreateForm(); }} title="사용자 추가">
        <form onSubmit={handleCreate} className="space-y-4">
          <Input
            label="이메일"
            type="email"
            value={newEmail}
            onChange={(e) => setNewEmail(e.target.value)}
            required
            data-testid="create-user-email"
          />
          <Input
            label="이름"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            required
            data-testid="create-user-name"
          />
          <Input
            label="비밀번호"
            type="password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            required
            data-testid="create-user-password"
          />
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={newIsAdmin}
              onChange={(e) => setNewIsAdmin(e.target.checked)}
              className="rounded border-gray-300"
              data-testid="create-user-admin"
            />
            관리자 권한 부여
          </label>
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="outline" type="button" onClick={() => { setShowCreateModal(false); resetCreateForm(); }}>
              취소
            </Button>
            <Button type="submit" isLoading={createLoading} data-testid="create-user-submit">
              생성
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
