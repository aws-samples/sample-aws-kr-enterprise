"use client";

import Link from "next/link";
import type { Project } from "@/lib/api/projects";

const STAGE_LABELS: Record<string, string> = {
  requirements: "요구사항",
  wireframe: "와이어프레임",
  design: "디자인",
  handoff: "핸드오프",
};

interface ProjectCardProps {
  project: Project;
  onDelete: () => void;
}

export function ProjectCard({ project, onDelete }: ProjectCardProps) {
  const stageLabel = STAGE_LABELS[project.current_stage] || project.current_stage;
  const date = new Date(project.created_at).toLocaleDateString("ko-KR");

  return (
    <Link href={`/project/${project.project_id}`} className="block" data-testid={`project-card-${project.project_id}`}>
      <div className="p-4 bg-white rounded-mdesigner border border-gray-200 hover:border-primary/50 hover:shadow-sm transition-all">
        <h3 className="font-semibold text-base mb-1 truncate">{project.name}</h3>
        <p className="text-xs text-gray-500 mb-3">{date}</p>
        <div className="flex items-center justify-between">
          <span className="text-xs px-2 py-0.5 bg-primary/10 text-primary rounded-full">{stageLabel}</span>
          <button
            onClick={(e) => { e.preventDefault(); e.stopPropagation(); onDelete(); }}
            className="text-xs text-gray-400 hover:text-error"
            data-testid={`delete-project-${project.project_id}`}
          >
            삭제
          </button>
        </div>
      </div>
    </Link>
  );
}
