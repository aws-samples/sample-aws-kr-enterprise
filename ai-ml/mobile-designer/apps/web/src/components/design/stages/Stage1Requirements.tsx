"use client";

import { useParams, useRouter } from "next/navigation";
import { useState, useCallback, useRef, useEffect } from "react";
import { ChatInterface } from "@/components/design/ChatInterface";
import { FileUpload } from "@/components/design/FileUpload";
import { TaskProgress } from "@/components/design/TaskProgress";
import { RequirementsView } from "@/components/design/RequirementsView";
import { useFileUpload } from "@/lib/hooks/useFileUpload";
import { useGenerateTask } from "@/lib/hooks/useGenerateTask";
import { Button } from "@/components/common/Button";
import { startChat, getTaskStatus, startGenerate, getChatHistory, getChatStatus } from "@/lib/api/ai";
import { getStageSnapshot } from "@/lib/api/projects";
import { useProject } from "@/lib/contexts/ProjectContext";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export default function Stage1Requirements() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { refresh } = useProject();
  const fileUpload = useFileUpload(id);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [readyToAnalyze, setReadyToAnalyze] = useState(false);
  const [isChatting, setIsChatting] = useState(false);
  const sessionIdRef = useRef(`chat-${id}`);

  // Phase: "chat" → "requirements_doc" → "wireframe_generating"
  const [phase, setPhase] = useState<"chat" | "requirements_doc" | "wireframe_generating">("chat");
  const [requirementsDoc, setRequirementsDoc] = useState<Record<string, any> | null>(null);
  const [editingDoc, setEditingDoc] = useState<string>("");
  // "view" = readable structured view, "edit" = raw JSON editor.
  const [docViewMode, setDocViewMode] = useState<"view" | "edit">("view");
  const [docParseError, setDocParseError] = useState<string | null>(null);

  const task = useGenerateTask({ projectId: id, stage: "requirements" });
  const wireframeTask = useGenerateTask({ projectId: id, stage: "wireframe" });

  useEffect(() => {
    if (wireframeTask.isCompleted) {
      refresh();
    }
  }, [wireframeTask.isCompleted, refresh]);

  // Load existing requirements doc if available
  useEffect(() => {
    getStageSnapshot(id, "requirements").then((snap) => {
      if (snap.design && (snap.design as any).app_name) {
        setRequirementsDoc(snap.design as Record<string, any>);
        setEditingDoc(JSON.stringify(snap.design, null, 2));
        setPhase("requirements_doc");
      }
    }).catch(() => {});
  }, [id]);

  // Load chat history
  useEffect(() => {
    getChatHistory(id, sessionIdRef.current).then((history) => {
      if (history.messages.length > 0) {
        setChatMessages(history.messages);
      }
      if (history.ready_to_proceed) {
        setReadyToAnalyze(true);
      }
    }).catch(() => {});

    getChatStatus(id, sessionIdRef.current).then((status) => {
      if (status.is_responding) {
        setIsChatting(true);
        const poll = setInterval(() => {
          getChatStatus(id, sessionIdRef.current).then((s) => {
            if (!s.is_responding) {
              clearInterval(poll);
              setIsChatting(false);
              getChatHistory(id, sessionIdRef.current).then((h) => {
                setChatMessages(h.messages);
                if (h.ready_to_proceed) setReadyToAnalyze(true);
              }).catch(() => {});
            }
          }).catch(() => clearInterval(poll));
        }, 2000);
      }
    }).catch(() => {});
  }, [id]);

  // When requirements task completes, show the doc
  useEffect(() => {
    if (task.isCompleted && task.result) {
      const doc = (task.result as any).design;
      if (doc && doc.app_name) {
        setRequirementsDoc(doc);
        setEditingDoc(JSON.stringify(doc, null, 2));
        setPhase("requirements_doc");
      }
    }
  }, [task.isCompleted, task.result]);

  const handleSend = useCallback(async (message: string) => {
    if (task.isRunning || isChatting) return;

    const userMsg: ChatMessage = { role: "user", content: message };
    setChatMessages((prev) => [...prev, userMsg]);
    setIsChatting(true);

    try {
      const { task_id } = await startChat(id, message, sessionIdRef.current, fileUpload.fileIds || [], "requirements");

      const poll = setInterval(async () => {
        try {
          const status = await getTaskStatus(task_id);
          if (status.status === "completed" && status.result) {
            clearInterval(poll);
            const reply = (status.result as any).reply || "";
            const ready = (status.result as any).ready_to_proceed || false;
            setChatMessages((prev) => [...prev, { role: "assistant", content: reply }]);
            if (ready) setReadyToAnalyze(true);
            setIsChatting(false);
          } else if (status.status === "failed") {
            clearInterval(poll);
            setChatMessages((prev) => [...prev, { role: "assistant", content: `오류: ${status.error || "응답 실패"}` }]);
            setIsChatting(false);
          }
        } catch {
          clearInterval(poll);
          setChatMessages((prev) => [...prev, { role: "assistant", content: "오류: 상태 확인 실패" }]);
          setIsChatting(false);
        }
      }, 2000);
    } catch (e: any) {
      setChatMessages((prev) => [...prev, { role: "assistant", content: `오류: ${e.message || "요청 실패"}` }]);
      setIsChatting(false);
    }
  }, [id, task.isRunning, isChatting, fileUpload.fileIds]);

  // Step 1: Synthesize requirements document
  const handleSynthesizeRequirements = useCallback(async () => {
    const allMessages = chatMessages
      .map((m) => `${m.role === "user" ? "User" : "AI"}: ${m.content}`)
      .join("\n\n");

    await task.generate(allMessages, fileUpload.fileIds);
  }, [chatMessages, fileUpload.fileIds, task]);

  // Step 2: Generate wireframe from requirements
  const handleGenerateWireframe = useCallback(async () => {
    setPhase("wireframe_generating");
    try {
      // Parse edited doc if user modified it
      let finalDoc = requirementsDoc;
      try {
        finalDoc = JSON.parse(editingDoc);
      } catch {
        // If JSON parse fails, use original
      }

      await wireframeTask.generate(
        JSON.stringify(finalDoc, null, 2),
        []
      );
    } catch {
      setPhase("requirements_doc");
    }
  }, [requirementsDoc, editingDoc, wireframeTask]);

  // Chat phase
  if (phase === "chat") {
    return (
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-4">
          <ChatInterface
            messages={chatMessages}
            onSend={handleSend}
            isStreaming={task.isRunning || isChatting}
            placeholder="앱에 대해 설명해주세요... (예: 할일 관리 앱을 만들려고 해)"
          />

          <TaskProgress
            isRunning={task.isRunning}
            isCompleted={task.isCompleted}
            isFailed={task.isFailed}
            progress={task.progress}
            currentStep={task.currentStep}
            logs={task.logs}
            error={task.error}
          />

          {readyToAnalyze && !task.isRunning && !task.isCompleted && (
            <div className="flex items-center gap-3 p-4 bg-blue-50 border border-blue-200 rounded-mdesigner">
              <p className="flex-1 text-sm text-blue-800">
                충분한 정보가 수집되었습니다. 요구사항을 정리하시거나, 대화를 계속할 수 있습니다.
              </p>
              <Button size="sm" onClick={handleSynthesizeRequirements}>요구사항 정리</Button>
            </div>
          )}
        </div>
        <div className="space-y-4">
          <FileUpload projectId={id} fileUpload={fileUpload} />
        </div>
      </div>
    );
  }

  // Requirements document review phase
  if (phase === "requirements_doc") {
    return (
      <div className="max-w-3xl mx-auto space-y-6">
        <div>
          <h2 className="text-lg font-semibold">요구사항 문서</h2>
          <p className="text-sm text-gray-500 mt-1">AI가 정리한 요구사항을 확인하고 수정하세요. 확정 후 와이어프레임이 생성됩니다.</p>
        </div>

        <div className="border rounded-mdesigner overflow-hidden">
          <div className="bg-gray-50 px-4 py-2 border-b flex items-center justify-between gap-2">
            <div className="flex items-center gap-1 rounded-md bg-gray-200/60 p-0.5">
              <button
                onClick={() => {
                  // Re-parse edits before switching to the readable view.
                  try {
                    const parsed = JSON.parse(editingDoc);
                    setRequirementsDoc(parsed);
                    setDocParseError(null);
                    setDocViewMode("view");
                  } catch {
                    setDocParseError("JSON 형식이 올바르지 않습니다. 편집 내용을 확인하세요.");
                  }
                }}
                className={`px-3 py-1 text-xs rounded transition-colors ${
                  docViewMode === "view" ? "bg-white text-gray-900 shadow-sm" : "text-gray-500 hover:text-gray-700"
                }`}
                data-testid="doc-view-mode-btn"
              >
                보기
              </button>
              <button
                onClick={() => setDocViewMode("edit")}
                className={`px-3 py-1 text-xs rounded transition-colors ${
                  docViewMode === "edit" ? "bg-white text-gray-900 shadow-sm" : "text-gray-500 hover:text-gray-700"
                }`}
                data-testid="doc-edit-mode-btn"
              >
                JSON 편집
              </button>
            </div>
            <Button size="sm" variant="outline" onClick={() => setPhase("chat")}>
              채팅으로 돌아가기
            </Button>
          </div>

          {docViewMode === "view" ? (
            <div className="p-4 max-h-[500px] overflow-y-auto">
              {requirementsDoc ? (
                <RequirementsView doc={requirementsDoc} />
              ) : (
                <p className="text-sm text-gray-500">요구사항 데이터가 없습니다.</p>
              )}
            </div>
          ) : (
            <div>
              <textarea
                value={editingDoc}
                onChange={(e) => {
                  setEditingDoc(e.target.value);
                  if (docParseError) setDocParseError(null);
                }}
                className="w-full h-[500px] p-4 font-mono text-sm resize-none focus:outline-none"
                spellCheck={false}
              />
              {docParseError && (
                <p className="px-4 py-2 text-xs text-red-600 border-t bg-red-50">{docParseError}</p>
              )}
            </div>
          )}
        </div>

        <div className="flex items-center gap-3">
          <Button onClick={handleGenerateWireframe} data-testid="generate-wireframe-btn">
            {wireframeTask.isCompleted ? "와이어프레임 업데이트" : "와이어프레임 생성"}
          </Button>
          <p className="text-xs text-gray-500">
            {wireframeTask.isCompleted
              ? "수정된 요구사항으로 와이어프레임을 다시 생성합니다 (기존 와이어프레임은 대체됩니다)"
              : "요구사항을 기반으로 화면 구조를 생성합니다"}
          </p>
        </div>

        {wireframeTask.isRunning && (
          <TaskProgress
            isRunning={wireframeTask.isRunning}
            isCompleted={wireframeTask.isCompleted}
            isFailed={wireframeTask.isFailed}
            progress={wireframeTask.progress}
            currentStep={wireframeTask.currentStep}
            logs={wireframeTask.logs}
            error={wireframeTask.error}
          />
        )}

        {wireframeTask.isCompleted && (
          <div className="flex items-center gap-3 p-4 bg-green-50 border border-green-200 rounded-mdesigner">
            <p className="flex-1 text-sm text-green-800">와이어프레임이 생성되었습니다.</p>
            <Button size="sm" onClick={() => router.push(`/project/${id}/stage/2`)}>
              다음 단계 →
            </Button>
          </div>
        )}
      </div>
    );
  }

  // Wireframe generating phase (fallback)
  return (
    <div className="max-w-xl mx-auto space-y-4">
      <TaskProgress
        isRunning={wireframeTask.isRunning}
        isCompleted={wireframeTask.isCompleted}
        isFailed={wireframeTask.isFailed}
        progress={wireframeTask.progress}
        currentStep={wireframeTask.currentStep}
        logs={wireframeTask.logs}
        error={wireframeTask.error}
      />

      {wireframeTask.isCompleted && (
        <div className="flex items-center gap-3 p-4 bg-green-50 border border-green-200 rounded-mdesigner">
          <p className="flex-1 text-sm text-green-800">와이어프레임이 생성되었습니다.</p>
          <Button size="sm" onClick={() => router.push(`/project/${id}/stage/2`)}>
            다음 단계 →
          </Button>
        </div>
      )}

      {wireframeTask.isFailed && (
        <div className="flex items-center gap-3 p-4 bg-red-50 border border-red-200 rounded-mdesigner">
          <p className="flex-1 text-sm text-red-800">생성에 실패했습니다.</p>
          <Button size="sm" variant="outline" onClick={() => setPhase("requirements_doc")}>
            돌아가기
          </Button>
        </div>
      )}
    </div>
  );
}
