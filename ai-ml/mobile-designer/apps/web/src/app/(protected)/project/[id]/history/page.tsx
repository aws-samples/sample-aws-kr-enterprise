"use client";

import { useParams } from "next/navigation";
import { useEffect } from "react";
import { useVersionHistory } from "@/lib/hooks/useVersionHistory";
import { Button } from "@/components/common/Button";
import { useToast } from "@/components/common/Toast";

const ACTION_LABELS: Record<string, string> = {
  initial: "생성",
  modify: "수정",
  revert: "복원",
  propagate: "전파",
  tweak: "트윅",
};

export default function HistoryPage() {
  const { id } = useParams<{ id: string }>();
  const { versions, isLoading, loadVersions, revert } = useVersionHistory(id);
  const { showToast } = useToast();

  useEffect(() => { loadVersions(); }, [loadVersions]);

  const handleRevert = async (versionId: string) => {
    try {
      await revert(versionId);
      showToast("버전이 복원되었습니다", "success");
    } catch {
      showToast("복원에 실패했습니다", "error");
    }
  };

  return (
    <div className="max-w-2xl mx-auto">
      <h2 className="text-xl font-bold mb-6">버전 히스토리</h2>
      {isLoading ? (
        <p className="text-gray-500">로딩 중...</p>
      ) : versions.length === 0 ? (
        <p className="text-gray-500">아직 버전이 없습니다</p>
      ) : (
        <div className="space-y-3">
          {versions.map((v, i) => (
            <div key={v.version_id} className="flex items-center justify-between p-4 border rounded-mdesigner bg-white">
              <div>
                <p className="text-sm font-medium">{v.command}</p>
                <p className="text-xs text-gray-500 mt-0.5">
                  {ACTION_LABELS[v.action] || v.action} · {v.stage_id} · {new Date(v.created_at).toLocaleString("ko-KR")}
                </p>
              </div>
              {i > 0 && (
                <Button size="sm" variant="ghost" onClick={() => handleRevert(v.version_id)} data-testid={`revert-${v.version_id}`}>
                  복원
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
