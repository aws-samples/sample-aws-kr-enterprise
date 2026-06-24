"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState, useCallback, useRef } from "react";
import { ChatInterface } from "@/components/design/ChatInterface";
import { AndroidFrame } from "@/components/design/AndroidFrame";
import { WireframeRenderer } from "@/components/design/WireframeRenderer";
import { TaskProgress } from "@/components/design/TaskProgress";
import { useGenerateTask } from "@/lib/hooks/useGenerateTask";
import { useComponentSelector } from "@/lib/hooks/useComponentSelector";
import { Button } from "@/components/common/Button";
import { getProject } from "@/lib/api/projects";
import { useProject } from "@/lib/contexts/ProjectContext";
import { startChat, getTaskStatus, getChatHistory, getChatStatus } from "@/lib/api/ai";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export default function Stage2Wireframe() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { refresh } = useProject();
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [prevStageReady, setPrevStageReady] = useState<boolean | null>(null);
  const [activeScreenIndex, setActiveScreenIndex] = useState(0);
  const [isChatting, setIsChatting] = useState(false);
  const [hasModifications, setHasModifications] = useState(false);
  // True when the wireframe was changed after the downstream design stage had
  // already been generated, so the design is now out of date.
  const [downstreamStale, setDownstreamStale] = useState(false);
  const componentSelector = useComponentSelector();
  const sessionIdRef = useRef(`wireframe-chat-${id}`);

  const task = useGenerateTask({ projectId: id, stage: "wireframe" });
  const nextTask = useGenerateTask({ projectId: id, stage: "design" });
  const [showTaskProgress, setShowTaskProgress] = useState(false);
  const design = task.result?.design as any;
  const screens = design?.screens || [];
  const activeScreen = screens[activeScreenIndex];

  useEffect(() => {
    if (nextTask.isCompleted) {
      refresh();
    }
  }, [nextTask.isCompleted, refresh]);

  useEffect(() => {
    getProject(id).then((project) => {
      const reqStatus = project.stage_status?.["requirements"];
      setPrevStageReady(!!reqStatus && reqStatus !== "not_started");
    }).catch(() => setPrevStageReady(false));
  }, [id]);

  // Load chat history for wireframe modifications
  useEffect(() => {
    getChatHistory(id, sessionIdRef.current).then((history) => {
      if (history.messages.length > 0) {
        setChatMessages(history.messages);
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
              }).catch(() => {});
            }
          }).catch(() => clearInterval(poll));
        }, 2000);
      }
    }).catch(() => {});
  }, [id]);

  const handleSend = useCallback(async (message: string) => {
    if (task.isRunning || isChatting) return;

    setChatMessages((prev) => [...prev, { role: "user", content: message }]);
    setIsChatting(true);
    setHasModifications(true);

    try {
      const { task_id } = await startChat(id, message, sessionIdRef.current, [], "wireframe");

      const poll = setInterval(async () => {
        try {
          const status = await getTaskStatus(task_id);
          if (status.status === "completed" && status.result) {
            clearInterval(poll);
            const reply = (status.result as any).reply || "";
            setChatMessages((prev) => [...prev, { role: "assistant", content: reply }]);
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
  }, [id, task.isRunning, isChatting]);

  const handleApplyModifications = useCallback(async () => {
    const modificationMessages = chatMessages
      .filter((m) => m.role === "user")
      .map((m) => m.content)
      .join("\n\n");

    setShowTaskProgress(true);
    // If the downstream design stage was already generated, it is now stale and
    // should be offered for re-generation ("다음 단계 업데이트").
    if (nextTask.isCompleted) {
      setDownstreamStale(true);
    }
    await task.modify(modificationMessages, componentSelector.selectedId || undefined);
    setHasModifications(false);
  }, [chatMessages, task, componentSelector.selectedId, nextTask.isCompleted]);

  const handleUpdateDownstream = useCallback(async () => {
    setDownstreamStale(false);
    await nextTask.generate("수정된 와이어프레임을 기반으로 모바일 디자인을 다시 적용하세요.");
  }, [nextTask]);

  if (prevStageReady === false) {
    return (
      <div className="max-w-lg mx-auto p-6 text-center space-y-4">
        <p className="text-gray-600">이전 단계(요구사항 분석)를 먼저 완료해주세요.</p>
        <Button variant="outline" onClick={() => router.push(`/project/${id}/stage/1`)}>
          요구사항 분석으로 이동
        </Button>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <div className="space-y-4">
        <ChatInterface
          messages={chatMessages}
          onSend={handleSend}
          isStreaming={task.isRunning || isChatting}
          placeholder="와이어프레임 수정사항을 설명해주세요..."
        />

        {hasModifications && !task.isRunning && (
          <div className="flex items-center gap-3 p-4 bg-blue-50 border border-blue-200 rounded-mdesigner">
            <p className="flex-1 text-sm text-blue-800">
              수정사항이 확정되면 아래 버튼으로 와이어프레임에 적용하세요.
            </p>
            <Button size="sm" onClick={handleApplyModifications}>수정 적용</Button>
          </div>
        )}

        {showTaskProgress && (
          <TaskProgress
            isRunning={task.isRunning}
            isCompleted={task.isCompleted}
            isFailed={task.isFailed}
            progress={task.progress}
            currentStep={task.currentStep}
            logs={task.logs}
            error={task.error}
          />
        )}

        {task.isCompleted && !nextTask.isRunning && !nextTask.isCompleted && (
          <div className="flex items-center gap-3 p-4 bg-green-50 border border-green-200 rounded-mdesigner">
            <p className="flex-1 text-sm text-green-800">와이어프레임이 완료되었습니다.</p>
            <Button size="sm" onClick={() => {
              nextTask.generate("와이어프레임을 기반으로 모바일 디자인을 적용하세요.");
            }}>
              모바일 디자인 생성
            </Button>
          </div>
        )}

        {task.isCompleted && downstreamStale && !nextTask.isRunning && (
          <div className="flex items-center gap-3 p-4 bg-amber-50 border border-amber-200 rounded-mdesigner">
            <p className="flex-1 text-sm text-amber-800">
              와이어프레임이 변경되었습니다. 이미 생성된 모바일 디자인에 변경사항을 반영하시겠습니까?
            </p>
            <Button size="sm" onClick={handleUpdateDownstream} data-testid="update-downstream-btn">
              다음 단계 업데이트
            </Button>
          </div>
        )}

        {(nextTask.isRunning || nextTask.isCompleted || nextTask.isFailed) && (
          <TaskProgress
            isRunning={nextTask.isRunning}
            isCompleted={nextTask.isCompleted}
            isFailed={nextTask.isFailed}
            progress={nextTask.progress}
            currentStep={nextTask.currentStep}
            logs={nextTask.logs}
            error={nextTask.error}
          />
        )}
        {nextTask.isCompleted && (
          <div className="flex items-center gap-3 p-4 bg-green-50 border border-green-200 rounded-mdesigner">
            <p className="flex-1 text-sm text-green-800">모바일 디자인이 준비되었습니다.</p>
            <Button size="sm" onClick={() => router.push(`/project/${id}/stage/3`)}>
              다음 단계 →
            </Button>
          </div>
        )}
      </div>

      <div className="space-y-3">
        {screens.length > 1 && (
          <div className="flex gap-1 flex-wrap justify-center">
            {screens.map((screen: any, i: number) => (
              <button
                key={i}
                onClick={() => setActiveScreenIndex(i)}
                className={`px-3 py-1 text-xs rounded-full border transition-colors ${
                  i === activeScreenIndex
                    ? "bg-primary text-on-primary border-primary"
                    : "bg-white text-gray-600 border-gray-300 hover:border-gray-400"
                }`}
              >
                {screen.name || `화면 ${i + 1}`}
              </button>
            ))}
          </div>
        )}
        <div className="flex justify-center">
          <AndroidFrame rotation="portrait">
            <WireframeRenderer
              components={activeScreen?.components || []}
              selectedId={componentSelector.selectedId}
              hoveredId={componentSelector.hoveredId}
              onSelect={componentSelector.select}
              onHover={componentSelector.hover}
              onNavigate={(screenName) => {
                const idx = screens.findIndex((s: any) => s.name === screenName);
                if (idx >= 0) setActiveScreenIndex(idx);
              }}
            />
          </AndroidFrame>
        </div>
      </div>
    </div>
  );
}
