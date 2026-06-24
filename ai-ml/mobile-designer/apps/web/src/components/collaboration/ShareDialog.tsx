"use client";

import { useState } from "react";
import { Button } from "@/components/common/Button";
import { Modal } from "@/components/common/Modal";
import { createShareLink } from "@/lib/api/collaboration";
import { useToast } from "@/components/common/Toast";

interface ShareDialogProps {
  isOpen: boolean;
  onClose: () => void;
  projectId: string;
  teamId: string;
}

export function ShareDialog({ isOpen, onClose, projectId, teamId }: ShareDialogProps) {
  const [permission, setPermission] = useState<"read_only" | "edit">("read_only");
  const [shareUrl, setShareUrl] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const { showToast } = useToast();

  const handleCreate = async () => {
    setIsLoading(true);
    try {
      const link = await createShareLink(projectId, teamId, permission);
      const url = `${window.location.origin}/shared/${link.share_token}`;
      setShareUrl(url);
    } catch {
      showToast("공유 링크 생성에 실패했습니다", "error");
    } finally {
      setIsLoading(false);
    }
  };

  const handleCopy = () => {
    if (shareUrl) {
      navigator.clipboard.writeText(shareUrl);
      showToast("링크가 복사되었습니다", "success");
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="공유 링크 생성">
      <div className="space-y-4">
        <div>
          <label className="text-sm font-medium">권한</label>
          <select value={permission} onChange={(e) => setPermission(e.target.value as "read_only" | "edit")} className="w-full mt-1 border rounded-mdesigner px-3 py-2 text-sm">
            <option value="read_only">읽기 전용</option>
            <option value="edit">편집 가능</option>
          </select>
        </div>

        {shareUrl ? (
          <div className="space-y-2">
            <input value={shareUrl} readOnly className="w-full text-xs border rounded px-2 py-1.5 bg-gray-50" />
            <Button onClick={handleCopy} className="w-full" size="sm">링크 복사</Button>
          </div>
        ) : (
          <Button onClick={handleCreate} isLoading={isLoading} className="w-full" data-testid="share-create-btn">링크 생성</Button>
        )}
      </div>
    </Modal>
  );
}
