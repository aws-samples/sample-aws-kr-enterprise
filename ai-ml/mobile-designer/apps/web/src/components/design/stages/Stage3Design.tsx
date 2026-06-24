"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useState, useCallback, useRef } from "react";
import { ChatInterface } from "@/components/design/ChatInterface";
import { AndroidFrame } from "@/components/design/AndroidFrame";
import { TweakPanel } from "@/components/design/TweakPanel";
import { DesignRenderer } from "@/components/design/DesignRenderer";
import { TaskProgress } from "@/components/design/TaskProgress";
import { useGenerateTask } from "@/lib/hooks/useGenerateTask";
import { useDesignTokens } from "@/lib/hooks/useDesignTokens";
import { useSystemSettings } from "@/lib/hooks/useSystemSettings";
import { useComponentSelector } from "@/lib/hooks/useComponentSelector";
import { Button } from "@/components/common/Button";
import { getProject, updateTokens } from "@/lib/api/projects";
import { startChat, getTaskStatus as getAiTaskStatus, getChatHistory, getChatStatus } from "@/lib/api/ai";
import * as handoffApi from "@/lib/api/handoff";
import type { TaskStatusResponse } from "@/lib/api/handoff";
import { useProject } from "@/lib/contexts/ProjectContext";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export default function Stage3Design() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { refresh } = useProject();
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [prevStageReady, setPrevStageReady] = useState<boolean | null>(null);
  const [activeScreenIndex, setActiveScreenIndex] = useState(0);
  const [isChatting, setIsChatting] = useState(false);
  const [hasModifications, setHasModifications] = useState(false);
  // True when the design changed after the downstream handoff was already generated.
  const [downstreamStale, setDownstreamStale] = useState(false);
  const tokens = useDesignTokens();
  const systemSettings = useSystemSettings();
  const componentSelector = useComponentSelector();
  const sessionIdRef = useRef(`design-chat-${id}`);

  const task = useGenerateTask({ projectId: id, stage: "design" });
  const [showTaskProgress, setShowTaskProgress] = useState(false);
  const design = task.result?.design as any;

  // Handoff generation state
  const [handoffTask, setHandoffTask] = useState<TaskStatusResponse | null>(null);
  const [handoffReady, setHandoffReady] = useState(false);
  const handoffPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (task.isRunning) setShowTaskProgress(true);
  }, [task.isRunning]);

  useEffect(() => {
    return () => {
      if (handoffPollRef.current) clearInterval(handoffPollRef.current);
    };
  }, []);

  // Restore active/completed handoff task on mount
  useEffect(() => {
    handoffApi.getActiveHandoffTask(id).then((taskStatus) => {
      if (taskStatus.status === "running" || taskStatus.status === "pending") {
        setHandoffTask(taskStatus);
        handoffPollRef.current = setInterval(async () => {
          try {
            const status = await handoffApi.getTaskStatus(taskStatus.task_id);
            setHandoffTask(status);
            if (status.status === "completed" || status.status === "failed") {
              if (handoffPollRef.current) clearInterval(handoffPollRef.current);
              handoffPollRef.current = null;
              if (status.status === "completed") {
                setHandoffReady(true);
                refresh();
              }
            }
          } catch {
            if (handoffPollRef.current) clearInterval(handoffPollRef.current);
            handoffPollRef.current = null;
          }
        }, 2000);
      } else if (taskStatus.status === "completed") {
        setHandoffReady(true);
      }
    }).catch(() => {});
  }, [id]);

  const handleStartHandoff = useCallback(async () => {
    try {
      const { task_id } = await handoffApi.startProjectGeneration(id);
      setHandoffTask({ task_id, status: "pending", progress: 0, logs: [] });

      handoffPollRef.current = setInterval(async () => {
        try {
          const status = await handoffApi.getTaskStatus(task_id);
          setHandoffTask(status);

          if (status.status === "completed" || status.status === "failed") {
            if (handoffPollRef.current) clearInterval(handoffPollRef.current);
            handoffPollRef.current = null;
            if (status.status === "completed") {
              setHandoffReady(true);
              refresh();
            }
          }
        } catch {
          if (handoffPollRef.current) clearInterval(handoffPollRef.current);
          handoffPollRef.current = null;
        }
      }, 2000);
    } catch {
      // error handled silently
    }
  }, [id, refresh]);

  useEffect(() => {
    if (design?.tokens) {
      tokens.commitTokens(design.tokens);
    }
  }, [design?.tokens]);

  const colorMap = useMemo(() => {
    if (!tokens.isDirty || !design?.tokens?.colors) return undefined;
    const originalColors = design.tokens.colors as Record<string, string>;
    const currentColors = tokens.appliedTokens.colors;
    const map: Record<string, string> = {};
    for (const [key, originalHex] of Object.entries(originalColors)) {
      const currentHex = currentColors[key];
      if (currentHex && currentHex.toLowerCase() !== originalHex.toLowerCase()) {
        map[originalHex.toLowerCase()] = currentHex;
      }
    }
    // primary 변경 시, controlActivated/primaryDark도 연동
    // (FAB/Button이 이 색상들을 쓰는 기존 디자인 호환)
    const primaryChanged = originalColors.primary && map[originalColors.primary.toLowerCase()];
    if (primaryChanged) {
      const newPrimary = map[originalColors.primary.toLowerCase()];
      for (const relatedKey of ["controlActivated", "primaryDark"]) {
        const relatedHex = originalColors[relatedKey];
        if (relatedHex && !map[relatedHex.toLowerCase()]) {
          map[relatedHex.toLowerCase()] = newPrimary;
        }
      }
    }
    return Object.keys(map).length > 0 ? map : undefined;
  }, [tokens.isDirty, tokens.appliedTokens.colors, design?.tokens?.colors]);

  useEffect(() => {
    getProject(id).then((project) => {
      const wireStatus = project.stage_status?.["wireframe"];
      setPrevStageReady(!!wireStatus && wireStatus !== "not_started");
    }).catch(() => setPrevStageReady(false));
  }, [id]);

  // Load chat history for design modifications
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
      const { task_id } = await startChat(id, message, sessionIdRef.current, [], "design");

      const poll = setInterval(async () => {
        try {
          const status = await getAiTaskStatus(task_id);
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
    // If a handoff was already generated, it is now stale after this design change.
    if (handoffReady || handoffTask?.status === "completed") {
      setDownstreamStale(true);
    }
    await task.modify(modificationMessages, componentSelector.selectedId || undefined);
    setHasModifications(false);
  }, [chatMessages, task, componentSelector.selectedId, handoffReady, handoffTask?.status]);

  const handleUpdateDownstream = useCallback(async () => {
    setDownstreamStale(false);
    await handleStartHandoff();
  }, [handleStartHandoff]);

  if (prevStageReady === false) {
    return (
      <div className="max-w-lg mx-auto p-6 text-center space-y-4">
        <p className="text-gray-600">이전 단계(와이어프레임)를 먼저 완료해주세요.</p>
        <Button variant="outline" onClick={() => router.push(`/project/${id}/stage/2`)}>
          와이어프레임으로 이동
        </Button>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 xl:grid-cols-12 gap-4">
      <div className="xl:col-span-4 space-y-4">
        <ChatInterface
          messages={chatMessages}
          onSend={handleSend}
          isStreaming={task.isRunning || isChatting}
          placeholder="디자인 수정사항을 설명해주세요..."
        />

        {hasModifications && !task.isRunning && (
          <div className="flex items-center gap-3 p-4 bg-blue-50 border border-blue-200 rounded-mdesigner">
            <p className="flex-1 text-sm text-blue-800">
              수정사항이 확정되면 디자인에 적용하세요.
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

        {task.isCompleted && design && !handoffTask && !handoffReady && (
          <div className="flex items-center gap-3 p-4 bg-green-50 border border-green-200 rounded-mdesigner">
            <p className="flex-1 text-sm text-green-800">모바일 디자인이 완료되었습니다.</p>
            <Button size="sm" onClick={handleStartHandoff}>
              핸드오프 생성
            </Button>
          </div>
        )}

        {task.isCompleted && downstreamStale && (!handoffTask || handoffTask.status === "completed") && (
          <div className="flex items-center gap-3 p-4 bg-amber-50 border border-amber-200 rounded-mdesigner">
            <p className="flex-1 text-sm text-amber-800">
              디자인이 변경되었습니다. 이미 생성된 핸드오프에 변경사항을 반영하시겠습니까?
            </p>
            <Button size="sm" onClick={handleUpdateDownstream} data-testid="update-downstream-btn">
              다음 단계 업데이트
            </Button>
          </div>
        )}

        {handoffReady && !handoffTask && (
          <div className="flex items-center gap-3 p-4 bg-green-50 border border-green-200 rounded-mdesigner">
            <p className="flex-1 text-sm text-green-800">핸드오프가 준비되었습니다.</p>
            <Button size="sm" variant="outline" onClick={handleStartHandoff}>재생성</Button>
            <Button size="sm" onClick={() => router.push(`/project/${id}/stage/4`)}>다운로드 →</Button>
          </div>
        )}

        {handoffTask && (
          <TaskProgress
            isRunning={handoffTask.status === "running" || handoffTask.status === "pending"}
            isCompleted={handoffTask.status === "completed"}
            isFailed={handoffTask.status === "failed"}
            progress={handoffTask.progress}
            currentStep={handoffTask.current_step || ""}
            logs={handoffTask.logs || []}
            error={handoffTask.error || null}
          />
        )}

        {handoffTask?.status === "completed" && (
          <div className="flex items-center gap-3 p-4 bg-green-50 border border-green-200 rounded-mdesigner">
            <p className="flex-1 text-sm text-green-800">핸드오프가 준비되었습니다.</p>
            <Button size="sm" onClick={() => router.push(`/project/${id}/stage/4`)}>
              다운로드 →
            </Button>
          </div>
        )}
      </div>

      <div className="xl:col-span-4 space-y-3">
        {(design?.screens?.length || 0) > 1 && (
          <div className="flex gap-1 flex-wrap justify-center">
            {design.screens.map((screen: any, i: number) => (
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
          <AndroidFrame rotation={systemSettings.settings.rotation} darkMode={systemSettings.settings.darkMode}>
            <DesignRenderer
              components={design?.screens?.[activeScreenIndex]?.components || []}
              tokens={tokens.appliedTokens as any}
              colorMap={colorMap}
              selectedId={componentSelector.selectedId}
              hoveredId={componentSelector.hoveredId}
              onSelect={componentSelector.select}
              onHover={componentSelector.hover}
              onNavigate={(screenName) => {
                const idx = (design?.screens || []).findIndex((s: any) => s.name === screenName);
                if (idx >= 0) setActiveScreenIndex(idx);
              }}
              darkMode={systemSettings.settings.darkMode}
            />
          </AndroidFrame>
        </div>
      </div>

      <div className="xl:col-span-4">
        <TweakPanel tokens={tokens} systemSettings={systemSettings} onSaveTokens={async () => {
          await updateTokens(id, "design", tokens.appliedTokens as unknown as Record<string, unknown>);
          tokens.commitTokens(tokens.appliedTokens);
        }} />
      </div>
    </div>
  );
}
