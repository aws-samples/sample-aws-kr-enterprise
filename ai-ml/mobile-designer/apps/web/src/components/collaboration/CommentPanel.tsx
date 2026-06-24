"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/common/Button";
import * as collabApi from "@/lib/api/collaboration";
import type { Comment } from "@/lib/api/collaboration";

interface CommentPanelProps {
  projectId: string;
  screenId: string;
  stageId: string;
  selectedComponentId?: string | null;
}

export function CommentPanel({ projectId, screenId, stageId, selectedComponentId }: CommentPanelProps) {
  const [comments, setComments] = useState<Comment[]>([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    collabApi.listComments(projectId, screenId).then(setComments);
  }, [projectId, screenId]);

  const filtered = selectedComponentId
    ? comments.filter((c) => c.component_id === selectedComponentId || c.component_id === null)
    : comments;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim()) return;
    setIsLoading(true);
    try {
      const comment = await collabApi.createComment(projectId, screenId, stageId, input.trim(), selectedComponentId || undefined);
      setComments((prev) => [...prev, comment]);
      setInput("");
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="border rounded-mdesigner bg-white p-4 space-y-3" data-testid="comment-panel">
      <h3 className="text-sm font-medium">코멘트 {selectedComponentId && <span className="text-xs text-primary">(컴포넌트)</span>}</h3>
      <div className="max-h-48 overflow-y-auto space-y-2">
        {filtered.map((c) => (
          <div key={c.comment_id} className="text-xs p-2 bg-gray-50 rounded">
            <p>{c.content}</p>
            <span className="text-gray-400">{new Date(c.created_at).toLocaleTimeString("ko-KR")}</span>
          </div>
        ))}
      </div>
      <form onSubmit={handleSubmit} className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="코멘트 입력..."
          className="flex-1 text-xs border rounded px-2 py-1.5"
          data-testid="comment-input"
        />
        <Button size="sm" type="submit" isLoading={isLoading} data-testid="comment-submit">등록</Button>
      </form>
    </div>
  );
}
