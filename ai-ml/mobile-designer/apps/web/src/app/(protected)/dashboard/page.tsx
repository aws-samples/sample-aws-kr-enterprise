"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/common/Button";
import { useAuth } from "@/lib/contexts/AuthContext";
import { useToast } from "@/components/common/Toast";
import { ProjectCard } from "@/components/project/ProjectCard";
import { CreateProjectModal } from "@/components/project/CreateProjectModal";
import * as projectsApi from "@/lib/api/projects";
import type { Project } from "@/lib/api/projects";

export default function DashboardPage() {
  const { user } = useAuth();
  const router = useRouter();
  const { showToast } = useToast();
  const [projects, setProjects] = useState<Project[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);

  useEffect(() => {
    loadProjects();
  }, []);

  async function loadProjects() {
    setIsLoading(true);
    try {
      const result = await projectsApi.listProjects();
      setProjects(result.items);
    } catch {
      showToast("프로젝트 목록을 불러오지 못했습니다", "error");
    } finally {
      setIsLoading(false);
    }
  }

  async function handleDelete(projectId: string) {
    const confirmed = window.confirm("프로젝트를 삭제하시겠습니까? 모든 디자인 데이터가 영구적으로 삭제됩니다.");
    if (!confirmed) return;

    try {
      await projectsApi.deleteProject(projectId);
      setProjects((prev) => prev.filter((p) => p.project_id !== projectId));
      showToast("프로젝트가 삭제되었습니다", "success");
    } catch {
      showToast("삭제에 실패했습니다", "error");
    }
  }

  return (
    <div className="max-w-5xl mx-auto px-6 py-8">
      <div className="flex justify-between items-center mb-8">
        <h1 className="text-2xl font-bold">프로젝트</h1>
        <Button onClick={() => setShowCreate(true)} data-testid="create-project-btn">새 프로젝트</Button>
      </div>

      {isLoading ? (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {[1, 2, 3].map((i) => <div key={i} className="h-32 bg-gray-100 rounded-mdesigner animate-pulse" />)}
        </div>
      ) : projects.length === 0 ? (
        <div className="text-center py-16 text-gray-500">
          <p className="mb-4">아직 프로젝트가 없습니다</p>
          <Button variant="outline" onClick={() => setShowCreate(true)}>첫 프로젝트 만들기</Button>
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {projects.map((p) => <ProjectCard key={p.project_id} project={p} onDelete={() => handleDelete(p.project_id)} />)}
        </div>
      )}

      <CreateProjectModal isOpen={showCreate} onClose={() => setShowCreate(false)} onCreated={(p) => { router.push(`/project/${p.project_id}/stage/1`); }} />
    </div>
  );
}
