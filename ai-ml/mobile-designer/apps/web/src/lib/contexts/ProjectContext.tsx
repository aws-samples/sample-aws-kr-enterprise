"use client";

import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import type { Project } from "@/lib/api/projects";
import * as projectsApi from "@/lib/api/projects";

interface ProjectState {
  project: Project | null;
  isLoading: boolean;
  error: string | null;
  loadProject: (projectId: string) => Promise<void>;
  advanceStage: () => Promise<void>;
  refresh: () => Promise<void>;
}

const ProjectContext = createContext<ProjectState | null>(null);

export function ProjectProvider({ children, projectId }: { children: ReactNode; projectId: string }) {
  const [project, setProject] = useState<Project | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadProject = useCallback(async (id: string) => {
    setIsLoading(true);
    setError(null);
    try {
      const p = await projectsApi.getProject(id);
      setProject(p);
    } catch (e: any) {
      setError(e.message || "Failed to load project");
    } finally {
      setIsLoading(false);
    }
  }, []);

  const advanceStage = useCallback(async () => {
    if (!project) return;
    const updated = await projectsApi.advanceStage(project.project_id);
    setProject(updated);
  }, [project]);

  const refresh = useCallback(async () => {
    if (project) await loadProject(project.project_id);
  }, [project, loadProject]);

  return (
    <ProjectContext.Provider value={{ project, isLoading, error, loadProject, advanceStage, refresh }}>
      {children}
    </ProjectContext.Provider>
  );
}

export function useProject() {
  const ctx = useContext(ProjectContext);
  if (!ctx) throw new Error("useProject must be used within ProjectProvider");
  return ctx;
}
