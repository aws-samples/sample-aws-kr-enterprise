"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getActiveTask, getTaskStatus, startGenerate, startModify } from "@/lib/api/ai";
import type { TaskLog, TaskStatus } from "@/lib/api/ai";
import { getProject, getStageSnapshot } from "@/lib/api/projects";

interface UseGenerateTaskOptions {
  projectId: string;
  stage: string;
}

interface GenerateTaskState {
  taskId: string | null;
  status: TaskStatus["status"] | "idle";
  progress: number;
  currentStep: string;
  logs: TaskLog[];
  result: Record<string, unknown> | null;
  error: string | null;
}

const INITIAL_STATE: GenerateTaskState = {
  taskId: null,
  status: "idle",
  progress: 0,
  currentStep: "",
  logs: [],
  result: null,
  error: null,
};

export function useGenerateTask({ projectId, stage }: UseGenerateTaskOptions) {
  const [state, setState] = useState<GenerateTaskState>(INITIAL_STATE);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Tracks whether a task is actively being polled. Used so the mount-time
  // initializer never clobbers an in-flight generate/modify (the source of the
  // earlier "running ↔ completed" flicker race).
  const activeRef = useRef(false);

  const stopPolling = useCallback(() => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  }, []);

  // pollTask / startPolling are intentionally NOT in any effect dependency list;
  // they are stable (empty deps) and read nothing from render scope except refs.
  const pollTask = useCallback(async (taskId: string) => {
    try {
      const task = await getTaskStatus(taskId);
      // A transient "not_found"/"none" must not wipe an active task's state.
      if (task.status !== "running" && task.status !== "pending" &&
          task.status !== "completed" && task.status !== "failed") {
        return;
      }
      setState({
        taskId,
        status: task.status,
        progress: task.progress,
        currentStep: task.current_step,
        logs: task.logs || [],
        result: task.result,
        error: task.error,
      });
      if (task.status === "completed" || task.status === "failed") {
        activeRef.current = false;
        stopPolling();
      }
    } catch {
      // Keep polling on transient network errors; only the explicit terminal
      // states above stop the loop.
    }
  }, [stopPolling]);

  const startPolling = useCallback((taskId: string) => {
    stopPolling();
    activeRef.current = true;
    pollTask(taskId);
    pollingRef.current = setInterval(() => pollTask(taskId), 2000);
  }, [pollTask, stopPolling]);

  // Keep stable refs to the polling helpers so the initializer effect can use
  // them without listing them as dependencies (which would re-run it on every
  // state change and restart/kill the in-flight poll).
  const startPollingRef = useRef(startPolling);
  startPollingRef.current = startPolling;

  // Initialize once per (projectId, stage): restore an active or completed task.
  useEffect(() => {
    let cancelled = false;
    activeRef.current = false;
    setState(INITIAL_STATE);

    getActiveTask(projectId, stage).then((task) => {
      if (cancelled || activeRef.current) return;

      if (task.status === "running" || task.status === "pending") {
        setState({
          taskId: task.task_id,
          status: task.status,
          progress: task.progress,
          currentStep: task.current_step,
          logs: task.logs || [],
          result: null,
          error: null,
        });
        startPollingRef.current(task.task_id);
      } else if (task.status === "completed") {
        if (task.result) {
          setState({
            taskId: task.task_id,
            status: "completed",
            progress: 100,
            currentStep: "완료",
            logs: task.logs || [],
            result: task.result,
            error: null,
          });
        } else {
          getStageSnapshot(projectId, stage).then((snapshot) => {
            if (cancelled || activeRef.current) return;
            setState({
              taskId: task.task_id,
              status: "completed",
              progress: 100,
              currentStep: "완료",
              logs: task.logs || [],
              result: snapshot.design ? { design: snapshot.design } : null,
              error: null,
            });
          }).catch(() => {});
        }
      } else {
        // No task in memory ("none"/"not_found") — fall back to project status.
        getProject(projectId).then((project) => {
          if (cancelled || activeRef.current) return;
          const stageStatus = project.stage_status?.[stage];
          if (stageStatus && stageStatus !== "not_started") {
            getStageSnapshot(projectId, stage).then((snapshot) => {
              if (cancelled || activeRef.current) return;
              setState((s) => ({
                ...s,
                status: "completed",
                progress: 100,
                currentStep: "완료",
                result: snapshot.design ? { design: snapshot.design } : null,
              }));
            }).catch(() => {
              if (cancelled || activeRef.current) return;
              setState((s) => ({ ...s, status: "completed", progress: 100, currentStep: "완료" }));
            });
          }
        }).catch(() => {});
      }
    }).catch(() => {});

    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [projectId, stage, stopPolling]);

  const generate = useCallback(async (command: string, fileIds: string[] = []) => {
    const { task_id } = await startGenerate(projectId, command, stage, fileIds);
    setState({ taskId: task_id, status: "running", progress: 0, currentStep: "시작 중...", logs: [], result: null, error: null });
    startPolling(task_id);
  }, [projectId, stage, startPolling]);

  const modify = useCallback(async (command: string, selectedComponentId?: string) => {
    const { task_id } = await startModify(projectId, command, stage, selectedComponentId);
    setState({ taskId: task_id, status: "running", progress: 0, currentStep: "시작 중...", logs: [], result: null, error: null });
    startPolling(task_id);
  }, [projectId, stage, startPolling]);

  return {
    ...state,
    isRunning: state.status === "running" || state.status === "pending",
    isCompleted: state.status === "completed",
    isFailed: state.status === "failed",
    generate,
    modify,
  };
}
