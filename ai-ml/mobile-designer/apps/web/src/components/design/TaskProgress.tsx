"use client";

import { ProgressBar } from "@/components/common/ProgressBar";
import type { TaskLog } from "@/lib/api/ai";

interface TaskProgressProps {
  isRunning: boolean;
  isCompleted: boolean;
  isFailed: boolean;
  progress: number;
  currentStep: string;
  logs: TaskLog[];
  error: string | null;
}

export function TaskProgress({ isRunning, isCompleted, isFailed, progress, currentStep, logs, error }: TaskProgressProps) {
  if (!isRunning && !isCompleted && !isFailed) return null;

  return (
    <div className="space-y-3 p-4 border rounded-mdesigner bg-gray-50">
      {isRunning && (
        <>
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-primary animate-pulse" />
            <span className="text-sm font-medium">{currentStep}</span>
          </div>
          <ProgressBar percent={progress} />
        </>
      )}

      {isCompleted && (
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-green-500" />
          <span className="text-sm font-medium text-green-700">완료</span>
        </div>
      )}

      {isFailed && (
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-red-500" />
            <span className="text-sm font-medium text-red-700">오류 발생</span>
          </div>
          {error && <p className="text-xs text-red-600">{error}</p>}
        </div>
      )}

      {logs.length > 0 && (
        <div className="max-h-32 overflow-y-auto space-y-1 mt-2">
          {logs.map((log, i) => (
            <div key={i} className="flex items-start gap-2 text-xs text-gray-500">
              <span className="text-gray-400 shrink-0">
                {new Date(log.timestamp).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
              </span>
              <span>{log.detail}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
