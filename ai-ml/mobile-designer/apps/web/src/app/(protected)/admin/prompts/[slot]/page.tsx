"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Button } from "@/components/common/Button";
import { Input } from "@/components/common/Input";
import { useToast } from "@/components/common/Toast";
import * as adminApi from "@/lib/api/admin";
import type { PromptVersion, PromptContent } from "@/lib/api/admin";

export default function AdminPromptDetailPage() {
  const params = useParams();
  const router = useRouter();
  const slot = params.slot as string;
  const { showToast } = useToast();

  const [versions, setVersions] = useState<PromptVersion[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [selectedContent, setSelectedContent] = useState<PromptContent | null>(null);
  const [contentLoading, setContentLoading] = useState(false);

  const [showCreate, setShowCreate] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newContent, setNewContent] = useState("");
  const [createLoading, setCreateLoading] = useState(false);

  useEffect(() => {
    loadVersions();
  }, [slot]);

  async function loadVersions() {
    setIsLoading(true);
    try {
      const data = await adminApi.getPromptVersions(slot);
      setVersions(data);
    } catch {
      showToast("버전 목록을 불러오지 못했습니다", "error");
    } finally {
      setIsLoading(false);
    }
  }

  async function handleViewContent(version: string) {
    if (selectedContent?.version === version) {
      setSelectedContent(null);
      return;
    }
    setContentLoading(true);
    try {
      const data = await adminApi.getPromptContent(slot, version);
      setSelectedContent(data);
    } catch {
      showToast("프롬프트 내용을 불러오지 못했습니다", "error");
    } finally {
      setContentLoading(false);
    }
  }

  async function handleActivate(version: string) {
    try {
      await adminApi.activatePromptVersion(slot, version);
      setVersions((prev) => prev.map((v) => ({ ...v, is_active: v.version === version })));
      showToast("버전이 활성화되었습니다", "success");
    } catch (err: any) {
      showToast(err.message || "활성화에 실패했습니다", "error");
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newTitle.trim() || !newContent.trim()) return;
    setCreateLoading(true);
    try {
      const created = await adminApi.createPromptVersion(slot, newTitle.trim(), newContent.trim());
      setVersions((prev) => [...prev, created]);
      showToast("새 버전이 생성되었습니다", "success");
      setShowCreate(false);
      setNewTitle("");
      setNewContent("");
    } catch (err: any) {
      showToast(err.message || "버전 생성에 실패했습니다", "error");
    } finally {
      setCreateLoading(false);
    }
  }

  return (
    <div>
      <div className="flex items-center gap-3 mb-6">
        <button
          onClick={() => router.push("/admin/prompts")}
          className="text-gray-500 hover:text-gray-700 transition-colors"
          aria-label="뒤로 가기"
        >
          <span className="material-icons-outlined text-xl">arrow_back</span>
        </button>
        <h2 className="text-lg font-semibold">{slot}</h2>
      </div>

      <div className="flex justify-between items-center mb-4">
        <p className="text-sm text-gray-500">버전 {versions.length}개</p>
        <Button size="sm" onClick={() => setShowCreate(!showCreate)} data-testid="create-version-btn">
          {showCreate ? "취소" : "새 버전 생성"}
        </Button>
      </div>

      {showCreate && (
        <form onSubmit={handleCreate} className="mb-6 p-4 border rounded-mdesigner bg-gray-50 space-y-3">
          <Input
            label="제목"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            required
            data-testid="new-prompt-title"
          />
          <div className="space-y-1">
            <label className="block text-sm font-medium text-gray-700">프롬프트 내용</label>
            <textarea
              className="w-full px-3 py-2 border border-gray-300 rounded-mdesigner text-sm focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary min-h-[200px] resize-y"
              value={newContent}
              onChange={(e) => setNewContent(e.target.value)}
              required
              data-testid="new-prompt-content"
            />
          </div>
          <Button type="submit" isLoading={createLoading} data-testid="submit-prompt-version">
            생성
          </Button>
        </form>
      )}

      {isLoading ? (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => <div key={i} className="h-14 bg-gray-100 rounded-mdesigner animate-pulse" />)}
        </div>
      ) : versions.length === 0 ? (
        <p className="text-gray-500 text-sm">이 슬롯에 버전이 없습니다.</p>
      ) : (
        <div className="space-y-2">
          {versions.map((v) => (
            <div key={v.version} className="border rounded-mdesigner overflow-hidden">
              <div className="flex justify-between items-center p-3">
                <div className="flex items-center gap-3">
                  <button
                    onClick={() => handleViewContent(v.version)}
                    className="text-sm font-medium text-gray-800 hover:text-primary transition-colors"
                  >
                    v{v.version}: {v.title}
                  </button>
                  {v.is_active && (
                    <span className="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded font-medium">
                      활성
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-gray-400">{new Date(v.created_at).toLocaleDateString("ko-KR")}</span>
                  {!v.is_active && (
                    <Button variant="outline" size="sm" onClick={() => handleActivate(v.version)}>
                      활성화
                    </Button>
                  )}
                </div>
              </div>
              {selectedContent?.version === v.version && (
                <div className="border-t bg-gray-50 p-4">
                  {contentLoading ? (
                    <div className="h-24 bg-gray-100 rounded animate-pulse" />
                  ) : (
                    <pre className="text-xs text-gray-700 whitespace-pre-wrap font-mono overflow-x-auto">
                      {selectedContent.content}
                    </pre>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
