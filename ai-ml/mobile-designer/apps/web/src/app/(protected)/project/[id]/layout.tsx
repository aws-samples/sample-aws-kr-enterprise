"use client";

import Link from "next/link";
import { useParams, usePathname } from "next/navigation";
import { useEffect } from "react";
import { ProjectProvider, useProject } from "@/lib/contexts/ProjectContext";
import { clsx } from "clsx";

const STAGES = [
  { id: "1", label: "요구사항", key: "requirements" },
  { id: "2", label: "와이어프레임", key: "wireframe" },
  { id: "3", label: "디자인", key: "design" },
  { id: "4", label: "핸드오프", key: "handoff" },
];

function isStageAccessible(stageKey: string, stageStatus: Record<string, string> | undefined): boolean {
  if (!stageStatus) return stageKey === "requirements";
  if (stageKey === "requirements") return true;
  // Handoff tab is accessible only after handoff generation is complete
  if (stageKey === "handoff") {
    return stageStatus["handoff"] === "completed";
  }
  // Other stages accessible when their own status is not "not_started"
  const ownStatus = stageStatus[stageKey];
  return !!ownStatus && ownStatus !== "not_started";
}

function ProjectLayoutInner({ children }: { children: React.ReactNode }) {
  const { id } = useParams<{ id: string }>();
  const { project, loadProject } = useProject();
  const pathname = usePathname();

  useEffect(() => { loadProject(id); }, [id, loadProject]);

  return (
    <div className="min-h-screen">
      <header className="border-b bg-white sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-6 py-3 flex items-center justify-between">
          <Link href="/dashboard" className="text-sm text-gray-500 hover:text-primary">← 프로젝트 목록</Link>
          <h1 className="font-semibold truncate max-w-xs">{project?.name || "..."}</h1>
          <Link href={`/project/${id}/history`} className="text-sm text-gray-500 hover:text-primary">히스토리</Link>
        </div>
        <nav className="max-w-6xl mx-auto px-6 flex gap-1">
          {STAGES.map((stage) => {
            const accessible = isStageAccessible(stage.key, project?.stage_status);
            const isActive = pathname?.includes(`/stage/${stage.id}`);

            if (!accessible) {
              return (
                <span
                  key={stage.id}
                  className="px-4 py-2 text-sm border-b-2 border-transparent text-gray-300 cursor-not-allowed"
                >
                  {stage.label}
                </span>
              );
            }

            return (
              <Link
                key={stage.id}
                href={`/project/${id}/stage/${stage.id}`}
                className={clsx(
                  "px-4 py-2 text-sm border-b-2 transition-colors",
                  isActive ? "border-primary text-primary font-medium" : "border-transparent text-gray-500 hover:text-gray-700",
                )}
              >
                {stage.label}
              </Link>
            );
          })}
        </nav>
      </header>
      <main className="max-w-6xl mx-auto px-6 py-6">{children}</main>
    </div>
  );
}

export default function ProjectLayout({ children }: { children: React.ReactNode }) {
  const { id } = useParams<{ id: string }>();
  return (
    <ProjectProvider projectId={id}>
      <ProjectLayoutInner>{children}</ProjectLayoutInner>
    </ProjectProvider>
  );
}
